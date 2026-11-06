//! POST /v1/exec 与 /v1/exec/cancel（语义同 Go 版 agent.go 的 exec、cancelExec）。
//!
//! - 命令自成一个进程组；超时、取消、调用方断开时杀整个组（SIGKILL），确认它退出了再回（ExecNotStopped 的前提）。
//! - stdout、stderr 各自只留最后 output_limit 个字节；进程退出后最多再等 wait_delay 收尾（孙进程占着管道时不无限等）。
//! - 超时不报错：timed_out 为真、exit_code 为 null；被信号杀掉的退出码是负的信号编号。
//! - 命令只拿到配置的环境变量，加上这次请求的 env（名字要合法，AGENT_ 开头的一律拒绝）。

use std::ffi::OsString;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::time::{Duration, Instant};

use crate::agent::{App, fail, write_json};
use crate::http::{Ctx, is_too_large};
use crate::json::{self, Obj};

/// 一次请求最多带多少个环境变量、总共多少字节。
const MAX_REQUEST_ENV: usize = 64;
const MAX_REQUEST_ENV_BYTES: usize = 64 << 10;
/// 先到的取消记多久。
const CANCELLED_MEMORY: Duration = Duration::from_secs(60);
/// 多久看一次调用方是不是断开了。
const PEER_CHECK: Duration = Duration::from_millis(50);

pub enum Event {
    Exited(ar_sys::Exit),
    Cancel,
}

/// 环境变量名的规则：`^[A-Za-z_][A-Za-z0-9_]*$`。
pub fn valid_env_name(k: &str) -> bool {
    let b = k.as_bytes();
    !b.is_empty()
        && (b[0].is_ascii_alphabetic() || b[0] == b'_')
        && b.iter().all(|c| c.is_ascii_alphanumeric() || *c == b'_')
}

fn agent_var(k: &str) -> bool {
    k.to_ascii_uppercase().starts_with("AGENT_")
}

/// 把这次请求的环境变量并到配置的环境上（同名的以请求为准）。
pub fn merge_env(
    base: &[(OsString, OsString)],
    extra: Option<&[(String, String)]>,
) -> Result<Vec<(OsString, OsString)>, String> {
    let Some(extra) = extra.filter(|e| !e.is_empty()) else {
        return Ok(base.to_vec());
    };
    if extra.len() > MAX_REQUEST_ENV {
        return Err(format!("env 最多 {MAX_REQUEST_ENV} 个"));
    }
    let mut size = 0;
    for (k, v) in extra {
        if !valid_env_name(k) || agent_var(k) || v.contains('\0') {
            return Err(format!("env 的名字 {} 不合法", crate::gostr::quote(k)));
        }
        size += k.len() + v.len();
    }
    if size > MAX_REQUEST_ENV_BYTES {
        return Err("env 太大".into());
    }
    let mut out: Vec<(OsString, OsString)> = base
        .iter()
        .filter(|(k, _)| !extra.iter().any(|(e, _)| OsString::from(e) == *k))
        .cloned()
        .collect();
    let mut sorted: Vec<&(String, String)> = extra.iter().collect();
    sorted.sort();
    out.extend(
        sorted
            .into_iter()
            .map(|(k, v)| (OsString::from(k), OsString::from(v))),
    );
    Ok(out)
}

/// 从 agent 自己的环境里挑出 names（空格或逗号分隔）列出的变量交给命令（镜像用 AGENT_ENV_PASSTHROUGH 声明，D-14）。
/// AGENT_ 开头的不给。按 "K=V" 排序（与 Go 版一致）。
pub fn pass_through(environ: &[(OsString, OsString)], names: &str) -> Vec<(OsString, OsString)> {
    let wanted: Vec<&str> = names
        .split([' ', ','])
        .filter(|n| !n.is_empty() && valid_env_name(n) && !agent_var(n))
        .collect();
    let mut out: Vec<(OsString, OsString)> = environ
        .iter()
        .filter(|(k, _)| k.to_str().is_some_and(|k| wanted.contains(&k)))
        .cloned()
        .collect();
    out.sort_by(|a, b| {
        let key = |kv: &(OsString, OsString)| {
            let mut s = kv.0.clone();
            s.push("=");
            s.push(&kv.1);
            s
        };
        key(a).cmp(&key(b))
    });
    out
}

pub fn cancel(app: &App, ctx: &mut Ctx<'_>) {
    let mut body = Vec::new();
    let _ = (&mut ctx.body).take(4096).read_to_end(&mut body);
    let id = json::parse_first(&body)
        .and_then(|v| {
            json::object(&v)
                .ok()
                .map(|m| json::get_string(json::field(m, "exec_id")))
        })
        .and_then(Result::ok)
        .unwrap_or_default();
    if id.is_empty() {
        return fail(ctx, 400, "invalid_argument", "要有 exec_id");
    }
    {
        let mut reg = app.reg.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(tx) = reg.running.remove(&id) {
            let _ = tx.send(Event::Cancel);
        } else {
            let now = Instant::now();
            reg.cancelled
                .retain(|_, at| now.duration_since(*at) <= CANCELLED_MEMORY);
            reg.cancelled.insert(id, now);
        }
    }
    ctx.resp.no_content();
}

struct ExecRequest {
    offload_inline_bytes: i64,
    argv: Vec<String>,
    stdin: Vec<u8>,
    timeout_ms: i64,
    env: Option<Vec<(String, String)>>,
    exec_id: String,
}

fn parse_request(body: &[u8]) -> Option<ExecRequest> {
    let v = json::parse_first(body)?;
    let m = json::object(&v).ok()?;
    Some(ExecRequest {
        offload_inline_bytes: json::get_i64(json::field(m, "offload_inline_bytes")).ok()?,
        argv: json::get_strings(json::field(m, "argv")).ok()?,
        stdin: json::get_bytes(json::field(m, "stdin")).ok()?,
        timeout_ms: json::get_i64(json::field(m, "timeout_ms")).ok()?,
        env: json::get_string_map(json::field(m, "env")).ok()?,
        exec_id: json::get_string(json::field(m, "exec_id")).ok()?,
    })
}

pub fn exec(app: &App, ctx: &mut Ctx<'_>) {
    // stdin 以 base64 放在 JSON 里，长度约为原文的 4/3。
    ctx.body
        .set_limit(app.c.file_limit.saturating_mul(2).saturating_add(4096));
    let body = match ctx.body.read_to_end_limited() {
        Ok(b) => b,
        Err(e) if is_too_large(&e) => return fail(ctx, 413, "too_large", "stdin 太大"),
        Err(_) => return fail(ctx, 400, "invalid_request", "请求体不是合法的 JSON"),
    };
    let Some(req) = parse_request(&body) else {
        return fail(ctx, 400, "invalid_request", "请求体不是合法的 JSON");
    };
    let max_ms = app.c.max_timeout.as_millis() as i64;
    if req.argv.is_empty() || req.argv[0].is_empty() {
        return fail(ctx, 400, "invalid_argument", "argv 不能为空");
    }
    if req.timeout_ms <= 0 || req.timeout_ms > max_ms {
        return fail(
            ctx,
            400,
            "invalid_timeout",
            &format!("timeout_ms 要在 1 到 {max_ms} 之间"),
        );
    }
    if req.stdin.len() as u64 > app.c.file_limit {
        return fail(ctx, 413, "too_large", "stdin 太大");
    }
    let env = match merge_env(&app.c.env, req.env.as_deref()) {
        Ok(e) => e,
        Err(msg) => return fail(ctx, 400, "invalid_env", &msg),
    };
    let Some(program) = look_path(&req.argv[0], &env) else {
        return fail(
            ctx,
            400,
            "command_not_found",
            &format!("找不到 {}", req.argv[0]),
        );
    };
    let (tx, rx) = mpsc::channel::<Event>();
    if !req.exec_id.is_empty() {
        let mut reg = app.reg.lock().unwrap_or_else(|e| e.into_inner());
        if reg.cancelled.remove(&req.exec_id).is_some() {
            drop(reg);
            return fail(ctx, 409, "exec_cancelled", "这条命令已经取消了");
        }
        reg.running.insert(req.exec_id.clone(), tx.clone());
    }
    let result = run(app, ctx, &program, &req, env, tx, rx);
    if !req.exec_id.is_empty() {
        app.reg
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .running
            .remove(&req.exec_id);
    }
    match result {
        Outcome::StartFailed => fail(
            ctx,
            400,
            "command_not_found",
            &format!("启动不了 {}", req.argv[0]),
        ),
        Outcome::Cancelled => fail(ctx, 409, "exec_cancelled", "这条命令已经取消了"),
        Outcome::Gone => ctx.resp.aborted = true,
        Outcome::Done {
            exit,
            timed_out,
            mut stdout,
            mut stderr,
        } => {
            let code = if timed_out {
                None
            } else {
                Some(exit.returncode() as i64)
            };
            let mut refs = Vec::new();
            if req.offload_inline_bytes > 0 {
                let limit = (req.offload_inline_bytes as usize).min(1 << 20);
                for (name, output) in [
                    ("stdout_transfer", &mut stdout),
                    ("stderr_transfer", &mut stderr),
                ] {
                    if output.buf.len() > limit
                        && let Ok(prepared) = app.transfers.prepare(output.buf.clone())
                    {
                        let half = limit / 2;
                        let mut excerpt = output.buf[..half].to_vec();
                        excerpt.extend_from_slice(b"\n...[externalized]...\n");
                        excerpt.extend_from_slice(&output.buf[output.buf.len() - (limit - half)..]);
                        output.buf = excerpt;
                        refs.push((name, prepared.to_string()));
                    }
                }
            }
            let mut body = Obj::new()
                .opt_i("exit_code", code)
                .bool("timed_out", timed_out)
                .s("stdout", &json::b64_encode(&stdout.buf))
                .s("stderr", &json::b64_encode(&stderr.buf))
                .bool("truncated", stdout.truncated || stderr.truncated)
                .i("cpu_ms", (exit.cpu.as_micros() / 1000) as i64);
            for (name, value) in refs {
                body = body.raw(name, &value);
            }
            write_json(ctx, 200, body.done());
        }
    }
}

enum Outcome {
    StartFailed,
    Cancelled,
    Gone,
    Done {
        exit: ar_sys::Exit,
        timed_out: bool,
        stdout: Tail,
        stderr: Tail,
    },
}

/// 只保留最后 limit 个字节。
#[derive(Default)]
pub struct Tail {
    limit: usize,
    pub buf: Vec<u8>,
    pub truncated: bool,
}

impl Tail {
    pub fn new(limit: usize) -> Tail {
        Tail {
            limit,
            buf: Vec::new(),
            truncated: false,
        }
    }

    pub fn write(&mut self, p: &[u8]) {
        if p.len() >= self.limit {
            self.truncated |= p.len() > self.limit || !self.buf.is_empty();
            self.buf.clear();
            self.buf.extend_from_slice(&p[p.len() - self.limit..]);
            return;
        }
        self.buf.extend_from_slice(p);
        if self.buf.len() > self.limit {
            let over = self.buf.len() - self.limit;
            self.buf.drain(..over);
            self.truncated = true;
        }
    }
}

fn run(
    app: &App,
    ctx: &Ctx<'_>,
    program: &Path,
    req: &ExecRequest,
    env: Vec<(OsString, OsString)>,
    tx: mpsc::Sender<Event>,
    rx: mpsc::Receiver<Event>,
) -> Outcome {
    let mut child = match proc::spawn(program, &req.argv, &app.c.workdir, &env) {
        Ok(c) => c,
        Err(_) => return Outcome::StartFailed,
    };
    let stdin = child.stdin.take();
    let data = req.stdin.clone();
    std::thread::spawn(move || {
        if let Some(mut w) = stdin {
            use std::io::Write;
            let _ = w.write_all(&data);
        }
    });
    let output = proc::Output::start(
        &mut child,
        if req.offload_inline_bytes > 0 {
            app.c.file_limit.min(rs_transfer::MAX_BLOB_BYTES) as usize
        } else {
            app.c.output_limit
        },
    );
    let pid = proc::Waiter::start(child, tx);
    let deadline = Instant::now() + Duration::from_millis(req.timeout_ms as u64);
    let mut timed_out = false;
    let exit = loop {
        let now = Instant::now();
        if now >= deadline {
            pid.kill();
            timed_out = true;
            break wait_exit(&rx);
        }
        match rx.recv_timeout((deadline - now).min(PEER_CHECK)) {
            Ok(Event::Exited(e)) => break e,
            Ok(Event::Cancel) => {
                pid.kill();
                wait_exit(&rx);
                output.finish(app.c.wait_delay);
                return Outcome::Cancelled;
            }
            Err(RecvTimeoutError::Timeout) => {
                if ctx.peer_gone() {
                    // 调用方断开了：命令没人等，杀掉。
                    pid.kill();
                    wait_exit(&rx);
                    output.finish(app.c.wait_delay);
                    return Outcome::Gone;
                }
            }
            Err(RecvTimeoutError::Disconnected) => {
                break ar_sys::Exit {
                    code: None,
                    signal: None,
                    cpu: Duration::ZERO,
                };
            }
        }
    };
    let (stdout, stderr) = output.finish(app.c.wait_delay);
    Outcome::Done {
        exit,
        timed_out,
        stdout,
        stderr,
    }
}

fn wait_exit(rx: &mpsc::Receiver<Event>) -> ar_sys::Exit {
    loop {
        match rx.recv() {
            Ok(Event::Exited(e)) => return e,
            Ok(Event::Cancel) => continue,
            Err(_) => {
                return ar_sys::Exit {
                    code: None,
                    signal: None,
                    cpu: Duration::ZERO,
                };
            }
        }
    }
}

/// 按命令自己的 PATH 找程序（不是 agent 进程的 PATH）；带路径分隔符的照原样用（相对的按 agent 的当前目录核对，与 Go 一样）。
pub fn look_path(name: &str, env: &[(OsString, OsString)]) -> Option<PathBuf> {
    if name.contains('/') || name.contains('\\') {
        let p = PathBuf::from(name);
        return proc::executable(&p).then_some(p);
    }
    let path = env
        .iter()
        .rev()
        .find(|(k, _)| k.to_str().is_some_and(|k| k.eq_ignore_ascii_case("PATH")))
        .map(|(_, v)| v.clone())?;
    for dir in std::env::split_paths(&path) {
        if dir.as_os_str().is_empty() {
            continue;
        }
        // Go 用 filepath.Join：结果不带分隔符（PATH 里有 "."）时它会去找 agent 自己的 PATH，这里直接跳过这一项。
        let joined = dir.join(name);
        if !joined.to_string_lossy().contains(std::path::MAIN_SEPARATOR) {
            continue;
        }
        if let Some(found) = proc::find_in_dir(&joined) {
            return Some(found);
        }
    }
    None
}

/// 平台相关的部分：起进程、读输出、等退出、杀进程组。
mod proc {
    use super::*;

    pub fn executable(p: &Path) -> bool {
        imp::executable(p)
    }

    pub fn find_in_dir(p: &Path) -> Option<PathBuf> {
        imp::find_in_dir(p)
    }

    pub fn spawn(
        program: &Path,
        argv: &[String],
        dir: &Path,
        env: &[(OsString, OsString)],
    ) -> std::io::Result<std::process::Child> {
        imp::spawn(program, argv, dir, env)
    }

    /// 读 stdout、stderr 的后台线程。
    pub struct Output {
        handle: std::thread::JoinHandle<(Tail, Tail)>,
        done: mpsc::Receiver<()>,
        abort: std::sync::Arc<std::sync::atomic::AtomicBool>,
    }

    impl Output {
        pub fn start(child: &mut std::process::Child, limit: usize) -> Output {
            let (out, err) = (child.stdout.take(), child.stderr.take());
            let abort = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
            let (done_tx, done) = mpsc::channel();
            let flag = abort.clone();
            let handle = std::thread::spawn(move || {
                let r = imp::collect(out, err, limit, &flag);
                let _ = done_tx.send(());
                r
            });
            Output {
                handle,
                done,
                abort,
            }
        }

        /// 进程已经退出：等输出读完，最多等 wait_delay；还没完（孙进程占着管道）就不等了，留下已经读到的。
        pub fn finish(self, wait_delay: Duration) -> (Tail, Tail) {
            if self.done.recv_timeout(wait_delay).is_err() {
                self.abort.store(true, std::sync::atomic::Ordering::SeqCst);
            }
            imp::join(self.handle)
        }
    }

    /// 等进程退出的后台线程；kill 杀整个进程组。
    pub struct Waiter {
        pid: u32,
        exited: std::sync::Arc<std::sync::atomic::AtomicBool>,
    }

    impl Waiter {
        pub fn start(child: std::process::Child, tx: mpsc::Sender<Event>) -> Waiter {
            let pid = child.id();
            let exited = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
            let flag = exited.clone();
            std::thread::spawn(move || {
                let e = imp::wait(child);
                flag.store(true, std::sync::atomic::Ordering::SeqCst);
                let _ = tx.send(Event::Exited(e));
            });
            Waiter { pid, exited }
        }

        pub fn kill(&self) {
            imp::kill_tree(self.pid, &self.exited);
        }
    }

    #[cfg(unix)]
    mod imp {
        use super::*;
        use std::os::fd::AsRawFd;
        use std::os::unix::fs::PermissionsExt;
        use std::os::unix::process::CommandExt;
        use std::process::{Child, ChildStderr, ChildStdout, Command, Stdio};
        use std::sync::atomic::{AtomicBool, Ordering};

        /// Go 的 findExecutable：stat（跟随链接），不是目录，按有效 uid 可执行（内核不支持时看权限位）。
        pub fn executable(p: &Path) -> bool {
            let Ok(md) = std::fs::metadata(p) else {
                return false;
            };
            if md.is_dir() {
                return false;
            }
            ar_sys::eaccess_exec(p).unwrap_or(md.permissions().mode() & 0o111 != 0)
        }

        pub fn find_in_dir(p: &Path) -> Option<PathBuf> {
            executable(p).then(|| p.to_path_buf())
        }

        pub fn spawn(
            program: &Path,
            argv: &[String],
            dir: &Path,
            env: &[(OsString, OsString)],
        ) -> std::io::Result<Child> {
            let mut cmd = Command::new(program);
            cmd.arg0(&argv[0])
                .args(&argv[1..])
                .current_dir(dir)
                .env_clear()
                .envs(env.iter().map(|(k, v)| (k, v)))
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .process_group(0);
            cmd.spawn()
        }

        pub fn collect(
            out: Option<ChildStdout>,
            err: Option<ChildStderr>,
            limit: usize,
            abort: &AtomicBool,
        ) -> (Tail, Tail) {
            let mut tails = [Tail::new(limit), Tail::new(limit)];
            let mut srcs: [Option<(i32, Box<dyn Read + Send>)>; 2] = [
                out.map(|o| (o.as_raw_fd(), Box::new(o) as Box<dyn Read + Send>)),
                err.map(|e| (e.as_raw_fd(), Box::new(e) as Box<dyn Read + Send>)),
            ];
            let mut buf = vec![0u8; 64 << 10];
            while srcs.iter().any(Option::is_some) && !abort.load(Ordering::SeqCst) {
                let live: Vec<usize> = (0..2).filter(|i| srcs[*i].is_some()).collect();
                let fds: Vec<i32> = live
                    .iter()
                    .filter_map(|i| srcs[*i].as_ref().map(|(fd, _)| *fd))
                    .collect();
                let Ok(ready) = ar_sys::poll_readable(&fds, Duration::from_millis(50)) else {
                    break;
                };
                for (k, i) in live.iter().enumerate() {
                    if !ready[k] {
                        continue;
                    }
                    let Some((_, src)) = srcs[*i].as_mut() else {
                        continue;
                    };
                    match src.read(&mut buf) {
                        Ok(0) | Err(_) => srcs[*i] = None,
                        Ok(n) => tails[*i].write(&buf[..n]),
                    }
                }
            }
            let [a, b] = tails;
            (a, b)
        }

        pub fn join(h: std::thread::JoinHandle<(Tail, Tail)>) -> (Tail, Tail) {
            h.join().unwrap_or_default()
        }

        pub fn wait(child: Child) -> ar_sys::Exit {
            let pid = child.id() as i32;
            // 由我们自己回收（wait4 拿 rusage）；Child 不再用来 wait。
            std::mem::forget(child);
            ar_sys::wait4(pid).unwrap_or(ar_sys::Exit {
                code: None,
                signal: None,
                cpu: Duration::ZERO,
            })
        }

        pub fn kill_tree(pid: u32, exited: &AtomicBool) {
            let pid = pid as i32;
            // 进程组：直接的进程退出了，组里可能还有子进程；组号在组里还有进程时不会被复用。
            let _ = ar_sys::kill_group(pid, ar_sys::SIGKILL);
            if !exited.load(Ordering::SeqCst) {
                let _ = ar_sys::kill(pid, ar_sys::SIGKILL);
            }
        }
    }

    #[cfg(windows)]
    mod imp {
        use super::*;
        use std::process::{Child, ChildStderr, ChildStdout, Command, Stdio};
        use std::sync::atomic::{AtomicBool, Ordering};
        use std::sync::{Arc, Mutex};

        // Windows 上只为本机跑测试（与 Go 版 proc_windows.go 一样）：没有进程组，用 taskkill /T 杀命令和它的子进程；
        // CPU 时间不记（0）。

        pub fn executable(p: &Path) -> bool {
            p.is_file() || find_in_dir(p).is_some()
        }

        pub fn find_in_dir(p: &Path) -> Option<PathBuf> {
            if p.extension().is_some() && p.is_file() {
                return Some(p.to_path_buf());
            }
            let exts = std::env::var("PATHEXT").unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD".into());
            for ext in exts.split(';').filter(|e| !e.is_empty()) {
                let mut cand = p.as_os_str().to_os_string();
                cand.push(ext.to_ascii_lowercase());
                let cand = PathBuf::from(cand);
                if cand.is_file() {
                    return Some(cand);
                }
            }
            None
        }

        pub fn spawn(
            program: &Path,
            argv: &[String],
            dir: &Path,
            env: &[(OsString, OsString)],
        ) -> std::io::Result<Child> {
            Command::new(program)
                .args(&argv[1..])
                .current_dir(dir)
                .env_clear()
                .envs(env.iter().map(|(k, v)| (k, v)))
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
        }

        pub fn collect(
            out: Option<ChildStdout>,
            err: Option<ChildStderr>,
            limit: usize,
            abort: &AtomicBool,
        ) -> (Tail, Tail) {
            let tails = Arc::new(Mutex::new([Tail::new(limit), Tail::new(limit)]));
            let mut readers = Vec::new();
            let srcs: [Option<Box<dyn Read + Send>>; 2] =
                [out.map(|o| Box::new(o) as _), err.map(|e| Box::new(e) as _)];
            for (i, src) in srcs.into_iter().enumerate() {
                let Some(mut src) = src else { continue };
                let t = tails.clone();
                readers.push(std::thread::spawn(move || {
                    let mut buf = vec![0u8; 64 << 10];
                    while let Ok(n) = src.read(&mut buf) {
                        if n == 0 {
                            break;
                        }
                        t.lock().unwrap_or_else(|e| e.into_inner())[i].write(&buf[..n]);
                    }
                }));
            }
            while readers.iter().any(|r| !r.is_finished()) && !abort.load(Ordering::SeqCst) {
                std::thread::sleep(Duration::from_millis(20));
            }
            // 没读完的线程留着（管道关了自己退出），拿已经读到的。
            let mut g = tails.lock().unwrap_or_else(|e| e.into_inner());
            let a = std::mem::take(&mut g[0]);
            let b = std::mem::take(&mut g[1]);
            (a, b)
        }

        pub fn join(h: std::thread::JoinHandle<(Tail, Tail)>) -> (Tail, Tail) {
            h.join().unwrap_or_default()
        }

        pub fn wait(mut child: Child) -> ar_sys::Exit {
            let code = child.wait().ok().and_then(|s| s.code());
            ar_sys::Exit {
                code: Some(code.unwrap_or(-1)),
                signal: None,
                cpu: Duration::ZERO,
            }
        }

        pub fn kill_tree(pid: u32, _exited: &AtomicBool) {
            let _ = Command::new("taskkill")
                .args(["/T", "/F", "/PID", &pid.to_string()])
                .output();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tail_keeps_the_end() {
        let mut t = Tail::new(4);
        t.write(b"ab");
        t.write(b"cd");
        assert_eq!((t.buf.as_slice(), t.truncated), (&b"abcd"[..], false));
        t.write(b"e");
        assert_eq!((t.buf.as_slice(), t.truncated), (&b"bcde"[..], true));
        let mut t = Tail::new(4);
        t.write(b"abcd");
        assert!(!t.truncated);
        t.write(b"0123456");
        assert_eq!((t.buf.as_slice(), t.truncated), (&b"3456"[..], true));
    }

    #[test]
    fn env_rules() {
        let base = vec![
            (OsString::from("PATH"), OsString::from("/bin")),
            (OsString::from("VISIBLE"), OsString::from("yes")),
        ];
        let merged = merge_env(
            &base,
            Some(&[("VISIBLE".into(), "r".into()), ("A".into(), "1".into())]),
        )
        .unwrap();
        assert_eq!(
            merged,
            vec![
                ("PATH".into(), "/bin".into()),
                ("A".into(), "1".into()),
                ("VISIBLE".into(), "r".into())
            ]
        );
        for bad in ["AGENT_TOKEN", "agent_x", "BAD-NAME", "1A", ""] {
            assert!(
                merge_env(&base, Some(&[(bad.into(), "x".into())])).is_err(),
                "{bad}"
            );
        }
        assert!(merge_env(&base, Some(&[("A".into(), "x\0y".into())])).is_err());
        let environ: Vec<(OsString, OsString)> = [
            ("RUSTUP_HOME", "/usr/local/rustup"),
            ("GOCACHE", "/tmp/go"),
            ("SECRET", "nope"),
            ("AGENT_TOKEN", "nope"),
        ]
        .iter()
        .map(|(k, v)| (k.into(), v.into()))
        .collect();
        let got = pass_through(&environ, "RUSTUP_HOME, GOCACHE AGENT_TOKEN missing");
        assert_eq!(
            got,
            vec![
                ("GOCACHE".into(), "/tmp/go".into()),
                ("RUSTUP_HOME".into(), "/usr/local/rustup".into())
            ]
        );
    }
}
