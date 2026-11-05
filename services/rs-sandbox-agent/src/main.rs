//! sandbox-agent（Rust 版，D-28）：在沙箱环境（K8s 的沙箱 Pod、Cloudflare 的 Container，之后是 Firecracker 的客户机）里
//! 跑，替 Runtime 的 Sandbox 适配器执行命令、读写文件。接口与行为同 services/go-sandbox-agent（见 agent.rs），黑盒测试在
//! tests/sandbox-agent，两版跑同一套。静态编译（x86_64-unknown-linux-musl），放进任何镜像都能跑。
//!
//! ```text
//! sandbox-agent                 起服务
//! sandbox-agent install <路径>  把自己拷到 <路径>：K8s 的 initContainer 用它把 agent 放进共享卷，跑命令的镜像不用自带 agent
//! sandbox-agent version
//! sandbox-agent --init          Firecracker 客户机里的 PID 1（guest.rs，D-20）：挂文件系统、取令牌、起 agent 子进程、回收孤儿
//! ```
//!
//! 环境变量：
//!
//! ```text
//! AGENT_TOKEN 或 AGENT_TOKEN_FILE  请求要带的令牌（至少 32 个字符；必填）
//! AGENT_ADDR                       监听地址，默认 :7070
//! AGENT_LISTEN=vsock:<端口>         改在 vsock 上监听（Firecracker 客户机，由 --init 设）；优先于 AGENT_ADDR
//! AGENT_TOKEN_STDIN=1              令牌从标准输入的第一行读（--init 经管道给）
//! AGENT_UID                        绑好监听之后降到这个 uid（gid 相同；只在以 root 起来时）
//! AGENT_REAPED=1                   已经有回收孤儿的 PID 1（以 PID 1 起来、没设它时先分出回收者，guest.rs 的 run_reaper）
//! AGENT_WORKDIR                    工作目录，默认 /workspace
//! AGENT_MAX_TIMEOUT_S              一条命令最长多少秒，默认 3600
//! AGENT_OUTPUT_LIMIT               stdout、stderr 各自最多保留多少字节，默认 8 MiB
//! AGENT_FILE_LIMIT                 一个文件最多多少字节，默认 64 MiB
//! AGENT_ENV_PASSTHROUGH            另外交给命令的变量名（空格或逗号分隔）：镜像用它声明工具链要的变量（D-14）
//! ```
//!
//! 命令只拿到这些环境变量：PATH、HOME（默认 /tmp）、LANG（默认 C.UTF-8）、LC_ALL、TZ（Windows 上另有 SYSTEMROOT、WINDIR、
//! PATHEXT），加上 PYTHONIOENCODING=utf-8 与 PYTHONDONTWRITEBYTECODE=1，加上 AGENT_ENV_PASSTHROUGH 列出的；AGENT_* 一个都不给。
//!
//! 命令与 agent 是同一个 uid：Linux 上 agent 启动时把自己设成不可转储，命令读不到 /proc/<agent>/environ 与 mem（AGENT-TOK-01）。

mod agent;
mod archive;
mod exec;
mod files;
mod goregex;
mod gostr;
#[cfg(target_os = "linux")]
mod guest;
mod http;
mod json;

use std::ffi::{OsStr, OsString};
use std::net::{TcpListener, ToSocketAddrs};
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Duration;

const VERSION: &str = "0.1.0";

/// 与 Go 的 log 包一样：`2026/11/04 19:00:00 消息`，写标准错误。
fn log(msg: &str) {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    // 只要日期与时间，不依赖时区（容器里是 UTC）。
    let date = http::http_date(std::time::UNIX_EPOCH + Duration::from_secs(now));
    let parts: Vec<&str> = date.split(' ').collect();
    let months = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    let mon = months.iter().position(|m| *m == parts[2]).unwrap_or(0) + 1;
    eprintln!("{}/{:02}/{} {} {}", parts[3], mon, parts[1], parts[4], msg);
}

fn fatal(msg: &str) -> ExitCode {
    log(msg);
    ExitCode::from(1)
}

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

fn env_or(key: &str, fallback: &str) -> String {
    env(key).unwrap_or_else(|| fallback.to_string())
}

fn int_env(key: &str, fallback: i64) -> Result<i64, String> {
    match std::env::var(key) {
        Ok(v) if !v.is_empty() => match v.parse::<i64>() {
            Ok(n) if n > 0 => Ok(n),
            _ => Err(format!("{key} 要是正整数")),
        },
        _ => Ok(fallback),
    }
}

fn command_env() -> Vec<(OsString, OsString)> {
    let mut env_list: Vec<(OsString, OsString)> = vec![
        (
            "PATH".into(),
            env_or(
                "PATH",
                "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            )
            .into(),
        ),
        ("HOME".into(), env_or("HOME", "/tmp").into()),
        ("LANG".into(), env_or("LANG", "C.UTF-8").into()),
        ("PYTHONIOENCODING".into(), "utf-8".into()),
        ("PYTHONDONTWRITEBYTECODE".into(), "1".into()),
    ];
    // SYSTEMROOT、WINDIR、PATHEXT 只在 Windows 上有：本机测试时 Python 没有 SYSTEMROOT 启动不了。
    for k in ["LC_ALL", "TZ", "SYSTEMROOT", "WINDIR", "PATHEXT"] {
        if let Some(v) = std::env::var_os(k).filter(|v| !v.is_empty()) {
            env_list.push((k.into(), v));
        }
    }
    let environ: Vec<(OsString, OsString)> = std::env::vars_os().collect();
    env_list.extend(exec::pass_through(
        &environ,
        &env_or("AGENT_ENV_PASSTHROUGH", ""),
    ));
    env_list
}

fn install(dest: &OsStr) -> std::io::Result<()> {
    let me = std::env::current_exe()?;
    let dest = PathBuf::from(dest);
    if let Some(parent) = dest.parent().filter(|p| !p.as_os_str().is_empty()) {
        files::create_dirs(parent)?;
    }
    let mut src = std::fs::File::open(me)?;
    let mut o = std::fs::OpenOptions::new();
    o.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        o.mode(0o755);
    }
    let mut out = o.open(&dest)?;
    std::io::copy(&mut src, &mut out)?;
    out.sync_all()
}

/// Go 的 net.Listen("tcp", addr)：`:端口` 听所有地址（先试 IPv6 双栈，没有 IPv6 时 IPv4）。
fn listen(addr: &str) -> std::io::Result<TcpListener> {
    if let Some(port) = addr.strip_prefix(':') {
        return TcpListener::bind(format!("[::]:{port}"))
            .or_else(|_| TcpListener::bind(format!("0.0.0.0:{port}")));
    }
    let addrs: Vec<_> = addr.to_socket_addrs()?.collect();
    TcpListener::bind(&addrs[..])
}

fn main() -> ExitCode {
    let args: Vec<OsString> = std::env::args_os().collect();
    if args.len() > 1 {
        return match args[1].to_str() {
            Some("install") => {
                if args.len() != 3 {
                    return fatal("用法：sandbox-agent install <路径>");
                }
                match install(&args[2]) {
                    Ok(()) => ExitCode::SUCCESS,
                    Err(e) => fatal(&format!("install：{e}")),
                }
            }
            #[cfg(target_os = "linux")]
            Some("--init") => guest::run_init(),
            Some("version") => {
                println!("{VERSION}");
                ExitCode::SUCCESS
            }
            _ => fatal(&format!("不认识的子命令 {}", args[1].to_string_lossy())),
        };
    }
    // 当了容器的 PID 1（K8s 沙箱 Pod）：先分出回收孤儿的 PID 1，真正的 agent 在子进程里（guest.rs 的 run_reaper，SBX-ZOMBIE）。
    #[cfg(target_os = "linux")]
    if ar_sys::guest::is_pid1() && env("AGENT_REAPED").is_none() {
        return guest::run_reaper();
    }
    // 先于读令牌：令牌在启动时的环境块里，remove_var 删不掉 /proc/<pid>/environ，只能不让命令读 agent 的 /proc。
    // 做不到就不起服务。
    if let Err(e) = ar_sys::set_not_dumpable() {
        return fatal(&format!("PR_SET_DUMPABLE：{e}"));
    }
    let mut token = std::env::var("AGENT_TOKEN").unwrap_or_default();
    if token.is_empty()
        && let Some(file) = env("AGENT_TOKEN_FILE")
    {
        match std::fs::read_to_string(&file) {
            Ok(t) => token = t.trim().to_string(),
            Err(e) => return fatal(&format!("读不了 AGENT_TOKEN_FILE：{e}")),
        }
    }
    if token.is_empty() && env("AGENT_TOKEN_STDIN").as_deref() == Some("1") {
        let mut line = String::new();
        if let Err(e) = std::io::stdin().read_line(&mut line) {
            return fatal(&format!("从标准输入读不了令牌：{e}"));
        }
        token = line.trim().to_string();
    }
    // 这时还没有别的线程。
    ar_sys::remove_env_before_threads("AGENT_TOKEN");
    let env_list = command_env();
    let (output_limit, file_limit, max_timeout) = match (
        int_env("AGENT_OUTPUT_LIMIT", 8 << 20),
        int_env("AGENT_FILE_LIMIT", 64 << 20),
        int_env("AGENT_MAX_TIMEOUT_S", 3600),
    ) {
        (Ok(a), Ok(b), Ok(c)) => (a, b, c),
        (Err(e), _, _) | (_, Err(e), _) | (_, _, Err(e)) => return fatal(&e),
    };
    let app = match agent::App::new(agent::Config {
        workdir: PathBuf::from(env_or("AGENT_WORKDIR", "/workspace")),
        token,
        env: env_list,
        output_limit: output_limit as usize,
        file_limit: file_limit as u64,
        max_timeout: Duration::from_secs(max_timeout as u64),
        wait_delay: Duration::ZERO,
    }) {
        Ok(a) => Arc::new(a),
        Err(e) => return fatal(&e),
    };
    #[cfg(target_os = "linux")]
    if let Some(port) =
        env("AGENT_LISTEN").and_then(|v| v.strip_prefix("vsock:").map(str::to_string))
    {
        let Ok(port) = port.parse::<u32>() else {
            return fatal("AGENT_LISTEN 要写成 vsock:<端口>");
        };
        let listener = match ar_sys::guest::vsock_listen(port, 128) {
            Ok(l) => l,
            Err(e) => return fatal(&format!("listen vsock {port}: {e}")),
        };
        if let Some(uid) = env("AGENT_UID") {
            let Ok(uid) = uid.parse::<u32>() else {
                return fatal("AGENT_UID 要是整数");
            };
            if ar_sys::geteuid() == 0 && uid != 0 {
                if let Err(e) = ar_sys::guest::drop_privileges(uid, uid) {
                    return fatal(&format!("降权失败：{e}"));
                }
                // 换 uid 之后内核把进程重置成可转储，再关一次。
                if let Err(e) = ar_sys::set_not_dumpable() {
                    return fatal(&format!("PR_SET_DUMPABLE：{e}"));
                }
            }
        }
        log(&format!("sandbox-agent {VERSION} 在 vsock:{port} 上运行"));
        loop {
            let stream = match ar_sys::guest::vsock_accept(&listener) {
                Ok(s) => s,
                Err(_) => {
                    std::thread::sleep(Duration::from_millis(10));
                    continue;
                }
            };
            let app = app.clone();
            let spawned = std::thread::Builder::new()
                .stack_size(256 << 10)
                .spawn(move || http::serve_conn(stream, &|ctx| app.handle(ctx)));
            if spawned.is_err() {
                std::thread::sleep(Duration::from_millis(10));
            }
        }
    }
    let addr = env_or("AGENT_ADDR", ":7070");
    let listener = match listen(&addr) {
        Ok(l) => l,
        Err(e) => return fatal(&format!("listen tcp {addr}: {e}")),
    };
    log(&format!("sandbox-agent {VERSION} 在 {addr} 上运行"));
    for conn in listener.incoming() {
        let Ok(stream) = conn else { continue };
        let app = app.clone();
        let spawned = std::thread::Builder::new()
            .stack_size(256 << 10)
            .spawn(move || http::serve_conn(stream, &|ctx| app.handle(ctx)));
        if spawned.is_err() {
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    ExitCode::from(1)
}
