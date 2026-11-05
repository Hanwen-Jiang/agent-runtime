//! Firecracker 客户机里的 PID 1（`sandbox-agent --init`，D-20 第二段、D-22、SBX-ZOMBIE）。fc-manager 的内核命令行是
//! `init=<agent> AGENT_LISTEN=vsock:700 AGENT_TOKEN_VSOCK=701 -- --init`（services/rs-fc-manager/src/spec.rs），内核把
//! `KEY=VALUE` 交给 init 当环境变量、`--` 之后的当参数。
//!
//! PID 1 做的事，按顺序：
//!
//! 1. 挂 /proc、/sys、/dev（devtmpfs，加 /dev/pts、/dev/shm）；根是只读的 /dev/vda。
//! 2. 把 /dev/vdb（fc-manager 用 mkfs.ext4 格式化好的 scratch）挂到 /mnt/scratch，在里面建 workspace 与 tmp，
//!    绑定挂到 /workspace 与 /tmp（nosuid、nodev）。可写的只有这两处，与 K8s 后端一致。这三个挂载点由 D-21 的
//!    rootfs 转换建好（deploy/images/firecracker/rootfs.sh）。
//! 3. 主机名 sandbox，lo 设成 up。
//! 4. 连宿主（CID 2）的 AGENT_TOKEN_VSOCK 端口读一行令牌（fc-manager 交出一次就关掉这个端口）。
//! 5. 起 agent 子进程（同一个二进制，不带参数）：环境清空后只给镜像的环境（/etc/agentruntime/env，D-21 转换时从镜像
//!    配置写出）与 AGENT_LISTEN、AGENT_WORKDIR、AGENT_UID；令牌经标准输入的管道给（不进环境块与命令行）。agent 以 root
//!    绑好 vsock 700（小于 1024，降权之后的命令占不了），然后降到 10001。
//! 6. 起转发线程（D-22）：127.0.0.1:3128 → 宿主 vsock 1080（出网网关），127.0.0.1:3129 → 1081（包镜像）；vsock 702
//!    收宿主来的预览连接，第一行 `PORT <n>` 之后转到 127.0.0.1:<n>。目标都写死在这里，宿主那一侧的目标写在 fc-manager
//!    的配置里，谁都不解析对方给的地址。
//! 7. 之后只回收子进程：孤儿（命令留下的后台进程，父进程先退了）都过继给 PID 1，在这里 wait 掉，不留僵尸（SBX-ZOMBIE）。
//!    agent 子进程自己退出了，这台 VM 就没用了（令牌只交一次）：写回盘、关机，fc-manager 发现 VM 没了，Sandbox Service
//!    记 lost、下一条命令透明换新（D-13）。
//!
//! agent 自己的命令照旧由 agent 等（它是这些命令的父进程），PID 1 的 wait 只会收到过继来的孤儿与 agent 本身，不会抢走
//! agent 子进程的退出状态：这是把 init 与 agent 分成两个进程的原因（Go 版在同一个进程里做会撞上 os/exec 的 wait）。

use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::{Shutdown, TcpListener, TcpStream};
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::{Command, ExitCode, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use ar_sys::guest;

/// 客户机里跑命令的 uid、gid（与 K8s 后端的 run_as_user 一致）。
pub const SANDBOX_UID: u32 = 10001;
/// 环回转发器：客户机里的地址 → 宿主的 vsock 端口。出网网关、包镜像各一条（D-22）。
pub const FORWARDS: &[(u16, u32)] = &[(3128, 1080), (3129, 1081)];
/// 宿主来的预览连接（D-22）。
pub const PREVIEW_VSOCK_PORT: u32 = 702;
/// 转发的连接同时最多多少条（每台 VM）。
const MAX_FORWARD_CONNS: usize = 256;
const IMAGE_ENV_FILE: &str = "/etc/agentruntime/env";

fn log(msg: &str) {
    // PID 1 的标准错误是串口控制台（console=ttyS0），fc-manager 把它记进这台 VM 的日志。
    eprintln!("sandbox-agent init: {msg}");
}

fn mount_or_log(src: &str, target: &str, fstype: &str, flags: u64, data: &str) -> io::Result<()> {
    guest::mount(src, Path::new(target), fstype, flags, data).map_err(|e| {
        log(&format!("挂 {target}（{fstype}）失败：{e}"));
        e
    })
}

fn setup_filesystems() -> io::Result<()> {
    let nsd = guest::MS_NOSUID | guest::MS_NODEV;
    mount_or_log("proc", "/proc", "proc", nsd | guest::MS_NOEXEC, "")?;
    mount_or_log("sysfs", "/sys", "sysfs", nsd | guest::MS_NOEXEC, "")?;
    mount_or_log(
        "devtmpfs",
        "/dev",
        "devtmpfs",
        guest::MS_NOSUID,
        "mode=0755",
    )?;
    for d in ["/dev/pts", "/dev/shm"] {
        let _ = std::fs::create_dir(d);
    }
    mount_or_log(
        "devpts",
        "/dev/pts",
        "devpts",
        guest::MS_NOSUID | guest::MS_NOEXEC,
        "mode=0620,ptmxmode=0666",
    )?;
    mount_or_log("tmpfs", "/dev/shm", "tmpfs", nsd, "mode=1777")?;
    if Path::new("/run").is_dir() {
        mount_or_log("tmpfs", "/run", "tmpfs", nsd, "mode=0755")?;
    }
    mount_or_log("/dev/vdb", "/mnt/scratch", "ext4", nsd, "")?;
    for (sub, target, mode) in [("workspace", "/workspace", 0o755), ("tmp", "/tmp", 0o1777)] {
        let dir = Path::new("/mnt/scratch").join(sub);
        std::fs::create_dir_all(&dir)?;
        std::os::unix::fs::chown(&dir, Some(SANDBOX_UID), Some(SANDBOX_UID))?;
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(mode))?;
        mount_or_log(&dir.to_string_lossy(), target, "", guest::MS_BIND, "")?;
        // 绑定挂载的 nosuid、nodev 要再 remount 一次才生效。
        mount_or_log("", target, "", guest::MS_BIND | guest::MS_REMOUNT | nsd, "")?;
    }
    Ok(())
}

fn read_token(port: u32) -> io::Result<String> {
    let mut last = io::Error::other("没试过");
    for _ in 0..50 {
        match guest::vsock_connect(guest::VMADDR_CID_HOST, port) {
            Ok(s) => {
                s.set_read_timeout(Some(Duration::from_secs(5)))?;
                let mut line = String::new();
                BufReader::new(s.take(4096)).read_line(&mut line)?;
                let t = line.trim().to_string();
                if t.len() < 32 {
                    return Err(io::Error::other("宿主给的令牌太短"));
                }
                return Ok(t);
            }
            Err(e) => last = e,
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    Err(last)
}

/// 镜像的环境（`KEY=VALUE` 一行一个，D-21 转换时从镜像配置的 Env 写出）。名字不合法的、AGENT_ 开头的跳过。
fn image_env() -> Vec<(String, String)> {
    let Ok(text) = std::fs::read_to_string(IMAGE_ENV_FILE) else {
        return vec![];
    };
    text.lines()
        .filter_map(|l| l.split_once('='))
        .filter(|(k, _)| {
            !k.is_empty()
                && !k.starts_with("AGENT_")
                && k.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
                && !k.as_bytes()[0].is_ascii_digit()
        })
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect()
}

fn spawn_agent(token: &str) -> io::Result<std::process::Child> {
    let exe = std::env::current_exe()?;
    let env = image_env();
    let passthrough: Vec<&str> = env
        .iter()
        .map(|(k, _)| k.as_str())
        .filter(|k| !["PATH", "HOME", "LANG", "LC_ALL", "TZ"].contains(k))
        .collect();
    let mut cmd = Command::new(exe);
    cmd.env_clear()
        .envs(env.iter().map(|(k, v)| (k.as_str(), v.as_str())))
        .env("HOME", "/tmp")
        .env(
            "AGENT_LISTEN",
            std::env::var("AGENT_LISTEN").unwrap_or_else(|_| "vsock:700".into()),
        )
        .env("AGENT_WORKDIR", "/workspace")
        .env("AGENT_UID", SANDBOX_UID.to_string())
        .env("AGENT_TOKEN_STDIN", "1")
        .env("AGENT_ENV_PASSTHROUGH", passthrough.join(" "))
        .current_dir("/workspace")
        .stdin(Stdio::piped());
    if !env.iter().any(|(k, _)| k == "PATH") {
        cmd.env(
            "PATH",
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        );
    }
    let mut child = cmd.spawn()?;
    let mut stdin = child.stdin.take().expect("piped");
    stdin.write_all(token.as_bytes())?;
    stdin.write_all(b"\n")?;
    drop(stdin);
    Ok(child)
}

fn pipe(a: TcpStream, b: TcpStream) {
    let (Ok(a2), Ok(b2)) = (a.try_clone(), b.try_clone()) else {
        return;
    };
    let t = std::thread::spawn(move || {
        let (mut r, mut w) = (a2, b2);
        let _ = io::copy(&mut r, &mut w);
        let _ = w.shutdown(Shutdown::Write);
    });
    let (mut r, mut w) = (b, a);
    let _ = io::copy(&mut r, &mut w);
    let _ = w.shutdown(Shutdown::Write);
    let _ = t.join();
}

struct Slots(AtomicUsize);

impl Slots {
    fn take(self: &Arc<Self>) -> Option<SlotGuard> {
        if self.0.fetch_add(1, Ordering::SeqCst) >= MAX_FORWARD_CONNS {
            self.0.fetch_sub(1, Ordering::SeqCst);
            return None;
        }
        Some(SlotGuard(self.clone()))
    }
}

struct SlotGuard(Arc<Slots>);

impl Drop for SlotGuard {
    fn drop(&mut self) {
        self.0.0.fetch_sub(1, Ordering::SeqCst);
    }
}

fn spawn_conn(slots: &Arc<Slots>, f: impl FnOnce() + Send + 'static) {
    let Some(guard) = slots.take() else { return };
    let _ = std::thread::Builder::new()
        .stack_size(64 << 10)
        .spawn(move || {
            f();
            drop(guard);
        });
}

/// 客户机里的 127.0.0.1:<local> → 宿主的 vsock <host_port>。
fn forward_out(local: u16, host_port: u32, slots: Arc<Slots>) {
    let l = match TcpListener::bind(("127.0.0.1", local)) {
        Ok(l) => l,
        Err(e) => return log(&format!("开不了 127.0.0.1:{local} 的转发：{e}")),
    };
    for c in l.incoming() {
        let Ok(c) = c else { continue };
        spawn_conn(&slots, move || {
            match guest::vsock_connect(guest::VMADDR_CID_HOST, host_port) {
                Ok(up) => pipe(c, up),
                Err(_) => {
                    let _ = c.shutdown(Shutdown::Both);
                }
            }
        });
    }
}

/// 宿主来的预览连接：第一行 `PORT <n>\n`，转到客户机里的 127.0.0.1:<n>（dev server 听 0.0.0.0 或 127.0.0.1 都行）。
fn preview_in(slots: Arc<Slots>) {
    let l = match guest::vsock_listen(PREVIEW_VSOCK_PORT, 64) {
        Ok(l) => l,
        Err(e) => return log(&format!("开不了预览的 vsock 端口：{e}")),
    };
    loop {
        let Ok(c) = guest::vsock_accept(&l) else {
            std::thread::sleep(Duration::from_millis(10));
            continue;
        };
        spawn_conn(&slots, move || {
            let _ = c.set_read_timeout(Some(Duration::from_secs(5)));
            let mut line = Vec::new();
            let mut byte = [0u8; 1];
            // 逐字节读到换行：不能多读，后面的字节属于转发的流。
            while line.len() < 16 {
                match (&c).read(&mut byte) {
                    Ok(1) if byte[0] == b'\n' => break,
                    Ok(1) => line.push(byte[0]),
                    _ => return,
                }
            }
            let port = std::str::from_utf8(&line)
                .ok()
                .and_then(|s| s.strip_prefix("PORT "))
                .and_then(|p| p.parse::<u16>().ok())
                .filter(|p| *p > 0 && !FORWARDS.iter().any(|(l, _)| l == p));
            let Some(port) = port else { return };
            let _ = c.set_read_timeout(None);
            match TcpStream::connect(("127.0.0.1", port)) {
                Ok(up) => {
                    let _ = (&c).write_all(b"OK\n");
                    pipe(c, up);
                }
                Err(_) => {
                    let _ = (&c).write_all(b"REFUSED\n");
                }
            }
        });
    }
}

pub fn run_init() -> ExitCode {
    if !guest::is_pid1() {
        log("--init 只能当 PID 1");
        return ExitCode::from(2);
    }
    let fatal = |msg: String| -> ExitCode {
        log(&msg);
        let e = guest::power_off();
        log(&format!("关机失败：{e}"));
        ExitCode::from(1)
    };
    if let Err(e) = setup_filesystems() {
        return fatal(format!("文件系统没挂好：{e}"));
    }
    let _ = guest::sethostname("sandbox");
    if let Err(e) = guest::loopback_up() {
        log(&format!("lo 没起来：{e}"));
    }
    let token_port = std::env::var("AGENT_TOKEN_VSOCK")
        .ok()
        .and_then(|p| p.parse::<u32>().ok())
        .unwrap_or(701);
    let token = match read_token(token_port) {
        Ok(t) => t,
        Err(e) => return fatal(format!("取不到令牌：{e}")),
    };
    // PID 1 自己不可转储：令牌在它的内存里（客户机里的命令虽然是另一个 uid，也不留口子）。
    let _ = ar_sys::set_not_dumpable();
    let agent = match spawn_agent(&token) {
        Ok(c) => c,
        Err(e) => return fatal(format!("起不了 agent：{e}")),
    };
    drop(token);
    let slots = Arc::new(Slots(AtomicUsize::new(0)));
    for &(local, host_port) in FORWARDS {
        let s = slots.clone();
        let _ = std::thread::Builder::new().spawn(move || forward_out(local, host_port, s));
    }
    {
        let s = slots.clone();
        let _ = std::thread::Builder::new().spawn(move || preview_in(s));
    }
    let agent_pid = agent.id() as i32;
    log(&format!("agent 已启动（pid {agent_pid}）"));
    loop {
        match guest::wait_any() {
            Ok((pid, exit)) if pid == agent_pid => {
                return fatal(format!("agent 退出了（{}），关机", exit.returncode()));
            }
            Ok(_) => {}
            Err(e) => return fatal(format!("wait 出错：{e}")),
        }
    }
}
