//! 测试替身（只给 tests/，install.sh 不装它）：按自己的文件名扮演 jailer、firecracker 或 ip，让 fc-manager 的测试在
//! 没有 KVM、不是 root 的 Linux 上跑（docs/plan/firecracker-backend.md 第 6 节）。行为由同目录的 fake.json 决定。
//!
//! - jailer：按 v1.17 jailer 的参数检查（不认的参数直接失败），建 <chroot_base>/<exec>/<id>/root，把 exec 文件与 fake.json
//!   拷进去，按 --cgroup 在 fake.json 的 cgroup_root 下写文件，记下参数与环境（fake-jailer.json），在 jail 里起 firecracker、
//!   写 pid 文件，然后自己退出（同 --new-pid-ns 时真 jailer 的样子）。
//! - firecracker：在 jail 里的 API socket 上应答 PUT，记进 fake-api.jsonl；InstanceStart 之后扮演客户机：按启动参数里的
//!   AGENT_TOKEN_VSOCK 去连 <uds>_<端口> 取令牌，在 <uds> 上应答 `CONNECT <端口>`，后面是一个假的 agent。
//! - ip：`ip netns add|delete <名字>` 在 fake.json 的 netns_dir 里建、删一个文件。

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("fc-fake 只在 Linux 上运行");
    std::process::exit(2);
}

#[cfg(target_os = "linux")]
fn main() {
    std::process::exit(linux::main());
}

#[cfg(target_os = "linux")]
mod linux {
    use std::fs;
    use std::io::{Read, Write};
    use std::os::unix::net::{UnixListener, UnixStream};
    use std::path::{Path, PathBuf};
    use std::process::{Command, Stdio};
    use std::sync::{Arc, Mutex};
    use std::time::{Duration, Instant};

    use fc_manager::http::{self, Body, Conn};
    use serde_json::{Value, json};

    pub fn main() -> i32 {
        let exe = std::env::current_exe().expect("current_exe");
        let name = exe
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        let args: Vec<String> = std::env::args().skip(1).collect();
        match name.as_str() {
            "jailer" => jailer(&exe, &args),
            "firecracker" => firecracker(&args),
            "ip" => ip(&exe, &args),
            other => {
                eprintln!("fc-fake：不认识的角色 {other}");
                2
            }
        }
    }

    fn load_cfg(dir: &Path) -> Value {
        fs::read(dir.join("fake.json"))
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or_else(|| json!({}))
    }

    fn say(msg: &str) {
        // fc-manager 被杀后管道没人读，写失败也不能让替身退出。
        let _ = writeln!(std::io::stdout(), "{msg}");
    }

    fn append(path: &Path, line: &str) {
        if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(path) {
            let _ = writeln!(f, "{line}");
        }
    }

    fn jailer(exe: &Path, args: &[String]) -> i32 {
        let cfg = load_cfg(exe.parent().unwrap());
        if let Some(code) = cfg["jailer_exit"].as_i64() {
            eprintln!("fake jailer：按 fake.json 退出 {code}");
            return code as i32;
        }
        let split = args.iter().position(|a| a == "--").unwrap_or(args.len());
        let (ours, rest) = (&args[..split], &args[(split + 1).min(args.len())..]);
        let mut one: std::collections::BTreeMap<&str, &str> = Default::default();
        let mut cgroups = vec![];
        let mut new_pid_ns = false;
        let mut i = 0;
        while i < ours.len() {
            let a = ours[i].as_str();
            match a {
                "--new-pid-ns" | "--daemonize" => {
                    new_pid_ns |= a == "--new-pid-ns";
                    i += 1;
                }
                "--id" | "--exec-file" | "--uid" | "--gid" | "--chroot-base-dir" | "--netns"
                | "--cgroup-version" | "--parent-cgroup" | "--cgroup" | "--resource-limit" => {
                    let Some(v) = ours.get(i + 1) else {
                        eprintln!("fake jailer：{a} 缺值");
                        return 2;
                    };
                    if a == "--cgroup" {
                        cgroups.push(v.as_str());
                    } else if a != "--resource-limit" && one.insert(a, v.as_str()).is_some() {
                        eprintln!("fake jailer：{a} 给了两次");
                        return 2;
                    }
                    i += 2;
                }
                _ => {
                    eprintln!("fake jailer：不认识的参数 {a}");
                    return 2;
                }
            }
        }
        for need in ["--id", "--exec-file", "--uid", "--gid", "--chroot-base-dir"] {
            if !one.contains_key(need) {
                eprintln!("fake jailer：缺 {need}");
                return 2;
            }
        }
        let id = one["--id"];
        if id.is_empty()
            || id.len() > 64
            || !id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
        {
            eprintln!("fake jailer：--id 不对");
            return 2;
        }
        let (Ok(uid), Ok(gid)) = (one["--uid"].parse::<u32>(), one["--gid"].parse::<u32>()) else {
            eprintln!("fake jailer：uid、gid 不是数字");
            return 2;
        };
        if uid == 0 || gid == 0 || one.get("--cgroup-version") != Some(&"2") {
            eprintln!("fake jailer：要非 root 与 cgroup v2");
            return 2;
        }
        let exec_file = Path::new(one["--exec-file"]);
        let Some(exec_name) = exec_file
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
        else {
            return 2;
        };
        let netns_existed = one.get("--netns").is_some_and(|p| Path::new(p).exists());
        let jail = Path::new(one["--chroot-base-dir"])
            .join(&exec_name)
            .join(id);
        let root = jail.join("root");
        if fs::create_dir_all(&root).is_err() || fs::copy(exec_file, root.join(&exec_name)).is_err()
        {
            eprintln!("fake jailer：建不了 jail");
            return 1;
        }
        let _ = fs::copy(
            exe.parent().unwrap().join("fake.json"),
            root.join("fake.json"),
        );
        let cg_root = PathBuf::from(cfg["cgroup_root"].as_str().unwrap_or("/nonexistent"));
        let cg = cg_root
            .join(one.get("--parent-cgroup").copied().unwrap_or("firecracker"))
            .join(id);
        let _ = fs::create_dir_all(&cg);
        for kv in &cgroups {
            if let Some((k, v)) = kv.split_once('=') {
                let _ = fs::write(cg.join(k), v);
            }
        }
        let env: Vec<String> = std::env::vars().map(|(k, v)| format!("{k}={v}")).collect();
        let record = json!({"argv": args, "env": env, "netns_existed": netns_existed, "uid": uid, "gid": gid});
        let _ = fs::write(jail.join("fake-jailer.json"), record.to_string());
        if !netns_existed {
            eprintln!("fake jailer：netns 不存在");
            return 1;
        }
        let child = Command::new(root.join(&exec_name))
            .arg("--id")
            .arg(id)
            .args(rest)
            .current_dir(&root)
            .stdin(Stdio::null())
            .spawn();
        let Ok(child) = child else {
            eprintln!("fake jailer：起不了 firecracker");
            return 1;
        };
        let _ = fs::write(
            root.join(format!("{exec_name}.pid")),
            child.id().to_string(),
        );
        append(&cg.join("cgroup.procs"), &child.id().to_string());
        let _ = new_pid_ns;
        0
    }

    #[derive(Default)]
    struct FakeVm {
        boot_args: Option<String>,
        vsock_uds: Option<String>,
        started: bool,
    }

    fn flag<'a>(args: &'a [String], name: &str) -> Option<&'a str> {
        args.windows(2)
            .find(|w| w[0] == name)
            .map(|w| w[1].as_str())
    }

    fn firecracker(args: &[String]) -> i32 {
        let root = std::env::current_dir().unwrap();
        let cfg = load_cfg(&root);
        let env: Vec<String> = std::env::vars().map(|(k, v)| format!("{k}={v}")).collect();
        let _ = fs::write(
            root.join("fake-firecracker.json"),
            json!({"argv": args, "env": env}).to_string(),
        );
        let Some(api) = flag(args, "--api-sock") else {
            eprintln!("fake firecracker：缺 --api-sock");
            return 2;
        };
        say(&format!(
            "fake firecracker {} 起来了",
            flag(args, "--id").unwrap_or("?")
        ));
        let api_path = root.join(api.trim_start_matches('/'));
        let Ok(listener) = UnixListener::bind(&api_path) else {
            eprintln!("fake firecracker：绑不了 API socket");
            return 1;
        };
        let vm = Arc::new(Mutex::new(FakeVm::default()));
        for stream in listener.incoming().flatten() {
            api_conn(stream, &root, &cfg, &vm);
        }
        0
    }

    fn api_conn(stream: UnixStream, root: &Path, cfg: &Value, vm: &Arc<Mutex<FakeVm>>) {
        let mut c = Conn::new(stream);
        loop {
            let Ok(head) = c.read_request() else { return };
            let body = c
                .read_body(head.body().unwrap_or(Body::Empty), 1 << 20)
                .unwrap_or_default();
            let value: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
            let path = head.path().to_string();
            append(
                &root.join("fake-api.jsonl"),
                &json!({"method": head.method, "path": path, "body": value}).to_string(),
            );
            let fail = |c: &mut Conn<UnixStream>, msg: &str| {
                let _ = http::write_json(c.get_mut(), 400, &json!({"fault_message": msg}), &[]);
            };
            if cfg["api_fail"].as_str() == Some(path.as_str()) {
                fail(&mut c, "fake firecracker 按 fake.json 拒绝");
                return;
            }
            let mut g = vm.lock().unwrap();
            match (head.method.as_str(), path.as_str()) {
                ("PUT", "/boot-source") => {
                    g.boot_args = value["boot_args"].as_str().map(String::from)
                }
                ("PUT", "/vsock") => g.vsock_uds = value["uds_path"].as_str().map(String::from),
                ("PUT", "/actions") if value["action_type"] == "InstanceStart" => {
                    let (Some(args), Some(uds)) = (g.boot_args.clone(), g.vsock_uds.clone()) else {
                        drop(g);
                        fail(&mut c, "InstanceStart 之前要配 boot-source 与 vsock");
                        return;
                    };
                    if g.started {
                        drop(g);
                        fail(&mut c, "已经启动过了");
                        return;
                    }
                    g.started = true;
                    let (root, cfg) = (root.to_path_buf(), cfg.clone());
                    std::thread::spawn(move || guest(&root, &cfg, &args, &uds));
                }
                ("PUT", _) => {}
                _ => {
                    drop(g);
                    fail(&mut c, "fake firecracker 只认 PUT");
                    return;
                }
            }
            drop(g);
            let _ = c.get_mut().write_all(b"HTTP/1.1 204 No Content\r\nServer: Firecracker API\r\nConnection: keep-alive\r\n\r\n");
        }
    }

    fn boot_param(args: &str, key: &str) -> Option<String> {
        args.split(" -- ")
            .next()?
            .split_whitespace()
            .find_map(|t| t.strip_prefix(key))
            .map(String::from)
    }

    /// 假的客户机：取令牌，然后在 vsock 上应答 agent 的请求。
    fn guest(root: &Path, cfg: &Value, boot_args: &str, uds: &str) {
        let agent_port: u32 = boot_param(boot_args, "AGENT_LISTEN=vsock:")
            .and_then(|p| p.parse().ok())
            .unwrap_or(0);
        let token_port = boot_param(boot_args, "AGENT_TOKEN_VSOCK=").unwrap_or_default();
        let uds_path = root.join(uds.trim_start_matches('/'));
        let Ok(listener) = UnixListener::bind(&uds_path) else {
            return;
        };
        if cfg["guest"] == "never_fetch_token" {
            for s in listener.incoming() {
                drop(s);
            }
            return;
        }
        let mut token_path = uds_path.as_os_str().to_owned();
        token_path.push(format!("_{token_port}"));
        let deadline = Instant::now() + Duration::from_secs(5);
        let token = loop {
            if let Ok(s) = UnixStream::connect(&token_path) {
                let mut c = Conn::new(s);
                if let Ok(line) = c.read_line(256) {
                    break String::from_utf8_lossy(&line).into_owned();
                }
            }
            if Instant::now() >= deadline {
                say("fake guest：没取到令牌");
                return;
            }
            std::thread::sleep(Duration::from_millis(20));
        };
        say("fake guest：agent 就绪");
        let token = Arc::new(token);
        let root = root.to_path_buf();
        for stream in listener.incoming().flatten() {
            let (token, root) = (token.clone(), root.clone());
            std::thread::spawn(move || {
                let mut c = Conn::new(stream);
                let Ok(line) = c.read_line(64) else { return };
                let port = String::from_utf8_lossy(&line)
                    .strip_prefix("CONNECT ")
                    .and_then(|p| p.trim().parse::<u32>().ok());
                if port != Some(agent_port) {
                    return;
                }
                if c.get_mut().write_all(b"OK 1073741824\n").is_ok() {
                    agent(c, &token, &root);
                }
            });
        }
    }

    fn fnv1a64(data: &[u8]) -> u64 {
        data.iter().fold(0xcbf29ce484222325u64, |h, b| {
            (h ^ u64::from(*b)).wrapping_mul(0x100000001b3)
        })
    }

    fn agent(mut c: Conn<UnixStream>, token: &str, root: &Path) {
        let Ok(head) = c.read_request() else { return };
        let path = head.path().to_string();
        if path == "/healthz" {
            let _ = http::write_response(c.get_mut(), 200, &[], b"ok");
            return;
        }
        let toks = head.all("x-sandbox-token");
        if toks.len() != 1 || toks[0] != token {
            let _ = http::write_json(
                c.get_mut(),
                401,
                &json!({"code": "unauthorized", "message": "令牌不对"}),
                &[],
            );
            return;
        }
        let log = root.join("fake-agent.log");
        append(&log, &format!("{} {}", head.method, head.target));
        let kind = head.body().unwrap_or(Body::Empty);
        if path.starts_with("/v1/echo") {
            let body = c.read_body(kind, 64 << 20).unwrap_or_default();
            let names: Vec<String> = head
                .headers
                .iter()
                .map(|(k, _)| k.to_ascii_lowercase())
                .collect();
            let v = json!({
                "method": head.method,
                "target": head.target,
                "header_names": names,
                "token_headers": toks.len(),
                "body_len": body.len(),
                "body_fnv": format!("{:016x}", fnv1a64(&body)),
            });
            let _ = http::write_json(c.get_mut(), 200, &v, &[]);
        } else if path == "/v1/fake/token" {
            let _ = http::write_response(
                c.get_mut(),
                200,
                &[("Content-Type", "text/plain")],
                token.as_bytes(),
            );
        } else if path == "/v1/slow" {
            let _ = c.read_body(kind, 1 << 20);
            append(&log, "slow started");
            let _ = c
                .get_mut()
                .set_read_timeout(Some(Duration::from_millis(100)));
            let deadline = Instant::now() + Duration::from_secs(10);
            let mut one = [0u8; 1];
            while Instant::now() < deadline {
                match c.read(&mut one) {
                    Ok(0) => {
                        append(&log, "slow disconnected");
                        return;
                    }
                    Ok(_) => {}
                    Err(e)
                        if matches!(
                            e.kind(),
                            std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                        ) => {}
                    Err(_) => {
                        append(&log, "slow disconnected");
                        return;
                    }
                }
            }
            let _ = http::write_response(c.get_mut(), 200, &[], b"slow done");
        } else if path == "/v1/big" {
            let n: usize = head
                .target
                .split("n=")
                .nth(1)
                .and_then(|v| v.parse().ok())
                .unwrap_or(0);
            let s = c.get_mut();
            if s.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n").is_err() {
                return;
            }
            let mut sent = 0usize;
            while sent < n {
                let len = (n - sent).min(65_536);
                let chunk: Vec<u8> = (sent..sent + len).map(|i| (i % 251) as u8).collect();
                if s.write_all(format!("{len:x}\r\n").as_bytes())
                    .and_then(|_| s.write_all(&chunk))
                    .and_then(|_| s.write_all(b"\r\n"))
                    .is_err()
                {
                    return;
                }
                sent += len;
            }
            let _ = s.write_all(b"0\r\n\r\n");
        } else {
            let _ = http::write_json(
                c.get_mut(),
                404,
                &json!({"code": "fake_not_found", "message": path}),
                &[],
            );
        }
    }

    fn ip(exe: &Path, args: &[String]) -> i32 {
        let cfg = load_cfg(exe.parent().unwrap());
        let dir = PathBuf::from(cfg["netns_dir"].as_str().unwrap_or("/nonexistent"));
        let a: Vec<&str> = args.iter().map(String::as_str).collect();
        match a.as_slice() {
            ["netns", "add", name] => {
                let p = dir.join(name);
                if p.exists() || fs::write(&p, b"").is_err() {
                    eprintln!("fake ip：建不了 netns {name}");
                    return 1;
                }
                0
            }
            ["netns", "delete", name] => match fs::remove_file(dir.join(name)) {
                Ok(()) => 0,
                Err(_) => {
                    eprintln!("fake ip：没有 netns {name}");
                    1
                }
            },
            _ => {
                eprintln!("fake ip：只认 netns add|delete");
                2
            }
        }
    }
}
