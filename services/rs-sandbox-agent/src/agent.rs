//! 沙箱里的执行代理（D-28；接口与语义同 services/go-sandbox-agent/internal/agent，设计书 4.10，计划 cloud-deploy.md 的
//! D-05、D-07）。适配器只认这一套 HTTP 接口：
//!
//! ```text
//! GET  /healthz                 不要令牌，就绪探针用
//! POST /v1/exec                 {"argv", "stdin": base64, "timeout_ms", "env", "exec_id"} → {"exit_code", "timed_out", "stdout", "stderr", "truncated", "cpu_ms"}
//! POST /v1/exec/cancel          {"exec_id"} → 204：杀掉这条命令的进程组（还没开始的不再开始）；调用方断开连接也一样
//! PUT  /v1/files/<相对路径>      请求体是文件内容
//! GET  /v1/files/<相对路径>      响应体是文件内容；不存在（或是目录）时 404
//! GET、PUT /v1/archive、GET /v1/manifest、GET /v1/list/<相对路径>、POST /v1/grep   工作区的存档与恢复、清单、列目录与搜索（archive.rs）
//! ```
//!
//! 认证：除 /healthz 外都要带 X-Sandbox-Token（恒定时间比较）。出错时返回 {"code", "message"}：400 参数不对，401 令牌不对，
//! 404 不存在，409 冲突，413 太大，500 agent 自己出错。路由与路径规范化照 Go 的 ServeMux（非规范的路径 307，方法不对 405）。

use std::collections::HashMap;
use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::Mutex;
use std::sync::mpsc::Sender;
use std::time::{Duration, Instant};

use crate::gostr;
use crate::http::Ctx;
use crate::json::Obj;

pub const TOKEN_HEADER: &str = "X-Sandbox-Token";
pub const MIN_TOKEN_LENGTH: usize = 32;

/// agent 的配置（main.rs 从环境变量读）。
pub struct Config {
    /// 工作目录：命令在这里跑，文件路径相对它。
    pub workdir: PathBuf,
    /// 请求要带的令牌。
    pub token: String,
    /// 命令的环境变量，不含 agent 自己的配置与令牌。
    pub env: Vec<(OsString, OsString)>,
    /// stdout、stderr 各自最多保留的字节数；超出时保留末尾。
    pub output_limit: usize,
    /// 一个文件（与 stdin）最多多少字节。
    pub file_limit: u64,
    /// 一条命令最长能跑多久。
    pub max_timeout: Duration,
    /// 进程退出后最多再等多久收尾（输出管道被孙进程占着时不无限等）。
    pub wait_delay: Duration,
}

/// 正在跑的命令与先到的取消（请求可能比命令先到；一分钟后忘掉）。
#[derive(Default)]
pub struct Registry {
    pub running: HashMap<String, Sender<crate::exec::Event>>,
    pub cancelled: HashMap<String, Instant>,
}

pub struct App {
    pub c: Config,
    pub reg: Mutex<Registry>,
    pub transfers: crate::transfer::Transfers,
    pub restores: crate::restore::Restores,
}

impl App {
    pub fn new(mut c: Config) -> Result<App, String> {
        if c.token.len() < MIN_TOKEN_LENGTH {
            return Err(format!("令牌至少要 {MIN_TOKEN_LENGTH} 个字符"));
        }
        if c.workdir.as_os_str().is_empty() {
            return Err("缺少工作目录".into());
        }
        std::fs::create_dir_all(&c.workdir).map_err(|e| format!("建不了工作目录：{e}"))?;
        if c.output_limit == 0 {
            c.output_limit = 8 << 20;
        }
        if c.file_limit == 0 {
            c.file_limit = 64 << 20;
        }
        if c.max_timeout.is_zero() {
            c.max_timeout = Duration::from_secs(3600);
        }
        if c.wait_delay.is_zero() {
            c.wait_delay = Duration::from_secs(2);
        }
        Ok(App {
            c,
            reg: Mutex::new(Registry::default()),
            transfers: crate::transfer::Transfers::default(),
            restores: crate::restore::Restores::default(),
        })
    }

    fn authed(&self, ctx: &Ctx<'_>) -> bool {
        let got = ctx.req.header(TOKEN_HEADER).unwrap_or("");
        !got.is_empty() && constant_time_eq(got.as_bytes(), self.c.token.as_bytes())
    }

    /// 一个请求。
    pub fn handle(&self, ctx: &mut Ctx<'_>) {
        if ctx.req.method == "OPTIONS" && ctx.req.path == "*" {
            ctx.resp.send(200, &[], b"");
            return;
        }
        let escaped = ctx.req.path.clone();
        let method = ctx.req.method.clone();
        let path = gostr::clean_url_path(&escaped);
        let found = route(&method, &path);
        if found.is_none() && !path.ends_with('/') && route(&method, &format!("{path}/")).is_some()
        {
            // Go 的 matchOrRedirect：补上结尾的 /。
            let raw = gostr::path_unescape(&path).unwrap_or_default();
            let target = gostr::escape_path(&String::from_utf8_lossy(&raw)) + "/";
            return redirect(ctx, &target);
        }
        if path != escaped {
            return redirect(ctx, &gostr::escape_path(&path));
        }
        let Some((route, wildcard)) = found else {
            let mut allowed: Vec<&str> = [
                "GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "CONNECT", "TRACE",
            ]
            .into_iter()
            .filter(|m| {
                route(m, &path).is_some()
                    || (!path.ends_with('/') && route(m, &format!("{path}/")).is_some())
            })
            .collect();
            allowed.sort_unstable();
            if allowed.is_empty() {
                plain_error(ctx, 404, "404 page not found\n");
            } else {
                let allow = allowed.join(", ");
                ctx.resp.send(
                    405,
                    &[
                        ("Allow", allow),
                        ("Content-Type", "text/plain; charset=utf-8".into()),
                        ("X-Content-Type-Options", "nosniff".into()),
                    ],
                    b"Method Not Allowed\n",
                );
            }
            return;
        };
        if route == Route::Health {
            #[cfg(target_os = "linux")]
            if std::env::var("AGENT_GUEST_PROFILE").as_deref() == Ok("benchmark-system") {
                let body = serde_json::json!({"status":"ok","guest_profile":"benchmark-system","uid":ar_sys::geteuid(),"max_timeout_s":self.c.max_timeout.as_secs()}).to_string();
                ctx.resp.send(
                    200,
                    &[("Content-Type", "application/json".into())],
                    body.as_bytes(),
                );
                return;
            }
            ctx.resp.send(
                200,
                &[("Content-Type", "text/plain; charset=utf-8".into())],
                b"ok",
            );
            return;
        }
        if !self.authed(ctx) {
            return fail(ctx, 401, "unauthorized", "令牌不对");
        }
        if route != Route::Cancel && crate::archive::restore_incomplete(&self.c.workdir) {
            return fail(
                ctx,
                409,
                "workspace_restore_incomplete",
                "workspace restore incomplete; discard this environment",
            );
        }
        match route {
            Route::PrepareTransfer => crate::transfer::prepare(self, ctx),
            Route::CommitTransfer => crate::transfer::commit(self, ctx),
            Route::ReleaseTransfer => crate::transfer::release(self, ctx),
            Route::ReadTransfer => crate::transfer::read(self, ctx),
            Route::Health => {}
            Route::Exec => crate::exec::exec(self, ctx),
            Route::Cancel => crate::exec::cancel(self, ctx),
            Route::WriteFile => crate::files::write_file(self, ctx, &wildcard),
            Route::ReadFile => crate::files::read_file(self, ctx, &wildcard),
            Route::Export => crate::archive::export(self, ctx),
            Route::Import => crate::archive::import(self, ctx),
            Route::RestoreBlocks => crate::restore::blocks(self, ctx),
            Route::RestoreFinish => crate::restore::finish(self, ctx),
            Route::Manifest => crate::archive::manifest(self, ctx),
            Route::Snapshot => crate::archive::snapshot(self, ctx),
            Route::List => crate::archive::list(self, ctx, &wildcard),
            Route::Grep => crate::archive::grep(self, ctx),
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Route {
    RestoreBlocks,
    RestoreFinish,
    PrepareTransfer,
    CommitTransfer,
    ReadTransfer,
    ReleaseTransfer,
    Health,
    Exec,
    Cancel,
    WriteFile,
    ReadFile,
    Export,
    Import,
    Manifest,
    Snapshot,
    List,
    Grep,
}

/// 按 Go 的 ServeMux 匹配：字面的段比较反转义后的值，`{path...}` 取剩下的部分（反转义）。GET 的模式也收 HEAD。
fn route(method: &str, path: &str) -> Option<(Route, Vec<u8>)> {
    let get = method == "GET" || method == "HEAD";
    let exact = |lits: &[&str]| {
        matches(path, lits)
            .filter(|rest| rest.is_none())
            .map(|_| Vec::new())
    };
    let wild = |lits: &[&str]| matches(path, lits).and_then(|rest| rest);
    if method == "POST" {
        for (name, route) in [
            ("prepare", Route::PrepareTransfer),
            ("commit", Route::CommitTransfer),
            ("read", Route::ReadTransfer),
            ("release", Route::ReleaseTransfer),
        ] {
            if let Some(v) = exact(&["v1", "transfer", name]) {
                return Some((route, v));
            }
        }
    }
    if method == "POST" {
        if let Some(v) = exact(&["v1", "restore", "blocks"]) {
            return Some((Route::RestoreBlocks, v));
        }
        if let Some(v) = exact(&["v1", "restore"]) {
            return Some((Route::RestoreFinish, v));
        }
        if let Some(v) = exact(&["v1", "snapshot"]) {
            return Some((Route::Snapshot, v));
        }
    }
    if get && let Some(v) = exact(&["healthz"]) {
        return Some((Route::Health, v));
    }
    if method == "POST"
        && let Some(v) = exact(&["v1", "exec"])
    {
        return Some((Route::Exec, v));
    }
    if method == "POST"
        && let Some(v) = exact(&["v1", "exec", "cancel"])
    {
        return Some((Route::Cancel, v));
    }
    if let Some(v) = wild(&["v1", "files"]) {
        if method == "PUT" {
            return Some((Route::WriteFile, v));
        }
        if get {
            return Some((Route::ReadFile, v));
        }
    }
    if let Some(v) = exact(&["v1", "archive"]) {
        if get {
            return Some((Route::Export, v));
        }
        if method == "PUT" {
            return Some((Route::Import, v));
        }
    }
    if get && let Some(v) = exact(&["v1", "manifest"]) {
        return Some((Route::Manifest, v));
    }
    if get && let Some(v) = exact(&["v1", "list"]).or_else(|| wild(&["v1", "list"])) {
        return Some((Route::List, v));
    }
    if method == "POST"
        && let Some(v) = exact(&["v1", "grep"])
    {
        return Some((Route::Grep, v));
    }
    None
}

/// path 依次是这些字面段：剩下为空时 Some(None)，剩下以 / 开头时 Some(Some(反转义的剩余))。
fn matches(path: &str, lits: &[&str]) -> Option<Option<Vec<u8>>> {
    let mut rest = path;
    for lit in lits {
        let after = rest.strip_prefix('/')?;
        let (seg, tail) = after.split_at(after.find('/').unwrap_or(after.len()));
        let unescaped = gostr::path_unescape(seg).unwrap_or_else(|| seg.as_bytes().to_vec());
        if unescaped != lit.as_bytes() {
            return None;
        }
        rest = tail;
    }
    if rest.is_empty() {
        return Some(None);
    }
    let tail = rest.strip_prefix('/')?;
    Some(Some(
        gostr::path_unescape(tail).unwrap_or_else(|| tail.as_bytes().to_vec()),
    ))
}

fn redirect(ctx: &mut Ctx<'_>, target: &str) {
    let mut url = target.to_string();
    if !ctx.req.query.is_empty() {
        url = format!("{url}?{}", ctx.req.query);
    }
    let get = ctx.req.method == "GET";
    let mut headers = vec![("Location", url.clone())];
    if get || ctx.req.method == "HEAD" {
        headers.insert(0, ("Content-Type", "text/html; charset=utf-8".into()));
    }
    let body = if get {
        format!(
            "<a href=\"{}\">Temporary Redirect</a>.\n\n",
            gostr::html_escape(&url)
        )
    } else {
        String::new()
    };
    ctx.resp.send(307, &headers, body.as_bytes());
}

fn plain_error(ctx: &mut Ctx<'_>, status: u16, body: &str) {
    ctx.resp.send(
        status,
        &[
            ("Content-Type", "text/plain; charset=utf-8".into()),
            ("X-Content-Type-Options", "nosniff".into()),
        ],
        body.as_bytes(),
    );
}

/// 写 JSON 响应（末尾一个换行，与 Go 的 json.Encoder 一样）。
pub fn write_json(ctx: &mut Ctx<'_>, status: u16, body: String) {
    let mut b = body.into_bytes();
    b.push(b'\n');
    ctx.resp
        .send(status, &[("Content-Type", "application/json".into())], &b);
}

pub fn fail(ctx: &mut Ctx<'_>, status: u16, code: &str, message: &str) {
    write_json(
        ctx,
        status,
        Obj::new().s("code", code).s("message", message).done(),
    );
}

/// 恒定时间比较（与 Go 的 subtle.ConstantTimeCompare 一样，长度不同时直接不等）。
pub fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b) {
        diff |= x ^ y;
    }
    std::hint::black_box(diff) == 0
}

/// 请求里的相对路径 → 工作目录里的路径；不合法时返回错误信息（与 Go 版 resolve 相同的规则）。
pub fn resolve(workdir: &std::path::Path, p: &[u8]) -> Result<PathBuf, &'static str> {
    if p.is_empty() || p.contains(&b'\\') || p.contains(&0) || p.starts_with(b"/") || has_drive(p) {
        return Err("路径必须是工作目录内的相对路径");
    }
    for part in p.split(|&c| c == b'/') {
        if part.is_empty() || part == b"." || part == b".." {
            return Err("路径不得含空段、. 或 ..");
        }
    }
    Ok(workdir.join(bytes_path(p)))
}

/// `C:` 这样的盘符开头。
pub fn has_drive(p: &[u8]) -> bool {
    p.len() >= 2 && p[0].is_ascii_alphabetic() && p[1] == b':'
}

/// `/` 分隔的相对路径（字节）→ 本机路径。
pub fn bytes_path(p: &[u8]) -> PathBuf {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        PathBuf::from(std::ffi::OsStr::from_bytes(p))
    }
    #[cfg(not(unix))]
    {
        PathBuf::from(String::from_utf8_lossy(p).replace('/', "\\"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn routes_like_go() {
        assert_eq!(route("GET", "/healthz").map(|r| r.0), Some(Route::Health));
        assert_eq!(route("HEAD", "/healthz").map(|r| r.0), Some(Route::Health));
        assert_eq!(route("GET", "/v1/files/a%2Fb").unwrap().1, b"a/b");
        assert_eq!(route("GET", "/v1/files/").unwrap().1, b"");
        assert!(route("GET", "/v1/files").is_none());
        assert_eq!(route("GET", "/v1/list").map(|r| r.0), Some(Route::List));
        assert_eq!(route("GET", "/v1/list/").unwrap().1, b"");
        assert_eq!(
            route("GET", "/v1/%66iles/x").map(|r| r.0),
            Some(Route::ReadFile)
        );
        assert!(route("GET", "/v1/archive/").is_none());
        assert!(route("DELETE", "/v1/files/x").is_none());
    }

    #[test]
    fn resolve_like_go() {
        let w = std::path::Path::new("/w");
        assert!(resolve(w, b"a/b.txt").is_ok());
        for bad in [
            &b""[..],
            b"/a",
            b"a/../b",
            b"a//b",
            b"./a",
            b"a/",
            b"a\\b",
            b"C:x",
            b"a\0b",
            b"..",
        ] {
            assert!(resolve(w, bad).is_err(), "{bad:?}");
        }
        assert!(
            constant_time_eq(b"abc", b"abc")
                && !constant_time_eq(b"abc", b"abd")
                && !constant_time_eq(b"abc", b"ab")
        );
    }
}
