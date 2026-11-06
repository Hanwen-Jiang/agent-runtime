//! 真 VM 的验收（FC-REAL：D-20 验收 2～6、8 里只有真 VM 才查得到的部分）。只在 AR_TEST_FIRECRACKER=1 时编译（build.rs），
//! 不设时这些用例不收集。前提缺一个就失败，不跳过：Linux、root、/dev/kvm 可读写，PATH 上有 firecracker、jailer、ip、
//! mkfs.ext4；AR_FC_TEST_KERNEL 是客户机内核（deploy/images/firecracker 构建的 vmlinux），AR_FC_TEST_IMAGES_DIR 里有
//! python.ext4（D-21 的 rootfs：带 Rust 版 sandbox-agent，支持 --init、AGENT_LISTEN=vsock: 与 AGENT_TOKEN_VSOCK=，D-28）。
//! 在生产独服上跑要人同意（docs/plan/firecracker-backend.md 第 7 节第 4 步）。
#![cfg(all(target_os = "linux", ar_test_firecracker))]

mod common;

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use common::*;
use serde_json::{Value, json};

struct Real {
    work: PathBuf,
    env: Vec<(String, String)>,
}

fn which(name: &str) -> String {
    let path = std::env::var("PATH").unwrap_or_default();
    for dir in path.split(':') {
        let p = Path::new(dir).join(name);
        if p.is_file() {
            return p.to_string_lossy().into_owned();
        }
    }
    panic!("AR_TEST_FIRECRACKER=1 要 PATH 上有 {name}");
}

fn required(var: &str) -> String {
    std::env::var(var).unwrap_or_else(|_| panic!("AR_TEST_FIRECRACKER=1 要设 {var}"))
}

impl Real {
    fn new() -> Real {
        let kvm = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open("/dev/kvm");
        assert!(
            kvm.is_ok(),
            "AR_TEST_FIRECRACKER=1 要 /dev/kvm 可读写：{:?}",
            kvm.err()
        );
        let uid = Command::new("id").arg("-u").output().unwrap();
        assert_eq!(
            String::from_utf8_lossy(&uid.stdout).trim(),
            "0",
            "jailer 要 root"
        );
        let kernel = required("AR_FC_TEST_KERNEL");
        let images = PathBuf::from(required("AR_FC_TEST_IMAGES_DIR"));
        assert!(
            images.join("python.ext4").is_file(),
            "{} 里没有 python.ext4",
            images.display()
        );
        // jail 与镜像要在同一个文件系统上（rootfs 是硬链接进 jail 的）。
        let work = images
            .parent()
            .unwrap()
            .join(format!("fcr-{}", fc_manager::vm::random_hex(4).unwrap()));
        for d in ["j", "s"] {
            fs::create_dir_all(work.join(d)).unwrap();
        }
        let token = TOKEN;
        let secrets = work.join("secrets.json");
        fs::write(&secrets, json!({"version": 1, "scopes": {"_platform": {"secrets": {"sandbox/fc-manager-token": token}, "roles": []}}}).to_string()).unwrap();
        let p = |rel: &str| work.join(rel).to_string_lossy().into_owned();
        let env = vec![
            ("PATH".to_string(), std::env::var("PATH").unwrap()),
            ("AR_FC_LISTEN".into(), "127.0.0.1:0".into()),
            (
                "AR_SECRETS_FILE".into(),
                secrets.to_string_lossy().into_owned(),
            ),
            ("AR_FC_STATE_DIR".into(), p("s")),
            ("AR_FC_CHROOT_BASE".into(), p("j")),
            ("AR_FC_JAILER_BIN".into(), which("jailer")),
            ("AR_FC_FIRECRACKER_BIN".into(), which("firecracker")),
            ("AR_FC_IP_BIN".into(), which("ip")),
            ("AR_FC_MKFS_BIN".into(), which("mkfs.ext4")),
            ("AR_FC_CGROUP_ROOT".into(), "/sys/fs/cgroup".into()),
            (
                "AR_FC_PARENT_CGROUP".into(),
                work.file_name().unwrap().to_string_lossy().into_owned(),
            ),
            ("AR_FC_UID_BASE".into(), "950000".into()),
            ("AR_FC_NETNS_DIR".into(), "/run/netns".into()),
            ("AR_FC_BOOT_TIMEOUT_S".into(), "30".into()),
            ("AR_FC_REAP_INTERVAL_MS".into(), "1000".into()),
            ("AR_FC_KERNEL".into(), kernel),
            (
                "AR_FC_IMAGES_DIR".into(),
                images.to_string_lossy().into_owned(),
            ),
            ("AR_FC_MAX_VMS".into(), "8".into()),
        ];
        Real { work, env }
    }

    fn start(&self, extra: &[(&str, &str)]) -> (Manager, Rig) {
        // Rig 只用来起进程与发请求；目录用真的（env 覆盖 Rig 的默认值）。
        let mut rig = Rig::new();
        for (k, v) in &self.env {
            rig = rig.with(k, v);
        }
        rig = rig.with("AR_FC_ALLOW_NON_ROOT", "");
        for (k, v) in extra {
            rig = rig.with(k, v);
        }
        (rig.start(), rig)
    }

    fn residue(&self) -> Vec<String> {
        let mut out = vec![];
        for dir in [
            self.work.join("j/firecracker"),
            self.work.join("s/vms"),
            PathBuf::from("/sys/fs/cgroup").join(self.work.file_name().unwrap()),
        ] {
            if let Ok(rd) = fs::read_dir(&dir) {
                for e in rd.flatten() {
                    if e.path().is_dir() || !dir.starts_with("/sys") {
                        out.push(e.path().to_string_lossy().into_owned());
                    }
                }
            }
        }
        // /run/netns also contains live production VMs; only this manager's
        // durable records, jail and cgroup directories establish ownership.
        out
    }
}

#[test]
fn memory_snapshot_clones_refresh_identity_and_keep_tenant_disks_private() {
    let real = Real::new();
    let (m, _rig) = real.start(&[("AR_FC_SNAPSHOT_CACHE_MIB", "2048")]);
    let spec = json!({"runtime":"python", "cpu_millis":1000, "memory_mb":256, "disk_mb":256});
    let a = m.create_ok("t1", spec.clone());
    let cache = real.work.join("s/base-snapshots");
    let entries: Vec<_> = fs::read_dir(&cache).unwrap().flatten().collect();
    assert_eq!(
        entries.len(),
        1,
        "real snapshot must have been created: {}",
        m.log()
    );
    let entry = entries[0].path();
    assert!(fs::metadata(entry.join("memory")).unwrap().len() >= 256 << 20);
    let identity = "cat /etc/machine-id; cat /proc/sys/kernel/random/boot_id; cat /proc/sys/kernel/random/uuid; date +%s; hostname";
    let first = sh(&m, "t1", &a, identity).1;
    assert_eq!(
        sh(&m, "t1", &a, "echo tenant-one > private-file").0,
        Some(0)
    );
    let b = m.create_ok("t2", spec.clone());
    let second = sh(&m, "t2", &b, identity).1;
    let first: Vec<_> = first.lines().collect();
    let second: Vec<_> = second.lines().collect();
    assert_eq!(first.len(), 5);
    assert_eq!(second.len(), 5);
    assert_ne!(
        first[0], second[0],
        "machine-id must be fresh after restore"
    );
    assert_ne!(first[1], second[1], "boot_id must be fresh after restore");
    assert_ne!(
        first[2], second[2],
        "kernel RNG must be reseeded before tenant code"
    );
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs();
    assert!(now.abs_diff(second[3].parse::<u64>().unwrap()) < 5);
    assert_ne!(first[4], second[4], "hostname must be fresh after restore");
    assert!(
        m.log()
            .contains(&format!("{b} bootstrap path: memory-snapshot")),
        "{}",
        m.log()
    );
    assert_ne!(sh(&m, "t2", &b, "cat private-file").0, Some(0));
    assert_eq!(
        m.agent("t1", &b, "POST", "/v1/exec", &[], Some(b"{}"))
            .status,
        404
    );
    let log = fs::read_to_string(real.work.join("s/logs").join(format!("{b}.log"))).unwrap();
    assert!(
        !log.contains("Kernel command line:"),
        "clone should resume memory, not reboot kernel"
    );
    assert_eq!(m.destroy("t1", &a).status, 204);
    assert_eq!(m.destroy("t2", &b).status, 204);
    // A corrupt snapshot must lead to a healthy cold boot, never a partial VM.
    let state = entry.join("state");
    fs::set_permissions(&state, std::os::unix::fs::PermissionsExt::from_mode(0o644)).unwrap();
    let mut bytes = fs::read(&state).unwrap();
    bytes[0] ^= 1;
    fs::write(&state, bytes).unwrap();
    fs::set_permissions(&state, std::os::unix::fs::PermissionsExt::from_mode(0o444)).unwrap();
    let c = m.create_ok("t3", spec);
    assert!(m.log().contains("cold fallback"), "{}", m.log());
    assert_eq!(sh(&m, "t3", &c, "echo recovered").1.trim(), "recovered");
    assert_eq!(m.destroy("t3", &c).status, 204);
}

impl Drop for Real {
    fn drop(&mut self) {
        if std::thread::panicking() {
            let failed = self.work.join("failed-logs");
            let _ = fs::create_dir_all(&failed);
            if let Ok(logs) = fs::read_dir(self.work.join("s/logs")) {
                for log in logs.flatten() {
                    let _ = fs::copy(log.path(), failed.join(log.file_name()));
                }
            }
            eprintln!("failed VM logs: {}", failed.display());
        }
        let cleaned = Command::new(manager_bin())
            .arg("sweep")
            .env_clear()
            .envs(self.env.iter().cloned())
            .status()
            .is_ok_and(|status| status.success());
        if cleaned {
            let _ = fs::remove_dir(
                PathBuf::from("/sys/fs/cgroup").join(self.work.file_name().unwrap()),
            );
            if !std::thread::panicking() {
                let _ = fs::remove_dir_all(&self.work);
            }
        } else {
            eprintln!("cleanup failed; preserved state: {}", self.work.display());
        }
    }
}

/// 在客户机里跑一条 shell 命令：返回（退出码、stdout、stderr）。
fn sh(m: &Manager, owner: &str, id: &str, script: &str) -> (Option<i64>, String, String) {
    let body = json!({"argv": ["sh", "-c", script], "timeout_ms": 60000}).to_string();
    let r = m.agent(
        owner,
        id,
        "POST",
        "/v1/exec",
        &[("Content-Type", "application/json")],
        Some(body.as_bytes()),
    );
    assert_eq!(r.status, 200, "{}", r.text());
    let v: Value = r.json();
    let dec = |k: &str| String::from_utf8_lossy(&b64(v[k].as_str().unwrap_or(""))).into_owned();
    (v["exit_code"].as_i64(), dec("stdout"), dec("stderr"))
}

fn b64(s: &str) -> Vec<u8> {
    let val = |c: u8| match c {
        b'A'..=b'Z' => c - b'A',
        b'a'..=b'z' => c - b'a' + 26,
        b'0'..=b'9' => c - b'0' + 52,
        b'+' => 62,
        b'/' => 63,
        _ => 0,
    };
    let bytes: Vec<u8> = s.bytes().filter(|c| *c != b'=').collect();
    let mut out = vec![];
    for chunk in bytes.chunks(4) {
        let n = chunk
            .iter()
            .enumerate()
            .fold(0u32, |acc, (i, c)| acc | (val(*c) as u32) << (18 - 6 * i));
        out.push((n >> 16) as u8);
        if chunk.len() > 2 {
            out.push((n >> 8) as u8);
        }
        if chunk.len() > 3 {
            out.push(n as u8);
        }
    }
    out
}

#[test]
fn guest_has_its_own_kernel_agent_as_pid1_and_only_loopback() {
    let real = Real::new();
    let (m, _rig) = real.start(&[]);
    let id = m.create_ok(
        "t1",
        json!({"runtime": "python", "cpu_millis": 1000, "memory_mb": 512}),
    );
    let host_kernel =
        String::from_utf8(Command::new("uname").arg("-r").output().unwrap().stdout).unwrap();
    let (_, guest_kernel, _) = sh(&m, "t1", &id, "uname -r");
    assert_ne!(guest_kernel.trim(), host_kernel.trim());
    let (_, comm, _) = sh(&m, "t1", &id, "cat /proc/1/comm");
    assert_eq!(comm.trim(), "sandbox-agent");
    let (_, nets, _) = sh(&m, "t1", &id, "ls /sys/class/net");
    assert_eq!(nets.split_whitespace().collect::<Vec<_>>(), vec!["lo"]);
    assert_eq!(m.destroy("t1", &id).status, 204);
}

#[test]
fn commands_run_unprivileged_cannot_write_root_or_read_the_agent_token() {
    let real = Real::new();
    let (m, _rig) = real.start(&[]);
    let id = m.create_ok(
        "t1",
        json!({"runtime": "python", "cpu_millis": 1000, "memory_mb": 512}),
    );
    let token = m.agent("t1", &id, "GET", "/v1/fake/token", &[], None);
    assert_ne!(token.status, 200, "真 agent 不该有取令牌的接口");
    let (_, uid, _) = sh(&m, "t1", &id, "id -u");
    assert_ne!(uid.trim(), "0");
    let (code, _, _) = sh(&m, "t1", &id, "touch /fcm-probe");
    assert_ne!(code, Some(0), "根文件系统写进去了");
    let (code, _, _) = sh(&m, "t1", &id, "cat /proc/1/environ");
    assert_ne!(code, Some(0), "读到了 agent 的环境");
    let (_, cmdline, _) = sh(&m, "t1", &id, "cat /proc/cmdline");
    assert!(!cmdline.contains(TOKEN));
    assert!(
        !cmdline.to_ascii_lowercase().contains("token="),
        "{cmdline}"
    );
    let (_, env, _) = sh(&m, "t1", &id, "env");
    assert!(!env.contains("AGENT_"), "{env}");
}

#[test]
fn memory_disk_and_cpu_limits_hold() {
    let real = Real::new();
    let (m, _rig) = real.start(&[]);
    let id = m.create_ok(
        "t1",
        json!({"runtime": "python", "cpu_millis": 1000, "memory_mb": 512, "disk_mb": 256}),
    );
    // 内存超限：命令被杀，VM 还在。
    let (code, _, _) = sh(&m, "t1", &id, "python3 -c 'a = bytearray(2 << 30)'");
    assert_ne!(code, Some(0));
    assert_eq!(sh(&m, "t1", &id, "echo alive").1.trim(), "alive");
    // 磁盘写满：命令出错，宿主上的 scratch 不超过配额。
    let (code, _, _) = sh(&m, "t1", &id, "dd if=/dev/zero of=fill bs=1M count=400");
    assert_ne!(code, Some(0));
    let scratch = real
        .work
        .join("j/firecracker")
        .join(&id)
        .join("root/scratch.ext4");
    assert!(fs::metadata(&scratch).unwrap().len() <= 256 << 20);
    // CPU：客户机只有 1 个 vCPU。
    assert_eq!(sh(&m, "t1", &id, "nproc").1.trim(), "1");
}

#[test]
fn capacity_destroy_and_restart_leave_no_residue() {
    let real = Real::new();
    let (mut m, rig) = real.start(&[("AR_FC_MAX_VMS", "2")]);
    let a = m.create_ok(
        "t1",
        json!({"runtime": "python", "cpu_millis": 1000, "memory_mb": 256}),
    );
    let b = m.create_ok(
        "t1",
        json!({"runtime": "python", "cpu_millis": 1000, "memory_mb": 256}),
    );
    let r = m.create(
        "t1",
        json!({"runtime": "python", "cpu_millis": 1000, "memory_mb": 256}),
    );
    assert_eq!((r.status, r.code()), (503, "host_capacity".to_string()));
    assert_eq!(sh(&m, "t1", &a, "echo ok").1.trim(), "ok");
    assert_eq!(m.destroy("t1", &a).status, 204);
    assert_eq!(m.destroy("t1", &a).status, 204);
    m.kill();
    let _m2 = rig.start();
    let _ = b;
    assert!(
        wait_until(Duration::from_secs(10), || real.residue().is_empty()),
        "{:?}",
        real.residue()
    );
}

#[test]
fn record_cold_start_and_density() {
    // This is a configured-capacity regression, not physical host capacity.
    // Full evidence collection (three modes, resource counters, raw samples)
    // is scripts/fc_real_measure.py and requires a coordinated resource window.
    let real = Real::new();
    let (m, _rig) = real.start(&[
        ("AR_FC_MAX_VMS", "64"),
        ("AR_FC_MAX_VCPUS", "64"),
        ("AR_FC_MAX_MEMORY_MIB", "65536"),
    ]);
    let mut samples = vec![];
    for _ in 0..10 {
        let t = Instant::now();
        let id = m.create_ok(
            "t1",
            json!({"runtime": "python", "cpu_millis": 1000, "memory_mb": 512}),
        );
        sh(&m, "t1", &id, "true");
        samples.push(t.elapsed());
        assert_eq!(m.destroy("t1", &id).status, 204);
    }
    samples.sort();
    let p = |q: f64| samples[(samples.len() as f64 * q).ceil() as usize - 1];
    eprintln!(
        "FC-REAL 冷启动到第一条命令：P50 {:?}，P95 {:?}（{} 次）",
        p(0.5),
        p(0.95),
        samples.len()
    );
    let mut ids = vec![];
    loop {
        let r = m.create(
            "t1",
            json!({"runtime": "python", "cpu_millis": 1000, "memory_mb": 256}),
        );
        if r.status != 201 {
            eprintln!(
                "FC-REAL idle VM观测下界：{} 台（第 {} 台：{} {}）；MAX_VMS=64/MAX_VCPUS=64，未测定物理上限",
                ids.len(),
                ids.len() + 1,
                r.status,
                r.code()
            );
            break;
        }
        let id = r.json()["env_id"].as_str().unwrap().to_string();
        assert_eq!(sh(&m, "t1", &id, "true").0, Some(0));
        ids.push(id);
    }
    for id in ids {
        assert_eq!(m.destroy("t1", &id).status, 204);
    }
}
