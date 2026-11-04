//! fc-manager 用到的系统调用与 /proc 的读取。unsafe 的部分（kill、statfs、geteuid）在 libs/rs-sys（D-28 的根 workspace 落地后
//! 挪过去，docs/plan/firecracker-backend.md 第 10 节），这里只是转发与“怎么认定一个进程是我们的”。
//!
//! 宿主这一侧不需要 AF_VSOCK：Firecracker 的 vsock 在宿主上是 Unix socket 加一行 `CONNECT <端口>` 的握手（vsock.rs）。

pub use ar_sys::{SIGKILL, geteuid, is_cgroup2, kill};
#[cfg(test)]
use std::path::Path;

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
