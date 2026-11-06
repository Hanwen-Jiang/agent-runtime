//! 配置：全部来自环境变量（systemd 的 EnvironmentFile，样例在 deploy/cluster/fc-manager.env.example）。写错一律启动失败，
//! 错误里写明是哪个变量。文件系统上的核对（内核在不在、是不是 cgroup v2、是不是 root）在 app::preflight。

use std::path::PathBuf;
use std::time::Duration;

use crate::spec::{self, Limits, TOKEN_PORT};

/// fault 注入点（只给测试：tests/acceptance.rs 的 crash_in_the_middle_of_create_is_swept_on_restart）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Fault {
    AfterRecord,
    AfterSpawn,
    AfterStart,
}

#[derive(Debug, Clone)]
pub struct Config {
    pub listen: String,
    pub secrets_file: PathBuf,
    pub state_dir: PathBuf,
    pub chroot_base: PathBuf,
    pub jailer_bin: PathBuf,
    pub firecracker_bin: PathBuf,
    pub ip_bin: PathBuf,
    pub mkfs_bin: Option<PathBuf>,
    pub cgroup_root: PathBuf,
    pub parent_cgroup: String,
    pub netns_dir: PathBuf,
    pub kernel: PathBuf,
    pub images_dir: PathBuf,
    pub guest_init: String,
    /// 只给测试：不是 root 时也能起（不 chown，不要求 cgroup v2）。
    pub allow_non_root: bool,
    pub uid_base: u32,
    pub max_vms: u32,
    pub max_vcpus: u64,
    pub max_memory_mib: u64,
    pub max_scratch_mib: u64,
    pub limits: Limits,
    pub vmm_overhead_mib: u64,
    pub boot_timeout: Duration,
    pub reap_interval: Duration,
    pub max_connections: usize,
    pub max_agent_connections: usize,
    /// 客户机出网的两个目标（D-22，net.rs）；没配的不开。
    pub egress_target: Option<std::net::SocketAddr>,
    pub mirror_target: Option<std::net::SocketAddr>,
    pub max_forward_conns: usize,
    /// 预热池（D-26 的待命 VM）：每项（runtime、cpu_millis、memory_mb、台数）。
    pub warm_pool: Vec<WarmSpec>,
    /// Base-only memory snapshots. Zero disables creation and restore.
    pub snapshot_cache_mib: u64,
    pub fault: Option<Fault>,
}

/// 预热池的一项：按这个规格常备几台只有基础镜像、不属于任何租户的 VM。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WarmSpec {
    pub runtime: String,
    pub cpu_millis: u64,
    pub memory_mb: u64,
    pub count: u32,
}

/// `AR_FC_WARM_POOL=python:1000:512:2,universal:2000:4096:1`（runtime:毫核:MiB:台数，逗号分隔）。
fn parse_warm_pool(v: &str) -> Result<Vec<WarmSpec>, String> {
    let bad = || "AR_FC_WARM_POOL 要写成 runtime:毫核:MiB:台数，逗号分隔".to_string();
    let mut out = vec![];
    for item in v.split(',').map(str::trim).filter(|s| !s.is_empty()) {
        let parts: Vec<&str> = item.split(':').collect();
        let [runtime, cpu, mem, count] = parts.as_slice() else {
            return Err(bad());
        };
        if !spec::valid_runtime(runtime) {
            return Err(bad());
        }
        out.push(WarmSpec {
            runtime: runtime.to_string(),
            cpu_millis: cpu.parse().map_err(|_| bad())?,
            memory_mb: mem.parse().map_err(|_| bad())?,
            count: count.parse().map_err(|_| bad())?,
        });
    }
    Ok(out)
}

/// Unix socket 路径的上限（sun_path 108 字节，含结尾的 0）。
const MAX_SOCKET_PATH: usize = 107;

impl Config {
    pub fn from_env(get: impl Fn(&str) -> Option<String>) -> Result<Config, String> {
        let text = |k: &str, default: &str| get(k).unwrap_or_else(|| default.to_string());
        let path = |k: &str, default: &str| -> Result<PathBuf, String> {
            let v = text(k, default);
            // 宿主上的路径（Linux）；不用 Path::is_absolute，别的平台上跑单元测试时它的判断不一样。
            if !v.starts_with('/') {
                return Err(format!("{k} 要是绝对路径"));
            }
            Ok(PathBuf::from(v))
        };
        let num = |k: &str, default: u64| -> Result<u64, String> {
            match get(k) {
                None => Ok(default),
                Some(v) => v
                    .trim()
                    .parse::<u64>()
                    .ok()
                    .filter(|n| *n > 0)
                    .ok_or_else(|| format!("{k} 要是正整数")),
            }
        };
        let listen = text("AR_FC_LISTEN", "127.0.0.1:8097");
        if listen.parse::<std::net::SocketAddr>().is_err() {
            return Err("AR_FC_LISTEN 要是 IP:端口".into());
        }
        let secrets_file = path(
            "AR_SECRETS_FILE",
            "/etc/agentruntime/fc-manager-secrets.json",
        )?;
        let parent_cgroup = text("AR_FC_PARENT_CGROUP", "agentruntime-fc");
        if parent_cgroup.is_empty()
            || parent_cgroup.starts_with('/')
            || parent_cgroup
                .split('/')
                .any(|seg| seg.is_empty() || seg == "." || seg == "..")
            || !parent_cgroup
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._-/".contains(&b))
        {
            return Err(
                "AR_FC_PARENT_CGROUP 要是 cgroup 根下的相对路径（字母、数字与 ._-/）".into(),
            );
        }
        let guest_init = text("AR_FC_GUEST_INIT", "/usr/local/bin/sandbox-agent");
        if !guest_init.starts_with('/')
            || !guest_init
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._-/".contains(&b))
        {
            return Err("AR_FC_GUEST_INIT 要是客户机里的绝对路径（字母、数字与 ._-/）".into());
        }
        let mkfs = text("AR_FC_MKFS_BIN", "/usr/sbin/mkfs.ext4");
        // 空的：scratch 不格式化（测试；或者由客户机自己格式化）。
        let mkfs_bin = if mkfs.is_empty() {
            None
        } else {
            Some(path("AR_FC_MKFS_BIN", &mkfs)?)
        };
        let fault = match get("AR_FC_FAULT_INJECT").as_deref() {
            None | Some("") => None,
            Some("after_record") => Some(Fault::AfterRecord),
            Some("after_spawn") => Some(Fault::AfterSpawn),
            Some("after_start") => Some(Fault::AfterStart),
            Some(_) => {
                return Err(
                    "AR_FC_FAULT_INJECT 只能是 after_record、after_spawn、after_start".into(),
                );
            }
        };
        let target = |k: &str| -> Result<Option<std::net::SocketAddr>, String> {
            match get(k).filter(|v| !v.is_empty()) {
                None => Ok(None),
                Some(v) => v
                    .parse()
                    .map(Some)
                    .map_err(|_| format!("{k} 要是 IP:端口（宿主上解析不了集群里的域名）")),
            }
        };
        let warm_pool = parse_warm_pool(&text("AR_FC_WARM_POOL", ""))?;
        let egress_target = target("AR_FC_EGRESS_TARGET")?;
        let mirror_target = target("AR_FC_MIRROR_TARGET")?;
        let cfg = Config {
            listen,
            secrets_file,
            state_dir: path("AR_FC_STATE_DIR", "/var/lib/agentruntime/fc/state")?,
            chroot_base: path("AR_FC_CHROOT_BASE", "/var/lib/agentruntime/fc/jail")?,
            jailer_bin: path("AR_FC_JAILER_BIN", "/usr/local/bin/jailer")?,
            firecracker_bin: path("AR_FC_FIRECRACKER_BIN", "/usr/local/bin/firecracker")?,
            ip_bin: path("AR_FC_IP_BIN", "/usr/sbin/ip")?,
            mkfs_bin,
            cgroup_root: path("AR_FC_CGROUP_ROOT", "/sys/fs/cgroup")?,
            parent_cgroup,
            netns_dir: path("AR_FC_NETNS_DIR", "/run/netns")?,
            kernel: path("AR_FC_KERNEL", "/var/lib/agentruntime/fc/images/vmlinux")?,
            images_dir: path("AR_FC_IMAGES_DIR", "/var/lib/agentruntime/fc/images")?,
            guest_init,
            allow_non_root: get("AR_FC_ALLOW_NON_ROOT").as_deref() == Some("1"),
            uid_base: u32::try_from(num("AR_FC_UID_BASE", 900_000)?)
                .map_err(|_| "AR_FC_UID_BASE 太大".to_string())?,
            max_vms: u32::try_from(num("AR_FC_MAX_VMS", 16)?)
                .map_err(|_| "AR_FC_MAX_VMS 太大".to_string())?,
            max_vcpus: num("AR_FC_MAX_VCPUS", 32)?,
            max_memory_mib: num("AR_FC_MAX_MEMORY_MIB", 65_536)?,
            max_scratch_mib: num("AR_FC_MAX_SCRATCH_MIB", 262_144)?,
            limits: Limits {
                vm_max_vcpus: u32::try_from(num("AR_FC_VM_MAX_VCPUS", 8)?)
                    .map_err(|_| "AR_FC_VM_MAX_VCPUS 太大".to_string())?,
                vm_max_memory_mib: num("AR_FC_VM_MAX_MEMORY_MIB", 16_384)?,
                vm_default_disk_mib: num("AR_FC_VM_DEFAULT_DISK_MIB", 4_096)?,
                vm_max_disk_mib: num("AR_FC_VM_MAX_DISK_MIB", 51_200)?,
            },
            vmm_overhead_mib: num("AR_FC_VMM_OVERHEAD_MIB", 64)?,
            boot_timeout: Duration::from_secs(num("AR_FC_BOOT_TIMEOUT_S", 30)?),
            reap_interval: Duration::from_millis(num("AR_FC_REAP_INTERVAL_MS", 2_000)?),
            max_connections: num("AR_FC_MAX_CONNECTIONS", 512)? as usize,
            max_agent_connections: num("AR_FC_MAX_AGENT_CONNECTIONS", 64)? as usize,
            egress_target,
            mirror_target,
            max_forward_conns: num("AR_FC_MAX_FORWARD_CONNS", 128)? as usize,
            warm_pool,
            snapshot_cache_mib: text("AR_FC_SNAPSHOT_CACHE_MIB", "0").parse().map_err(|_| {
                "AR_FC_SNAPSHOT_CACHE_MIB must be a non-negative integer".to_string()
            })?,
            fault,
        };
        if u64::from(cfg.uid_base) + u64::from(cfg.max_vms) > u64::from(u32::MAX - 1) {
            return Err("AR_FC_UID_BASE 加 AR_FC_MAX_VMS 超出 uid 的范围".into());
        }
        if cfg.limits.vm_default_disk_mib > cfg.limits.vm_max_disk_mib
            || cfg.limits.vm_default_disk_mib < spec::MIN_DISK_MIB
        {
            return Err("AR_FC_VM_DEFAULT_DISK_MIB 要在最小盘与 AR_FC_VM_MAX_DISK_MIB 之间".into());
        }
        let longest = cfg.longest_socket_path();
        if longest > MAX_SOCKET_PATH {
            return Err(format!(
                "AR_FC_CHROOT_BASE 太长：jail 里的 socket 路径会有 {longest} 字节，超过 {MAX_SOCKET_PATH}"
            ));
        }
        Ok(cfg)
    }

    /// jailer 用 exec 文件的名字作 jail 的上一级目录（<chroot_base>/<名字>/<id>/root）。
    pub fn exec_name(&self) -> String {
        self.firecracker_bin
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_else(|| "firecracker".into())
    }

    fn longest_socket_path(&self) -> usize {
        // <chroot_base>/<exec>/fc-<16 位>/root/run/v.sock_<令牌端口>
        let tail = format!(
            "/{}/{}/root{}_{TOKEN_PORT}",
            self.exec_name(),
            "f".repeat(19),
            spec::VSOCK_UDS
        );
        self.chroot_base.as_os_str().len() + tail.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn cfg(pairs: &[(&str, &str)]) -> Result<Config, String> {
        let m: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        Config::from_env(|k| m.get(k).cloned())
    }

    #[test]
    fn defaults_are_production_paths() {
        let c = cfg(&[]).unwrap();
        assert_eq!(c.listen, "127.0.0.1:8097");
        assert_eq!(c.exec_name(), "firecracker");
        assert_eq!(c.cgroup_root, PathBuf::from("/sys/fs/cgroup"));
        assert!(!c.allow_non_root && c.fault.is_none());
        assert!(c.longest_socket_path() <= MAX_SOCKET_PATH);
    }

    #[test]
    fn every_error_names_its_variable() {
        for (k, v) in [
            ("AR_FC_LISTEN", "nope"),
            ("AR_FC_MAX_VMS", "zero"),
            ("AR_FC_MAX_VMS", "0"),
            ("AR_FC_STATE_DIR", "relative/dir"),
            ("AR_FC_PARENT_CGROUP", "../escape"),
            ("AR_FC_PARENT_CGROUP", "/abs"),
            ("AR_FC_GUEST_INIT", "sandbox-agent"),
            ("AR_FC_FAULT_INJECT", "whenever"),
            ("AR_FC_EGRESS_TARGET", "egress-gateway.svc:8095"),
            ("AR_FC_WARM_POOL", "python:1000"),
            ("AR_FC_CHROOT_BASE", &format!("/{}", "x".repeat(80))),
        ] {
            let e = cfg(&[(k, v)]).unwrap_err();
            assert!(e.contains(k), "{k}={v}: {e}");
        }
    }

    #[test]
    fn empty_mkfs_means_raw_scratch() {
        assert!(cfg(&[("AR_FC_MKFS_BIN", "")]).unwrap().mkfs_bin.is_none());
        assert!(cfg(&[]).unwrap().mkfs_bin.is_some());
    }
}
