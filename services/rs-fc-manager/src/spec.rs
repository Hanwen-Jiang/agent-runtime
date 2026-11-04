//! 建环境的请求、校验，以及由它算出的 jailer 参数、cgroup 上限与客户机的启动参数。都是纯函数，便于对着数字测。
//!
//! 与客户机里 sandbox-agent（D-28 的 Rust 版）的约定写在这里：内核命令行给 `init=<agent> AGENT_LISTEN=vsock:<端口>
//! AGENT_TOKEN_VSOCK=<端口> -- --init`；agent 以 PID 1 启动，先连宿主（CID 2）的令牌端口读一行令牌，再在 vsock 上监听。
//! 令牌不在命令行里（客户机里谁都能读 /proc/cmdline）。

use std::collections::BTreeMap;
use std::path::Path;

use serde::{Deserialize, Serialize};

pub const GUEST_CID: u32 = 3;
/// agent 在客户机里监听的 vsock 端口。小于 1024：客户机里非 root 的命令绑不了，agent 挂了也占不了它的位置。
pub const AGENT_PORT: u32 = 700;
/// 客户机启动时向宿主取令牌的端口（宿主侧是 <uds_path>_701，交出一次就删掉）。
pub const TOKEN_PORT: u32 = 701;
/// 以下路径都在 jail 里（firecracker 看到的根）。socket 放在 /run：它属于这台 VM 的 uid，firecracker 才能在里面建 socket。
pub const API_SOCK: &str = "/run/firecracker.socket";
pub const VSOCK_UDS: &str = "/run/v.sock";
pub const KERNEL_IN_JAIL: &str = "/vmlinux";
pub const ROOTFS_IN_JAIL: &str = "/rootfs.ext4";
pub const SCRATCH_IN_JAIL: &str = "/scratch.ext4";
pub const CPU_PERIOD_US: u64 = 100_000;
/// VMM 进程（firecracker 的 API、VMM 与 vCPU 线程）的 pids.max；一台 VM 只有十来个线程。
pub const PIDS_MAX: u32 = 64;
pub const MIN_CPU_MILLIS: u64 = 100;
pub const MIN_MEMORY_MIB: u64 = 128;
pub const MIN_DISK_MIB: u64 = 64;
pub const MAX_LABELS: usize = 16;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CreateRequest {
    pub runtime: String,
    pub cpu_millis: u64,
    pub memory_mb: u64,
    #[serde(default)]
    pub disk_mb: Option<u64>,
    #[serde(default)]
    pub labels: BTreeMap<String, String>,
}

/// 单台 VM 的上限（配置）。
#[derive(Debug, Clone)]
pub struct Limits {
    pub vm_max_vcpus: u32,
    pub vm_max_memory_mib: u64,
    pub vm_default_disk_mib: u64,
    pub vm_max_disk_mib: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct VmSpec {
    pub runtime: String,
    pub cpu_millis: u64,
    pub vcpus: u32,
    pub memory_mib: u64,
    pub disk_mib: u64,
}

pub fn validate(
    req: CreateRequest,
    l: &Limits,
) -> Result<(VmSpec, BTreeMap<String, String>), String> {
    if !valid_runtime(&req.runtime) {
        return Err("runtime 只能是小写字母、数字、- 与 _，最多 63 个字符".into());
    }
    let max_millis = u64::from(l.vm_max_vcpus) * 1000;
    if req.cpu_millis < MIN_CPU_MILLIS || req.cpu_millis > max_millis {
        return Err(format!(
            "cpu_millis 要在 {MIN_CPU_MILLIS} 到 {max_millis} 之间"
        ));
    }
    if req.memory_mb < MIN_MEMORY_MIB || req.memory_mb > l.vm_max_memory_mib {
        return Err(format!(
            "memory_mb 要在 {MIN_MEMORY_MIB} 到 {} 之间",
            l.vm_max_memory_mib
        ));
    }
    let disk = req.disk_mb.unwrap_or(l.vm_default_disk_mib);
    if disk < MIN_DISK_MIB || disk > l.vm_max_disk_mib {
        return Err(format!(
            "disk_mb 要在 {MIN_DISK_MIB} 到 {} 之间",
            l.vm_max_disk_mib
        ));
    }
    if req.labels.len() > MAX_LABELS {
        return Err(format!("labels 最多 {MAX_LABELS} 个"));
    }
    for (k, v) in &req.labels {
        if !valid_label_key(k) || !valid_label_value(v) {
            return Err("labels 的键只能是小写字母、数字与 ._/-（最多 63 个字符），值最多 256 字节、不含控制字符".into());
        }
    }
    let vcpus = req.cpu_millis.div_ceil(1000) as u32;
    let spec = VmSpec {
        runtime: req.runtime,
        cpu_millis: req.cpu_millis,
        vcpus,
        memory_mib: req.memory_mb,
        disk_mib: disk,
    };
    Ok((spec, req.labels))
}

fn lower_alnum(b: u8) -> bool {
    b.is_ascii_lowercase() || b.is_ascii_digit()
}

/// runtime 名同时是 images 目录里的文件名（<runtime>.ext4），所以不许有 . 与 /。
pub fn valid_runtime(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty()
        && b.len() <= 63
        && lower_alnum(b[0])
        && b.iter()
            .all(|c| lower_alnum(*c) || *c == b'-' || *c == b'_')
}

pub fn valid_label_key(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty()
        && b.len() <= 63
        && lower_alnum(b[0])
        && b.iter().all(|c| lower_alnum(*c) || b"._/-".contains(c))
}

pub fn valid_label_value(s: &str) -> bool {
    s.len() <= 256 && !s.chars().any(char::is_control)
}

/// 作用域：租户 ID 或 _platform（与 Cloudflare 桥的 OWNER 一致）。
pub fn valid_owner(s: &str) -> bool {
    if s == "_platform" {
        return true;
    }
    let b = s.as_bytes();
    !b.is_empty()
        && b.len() <= 63
        && lower_alnum(b[0])
        && b.iter()
            .all(|c| lower_alnum(*c) || *c == b'-' || *c == b'_')
}

pub const ENV_ID_PREFIX: &str = "fc-";

/// 环境 ID：fc- 加 16 位小写十六进制。它出现在 jail、cgroup、netns 的路径里，所以格式写死。
pub fn valid_env_id(s: &str) -> bool {
    s.len() == 19
        && s.starts_with(ENV_ID_PREFIX)
        && s[3..]
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// netns 的名字：arfc- 加环境 ID 的十六进制部分（sweep 按这个前缀找）。
pub const NETNS_PREFIX: &str = "arfc-";

pub fn netns_name(env_id: &str) -> String {
    format!("{NETNS_PREFIX}{}", &env_id[ENV_ID_PREFIX.len()..])
}

pub fn env_id_from_netns(name: &str) -> Option<String> {
    let id = format!("{ENV_ID_PREFIX}{}", name.strip_prefix(NETNS_PREFIX)?);
    valid_env_id(&id).then_some(id)
}

pub fn cpu_max(cpu_millis: u64) -> String {
    format!("{} {CPU_PERIOD_US}", cpu_millis * CPU_PERIOD_US / 1000)
}

pub fn memory_max_bytes(memory_mib: u64, overhead_mib: u64) -> u64 {
    (memory_mib + overhead_mib) << 20
}

pub fn boot_args(guest_init: &str) -> String {
    format!(
        "console=ttyS0 reboot=k panic=1 pci=off ro root=/dev/vda rootfstype=ext4 init={guest_init} \
         AGENT_LISTEN=vsock:{AGENT_PORT} AGENT_TOKEN_VSOCK={TOKEN_PORT} -- --init"
    )
}

pub struct JailerArgs<'a> {
    pub env_id: &'a str,
    pub firecracker_bin: &'a Path,
    pub uid: u32,
    pub chroot_base: &'a Path,
    pub netns: &'a Path,
    pub parent_cgroup: &'a str,
    pub spec: &'a VmSpec,
    pub overhead_mib: u64,
}

/// jailer 的参数（firecracker v1.17 的 jailer）。gid 与 uid 相同；`--` 之后是给 firecracker 的：只给 API socket 与日志级别，
/// seccomp 用 firecracker 自带的默认过滤器（不传 --no-seccomp、--seccomp-filter）。
pub fn jailer_args(a: &JailerArgs<'_>) -> Vec<String> {
    let s = |v: &str| v.to_string();
    let p = |v: &Path| v.to_string_lossy().into_owned();
    vec![
        s("--id"),
        s(a.env_id),
        s("--exec-file"),
        p(a.firecracker_bin),
        s("--uid"),
        a.uid.to_string(),
        s("--gid"),
        a.uid.to_string(),
        s("--chroot-base-dir"),
        p(a.chroot_base),
        s("--netns"),
        p(a.netns),
        s("--new-pid-ns"),
        s("--cgroup-version"),
        s("2"),
        s("--parent-cgroup"),
        s(a.parent_cgroup),
        s("--cgroup"),
        format!("cpu.max={}", cpu_max(a.spec.cpu_millis)),
        s("--cgroup"),
        format!(
            "memory.max={}",
            memory_max_bytes(a.spec.memory_mib, a.overhead_mib)
        ),
        s("--cgroup"),
        format!("pids.max={PIDS_MAX}"),
        s("--"),
        s("--api-sock"),
        s(API_SOCK),
        s("--level"),
        s("Warning"),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    fn limits() -> Limits {
        Limits {
            vm_max_vcpus: 8,
            vm_max_memory_mib: 16384,
            vm_default_disk_mib: 4096,
            vm_max_disk_mib: 51200,
        }
    }

    fn req(cpu: u64, mem: u64) -> CreateRequest {
        CreateRequest {
            runtime: "python".into(),
            cpu_millis: cpu,
            memory_mb: mem,
            disk_mb: None,
            labels: BTreeMap::new(),
        }
    }

    #[test]
    fn rounds_vcpus_up_and_fills_the_default_disk() {
        let (s, _) = validate(req(1500, 512), &limits()).unwrap();
        assert_eq!((s.vcpus, s.memory_mib, s.disk_mib), (2, 512, 4096));
        assert_eq!(validate(req(1000, 512), &limits()).unwrap().0.vcpus, 1);
        assert_eq!(validate(req(250, 512), &limits()).unwrap().0.vcpus, 1);
    }

    #[test]
    fn rejects_out_of_range_specs() {
        for (cpu, mem) in [(0, 512), (99, 512), (8001, 512), (1000, 127), (1000, 16385)] {
            assert!(validate(req(cpu, mem), &limits()).is_err(), "{cpu} {mem}");
        }
        let mut r = req(1000, 512);
        r.disk_mb = Some(51201);
        assert!(validate(r, &limits()).is_err());
        let mut r = req(1000, 512);
        r.labels = (0..17).map(|i| (format!("k{i}"), "v".into())).collect();
        assert!(validate(r, &limits()).is_err());
    }

    #[test]
    fn names_cannot_escape_their_directories() {
        for bad in ["", "../x", "a/b", "a.b", "A", "-a", &"a".repeat(64)] {
            assert!(!valid_runtime(bad), "{bad:?}");
        }
        assert!(valid_runtime("python") && valid_runtime("sandbox_go-1"));
        for bad in [
            "fc-0123",
            "fc-0123456789ABCDEF",
            "xx-0123456789abcdef",
            "fc-0123456789abcdeg",
            "fc-../../etc/passw",
        ] {
            assert!(!valid_env_id(bad), "{bad}");
        }
        assert!(valid_env_id("fc-0123456789abcdef"));
        assert_eq!(netns_name("fc-0123456789abcdef"), "arfc-0123456789abcdef");
        assert_eq!(
            env_id_from_netns("arfc-0123456789abcdef").as_deref(),
            Some("fc-0123456789abcdef")
        );
        assert_eq!(env_id_from_netns("arfc-../x"), None);
        assert!(valid_owner("_platform") && valid_owner("t1") && valid_owner("acme-co_2"));
        for bad in ["", "a b", "T1", "_other", "-a", "a/b"] {
            assert!(!valid_owner(bad), "{bad:?}");
        }
    }

    #[test]
    fn cgroup_values_follow_the_spec() {
        assert_eq!(cpu_max(1500), "150000 100000");
        assert_eq!(cpu_max(250), "25000 100000");
        assert_eq!(memory_max_bytes(512, 64), 576 << 20);
    }

    #[test]
    fn boot_args_carry_no_secret_and_start_the_agent_as_init() {
        let args = boot_args("/usr/local/bin/sandbox-agent");
        let tokens: Vec<&str> = args.split_whitespace().collect();
        for want in [
            "console=ttyS0",
            "panic=1",
            "ro",
            "init=/usr/local/bin/sandbox-agent",
            "AGENT_LISTEN=vsock:700",
            "--",
            "--init",
        ] {
            assert!(tokens.contains(&want), "{args}");
        }
        assert!(!args.to_ascii_lowercase().contains("token="), "{args}");
    }

    #[test]
    fn jailer_args_pin_isolation_flags() {
        let spec = VmSpec {
            runtime: "python".into(),
            cpu_millis: 2000,
            vcpus: 2,
            memory_mib: 1024,
            disk_mib: 4096,
        };
        let a = jailer_args(&JailerArgs {
            env_id: "fc-0123456789abcdef",
            firecracker_bin: Path::new("/usr/local/bin/firecracker"),
            uid: 900001,
            chroot_base: Path::new("/var/lib/agentruntime/fc/jail"),
            netns: Path::new("/run/netns/arfc-0123456789abcdef"),
            parent_cgroup: "agentruntime-fc",
            spec: &spec,
            overhead_mib: 64,
        });
        let joined = a.join(" ");
        for want in [
            "--uid 900001 --gid 900001",
            "--new-pid-ns",
            "--cgroup-version 2",
            "--netns /run/netns/arfc-0123456789abcdef",
            "--cgroup cpu.max=200000 100000",
            "--cgroup pids.max=64",
            "-- --api-sock /run/firecracker.socket",
        ] {
            assert!(joined.contains(want), "{joined}");
        }
        assert!(
            !joined.contains("seccomp") && !joined.contains("--daemonize"),
            "{joined}"
        );
    }
}
