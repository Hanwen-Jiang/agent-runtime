//! fc-manager 的 HTTP 接口（路由见 lib.rs）。一个连接一个线程、一个请求；同时的连接数有上限，超了直接 503。
//! 读请求头有 10 秒的超时；转发给 agent 的连接读写不设超时（命令可能跑一小时），靠调用方断开。
//!
//! 自己回的响应写完后先半关闭、再把对方没发完的读掉（最多 1 MiB、1 秒），然后才关：关的时候还有没读的数据，
//! 对方会收到 RST，可能连响应都看不到（例如请求头超长时回的 431）。

use std::io::Read;
use std::net::{Shutdown, TcpListener, TcpStream};
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use crate::http::{self, Body, Conn, Head, HttpError};
use crate::manager::{ApiError, Manager};
use crate::secrets::TokenSource;
use crate::spec::{self, CreateRequest};
use crate::{info, proxy, warn};

const API_BODY_LIMIT: u64 = 64 << 10;
const LINGER_BYTES: usize = 1 << 20;
const AGENT_METHODS: &[&str] = &["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"];

pub fn serve(
    mgr: Arc<Manager>,
    tokens: Arc<TokenSource>,
    listener: TcpListener,
) -> std::io::Result<()> {
    let active = Arc::new(AtomicUsize::new(0));
    for stream in listener.incoming() {
        let Ok(stream) = stream else { continue };
        if active.load(Ordering::SeqCst) >= mgr.cfg.max_connections {
            let _ = stream.set_write_timeout(Some(Duration::from_secs(1)));
            let e = ApiError::transient("too_many_connections", "fc-manager 的连接太多", 1);
            reply(Conn::new(stream), &e);
            continue;
        }
        active.fetch_add(1, Ordering::SeqCst);
        let (m, t, a) = (mgr.clone(), tokens.clone(), active.clone());
        let spawned = std::thread::Builder::new()
            .name("conn".into())
            .spawn(move || {
                handle(&m, &t, stream);
                a.fetch_sub(1, Ordering::SeqCst);
            });
        if spawned.is_err() {
            active.fetch_sub(1, Ordering::SeqCst);
            warn!("起不了处理连接的线程");
        }
    }
    Ok(())
}

fn handle(mgr: &Manager, tokens: &TokenSource, stream: TcpStream) {
    let started = Instant::now();
    let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(60)));
    let _ = stream.set_nodelay(true);
    let mut conn = Conn::new(stream);
    let head = match conn.read_request() {
        Ok(h) => h,
        Err(HttpError::TooLarge) => {
            reply(conn, &ApiError::new(431, "headers_too_large", "请求头太大"));
            return;
        }
        Err(HttpError::Bad(m)) => {
            reply(conn, &ApiError::new(400, "bad_request", m));
            return;
        }
        Err(_) => return,
    };
    let status = route(mgr, tokens, conn, &head);
    info!(
        "{} {} -> {status}（{} ms）",
        head.method,
        head.path(),
        started.elapsed().as_millis()
    );
}

/// 半关闭、读掉对方还在发的，再关。
fn linger(conn: Conn<TcpStream>) {
    let mut s = conn.into_inner();
    let _ = s.shutdown(Shutdown::Write);
    let _ = s.set_read_timeout(Some(Duration::from_secs(1)));
    let deadline = Instant::now() + Duration::from_secs(1);
    let mut buf = [0u8; 8192];
    let mut seen = 0;
    while seen < LINGER_BYTES && Instant::now() < deadline {
        match s.read(&mut buf) {
            Ok(0) | Err(_) => break,
            Ok(n) => seen += n,
        }
    }
}

fn respond(mut conn: Conn<TcpStream>, status: u16, value: &Value) -> u16 {
    let _ = http::write_json(conn.get_mut(), status, value, &[]);
    linger(conn);
    status
}

fn reply(mut conn: Conn<TcpStream>, e: &ApiError) -> u16 {
    let retry = e.retry_after.map(|s| s.to_string());
    let mut extra = vec![];
    if let Some(r) = &retry {
        extra.push(("Retry-After", r.as_str()));
    }
    let _ = http::write_json(
        conn.get_mut(),
        e.status,
        &http::error_json(e.code, &e.message),
        &extra,
    );
    linger(conn);
    e.status
}

fn route(mgr: &Manager, tokens: &TokenSource, mut conn: Conn<TcpStream>, head: &Head) -> u16 {
    let body = match head.body() {
        Ok(b) => b,
        Err(e) => return reply(conn, &ApiError::new(400, "bad_request", e.to_string())),
    };
    let path = head.path();
    let method = head.method.as_str();
    if path == "/healthz" {
        if method != "GET" && method != "HEAD" {
            return reply(
                conn,
                &ApiError::new(405, "method_not_allowed", "只支持 GET"),
            );
        }
        let _ = http::write_response(
            conn.get_mut(),
            200,
            &[("Content-Type", "text/plain")],
            b"ok",
        );
        linger(conn);
        return 200;
    }

    let auth = head.all("authorization");
    let presented = match auth.as_slice() {
        [v] => v.strip_prefix("Bearer ").unwrap_or(""),
        _ => "",
    };
    if presented.is_empty() || !tokens.check(presented) {
        return reply(
            conn,
            &ApiError::new(401, "unauthorized", "fc-manager 的令牌不对"),
        );
    }
    let owner = match head.all("x-sandbox-owner").as_slice() {
        [o] if spec::valid_owner(o) => o.to_string(),
        _ => {
            return reply(
                conn,
                &ApiError::new(400, "invalid_owner", "X-Sandbox-Owner 的格式不对"),
            );
        }
    };

    if path == "/v1/blob-cache" {
        if owner != "_platform" {
            return reply(
                conn,
                &ApiError::new(403, "platform_only", "cache metrics require platform scope"),
            );
        }
        if method != "GET" {
            return reply(
                conn,
                &ApiError::new(405, "method_not_allowed", "GET required"),
            );
        }
        return respond(conn, 200, &crate::blob::stats());
    }
    if path == "/v1/envs" {
        return match method {
            "GET" if owner == "_platform" => {
                respond(conn, 200, &json!({"environments": mgr.list()}))
            }
            "GET" => reply(
                conn,
                &ApiError::new(403, "platform_only", "列出环境只给平台作用域"),
            ),
            "POST" => create(mgr, &owner, conn, body),
            _ => reply(
                conn,
                &ApiError::new(405, "method_not_allowed", "只支持 GET 与 POST"),
            ),
        };
    }
    let Some(rest) = path.strip_prefix("/v1/envs/") else {
        return reply(conn, &ApiError::new(404, "not_found", "没有这个接口"));
    };
    let (env_id, tail) = rest.split_once('/').unwrap_or((rest, ""));
    if !spec::valid_env_id(env_id) {
        return reply(conn, &ApiError::unknown_env());
    }
    if !rest.contains('/') {
        if method != "DELETE" {
            return reply(
                conn,
                &ApiError::new(405, "method_not_allowed", "只支持 DELETE"),
            );
        }
        return match mgr.destroy(&owner, env_id) {
            Ok(()) => {
                let _ = http::write_response(conn.get_mut(), 204, &[], b"");
                linger(conn);
                204
            }
            Err(e) => reply(conn, &e),
        };
    }
    if tail == "blob-grants" {
        if method != "POST" {
            return reply(
                conn,
                &ApiError::new(405, "method_not_allowed", "POST required"),
            );
        }
        let _scope = match mgr.agent_route(&owner, env_id) {
            Ok(s) => s,
            Err(e) => return reply(conn, &e),
        };
        let value = match conn
            .read_body(body, API_BODY_LIMIT)
            .ok()
            .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
        {
            Some(v) => v,
            None => {
                return reply(
                    conn,
                    &ApiError::new(400, "invalid_grants", "invalid block plan"),
                );
            }
        };
        return match crate::blob::register(env_id, &value) {
            Ok(v) => respond(conn, 200, &v),
            Err(m) => reply(conn, &ApiError::new(400, "invalid_grants", m)),
        };
    }
    if tail == "preview" {
        if method != "POST" {
            return reply(
                conn,
                &ApiError::new(405, "method_not_allowed", "只支持 POST"),
            );
        }
        return preview(mgr, &owner, env_id, conn, body);
    }
    let Some(agent_path) = tail.strip_prefix("agent/") else {
        return reply(conn, &ApiError::new(404, "not_found", "没有这个接口"));
    };
    if !AGENT_METHODS.contains(&method) {
        return reply(
            conn,
            &ApiError::new(405, "method_not_allowed", "agent 不支持这个方法"),
        );
    }
    if head.get("upgrade").is_some() {
        return reply(
            conn,
            &ApiError::new(400, "upgrade_unsupported", "不转发协议升级"),
        );
    }
    let route = match mgr.agent_route(&owner, env_id) {
        Ok(r) => r,
        Err(e) => return reply(conn, &e),
    };
    let query = head
        .target
        .split_once('?')
        .map(|(_, q)| format!("?{q}"))
        .unwrap_or_default();
    let target = format!("/{agent_path}{query}");
    // 转发期间不设读超时：请求体可能来得慢，命令可能跑很久。
    let _ = conn.get_mut().set_read_timeout(None);
    proxy::forward(conn, head, &target, &route)
}

fn preview(mgr: &Manager, owner: &str, env_id: &str, mut conn: Conn<TcpStream>, body: Body) -> u16 {
    let raw = match body {
        Body::Length(n) if n <= 1024 => match conn.read_body(body, 1024) {
            Ok(b) => b,
            Err(_) => return reply(conn, &ApiError::new(400, "invalid_request", "请求体没读完")),
        },
        _ => {
            return reply(
                conn,
                &ApiError::new(
                    400,
                    "invalid_request",
                    "请求体要是带 Content-Length 的小 JSON",
                ),
            );
        }
    };
    let port = serde_json::from_slice::<Value>(&raw)
        .ok()
        .and_then(|v| v.get("port").and_then(Value::as_u64))
        .filter(|p| (1..=65535).contains(p));
    let Some(port) = port else {
        return reply(
            conn,
            &ApiError::new(400, "invalid_port", "port 要在 1 到 65535 之间"),
        );
    };
    match mgr.preview(owner, env_id, port as u16) {
        Ok(addr) => respond(conn, 200, &json!({"address": addr})),
        Err(e) => reply(conn, &e),
    }
}

fn create(mgr: &Manager, owner: &str, mut conn: Conn<TcpStream>, body: Body) -> u16 {
    let raw = match body {
        Body::Chunked => {
            return reply(
                conn,
                &ApiError::new(411, "length_required", "请求体要带 Content-Length"),
            );
        }
        Body::Length(n) if n > API_BODY_LIMIT => {
            return reply(conn, &ApiError::new(413, "too_large", "请求体最多 64 KiB"));
        }
        _ => match conn.read_body(body, API_BODY_LIMIT) {
            Ok(b) => b,
            Err(_) => return reply(conn, &ApiError::new(400, "invalid_request", "请求体没读完")),
        },
    };
    let req: CreateRequest = match serde_json::from_slice(&raw) {
        Ok(r) => r,
        Err(e) if e.is_data() => {
            let msg = "请求的字段不对（只认 runtime、cpu_millis、memory_mb、disk_mb、labels）";
            return reply(conn, &ApiError::new(400, "invalid_argument", msg));
        }
        Err(_) => {
            return reply(
                conn,
                &ApiError::new(400, "invalid_request", "请求体不是合法的 JSON"),
            );
        }
    };
    match mgr.create(owner, req) {
        Ok(c) => {
            let v = json!({
                "env_id": c.env_id,
                "created_at": c.created_at,
                "image": c.image,
                "runtime": c.spec.runtime,
                "vcpus": c.spec.vcpus,
                "memory_mb": c.spec.memory_mib,
                "disk_mb": c.spec.disk_mib,
            });
            respond(conn, 201, &v)
        }
        Err(e) => reply(conn, &e),
    }
}
