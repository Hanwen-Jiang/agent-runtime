//! 测试的共用部分（只在 Linux 上）：在临时目录里摆好假的 jailer、firecracker、ip（都是 fc-fake，按文件名扮演）、
//! 内核与 rootfs 的占位文件、SecretStore 文件，起 fc-manager 进程，经 TCP 发 HTTP 请求。
//! 假的 firecracker 在 InstanceStart 之后扮演客户机：经 vsock 的 UDS 取令牌，再在 v.sock 上应答 agent 的请求
//! （src/bin/fc-fake.rs）。所以没有 KVM 的机器上也能测 fc-manager 的全部宿主侧逻辑。
#![allow(dead_code)]

use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

pub const TOKEN: &str = "test-manager-token-0123456789abcdef0123456789";
pub const PARENT_CGROUP: &str = "agentruntime-fc";

static SEQ: AtomicU32 = AtomicU32::new(0);

/// 一套隔离的目录与配置；Drop 时删掉。
pub struct Rig {
    pub root: PathBuf,
    extra_env: Vec<(String, String)>,
}

impl Rig {
    pub fn new() -> Rig {
        // 路径要短：Unix socket 的路径最长 107 字节，jail 里最深的是 …/root/run/v.sock_<端口>。
        let root = PathBuf::from(format!(
            "/tmp/fcm{}-{}",
            std::process::id(),
            SEQ.fetch_add(1, Ordering::SeqCst)
        ));
        let _ = fs::remove_dir_all(&root);
        for d in ["bin", "j", "s", "cg", "ns", "img"] {
            fs::create_dir_all(root.join(d)).unwrap();
        }
        let fake = PathBuf::from(env!("CARGO_BIN_EXE_fc-fake"));
        for name in ["jailer", "firecracker", "ip"] {
            fs::copy(&fake, root.join("bin").join(name)).unwrap();
        }
        fs::write(root.join("img/vmlinux"), b"fake kernel").unwrap();
        fs::write(root.join("img/python.ext4"), b"fake rootfs").unwrap();
        let rig = Rig {
            root,
            extra_env: vec![],
        };
        rig.write_secrets(TOKEN);
        rig.set_fake(json!({}));
        rig
    }

    /// 额外的 fc-manager 环境变量（覆盖默认的）。
    pub fn with(mut self, key: &str, value: &str) -> Rig {
        self.extra_env.push((key.to_string(), value.to_string()));
        self
    }

    /// 假 jailer、firecracker、ip 的行为（bin/fake.json；jailer 把它拷进每个 jail）。
    pub fn set_fake(&self, behaviour: Value) {
        let mut v = json!({
            "cgroup_root": self.root.join("cg"),
            "netns_dir": self.root.join("ns"),
        });
        if let (Some(dst), Some(src)) = (v.as_object_mut(), behaviour.as_object()) {
            for (k, val) in src {
                dst.insert(k.clone(), val.clone());
            }
        }
        fs::write(
            self.root.join("bin/fake.json"),
            serde_json::to_vec(&v).unwrap(),
        )
        .unwrap();
    }

    pub fn write_secrets(&self, token: &str) {
        let doc = json!({"version": 1, "scopes": {"_platform": {"secrets": {"sandbox/fc-manager-token": token}, "roles": []}}});
        let tmp = self.root.join("secrets.json.tmp");
        fs::write(&tmp, serde_json::to_vec(&doc).unwrap()).unwrap();
        fs::rename(&tmp, self.root.join("secrets.json")).unwrap();
    }

    pub fn path(&self, rel: &str) -> PathBuf {
        self.root.join(rel)
    }

    pub fn jail(&self, env_id: &str) -> PathBuf {
        self.root.join("j/firecracker").join(env_id)
    }

    pub fn env(&self) -> Vec<(String, String)> {
        let p = |rel: &str| self.root.join(rel).to_string_lossy().into_owned();
        let mut env: Vec<(String, String)> = vec![
            ("PATH".into(), "/usr/local/bin:/usr/bin:/bin".into()),
            ("AR_FC_LISTEN".into(), "127.0.0.1:0".into()),
            ("AR_SECRETS_FILE".into(), p("secrets.json")),
            ("AR_FC_STATE_DIR".into(), p("s")),
            ("AR_FC_CHROOT_BASE".into(), p("j")),
            ("AR_FC_JAILER_BIN".into(), p("bin/jailer")),
            ("AR_FC_FIRECRACKER_BIN".into(), p("bin/firecracker")),
            ("AR_FC_IP_BIN".into(), p("bin/ip")),
            ("AR_FC_MKFS_BIN".into(), String::new()),
            ("AR_FC_CGROUP_ROOT".into(), p("cg")),
            ("AR_FC_PARENT_CGROUP".into(), PARENT_CGROUP.into()),
            ("AR_FC_NETNS_DIR".into(), p("ns")),
            ("AR_FC_KERNEL".into(), p("img/vmlinux")),
            ("AR_FC_IMAGES_DIR".into(), p("img")),
            ("AR_FC_ALLOW_NON_ROOT".into(), "1".into()),
            ("AR_FC_UID_BASE".into(), "900000".into()),
            ("AR_FC_BOOT_TIMEOUT_S".into(), "10".into()),
            ("AR_FC_REAP_INTERVAL_MS".into(), "200".into()),
        ];
        for (k, v) in &self.extra_env {
            env.retain(|(ek, _)| ek != k);
            env.push((k.clone(), v.clone()));
        }
        env
    }

    /// fc-manager 的命令（env 清空后只给 env()），给 sweep、check 这些子命令用。
    pub fn command(&self, args: &[&str]) -> Command {
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_fc-manager"));
        cmd.args(args).env_clear().envs(self.env());
        cmd
    }

    pub fn start(&self) -> Manager {
        self.start_with(&[])
    }

    pub fn start_with(&self, extra: &[(&str, &str)]) -> Manager {
        let mut cmd = self.command(&["serve"]);
        for (k, v) in extra {
            cmd.env(k, v);
        }
        let mut child = cmd
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .expect("起不来 fc-manager");
        let log = Arc::new(Mutex::new(String::new()));
        let (tx, rx) = std::sync::mpsc::channel::<SocketAddr>();
        let stderr = child.stderr.take().unwrap();
        let sink = log.clone();
        std::thread::spawn(move || {
            for line in BufReader::new(stderr).lines() {
                let Ok(line) = line else { break };
                if let Some(rest) = line.split("listening on ").nth(1)
                    && let Ok(addr) = rest.trim().parse::<SocketAddr>()
                {
                    let _ = tx.send(addr);
                }
                let mut s = sink.lock().unwrap();
                s.push_str(&line);
                s.push('\n');
            }
        });
        let addr = match rx.recv_timeout(Duration::from_secs(15)) {
            Ok(addr) => addr,
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                panic!("fc-manager 没有起来：\n{}", log.lock().unwrap());
            }
        };
        Manager {
            child: Some(child),
            addr,
            log,
        }
    }

    /// 宿主上残留的东西：jail 目录、VM 的 cgroup、netns、记录、日志。销毁或清扫之后应当为空。
    pub fn leftovers(&self) -> Vec<String> {
        let mut out = vec![];
        let mut list = |dir: PathBuf, only_dirs: bool| {
            if let Ok(rd) = fs::read_dir(&dir) {
                for e in rd.flatten() {
                    if only_dirs && !e.path().is_dir() {
                        continue;
                    }
                    out.push(e.path().to_string_lossy().into_owned());
                }
            }
        };
        list(self.root.join("j/firecracker"), false);
        list(self.root.join("cg").join(PARENT_CGROUP), true);
        list(self.root.join("ns"), false);
        list(self.root.join("s/vms"), false);
        list(self.root.join("s/logs"), false);
        out
    }

    /// 假 firecracker 的宿主 pid（jailer 写的 <jail>/root/firecracker.pid）。
    pub fn vm_pid(&self, env_id: &str) -> i32 {
        let text = fs::read_to_string(self.jail(env_id).join("root/firecracker.pid")).unwrap();
        text.trim().parse().unwrap()
    }

    pub fn read_json(&self, path: &Path) -> Value {
        serde_json::from_slice(
            &fs::read(path).unwrap_or_else(|e| panic!("{}: {e}", path.display())),
        )
        .unwrap()
    }

    /// 假 firecracker 收到的 API 调用（<jail>/root/fake-api.jsonl，一行一个）。
    pub fn api_calls(&self, env_id: &str) -> Vec<Value> {
        let text =
            fs::read_to_string(self.jail(env_id).join("root/fake-api.jsonl")).unwrap_or_default();
        text.lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect()
    }

    /// 根目录下所有文件的内容拼在一起（找令牌有没有落盘）。
    pub fn all_file_bytes(&self) -> Vec<u8> {
        fn walk(dir: &Path, out: &mut Vec<u8>) {
            let Ok(rd) = fs::read_dir(dir) else { return };
            for e in rd.flatten() {
                let p = e.path();
                let Ok(ft) = e.file_type() else { continue };
                if ft.is_dir() {
                    walk(&p, out);
                } else if ft.is_file()
                    && let Ok(b) = fs::read(&p)
                {
                    out.extend_from_slice(&b);
                    out.push(b'\n');
                }
            }
        }
        let mut out = vec![];
        walk(&self.root, &mut out);
        out
    }
}

impl Drop for Rig {
    fn drop(&mut self) {
        // 留下来的假 VM 进程（测试中途失败时）一并杀掉。
        if let Ok(rd) = fs::read_dir(self.root.join("j/firecracker")) {
            for e in rd.flatten() {
                if let Ok(t) = fs::read_to_string(e.path().join("root/firecracker.pid"))
                    && let Ok(pid) = t.trim().parse::<i32>()
                {
                    kill9(pid);
                }
            }
        }
        let _ = fs::remove_dir_all(&self.root);
    }
}

pub struct Manager {
    child: Option<Child>,
    pub addr: SocketAddr,
    log: Arc<Mutex<String>>,
}

impl Manager {
    /// SIGKILL，模拟进程崩溃（不给它收尾的机会）。
    pub fn kill(&mut self) {
        if let Some(mut c) = self.child.take() {
            let _ = c.kill();
            let _ = c.wait();
        }
    }

    pub fn pid(&self) -> u32 {
        self.child.as_ref().map(|c| c.id()).unwrap_or(0)
    }

    /// 进程是否已经退出（fault 注入的 abort）。
    pub fn wait_exit(&mut self, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if let Some(c) = self.child.as_mut() {
                if let Ok(Some(_)) = c.try_wait() {
                    self.child = None;
                    return true;
                }
            } else {
                return true;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        false
    }

    pub fn log(&self) -> String {
        self.log.lock().unwrap().clone()
    }

    pub fn send(
        &self,
        method: &str,
        path: &str,
        headers: &[(&str, &str)],
        body: Option<&[u8]>,
    ) -> Resp {
        let mut head = format!("{method} {path} HTTP/1.1\r\nHost: fcm\r\nConnection: close\r\n");
        for (k, v) in headers {
            head.push_str(&format!("{k}: {v}\r\n"));
        }
        if let Some(b) = body {
            head.push_str(&format!("Content-Length: {}\r\n", b.len()));
        }
        head.push_str("\r\n");
        let mut wire = head.into_bytes();
        if let Some(b) = body {
            wire.extend_from_slice(b);
        }
        self.raw(&wire)
    }

    /// 原样发一段字节，读到对方关连接为止。
    pub fn raw(&self, wire: &[u8]) -> Resp {
        let mut s = TcpStream::connect(self.addr).unwrap();
        s.set_read_timeout(Some(Duration::from_secs(60))).unwrap();
        s.write_all(wire).unwrap();
        let mut buf = vec![];
        let _ = s.read_to_end(&mut buf);
        Resp::parse(&buf)
    }

    /// 带 fc-manager 令牌与作用域的请求。
    pub fn call(&self, method: &str, path: &str, owner: &str, body: Option<&Value>) -> Resp {
        let auth = format!("Bearer {TOKEN}");
        let bytes = body.map(|v| serde_json::to_vec(v).unwrap());
        let mut headers = vec![("Authorization", auth.as_str()), ("X-Sandbox-Owner", owner)];
        if bytes.is_some() {
            headers.push(("Content-Type", "application/json"));
        }
        self.send(method, path, &headers, bytes.as_deref())
    }

    pub fn create(&self, owner: &str, spec: Value) -> Resp {
        self.call("POST", "/v1/envs", owner, Some(&spec))
    }

    pub fn create_ok(&self, owner: &str, spec: Value) -> String {
        let r = self.create(owner, spec);
        assert_eq!(r.status, 201, "建环境失败：{}\n{}", r.text(), self.log());
        r.json()["env_id"].as_str().unwrap().to_string()
    }

    pub fn destroy(&self, owner: &str, env_id: &str) -> Resp {
        self.call("DELETE", &format!("/v1/envs/{env_id}"), owner, None)
    }

    pub fn list(&self) -> Vec<Value> {
        let r = self.call("GET", "/v1/envs", "_platform", None);
        assert_eq!(r.status, 200, "{}", r.text());
        r.json()["environments"].as_array().unwrap().clone()
    }

    /// 经 fc-manager 转给客户机 agent 的请求：/v1/envs/<id>/agent/<path>。
    pub fn agent(
        &self,
        owner: &str,
        env_id: &str,
        method: &str,
        path: &str,
        extra: &[(&str, &str)],
        body: Option<&[u8]>,
    ) -> Resp {
        let auth = format!("Bearer {TOKEN}");
        let mut headers = vec![("Authorization", auth.as_str()), ("X-Sandbox-Owner", owner)];
        headers.extend_from_slice(extra);
        self.send(
            method,
            &format!("/v1/envs/{env_id}/agent{path}"),
            &headers,
            body,
        )
    }
}

impl Drop for Manager {
    fn drop(&mut self) {
        self.kill();
    }
}

pub fn spec() -> Value {
    json!({"runtime": "python", "cpu_millis": 1500, "memory_mb": 512, "disk_mb": 256, "labels": {"workspace": "w1"}})
}

#[derive(Debug)]
pub struct Resp {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Resp {
    pub fn parse(buf: &[u8]) -> Resp {
        let Some(end) = buf.windows(4).position(|w| w == b"\r\n\r\n") else {
            return Resp {
                status: 0,
                headers: vec![],
                body: buf.to_vec(),
            };
        };
        let head = String::from_utf8_lossy(&buf[..end]).into_owned();
        let mut lines = head.split("\r\n");
        let status = lines
            .next()
            .unwrap_or("")
            .split(' ')
            .nth(1)
            .and_then(|s| s.parse().ok())
            .unwrap_or(0);
        let headers: Vec<(String, String)> = lines
            .filter_map(|l| l.split_once(':'))
            .map(|(k, v)| (k.trim().to_ascii_lowercase(), v.trim().to_string()))
            .collect();
        let mut body = buf[end + 4..].to_vec();
        if headers
            .iter()
            .any(|(k, v)| k == "transfer-encoding" && v.eq_ignore_ascii_case("chunked"))
        {
            body = dechunk(&body);
        }
        Resp {
            status,
            headers,
            body,
        }
    }

    pub fn header(&self, name: &str) -> Option<&str> {
        let name = name.to_ascii_lowercase();
        self.headers
            .iter()
            .find(|(k, _)| *k == name)
            .map(|(_, v)| v.as_str())
    }

    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.body).into_owned()
    }

    pub fn json(&self) -> Value {
        serde_json::from_slice(&self.body)
            .unwrap_or_else(|e| panic!("不是 JSON（{e}）：{}", self.text()))
    }

    pub fn code(&self) -> String {
        self.json()["code"].as_str().unwrap_or("").to_string()
    }
}

fn dechunk(mut data: &[u8]) -> Vec<u8> {
    let mut out = vec![];
    while let Some(nl) = data.windows(2).position(|w| w == b"\r\n") {
        let size_text = String::from_utf8_lossy(&data[..nl]).into_owned();
        let size =
            usize::from_str_radix(size_text.split(';').next().unwrap().trim(), 16).unwrap_or(0);
        data = &data[nl + 2..];
        if size == 0 || data.len() < size {
            break;
        }
        out.extend_from_slice(&data[..size]);
        data = &data[(size + 2).min(data.len())..];
    }
    out
}

/// FNV-1a 64 位：假 agent 用它报请求体的摘要（不为此引入哈希库）。
pub fn fnv1a64(data: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    for b in data {
        h ^= *b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    h
}

pub fn wait_until(timeout: Duration, mut cond: impl FnMut() -> bool) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        if cond() {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(25));
    }
}

/// 进程还活着（/proc/<pid>/stat 在，且不是僵尸）。
pub fn pid_alive(pid: i32) -> bool {
    let Ok(stat) = fs::read_to_string(format!("/proc/{pid}/stat")) else {
        return false;
    };
    let state = stat
        .rsplit_once(')')
        .map(|(_, rest)| rest.trim_start().chars().next().unwrap_or('?'));
    !matches!(state, Some('Z') | Some('X') | None)
}

pub fn kill9(pid: i32) {
    let _ = Command::new("kill")
        .args(["-9", &pid.to_string()])
        .stderr(Stdio::null())
        .status();
}

pub fn contains(hay: &[u8], needle: &str) -> bool {
    hay.windows(needle.len()).any(|w| w == needle.as_bytes())
}
