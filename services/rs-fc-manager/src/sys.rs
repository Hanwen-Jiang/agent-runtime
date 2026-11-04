//! 系统调用的薄封装：fc-manager 里唯一允许 `unsafe` 的地方（Cargo.toml 的 lints 在别处一律 deny），单独评审。
//! 只有 kill、statfs 与 geteuid 三个；/proc 的读取是普通的文件读，也放在这里，便于集中看“怎么认定一个进程是我们的”。
//! D-28 的 libs/rs-sys 落地后，这些挪过去（docs/plan/firecracker-backend.md 第 10 节）。
//!
//! 宿主这一侧不需要 AF_VSOCK：Firecracker 的 vsock 在宿主上是 Unix socket 加一行 `CONNECT <端口>` 的握手（vsock.rs）。
#![allow(unsafe_code)]

use std::ffi::CString;
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::path::Path;

pub const SIGKILL: i32 = libc::SIGKILL;

/// cgroup2 文件系统的 magic（linux/magic.h 的 CGROUP2_SUPER_MAGIC）。
const CGROUP2_SUPER_MAGIC: u64 = 0x6367_7270;

pub fn kill(pid: i32, sig: i32) -> io::Result<()> {
    if pid <= 1 {
        // 0、-1 与 1 都有特殊含义（进程组、所有进程、init），这里一律不发。
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "拒绝向 pid <= 1 发信号",
        ));
    }
    // SAFETY: kill(2) 只读两个整数参数，不涉及内存。
    let rc = unsafe { libc::kill(pid, sig) };
    if rc == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

pub fn geteuid() -> u32 {
    // SAFETY: geteuid(2) 没有参数，总是成功。
    unsafe { libc::geteuid() }
}

/// path 所在的文件系统是不是 cgroup v2。
pub fn is_cgroup2(path: &Path) -> io::Result<bool> {
    let c = CString::new(path.as_os_str().as_bytes())
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "路径里有 NUL"))?;
    // SAFETY: st 是本函数里的栈变量，按 libc::statfs 的布局清零；c 是以 NUL 结尾的有效 C 字符串，调用期间一直有效。
    let mut st: libc::statfs = unsafe { std::mem::zeroed() };
    let rc = unsafe { libc::statfs(c.as_ptr(), &mut st) };
    if rc != 0 {
        return Err(io::Error::last_os_error());
    }
    #[allow(clippy::unnecessary_cast)]
    Ok(st.f_type as u64 == CGROUP2_SUPER_MAGIC)
}

/// /proc/<pid>/stat 的第 22 项（进程的启动时间，单位是时钟滴答）与状态。进程不在时为 None。
fn proc_stat(pid: i32) -> Option<(char, u64)> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    // comm 在括号里，可能含空格与括号：从最后一个 ')' 之后数。
    let rest = stat.rsplit_once(')')?.1;
    let fields: Vec<&str> = rest.split_whitespace().collect();
    let state = fields.first()?.chars().next()?;
    let start = fields.get(19)?.parse().ok()?;
    Some((state, start))
}

/// 进程活着（不在、僵尸、已死都算不活着）。
pub fn alive(pid: i32) -> bool {
    matches!(proc_stat(pid), Some((s, _)) if s != 'Z' && s != 'X' && s != 'x')
}

pub fn start_time(pid: i32) -> Option<u64> {
    proc_stat(pid).map(|(_, t)| t)
}

/// 这个 pid 还是当初那个进程：活着，且启动时间对得上（防 pid 复用）；不知道启动时间时，要求命令行里有 `--id <env_id>`
/// （jailer 交给 firecracker 的参数）。
pub fn is_ours(pid: i32, start: Option<u64>, env_id: &str) -> bool {
    if pid <= 1 || !alive(pid) {
        return false;
    }
    match start {
        Some(s) => start_time(pid) == Some(s),
        None => {
            let Ok(raw) = std::fs::read(format!("/proc/{pid}/cmdline")) else {
                return false;
            };
            let args: Vec<&[u8]> = raw.split(|b| *b == 0).collect();
            args.windows(2)
                .any(|w| w[0] == b"--id" && w[1] == env_id.as_bytes())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn refuses_special_pids() {
        for pid in [-1, 0, 1] {
            assert!(kill(pid, 0).is_err());
        }
    }

    #[test]
    fn knows_its_own_process() {
        let me = std::process::id() as i32;
        assert!(alive(me));
        let start = start_time(me);
        assert!(start.is_some());
        assert!(is_ours(me, start, "fc-0000000000000000"));
        assert!(!is_ours(me, start.map(|s| s + 1), "fc-0000000000000000"));
        assert!(!is_ours(me, None, "fc-0000000000000000"));
        assert!(!alive(i32::MAX));
    }

    #[test]
    fn proc_is_not_cgroup2() {
        assert!(!is_cgroup2(Path::new("/proc")).unwrap());
    }
}
