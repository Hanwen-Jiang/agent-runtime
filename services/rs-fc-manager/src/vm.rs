//! 一台 VM 在宿主上的全部东西，以及怎么建、怎么拆：
//!
//! ```text
//! <chroot_base>/<exec>/<env_id>/root/        jail（jailer 建；firecracker chroot 到这里，以这台 VM 的 uid 运行）
//!     vmlinux、rootfs.ext4                   内核与只读 rootfs 的硬链接（不拷 GB 级的文件）
//!     scratch.ext4                           稀疏的可写盘，大小按 disk_mb，属于这台 VM 的 uid，0600
//!     run/firecracker.socket、run/v.sock     firecracker 的 API 与 vsock（run 属于这台 VM 的 uid）
//!     firecracker.pid                        jailer 写的 firecracker 的宿主 pid（--new-pid-ns）
//! <cgroup_root>/<parent>/<env_id>/           jailer 建的 cgroup v2：cpu.max、memory.max、pids.max
//! <netns_dir>/arfc-<…>                       空的网络命名空间（ip netns add），jailer 进去后 VMM 连宿主的网络都看不到
//! <state_dir>/logs/<env_id>.log              firecracker 与客户机串口的输出，最多 1 MiB
//! ```
//!
//! 所有盘在启动前配好（不假设能热插）。客户机没有网卡：不调 PUT /network-interfaces。

use std::fs;
use std::io::{self, Read, Write};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use crate::config::Config;
use crate::http::Conn;
use crate::secrets::Token;
use crate::spec::{
    self, AGENT_PORT, API_SOCK, JailerArgs, KERNEL_IN_JAIL, ROOTFS_IN_JAIL, SCRATCH_IN_JAIL,
    TOKEN_PORT, VSOCK_UDS, VmSpec,
};
use crate::{sys, vsock};

/// 给 jailer、ip、mkfs 的 PATH；它们的环境一律清空，不继承 fc-manager 的配置。
const SAFE_PATH: &str = "/usr/sbin:/usr/bin:/sbin:/bin";
const LOG_CAP: u64 = 1 << 20;

pub struct Paths {
    pub jail: PathBuf,
    pub root: PathBuf,
    pub cgroup: PathBuf,
    pub netns_name: String,
    pub netns: PathBuf,
    pub log: PathBuf,
    exec_name: String,
}

impl Paths {
    pub fn new(cfg: &Config, env_id: &str) -> Paths {
        let exec_name = cfg.exec_name();
        let jail = cfg.chroot_base.join(&exec_name).join(env_id);
        let netns_name = spec::netns_name(env_id);
        Paths {
            root: jail.join("root"),
            jail,
            cgroup: cfg.cgroup_root.join(&cfg.parent_cgroup).join(env_id),
            netns: cfg.netns_dir.join(&netns_name),
            netns_name,
            log: cfg.state_dir.join("logs").join(format!("{env_id}.log")),
            exec_name,
        }
    }

    pub fn in_root(&self, jail_path: &str) -> PathBuf {
        self.root.join(jail_path.trim_start_matches('/'))
    }

    pub fn api_sock(&self) -> PathBuf {
        self.in_root(API_SOCK)
    }

    pub fn vsock(&self) -> PathBuf {
        self.in_root(VSOCK_UDS)
    }

    pub fn token_sock(&self) -> PathBuf {
        vsock::host_port_path(&self.vsock(), TOKEN_PORT)
    }

    pub fn pid_file(&self) -> PathBuf {
        self.root.join(format!("{}.pid", self.exec_name))
    }
}

#[derive(Debug)]
pub enum VmError {
    /// VM 没能起来（jailer 失败、客户机不应答）：调用方可以换个时间或换台宿主再试（transient）。
    Boot(String),
    /// firecracker 拒绝了配置：重试也一样。
    Rejected(String),
    /// 宿主上的操作失败（文件、命令）。原因只进日志，可能带宿主的路径。
    Host(String),
    /// 建的过程中被销毁了。
    Cancelled,
}

impl VmError {
    /// 给日志的原因（可能带宿主路径，不进响应）。
    pub fn detail(&self) -> String {
        match self {
            VmError::Boot(m) | VmError::Rejected(m) | VmError::Host(m) => m.clone(),
            VmError::Cancelled => "建的过程中被销毁".into(),
        }
    }
}

fn host(what: &str, e: io::Error) -> VmError {
    VmError::Host(format!("{what}：{e}"))
}

pub fn random_hex(bytes: usize) -> io::Result<String> {
    let mut buf = vec![0u8; bytes];
    fs::File::open("/dev/urandom")?.read_exact(&mut buf)?;
    Ok(buf.iter().map(|b| format!("{b:02x}")).collect())
}

pub fn new_env_id() -> io::Result<String> {
    Ok(format!("{}{}", spec::ENV_ID_PREFIX, random_hex(8)?))
}

fn chown(cfg: &Config, path: &Path, uid: u32) -> Result<(), VmError> {
    if cfg.allow_non_root && sys::geteuid() != 0 {
        return Ok(());
    }
    std::os::unix::fs::chown(path, Some(uid), Some(uid)).map_err(|e| host("chown", e))
}

fn run_tool(bin: &Path, args: &[&str], what: &str) -> Result<(), VmError> {
    let out = Command::new(bin)
        .args(args)
        .env_clear()
        .env("PATH", SAFE_PATH)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| host(what, e))?;
    if out.status.success() {
        return Ok(());
    }
    let err = String::from_utf8_lossy(&out.stderr);
    Err(VmError::Host(format!(
        "{what} 失败（{}）：{}",
        out.status,
        err.trim().chars().take(300).collect::<String>()
    )))
}

fn link(src: &Path, dst: &Path) -> Result<(), VmError> {
    fs::hard_link(src, dst).map_err(|e| {
        if e.kind() == io::ErrorKind::CrossesDevices {
            VmError::Host("内核与 rootfs 要和 jail 在同一个文件系统上（硬链接进 jail）".into())
        } else {
            host("把内核或 rootfs 链接进 jail", e)
        }
    })
}

/// jail 里的文件：目录、内核与 rootfs 的硬链接、scratch 盘。
pub fn prepare(
    cfg: &Config,
    p: &Paths,
    spec: &VmSpec,
    uid: u32,
    rootfs: &Path,
) -> Result<(), VmError> {
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o755)
        .create(&p.root)
        .map_err(|e| host("建 jail", e))?;
    let run = p.in_root("/run");
    fs::DirBuilder::new()
        .mode(0o700)
        .create(&run)
        .map_err(|e| host("建 jail 的 /run", e))?;
    chown(cfg, &run, uid)?;
    link(&cfg.kernel, &p.in_root(KERNEL_IN_JAIL))?;
    link(rootfs, &p.in_root(ROOTFS_IN_JAIL))?;
    let scratch = p.in_root(SCRATCH_IN_JAIL);
    let f = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&scratch)
        .map_err(|e| host("建 scratch", e))?;
    // 稀疏文件：宿主上只占写进去的部分；客户机最多写到这个大小，写满在客户机里是 ENOSPC。
    f.set_len(spec.disk_mib << 20)
        .map_err(|e| host("设 scratch 的大小", e))?;
    drop(f);
    if let Some(mkfs) = &cfg.mkfs_bin {
        let path = scratch.to_string_lossy().into_owned();
        run_tool(
            mkfs,
            &[
                "-q",
                "-F",
                "-m",
                "0",
                "-E",
                "lazy_itable_init=1,lazy_journal_init=1",
                &path,
            ],
            "mkfs.ext4",
        )?;
    }
    chown(cfg, &scratch, uid)
}

struct CappedLog {
    file: fs::File,
    written: u64,
}

impl CappedLog {
    fn write(&mut self, data: &[u8]) {
        if self.written >= LOG_CAP {
            return;
        }
        let take = data.len().min((LOG_CAP - self.written) as usize);
        let _ = self.file.write_all(&data[..take]);
        self.written += take as u64;
        if self.written >= LOG_CAP {
            let _ = self
                .file
                .write_all("\n[fc-manager：日志超过 1 MiB，之后的丢弃]\n".as_bytes());
        }
    }
}

fn pump_log<R: Read + Send + 'static>(mut r: R, log: Arc<Mutex<CappedLog>>) {
    std::thread::spawn(move || {
        let mut buf = [0u8; 8192];
        while let Ok(n) = r.read(&mut buf) {
            if n == 0 {
                break;
            }
            log.lock()
                .unwrap_or_else(|e| e.into_inner())
                .write(&buf[..n]);
        }
    });
}

/// 建 netns、起 jailer，等它在新的 PID 命名空间里起好 firecracker 后退出；返回 firecracker 的宿主 pid 与启动时间。
pub fn spawn(
    cfg: &Config,
    p: &Paths,
    env_id: &str,
    spec: &VmSpec,
    uid: u32,
) -> Result<(i32, Option<u64>), VmError> {
    run_tool(
        &cfg.ip_bin,
        &["netns", "add", &p.netns_name],
        "ip netns add",
    )?;
    let args = spec::jailer_args(&JailerArgs {
        env_id,
        firecracker_bin: &cfg.firecracker_bin,
        uid,
        chroot_base: &cfg.chroot_base,
        netns: &p.netns,
        parent_cgroup: &cfg.parent_cgroup,
        spec,
        overhead_mib: cfg.vmm_overhead_mib,
    });
    let file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(&p.log)
        .map_err(|e| host("建 VM 的日志", e))?;
    let log = Arc::new(Mutex::new(CappedLog { file, written: 0 }));
    let mut child = Command::new(&cfg.jailer_bin)
        .args(&args)
        .env_clear()
        .env("PATH", SAFE_PATH)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .spawn()
        .map_err(|e| host("起 jailer", e))?;
    if let Some(out) = child.stdout.take() {
        pump_log(out, log.clone());
    }
    if let Some(err) = child.stderr.take() {
        pump_log(err, log);
    }
    let deadline = Instant::now() + Duration::from_secs(10);
    let status = loop {
        match child.try_wait() {
            Ok(Some(s)) => break s,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(10)),
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(VmError::Boot("jailer 10 秒内没有退出".into()));
            }
            Err(e) => return Err(host("等 jailer", e)),
        }
    };
    if !status.success() {
        return Err(VmError::Boot(format!("jailer 失败（{status}）")));
    }
    let pid_file = p.pid_file();
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        if let Some(pid) = fs::read_to_string(&pid_file)
            .ok()
            .and_then(|t| t.trim().parse::<i32>().ok())
            .filter(|p| *p > 1)
        {
            return Ok((pid, sys::start_time(pid)));
        }
        if Instant::now() >= deadline {
            return Err(VmError::Boot("jailer 没写出 firecracker 的 pid".into()));
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}

fn api_put(p: &Paths, path: &str, body: &Value) -> Result<(), VmError> {
    let mut s = UnixStream::connect(p.api_sock())
        .map_err(|e| VmError::Boot(format!("连不上 firecracker 的 API：{e}")))?;
    let _ = s.set_read_timeout(Some(Duration::from_secs(10)));
    let _ = s.set_write_timeout(Some(Duration::from_secs(10)));
    let data = body.to_string();
    let req = format!(
        "PUT {path} HTTP/1.1\r\nHost: localhost\r\nAccept: application/json\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{data}",
        data.len()
    );
    s.write_all(req.as_bytes())
        .map_err(|e| VmError::Boot(format!("写 firecracker 的 API：{e}")))?;
    let mut c = Conn::new(s);
    let h = c
        .read_response()
        .map_err(|e| VmError::Boot(format!("firecracker 没有应答 PUT {path}：{e}")))?;
    if (200..300).contains(&h.status) {
        return Ok(());
    }
    let detail = h
        .body()
        .ok()
        .and_then(|b| c.read_body(b, 64 << 10).ok())
        .unwrap_or_default();
    crate::warn!(
        "firecracker 拒绝了 PUT {path}（HTTP {}）：{}",
        h.status,
        String::from_utf8_lossy(&detail).trim()
    );
    Err(VmError::Rejected(format!(
        "firecracker 拒绝了 PUT {path}（HTTP {}）",
        h.status
    )))
}

fn wait_for_path(path: &Path, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if path.exists() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    false
}

/// 经 API 配好机器、内核、两块盘与 vsock。
pub fn configure(cfg: &Config, p: &Paths, spec: &VmSpec) -> Result<(), VmError> {
    if !wait_for_path(&p.api_sock(), Duration::from_secs(5)) {
        return Err(VmError::Boot(
            "firecracker 的 API socket 5 秒内没有出现".into(),
        ));
    }
    api_put(
        p,
        "/machine-config",
        &json!({"vcpu_count": spec.vcpus, "mem_size_mib": spec.memory_mib, "smt": false}),
    )?;
    api_put(
        p,
        "/boot-source",
        &json!({"kernel_image_path": KERNEL_IN_JAIL, "boot_args": spec::boot_args(&cfg.guest_init)}),
    )?;
    api_put(
        p,
        "/drives/rootfs",
        &json!({"drive_id": "rootfs", "path_on_host": ROOTFS_IN_JAIL, "is_root_device": true, "is_read_only": true}),
    )?;
    api_put(
        p,
        "/drives/scratch",
        &json!({"drive_id": "scratch", "path_on_host": SCRATCH_IN_JAIL, "is_root_device": false, "is_read_only": false}),
    )?;
    api_put(
        p,
        "/vsock",
        &json!({"guest_cid": spec::GUEST_CID, "uds_path": VSOCK_UDS}),
    )
}

/// 客户机取令牌的端口：在 InstanceStart 之前开好，交出一次就关掉删掉。
pub fn token_listener(cfg: &Config, p: &Paths, uid: u32) -> Result<UnixListener, VmError> {
    let path = p.token_sock();
    let _ = fs::remove_file(&path);
    let l = UnixListener::bind(&path).map_err(|e| host("开令牌的 vsock 端口", e))?;
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600))
        .map_err(|e| host("令牌端口的权限", e))?;
    chown(cfg, &path, uid)?;
    l.set_nonblocking(true).map_err(|e| host("令牌端口", e))?;
    Ok(l)
}

pub fn start(p: &Paths) -> Result<(), VmError> {
    api_put(p, "/actions", &json!({"action_type": "InstanceStart"}))
}

/// 等客户机里的 agent（PID 1，最先起来的进程）来取令牌：写一行就关。之后这个端口不存在，客户机里后起的进程取不到。
pub fn deliver_token(
    p: &Paths,
    l: UnixListener,
    token: &Token,
    deadline: Instant,
) -> Result<(), VmError> {
    let res = loop {
        match l.accept() {
            Ok((mut s, _)) => {
                let _ = s.set_nonblocking(false);
                let _ = s.set_write_timeout(Some(Duration::from_secs(2)));
                let mut line = token.expose().as_bytes().to_vec();
                line.push(b'\n');
                break s
                    .write_all(&line)
                    .map_err(|e| VmError::Boot(format!("交令牌时客户机断开：{}", e.kind())));
            }
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                if Instant::now() >= deadline {
                    break Err(VmError::Boot("客户机里的 agent 没来取令牌".into()));
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            Err(e) => break Err(host("等客户机取令牌", e)),
        }
    };
    drop(l);
    let _ = fs::remove_file(p.token_sock());
    res
}

fn healthz(p: &Paths) -> io::Result<()> {
    let mut c = vsock::connect(&p.vsock(), AGENT_PORT, Duration::from_secs(2))?;
    c.get_mut().set_read_timeout(Some(Duration::from_secs(2)))?;
    c.get_mut()
        .write_all(b"GET /healthz HTTP/1.1\r\nHost: sandbox-agent\r\nConnection: close\r\n\r\n")?;
    let h = c.read_response().map_err(io::Error::other)?;
    if h.status == 200 {
        Ok(())
    } else {
        Err(io::Error::other("agent 还没就绪"))
    }
}

pub fn wait_healthy(p: &Paths, deadline: Instant) -> Result<(), VmError> {
    loop {
        if healthz(p).is_ok() {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err(VmError::Boot("客户机里的 agent 没有就绪".into()));
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

fn read_procs(cgroup: &Path) -> Vec<i32> {
    fs::read_to_string(cgroup.join("cgroup.procs"))
        .unwrap_or_default()
        .lines()
        .filter_map(|l| l.trim().parse().ok())
        .collect()
}

/// 拆掉一台 VM 在宿主上的全部东西；重复调用、东西本来就不在都不算错。pid 与 start 是记录里的（可能没有）。
/// 只杀确认是这台 VM 的进程：记录里的 pid 要对得上启动时间，jail 里 pid 文件的、假 cgroup 里的要命令行里有 `--id <env_id>`；
/// 真的 cgroup v2 里的进程一定是它的（jailer 把它放进去的），先写 cgroup.kill。
pub fn teardown(
    cfg: &Config,
    env_id: &str,
    pid: Option<i32>,
    start: Option<u64>,
) -> Result<(), String> {
    if !spec::valid_env_id(env_id) {
        return Err(format!("环境 ID 格式不对：{env_id}"));
    }
    let p = Paths::new(cfg, env_id);
    let real_cgroup = sys::is_cgroup2(&cfg.cgroup_root).unwrap_or(false);
    let mut victims: Vec<i32> = vec![];
    if let Some(pid) = pid.filter(|p| sys::is_ours(*p, start, env_id)) {
        victims.push(pid);
    }
    if let Some(from_file) = fs::read_to_string(p.pid_file())
        .ok()
        .and_then(|t| t.trim().parse::<i32>().ok())
    {
        let known_start = if Some(from_file) == pid { start } else { None };
        if sys::is_ours(from_file, known_start, env_id) {
            victims.push(from_file);
        }
    }
    if real_cgroup && p.cgroup.join("cgroup.kill").exists() {
        let _ = fs::write(p.cgroup.join("cgroup.kill"), "1");
    }
    for v in read_procs(&p.cgroup) {
        if (real_cgroup && sys::alive(v)) || sys::is_ours(v, None, env_id) {
            victims.push(v);
        }
    }
    victims.sort_unstable();
    victims.dedup();
    for v in &victims {
        let _ = sys::kill(*v, sys::SIGKILL);
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    while victims.iter().any(|v| sys::alive(*v))
        || (real_cgroup && !read_procs(&p.cgroup).is_empty())
    {
        if Instant::now() >= deadline {
            return Err(format!("{env_id} 的 VM 进程 5 秒内没有退出"));
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    if p.cgroup.exists() {
        if real_cgroup {
            // 进程刚退出时 rmdir 可能还是 EBUSY，稍等再试。
            let deadline = Instant::now() + Duration::from_secs(2);
            while let Err(e) = fs::remove_dir(&p.cgroup) {
                if e.kind() == io::ErrorKind::NotFound {
                    break;
                }
                if Instant::now() >= deadline {
                    return Err(format!("删不掉 {env_id} 的 cgroup：{e}"));
                }
                std::thread::sleep(Duration::from_millis(20));
            }
        } else {
            fs::remove_dir_all(&p.cgroup).map_err(|e| format!("删不掉 {env_id} 的 cgroup：{e}"))?;
        }
    }
    if p.netns.exists() {
        run_tool(
            &cfg.ip_bin,
            &["netns", "delete", &p.netns_name],
            "ip netns delete",
        )
        .map_err(|e| e.detail())?;
        if p.netns.exists() {
            return Err(format!("{env_id} 的 netns 还在"));
        }
    }
    if p.jail.exists() {
        // remove_dir_all 不跟随符号链接（jail 里的东西由不可信的 VMM 进程写过）。
        fs::remove_dir_all(&p.jail).map_err(|e| format!("删不掉 {env_id} 的 jail：{e}"))?;
    }
    match fs::remove_file(&p.log) {
        Err(e) if e.kind() != io::ErrorKind::NotFound => {
            Err(format!("删不掉 {env_id} 的日志：{e}"))
        }
        _ => Ok(()),
    }
}

/// 进程还是记录里那台 VM 的 firecracker。
pub fn running(env_id: &str, pid: Option<i32>, start: Option<u64>) -> bool {
    pid.is_some_and(|p| sys::is_ours(p, start, env_id))
}
