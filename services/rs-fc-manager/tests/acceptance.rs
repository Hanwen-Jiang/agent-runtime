//! fc-manager 的验收测试（D-20，docs/plan/firecracker-backend.md 第 5 节与第 6 节）：对着假的 jailer、firecracker 与客户机
//! agent（fc-fake），在没有 KVM 的 Linux 上跑。真 VM 的用例在 real_vm.rs（AR_TEST_FIRECRACKER=1）。
//!
//! 对照：验收 3（令牌不给命令）、4（内存、磁盘、CPU 的上限）的宿主侧在这里查参数与文件，客户机里的效果只能在真 VM 上查；
//! 验收 5（容量）、6（销毁不留残留、重复销毁、杀掉重启不留孤儿）在这里全测；验收 7 的宿主侧（VM 死了就忘掉它）在这里测，
//! 环境表记 lost 是适配器（下一段，SBX-01 的 Go Sandbox Service）的事。
#![cfg(target_os = "linux")]

mod common;

use std::collections::HashSet;
use std::fs;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::os::unix::fs::MetadataExt;
use std::time::Duration;

use common::*;
use serde_json::{Value, json};

#[test]
fn benchmark_profile_is_operator_gated_and_root_capacity_is_charged() {
    let request = json!({"runtime":"python","cpu_millis":1000,"memory_mb":512,"disk_mb":256,"guest_profile":"benchmark-system","root_disk_mb":512});
    let denied = Rig::new();
    let manager = denied.start();
    let response = manager.create("eval-tb20", request.clone());
    assert_eq!(response.status, 403);
    assert_eq!(response.code(), "guest_profile_denied");
    assert!(denied.leftovers().is_empty());
    let limited = Rig::new()
        .with("AR_FC_BENCHMARK_OWNERS", "eval-tb20")
        .with("AR_FC_MAX_SCRATCH_MIB", "600");
    let manager = limited.start();
    assert_eq!(
        manager.create("other", request.clone()).code(),
        "guest_profile_denied"
    );
    // Scratch alone fits; scratch plus private root does not.
    assert_eq!(manager.create("eval-tb20", request).code(), "host_capacity");
    assert!(limited.leftovers().is_empty());
}

#[test]
fn healthz_needs_no_token_and_everything_else_does() {
    let rig = Rig::new();
    let m = rig.start();
    assert_eq!(m.send("GET", "/healthz", &[], None).status, 200);

    let none = m.send("GET", "/v1/envs", &[("X-Sandbox-Owner", "_platform")], None);
    assert_eq!(none.status, 401);
    assert_eq!(none.code(), "unauthorized");
    let wrong = m.send(
        "GET",
        "/v1/envs",
        &[
            ("Authorization", "Bearer not-the-token-0123456789abcdef0123"),
            ("X-Sandbox-Owner", "_platform"),
        ],
        None,
    );
    assert_eq!(wrong.status, 401);
    // 少一个字符、多一个字符都不行（比较不能只比前缀）。
    let short = format!("Bearer {}", &TOKEN[..TOKEN.len() - 1]);
    assert_eq!(
        m.send(
            "GET",
            "/v1/envs",
            &[("Authorization", &short), ("X-Sandbox-Owner", "_platform")],
            None
        )
        .status,
        401
    );
    let long = format!("Bearer {TOKEN}x");
    assert_eq!(
        m.send(
            "GET",
            "/v1/envs",
            &[("Authorization", &long), ("X-Sandbox-Owner", "_platform")],
            None
        )
        .status,
        401
    );

    // 作用域头必填、格式要对。
    let auth = format!("Bearer {TOKEN}");
    let no_owner = m.send("GET", "/v1/envs", &[("Authorization", &auth)], None);
    assert_eq!(no_owner.status, 400);
    assert_eq!(no_owner.code(), "invalid_owner");
    let bad_owner = m.send(
        "GET",
        "/v1/envs",
        &[("Authorization", &auth), ("X-Sandbox-Owner", "a b")],
        None,
    );
    assert_eq!(bad_owner.status, 400);

    for r in [&none, &wrong, &no_owner] {
        assert!(!contains(&r.body, TOKEN), "错误里带了令牌");
    }
    assert!(!m.log().contains(TOKEN));
}

#[test]
fn warm_pool_survives_jail_hardlinks_and_hands_off_the_same_vm() {
    let rig = Rig::new()
        .with("AR_FC_WARM_POOL", "python:1000:256:1")
        .with("AR_FC_VM_DEFAULT_DISK_MIB", "256");
    let m = rig.start();
    assert!(wait_until(Duration::from_secs(5), || m
        .log()
        .contains("bootstrap path: cold")));
    // The initial image hardlink changes ctime but not content. It must not
    // invalidate the freshly booted pool VM or create an endless refill loop.
    std::thread::sleep(Duration::from_millis(100));
    let pool: Vec<_> = fs::read_dir(rig.path("j/firecracker"))
        .unwrap()
        .flatten()
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .collect();
    assert_eq!(pool.len(), 1);
    let id = m.create_ok(
        "t1",
        json!({"runtime":"python", "cpu_millis":1000, "memory_mb":256,"disk_mb":256}),
    );
    assert_eq!(id, pool[0], "{}", m.log());
    assert!(m.log().contains("从预热池交出"));
    assert_eq!(m.destroy("t1", &id).status, 204);
}

#[test]
fn create_list_destroy_round_trip_leaves_nothing_behind() {
    let rig = Rig::new();
    fs::write(rig.path("img/python.ext4.sha256"), "ab".repeat(32)).unwrap();
    let m = rig.start();
    let r = m.create("t1", spec());
    assert_eq!(r.status, 201, "{}\n{}", r.text(), m.log());
    let body = r.json();
    let id = body["env_id"].as_str().unwrap().to_string();
    assert!(id.starts_with("fc-") && id.len() == 19, "{id}");
    assert_eq!(body["image"], format!("sha256:{}", "ab".repeat(32)));
    assert!(body["created_at"].as_str().unwrap().ends_with('Z'));

    // 列出只给平台作用域；带 owner 与 labels，给对账用（D-11）。
    let listed = m.list();
    assert_eq!(listed.len(), 1);
    assert_eq!(listed[0]["env_id"], id.as_str());
    assert_eq!(listed[0]["owner"], "t1");
    assert_eq!(listed[0]["labels"], json!({"workspace": "w1"}));
    assert_eq!(listed[0]["state"], "running");
    assert_eq!(listed[0]["created_at"], body["created_at"]);
    let as_tenant = m.call("GET", "/v1/envs", "t1", None);
    assert_eq!(as_tenant.status, 403);
    assert_eq!(as_tenant.code(), "platform_only");

    // 别的作用域销毁：404，看不出环境在不在。
    let other = m.destroy("t2", &id);
    assert_eq!(other.status, 404);
    assert_eq!(other.code(), "unknown_env");
    assert_eq!(m.list().len(), 1);

    let pid = rig.vm_pid(&id);
    assert!(pid_alive(pid));
    assert_eq!(m.destroy("t1", &id).status, 204);
    assert!(!pid_alive(pid), "VM 进程还在");
    assert_eq!(rig.leftovers(), Vec::<String>::new());
    // 重复销毁不报错；没有的环境也是 204。
    assert_eq!(m.destroy("t1", &id).status, 204);
    assert_eq!(m.destroy("t1", "fc-0000000000000000").status, 204);
    assert!(m.list().is_empty());
}

#[test]
fn jail_cgroup_and_vm_are_configured_from_the_spec() {
    let rig = Rig::new();
    let m = rig.start();
    let a = m.create_ok("t1", spec());
    let b = m.create_ok(
        "t1",
        json!({"runtime": "python", "cpu_millis": 250, "memory_mb": 256}),
    );

    let ja = rig.read_json(&rig.jail(&a).join("fake-jailer.json"));
    let jb = rig.read_json(&rig.jail(&b).join("fake-jailer.json"));
    let argv: Vec<String> = serde_json::from_value(ja["argv"].clone()).unwrap();
    let flag = |name: &str| -> Vec<String> {
        argv.windows(2)
            .filter(|w| w[0] == name)
            .map(|w| w[1].clone())
            .collect()
    };
    // jailer：非 root、自己的 uid（每台不同）、chroot、独立的 PID 与网络命名空间、cgroup v2。
    let uid_a: u32 = flag("--uid")[0].parse().unwrap();
    let uid_b: u32 = jb["argv"]
        .as_array()
        .unwrap()
        .windows(2)
        .find(|w| w[0] == "--uid")
        .unwrap()[1]
        .as_str()
        .unwrap()
        .parse()
        .unwrap();
    assert!(
        uid_a >= 900000 && uid_b >= 900000 && uid_a != uid_b,
        "{uid_a} {uid_b}"
    );
    assert_eq!(flag("--gid")[0].parse::<u32>().unwrap(), uid_a);
    assert_eq!(flag("--id"), vec![a.clone()]);
    assert_eq!(
        flag("--exec-file"),
        vec![rig.path("bin/firecracker").to_string_lossy().into_owned()]
    );
    assert_eq!(
        flag("--chroot-base-dir"),
        vec![rig.path("j").to_string_lossy().into_owned()]
    );
    assert!(argv.contains(&"--new-pid-ns".to_string()));
    let netns = flag("--netns");
    assert_eq!(netns.len(), 1);
    assert!(netns[0].starts_with(&rig.path("ns").to_string_lossy().into_owned()));
    assert_eq!(ja["netns_existed"], true, "jailer 起来时 netns 要已经建好");
    assert_eq!(flag("--cgroup-version"), vec!["2".to_string()]);
    assert_eq!(flag("--parent-cgroup"), vec![PARENT_CGROUP.to_string()]);
    let cgroups: HashSet<String> = flag("--cgroup").into_iter().collect();
    assert!(cgroups.contains("cpu.max=150000 100000"), "{cgroups:?}");
    assert!(
        cgroups.contains(&format!("memory.max={}", (512u64 + 64) << 20)),
        "{cgroups:?}"
    );
    assert!(
        cgroups.iter().any(|c| c.starts_with("pids.max=")),
        "{cgroups:?}"
    );
    // 假 jailer 按参数写出的 cgroup 文件。
    let cg = rig.path("cg").join(PARENT_CGROUP).join(&a);
    assert_eq!(
        fs::read_to_string(cg.join("cpu.max")).unwrap(),
        "150000 100000"
    );
    let cgb = rig.path("cg").join(PARENT_CGROUP).join(&b);
    assert_eq!(
        fs::read_to_string(cgb.join("cpu.max")).unwrap(),
        "25000 100000"
    );
    // fc-manager 在父 cgroup 打开要用的控制器。
    let subtree = fs::read_to_string(
        rig.path("cg")
            .join(PARENT_CGROUP)
            .join("cgroup.subtree_control"),
    )
    .unwrap();
    for c in ["+cpu", "+memory", "+pids", "+io"] {
        assert!(subtree.contains(c), "{subtree}");
    }

    // 交给 firecracker 的参数：seccomp 不关、不换过滤器；API socket 在 jail 里。
    let tail: Vec<&String> = argv.iter().skip_while(|a| *a != "--").skip(1).collect();
    assert!(
        tail.windows(2)
            .any(|w| w[0] == "--api-sock" && w[1] == "/run/firecracker.socket"),
        "{tail:?}"
    );
    assert!(
        !argv
            .iter()
            .any(|a| a == "--no-seccomp" || a.starts_with("--seccomp-filter")),
        "{argv:?}"
    );
    // jailer 的环境是清空过的：拿不到 fc-manager 的配置与密钥文件的位置。
    let env: Vec<String> = serde_json::from_value(ja["env"].clone()).unwrap();
    assert!(env.iter().all(|kv| !kv.starts_with("AR_")), "{env:?}");

    // firecracker 的 API 调用：vCPU 向上取整、内存、关 SMT；rootfs 只读，scratch 可写；vsock；启动参数。
    let calls = rig.api_calls(&a);
    let put = |path: &str| -> Value {
        calls
            .iter()
            .find(|c| c["method"] == "PUT" && c["path"] == path)
            .unwrap_or_else(|| panic!("没有 PUT {path}：{calls:?}"))["body"]
            .clone()
    };
    let mc = put("/machine-config");
    assert_eq!(mc["vcpu_count"], 2);
    assert_eq!(mc["mem_size_mib"], 512);
    assert_eq!(mc["smt"], false);
    let root = put("/drives/rootfs");
    assert_eq!(root["is_root_device"], true);
    assert_eq!(root["is_read_only"], true);
    assert_eq!(root["path_on_host"], "/rootfs.ext4");
    let scratch = put("/drives/scratch");
    assert_eq!(scratch["is_root_device"], false);
    assert_eq!(scratch["is_read_only"], false);
    let vsock = put("/vsock");
    assert_eq!(vsock["guest_cid"], 3);
    assert_eq!(vsock["uds_path"], "/run/v.sock");
    let boot = put("/boot-source");
    assert_eq!(boot["kernel_image_path"], "/vmlinux");
    let args = boot["boot_args"].as_str().unwrap();
    let words: Vec<&str> = args.split_whitespace().collect();
    for want in ["console=ttyS0", "panic=1", "ro", "--init"] {
        assert!(words.contains(&want), "{args}");
    }
    assert!(
        words.iter().any(|w| w.starts_with("AGENT_LISTEN=vsock:")),
        "{args}"
    );
    assert!(!args.contains(TOKEN), "{args}");
    assert_eq!(
        calls.last().unwrap()["body"]["action_type"],
        "InstanceStart"
    );

    // 盘：内核与 rootfs 是硬链接（不拷 GB 级的文件）；scratch 是稀疏的、按 disk_mb 定大小、属于这台 VM 的 uid。
    let jroot = rig.jail(&a).join("root");
    assert_eq!(
        fs::metadata(jroot.join("rootfs.ext4")).unwrap().ino(),
        fs::metadata(rig.path("img/python.ext4")).unwrap().ino()
    );
    assert_eq!(
        fs::metadata(jroot.join("vmlinux")).unwrap().ino(),
        fs::metadata(rig.path("img/vmlinux")).unwrap().ino()
    );
    let sm = fs::metadata(jroot.join("scratch.ext4")).unwrap();
    assert_eq!(sm.len(), 256 << 20);
    assert!(
        sm.blocks() * 512 < 1 << 20,
        "scratch 不是稀疏文件：{} 块",
        sm.blocks()
    );
    assert_eq!(sm.mode() & 0o777, 0o600);
    // 默认的盘大小来自配置（b 没写 disk_mb）。
    assert!(
        fs::metadata(rig.jail(&b).join("root/scratch.ext4"))
            .unwrap()
            .len()
            > 0
    );
}

#[test]
fn proxy_reaches_the_guest_agent_with_its_own_token_only() {
    let rig = Rig::new();
    let m = rig.start();
    let id = m.create_ok("t1", spec());

    let body = b"{\"argv\": [\"true\"]}".to_vec();
    let r = m.agent(
        "t1",
        &id,
        "POST",
        "/v1/echo?x=1&y=%2F",
        &[
            ("X-Sandbox-Token", "forged-by-the-caller"),
            ("Content-Type", "application/json"),
        ],
        Some(&body),
    );
    assert_eq!(r.status, 200, "{}\n{}", r.text(), m.log());
    let seen = r.json();
    assert_eq!(seen["method"], "POST");
    assert_eq!(seen["target"], "/v1/echo?x=1&y=%2F");
    assert_eq!(seen["body_len"], body.len());
    assert_eq!(seen["body_fnv"], format!("{:016x}", fnv1a64(&body)));
    // agent 只看到 fc-manager 给的那一个令牌头；调用方的令牌、作用域、伪造的 agent 令牌都到不了。
    assert_eq!(seen["token_headers"], 1);
    let names: Vec<String> = serde_json::from_value(seen["header_names"].clone()).unwrap();
    for gone in ["authorization", "x-sandbox-owner"] {
        assert!(!names.contains(&gone.to_string()), "{names:?}");
    }
    assert!(names.contains(&"content-type".to_string()));
    assert_eq!(r.header("connection"), Some("close"));

    // 大的请求体与分块的请求体原样到达。
    let big: Vec<u8> = (0..(4 << 20)).map(|i| (i % 253) as u8).collect();
    let r = m.agent("t1", &id, "PUT", "/v1/echo", &[], Some(&big));
    assert_eq!(r.status, 200);
    assert_eq!(r.json()["body_len"], big.len());
    assert_eq!(r.json()["body_fnv"], format!("{:016x}", fnv1a64(&big)));
    let auth = format!("Bearer {TOKEN}");
    let chunked = format!(
        "POST /v1/envs/{id}/agent/v1/echo HTTP/1.1\r\nHost: x\r\nAuthorization: {auth}\r\nX-Sandbox-Owner: t1\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
    );
    let r = m.raw(chunked.as_bytes());
    assert_eq!(r.status, 200, "{}", r.text());
    assert_eq!(
        r.json()["body_fnv"],
        format!("{:016x}", fnv1a64(b"hello world"))
    );

    // 大的响应（agent 分块发）原样回来。
    let r = m.agent("t1", &id, "GET", "/v1/big?n=2000000", &[], None);
    assert_eq!(r.status, 200);
    assert_eq!(r.body.len(), 2_000_000);
    assert!(
        r.body
            .iter()
            .enumerate()
            .all(|(i, b)| *b == (i % 251) as u8)
    );

    // agent 自己的错误照样透传（不是 fc-manager 的 404）。
    let r = m.agent("t1", &id, "GET", "/v1/missing", &[], None);
    assert_eq!(r.status, 404);
    assert_eq!(r.code(), "fake_not_found");
}

#[test]
fn proxy_hides_environments_of_other_scopes() {
    let rig = Rig::new();
    let m = rig.start();
    let id = m.create_ok("t1", spec());
    for (owner, env) in [
        ("t2", id.as_str()),
        ("t1", "fc-0000000000000000"),
        ("t1", "../etc"),
        ("_platform", id.as_str()),
    ] {
        let r = m.agent(owner, env, "POST", "/v1/echo", &[], Some(b"x"));
        assert_eq!(r.status, 404, "{owner} {env}: {}", r.text());
        assert_eq!(r.code(), "unknown_env");
    }
    // 只转 agent 的路径；fc-manager 自己的路由之外一律 404。
    assert_eq!(
        m.call("GET", &format!("/v1/envs/{id}/other"), "t1", None)
            .status,
        404
    );
    // 不认的方法、协议升级拒绝。
    let r = m.agent(
        "t1",
        &id,
        "GET",
        "/v1/echo",
        &[("Upgrade", "websocket"), ("Connection", "Upgrade")],
        None,
    );
    assert_eq!(r.status, 400);
    let log = fs::read_to_string(rig.jail(&id).join("root/fake-agent.log")).unwrap_or_default();
    assert!(
        !log.contains("POST /v1/echo"),
        "别的作用域的请求到了 agent：{log}"
    );
}

#[test]
fn request_smuggling_shapes_are_rejected() {
    let rig = Rig::new();
    let m = rig.start();
    let id = m.create_ok("t1", spec());
    let auth = format!("Bearer {TOKEN}");
    let base = format!(
        "POST /v1/envs/{id}/agent/v1/echo HTTP/1.1\r\nHost: x\r\nAuthorization: {auth}\r\nX-Sandbox-Owner: t1\r\n"
    );
    for extra in [
        "Content-Length: 5\r\nTransfer-Encoding: chunked\r\n",
        "Content-Length: 5\r\nContent-Length: 6\r\n",
        "Transfer-Encoding: gzip\r\n",
        "Content-Length: -1\r\n",
        "Content-Length: 5 \r\nX-Bad\r\n",
    ] {
        let r = m.raw(format!("{base}{extra}\r\nhello").as_bytes());
        assert_eq!(r.status, 400, "{extra:?}: {}", r.text());
    }
    // 请求头太大：431，不读到内存撑爆。
    let huge = format!("{base}X-Pad: {}\r\n\r\n", "a".repeat(64 << 10));
    assert_eq!(m.raw(huge.as_bytes()).status, 431);
}

#[test]
fn caller_disconnect_reaches_the_agent() {
    // agent 靠连接断开取消命令（services/go-sandbox-agent 的 exec）；经 fc-manager 转发也要传到。
    let rig = Rig::new();
    let m = rig.start();
    let id = m.create_ok("t1", spec());
    let mut s = TcpStream::connect(m.addr).unwrap();
    let req = format!(
        "POST /v1/envs/{id}/agent/v1/slow HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\nX-Sandbox-Owner: t1\r\nContent-Length: 2\r\n\r\n{{}}"
    );
    s.write_all(req.as_bytes()).unwrap();
    let log = rig.jail(&id).join("root/fake-agent.log");
    assert!(wait_until(Duration::from_secs(5), || fs::read_to_string(
        &log
    )
    .unwrap_or_default()
    .contains("slow started")));
    drop(s);
    assert!(
        wait_until(Duration::from_secs(5), || fs::read_to_string(&log)
            .unwrap_or_default()
            .contains("slow disconnected")),
        "agent 没看到断开：{}",
        fs::read_to_string(&log).unwrap_or_default()
    );
}

#[test]
fn over_capacity_is_transient_and_spares_running_vms() {
    let rig = Rig::new().with("AR_FC_MAX_VMS", "2");
    let m = rig.start();
    let a = m.create_ok("t1", spec());
    let b = m.create_ok("t2", spec());
    let r = m.create("t1", spec());
    assert_eq!(r.status, 503, "{}", r.text());
    assert_eq!(r.code(), "host_capacity");
    assert!(r.header("retry-after").is_some());
    // 已有的 VM 不受影响。
    for (owner, id) in [("t1", &a), ("t2", &b)] {
        assert_eq!(
            m.agent(owner, id, "POST", "/v1/echo", &[], Some(b"ok"))
                .status,
            200
        );
    }
    assert_eq!(
        rig.leftovers()
            .iter()
            .filter(|p| p.contains("/j/firecracker/"))
            .count(),
        2
    );
    assert_eq!(m.destroy("t1", &a).status, 204);
    m.create_ok("t1", spec());
}

#[test]
fn memory_and_disk_budgets_count_toward_capacity() {
    let rig = Rig::new()
        .with("AR_FC_MAX_MEMORY_MIB", "1024")
        .with("AR_FC_MAX_SCRATCH_MIB", "1024");
    let m = rig.start();
    m.create_ok(
        "t1",
        json!({"runtime": "python", "cpu_millis": 1000, "memory_mb": 768, "disk_mb": 128}),
    );
    let r = m.create(
        "t1",
        json!({"runtime": "python", "cpu_millis": 1000, "memory_mb": 512, "disk_mb": 128}),
    );
    assert_eq!(r.status, 503);
    assert_eq!(r.code(), "host_capacity");
    let r = m.create(
        "t1",
        json!({"runtime": "python", "cpu_millis": 1000, "memory_mb": 128, "disk_mb": 1000}),
    );
    assert_eq!(r.status, 503);
    m.create_ok(
        "t1",
        json!({"runtime": "python", "cpu_millis": 1000, "memory_mb": 128, "disk_mb": 128}),
    );
}

#[test]
fn concurrent_creates_respect_the_cap() {
    let rig = Rig::new().with("AR_FC_MAX_VMS", "3");
    let m = rig.start();
    let statuses: Vec<u16> = std::thread::scope(|s| {
        let hs: Vec<_> = (0..6)
            .map(|_| s.spawn(|| m.create("t1", spec()).status))
            .collect();
        hs.into_iter().map(|h| h.join().unwrap()).collect()
    });
    assert_eq!(
        statuses.iter().filter(|s| **s == 201).count(),
        3,
        "{statuses:?}"
    );
    assert_eq!(
        statuses.iter().filter(|s| **s == 503).count(),
        3,
        "{statuses:?}"
    );
    assert_eq!(m.list().len(), 3);
}

#[test]
fn invalid_specs_are_rejected_before_anything_is_created() {
    let rig = Rig::new();
    let m = rig.start();
    let cases = [
        (
            json!({"runtime": "python", "cpu_millis": 0, "memory_mb": 512}),
            "invalid_argument",
        ),
        (
            json!({"runtime": "python", "cpu_millis": 1000, "memory_mb": 64}),
            "invalid_argument",
        ),
        (
            json!({"runtime": "python", "cpu_millis": 1000, "memory_mb": 1 << 20}),
            "invalid_argument",
        ),
        (
            json!({"runtime": "python", "cpu_millis": 64000, "memory_mb": 512}),
            "invalid_argument",
        ),
        (
            json!({"runtime": "python", "cpu_millis": 1000, "memory_mb": 512, "disk_mb": 1 << 30}),
            "invalid_argument",
        ),
        (
            json!({"runtime": "nope", "cpu_millis": 1000, "memory_mb": 512}),
            "unsupported_runtime",
        ),
        (
            json!({"runtime": "../img/python", "cpu_millis": 1000, "memory_mb": 512}),
            "invalid_argument",
        ),
        (
            json!({"runtime": "python", "cpu_millis": 1000, "memory_mb": 512, "labels": {"Bad Key": "v"}}),
            "invalid_argument",
        ),
        (
            json!({"runtime": "python", "cpu_millis": 1000, "memory_mb": 512, "labels": {"k": "a\nb"}}),
            "invalid_argument",
        ),
        (
            json!({"runtime": "python", "cpu_millis": 1000, "memory_mb": 512, "surprise": 1}),
            "invalid_argument",
        ),
    ];
    for (body, code) in cases {
        let r = m.create("t1", body.clone());
        assert_eq!(
            (r.status, r.code()),
            (400, code.to_string()),
            "{body}: {}",
            r.text()
        );
    }
    let auth = format!("Bearer {TOKEN}");
    let r = m.send(
        "POST",
        "/v1/envs",
        &[("Authorization", &auth), ("X-Sandbox-Owner", "t1")],
        Some(b"{not json"),
    );
    assert_eq!((r.status, r.code()), (400, "invalid_request".to_string()));
    let r = m.send(
        "POST",
        "/v1/envs",
        &[("Authorization", &auth), ("X-Sandbox-Owner", "t1")],
        Some(&vec![b' '; 200 << 10]),
    );
    assert_eq!(r.status, 413);
    assert_eq!(rig.leftovers(), Vec::<String>::new());
}

#[test]
fn boot_failures_leave_nothing_behind_and_release_capacity() {
    let rig = Rig::new()
        .with("AR_FC_MAX_VMS", "1")
        .with("AR_FC_BOOT_TIMEOUT_S", "2");
    let m = rig.start();
    // 客户机一直不来取令牌（agent 没起来）：超时，transient。
    rig.set_fake(json!({"guest": "never_fetch_token"}));
    let r = m.create("t1", spec());
    assert_eq!(
        (r.status, r.code()),
        (503, "boot_failed".to_string()),
        "{}",
        r.text()
    );
    assert_eq!(rig.leftovers(), Vec::<String>::new());
    // firecracker 拒绝配置：不是 transient。
    rig.set_fake(json!({"api_fail": "/machine-config"}));
    let r = m.create("t1", spec());
    assert_eq!(
        (r.status, r.code()),
        (500, "vm_config_rejected".to_string()),
        "{}",
        r.text()
    );
    assert!(
        !r.text().contains("/tmp/"),
        "错误里带了宿主路径：{}",
        r.text()
    );
    assert_eq!(rig.leftovers(), Vec::<String>::new());
    // jailer 失败。
    rig.set_fake(json!({"jailer_exit": 3}));
    let r = m.create("t1", spec());
    assert_eq!(r.status, 503, "{}", r.text());
    assert_eq!(rig.leftovers(), Vec::<String>::new());
    // 名额都还回来了。
    rig.set_fake(json!({}));
    m.create_ok("t1", spec());
}

#[test]
fn killed_manager_leaves_no_orphan_vms_after_restart() {
    // fault 注入：SIGKILL fc-manager（不给它收尾），VM 进程还在跑；再起一个 fc-manager，必须把它们全部终止并清理干净
    // （D-20 定的是“终止”，不接管：见 src/manager.rs 的说明）。
    let rig = Rig::new();
    let mut m = rig.start();
    let a = m.create_ok("t1", spec());
    let b = m.create_ok("t2", spec());
    let pids = [rig.vm_pid(&a), rig.vm_pid(&b)];
    m.kill();
    assert!(
        pids.iter().all(|p| pid_alive(*p)),
        "测试的前提：fc-manager 死了 VM 还活着"
    );
    assert_eq!(
        rig.leftovers()
            .iter()
            .filter(|p| p.contains("/s/vms/"))
            .count(),
        2
    );

    let m2 = rig.start();
    assert!(
        wait_until(Duration::from_secs(5), || pids
            .iter()
            .all(|p| !pid_alive(*p))),
        "孤儿 VM 没被终止"
    );
    assert!(m2.list().is_empty());
    assert_eq!(rig.leftovers(), Vec::<String>::new());
    // 之后照常能建。
    m2.create_ok("t1", spec());
}

#[test]
fn crash_in_the_middle_of_create_is_swept_on_restart() {
    for point in ["after_record", "after_spawn", "after_start"] {
        let rig = Rig::new();
        let mut m = rig.start_with(&[("AR_FC_FAULT_INJECT", point)]);
        let r = m.create("t1", spec());
        assert_eq!(
            r.status,
            0,
            "{point}：fc-manager 应当在这一步崩掉：{}",
            r.text()
        );
        assert!(m.wait_exit(Duration::from_secs(5)), "{point}");
        let m2 = rig.start();
        assert!(
            wait_until(Duration::from_secs(5), || rig.leftovers().is_empty()),
            "{point}：残留 {:?}\n{}",
            rig.leftovers(),
            m2.log()
        );
        assert!(m2.list().is_empty());
        assert!(!any_fake_vm_running(&rig), "{point}：还有假 VM 进程");
    }
}

#[test]
fn sweep_subcommand_cleans_up_after_a_crash() {
    // systemd 的 ExecStopPost 跑 fc-manager sweep：主进程不管怎么退出，VM 都不留。
    let rig = Rig::new();
    let mut m = rig.start();
    let id = m.create_ok("t1", spec());
    let pid = rig.vm_pid(&id);
    m.kill();
    let out = rig.command(&["sweep"]).output().unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(wait_until(Duration::from_secs(5), || !pid_alive(pid)));
    assert_eq!(rig.leftovers(), Vec::<String>::new());
    // 什么都没有时再跑一次也成功。
    assert!(rig.command(&["sweep"]).status().unwrap().success());
}

#[test]
fn sweep_does_not_remove_another_managers_network_namespace() {
    let rig = Rig::new();
    let foreign = rig.path("ns/arfc-0123456789abcdef");
    std::fs::write(&foreign, b"another manager owns this namespace").unwrap();
    assert!(rig.command(&["sweep"]).status().unwrap().success());
    assert!(foreign.exists());
}

#[test]
fn a_vm_that_dies_is_forgotten_and_cleaned() {
    // 验收 7 的宿主侧：VM 被手工杀掉后 fc-manager 自己发现、清理，之后的请求是 unknown_env（适配器据此记 lost、透明换新）。
    let rig = Rig::new();
    let m = rig.start();
    let id = m.create_ok("t1", spec());
    let keep = m.create_ok("t1", spec());
    kill9(rig.vm_pid(&id));
    assert!(
        wait_until(Duration::from_secs(5), || m
            .list()
            .iter()
            .all(|e| e["env_id"] != id.as_str())),
        "{}",
        m.log()
    );
    let r = m.agent("t1", &id, "POST", "/v1/echo", &[], Some(b"x"));
    assert_eq!((r.status, r.code()), (404, "unknown_env".to_string()));
    assert!(wait_until(Duration::from_secs(5), || !rig
        .jail(&id)
        .exists()));
    assert_eq!(
        m.agent("t1", &keep, "POST", "/v1/echo", &[], Some(b"x"))
            .status,
        200
    );
    assert!(m.log().contains(&id), "VM 的退出要记日志");
}

#[test]
fn tokens_stay_out_of_logs_errors_and_disk() {
    let rig = Rig::new();
    let m = rig.start();
    let id = m.create_ok("t1", spec());
    let agent_token = m
        .agent("t1", &id, "GET", "/v1/fake/token", &[], None)
        .text();
    assert!(agent_token.len() >= 32, "{agent_token}");
    assert_ne!(agent_token, TOKEN);

    // 走一遍会出错的路：错的令牌、别的作用域、坏的规格、agent 的 401。
    let mut bodies: Vec<Vec<u8>> = vec![];
    bodies.push(
        m.send(
            "GET",
            "/v1/envs",
            &[("Authorization", "Bearer x"), ("X-Sandbox-Owner", "t1")],
            None,
        )
        .body,
    );
    bodies.push(m.agent("t2", &id, "GET", "/v1/echo", &[], None).body);
    bodies.push(
        m.create(
            "t1",
            json!({"runtime": "nope", "cpu_millis": 1, "memory_mb": 512}),
        )
        .body,
    );
    bodies.push(m.agent("t1", &id, "GET", "/v1/echo", &[], None).body);
    assert_eq!(m.destroy("t1", &id).status, 204);

    let log = m.log();
    for secret in [TOKEN, agent_token.as_str()] {
        assert!(!log.contains(secret), "日志里有令牌");
        assert!(bodies.iter().all(|b| !contains(b, secret)), "响应里有令牌");
    }
    // 落盘的东西（记录、日志、jail、假 jailer 记下的 argv 与环境、假 firecracker 收到的启动参数）都没有 agent 令牌。
    // 只在 SecretStore 文件里有 fc-manager 的令牌（那是它的来源）。
    let disk = rig.all_file_bytes();
    assert!(!contains(&disk, &agent_token), "agent 令牌落盘了");
    let secrets = fs::read(rig.path("secrets.json")).unwrap();
    let count = disk
        .windows(TOKEN.len())
        .filter(|w| *w == TOKEN.as_bytes())
        .count();
    assert_eq!(
        count,
        secrets
            .windows(TOKEN.len())
            .filter(|w| *w == TOKEN.as_bytes())
            .count()
    );
}

#[test]
fn the_token_can_be_rotated_without_a_restart() {
    let rig = Rig::new();
    let m = rig.start();
    let id = m.create_ok("t1", spec());
    let fresh = "rotated-token-abcdefghijklmnopqrstuvwxyz012345";
    rig.write_secrets(fresh);
    assert!(wait_until(Duration::from_secs(3), || m
        .call("GET", "/v1/envs", "_platform", None)
        .status
        == 401));
    let r = m.send(
        "GET",
        "/v1/envs",
        &[
            ("Authorization", &format!("Bearer {fresh}")),
            ("X-Sandbox-Owner", "_platform"),
        ],
        None,
    );
    assert_eq!(r.status, 200);
    assert_eq!(
        r.json()["environments"][0]["env_id"],
        id.as_str(),
        "换令牌不能动已有的 VM"
    );
}

#[test]
fn bad_configuration_refuses_to_start_without_echoing_secrets() {
    let check = |rig: &Rig, extra: &[(&str, &str)], want: &str| {
        let mut cmd = rig.command(&["serve"]);
        for (k, v) in extra {
            cmd.env(k, v);
        }
        let out = cmd.output().unwrap();
        let err = String::from_utf8_lossy(&out.stderr).into_owned();
        assert!(!out.status.success(), "应当拒绝启动：{extra:?}");
        assert!(err.contains(want), "{extra:?}：{err}");
        assert!(!err.contains(TOKEN));
    };
    let rig = Rig::new();
    check(
        &rig,
        &[("AR_SECRETS_FILE", "/nonexistent/secrets.json")],
        "AR_SECRETS_FILE",
    );
    // 以 root 跑测试时这一条不成立（root 不需要 AR_FC_ALLOW_NON_ROOT）。
    if ar_sys::geteuid() != 0 {
        check(&rig, &[("AR_FC_ALLOW_NON_ROOT", "")], "root");
    }
    check(
        &rig,
        &[("AR_FC_KERNEL", "/nonexistent/vmlinux")],
        "AR_FC_KERNEL",
    );
    check(&rig, &[("AR_FC_MAX_VMS", "zero")], "AR_FC_MAX_VMS");
    let long = format!("{}/{}", rig.path("j").display(), "x".repeat(60));
    check(&rig, &[("AR_FC_CHROOT_BASE", &long)], "AR_FC_CHROOT_BASE");
    rig.write_secrets("too-short");
    check(&rig, &[], "sandbox/fc-manager-token");
}

fn any_fake_vm_running(rig: &Rig) -> bool {
    // 假 firecracker 的命令行里有 jail 的路径（cwd）；按 /proc 找本测试目录下的进程。
    let needle = rig.path("j").to_string_lossy().into_owned();
    let Ok(rd) = fs::read_dir("/proc") else {
        return false;
    };
    for e in rd.flatten() {
        let Ok(pid) = e.file_name().to_string_lossy().parse::<i32>() else {
            continue;
        };
        if let Ok(cwd) = fs::read_link(format!("/proc/{pid}/cwd"))
            && cwd.to_string_lossy().starts_with(&needle)
            && pid_alive(pid)
        {
            return true;
        }
        let mut buf = vec![];
        if let Ok(mut f) = fs::File::open(format!("/proc/{pid}/cmdline")) {
            let _ = f.read_to_end(&mut buf);
            if contains(&buf, &needle) && pid_alive(pid) && pid as u32 != std::process::id() {
                return true;
            }
        }
    }
    false
}
