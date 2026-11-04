//! 把 `/v1/envs/<id>/agent/<路径>` 转给客户机里的 agent（经 vsock）。请求头按白名单之外的规则过滤：去掉逐跳的头、
//! 调用方的 `Authorization`（fc-manager 的令牌）与 `X-Sandbox-Owner`，以及调用方自己带的 `X-Sandbox-Token`，换上这台
//! VM 的 agent 令牌；请求体与响应体原样流过（不读进内存）。上游与下游都是 `Connection: close`，一个连接一个请求。
//!
//! 断开要传到 agent：agent 靠连接断开取消正在跑的命令（services/go-sandbox-agent 的 exec）。调用方关了连接，
//! 就把到 agent 的连接整个关掉。所以调用方不能在发完请求后半关闭写的一端（httpx、Go 的客户端都不会）。

use std::io::{self, Write};
use std::net::{Shutdown, TcpStream};
use std::time::Duration;

use crate::http::{self, Conn, Head};
use crate::manager::AgentRoute;
use crate::spec::AGENT_PORT;
use crate::{vsock, warn};

/// 不转给 agent 的请求头（小写）。Connection 头里点名的也不转。
const DROP_REQUEST: &[&str] = &[
    "host",
    "connection",
    "keep-alive",
    "proxy-connection",
    "proxy-authorization",
    "te",
    "upgrade",
    "authorization",
    "x-sandbox-owner",
    "x-sandbox-token",
];

const DROP_RESPONSE: &[&str] = &["connection", "keep-alive", "proxy-connection", "upgrade"];

fn fail(client: &mut TcpStream, status: u16, code: &str, message: &str) -> u16 {
    let _ = http::write_json(client, status, &http::error_json(code, message), &[]);
    status
}

/// 转发；返回给调用方的状态码（记日志用）。
pub fn forward(
    mut client: Conn<TcpStream>,
    head: &Head,
    agent_target: &str,
    route: &AgentRoute<'_>,
) -> u16 {
    let buffered = client.take_buffered();
    let mut down = client.into_inner();
    let up = match vsock::connect(&route.vsock, AGENT_PORT, Duration::from_secs(5)) {
        Ok(up) => up,
        Err(e) => {
            warn!("连不上 {} 的 agent：{e}", route.env_id);
            return fail(&mut down, 502, "agent_unreachable", "连不上环境里的 agent");
        }
    };
    let named: Vec<String> = head
        .all("connection")
        .iter()
        .flat_map(|v| v.split(','))
        .map(|t| t.trim().to_ascii_lowercase())
        .filter(|t| !t.is_empty())
        .collect();
    let mut wire = format!(
        "{} {agent_target} HTTP/1.1\r\nHost: sandbox-agent\r\n",
        head.method
    );
    for (k, v) in &head.headers {
        let lk = k.to_ascii_lowercase();
        if DROP_REQUEST.contains(&lk.as_str()) || named.contains(&lk) {
            continue;
        }
        wire.push_str(&format!("{k}: {v}\r\n"));
    }
    wire.push_str(&format!(
        "X-Sandbox-Token: {}\r\nConnection: close\r\n\r\n",
        route.token.expose()
    ));

    let (Ok(mut up_w), Ok(up_shut), Ok(mut down_r)) = (
        up.get_ref().try_clone(),
        up.get_ref().try_clone(),
        down.try_clone(),
    ) else {
        return fail(&mut down, 500, "internal", "fc-manager 内部错误");
    };
    if up_w
        .write_all(wire.as_bytes())
        .and_then(|_| up_w.write_all(&buffered))
        .is_err()
    {
        return fail(&mut down, 502, "agent_unreachable", "环境里的 agent 断开了");
    }
    drop(wire);
    // 调用方 → agent：剩下的请求体，以及断开。调用方一关连接，就关掉到 agent 的连接。
    let _ = down_r.set_read_timeout(None);
    let pump = std::thread::spawn(move || {
        let _ = io::copy(&mut down_r, &mut up_w);
        let _ = up_shut.shutdown(Shutdown::Both);
    });

    let status = relay_response(up, &mut down);
    let _ = down.shutdown(Shutdown::Both);
    let _ = pump.join();
    status
}

fn relay_response(mut up: Conn<std::os::unix::net::UnixStream>, down: &mut TcpStream) -> u16 {
    let resp = match up.read_response() {
        Ok(h) => h,
        Err(e) => {
            warn!("agent 的响应不对：{e}");
            return fail(
                down,
                502,
                "agent_unreachable",
                "环境里的 agent 没有正常应答",
            );
        }
    };
    let mut out = format!("HTTP/1.1 {} {}\r\n", resp.status, http::reason(resp.status));
    for (k, v) in &resp.headers {
        if DROP_RESPONSE.contains(&k.to_ascii_lowercase().as_str()) {
            continue;
        }
        out.push_str(&format!("{k}: {v}\r\n"));
    }
    out.push_str("Connection: close\r\n\r\n");
    let rest = up.take_buffered();
    let mut up = up.into_inner();
    let _ = down
        .write_all(out.as_bytes())
        .and_then(|_| down.write_all(&rest))
        .and_then(|_| io::copy(&mut up, down).map(|_| ()));
    let _ = down.flush();
    resp.status
}
