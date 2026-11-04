//! Unix（Linux 为主）上的系统调用。

use std::ffi::CString;
use std::io;
use std::os::fd::RawFd;
use std::os::unix::ffi::OsStrExt;
use std::path::Path;
use std::time::Duration;

use crate::Exit;

pub const SIGKILL: i32 = libc::SIGKILL;
pub const SIGTERM: i32 = libc::SIGTERM;

fn cstr(path: &Path) -> io::Result<CString> {
    CString::new(path.as_os_str().as_bytes())
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "路径里有 NUL"))
}

fn check(rc: libc::c_int) -> io::Result<()> {
    if rc == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

/// 向一个进程发信号。pid 小于等于 1 一律拒绝：0、-1 与 1 都有特殊含义（本进程组、所有进程、init）。
pub fn kill(pid: i32, sig: i32) -> io::Result<()> {
    if pid <= 1 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "拒绝向 pid <= 1 发信号",
        ));
    }
    // SAFETY: kill(2) 只读两个整数参数，不涉及内存。
    check(unsafe { libc::kill(pid, sig) })
}

/// 向整个进程组发信号（kill(-pgid, sig)）。pgid 小于等于 1 一律拒绝（同上）。
pub fn kill_group(pgid: i32, sig: i32) -> io::Result<()> {
    if pgid <= 1 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "拒绝向进程组 <= 1 发信号",
        ));
    }
    // SAFETY: 同上；负的 pid 表示进程组。
    check(unsafe { libc::kill(-pgid, sig) })
}

pub fn geteuid() -> u32 {
    // SAFETY: geteuid(2) 没有参数，总是成功。
    unsafe { libc::geteuid() }
}

/// 等一个子进程结束并回收它（wait4），返回结束状态与 rusage 里的 CPU 时间。EINTR 时重试。
pub fn wait4(pid: i32) -> io::Result<Exit> {
    if pid <= 0 {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "pid 要是正数"));
    }
    loop {
        let mut status: libc::c_int = 0;
        // SAFETY: rusage 按 libc 的布局清零，是本函数的栈变量；status 同样；两个指针在调用期间都有效。
        let mut ru: libc::rusage = unsafe { std::mem::zeroed() };
        let rc = unsafe { libc::wait4(pid, &mut status, 0, &mut ru) };
        if rc < 0 {
            let err = io::Error::last_os_error();
            if err.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(err);
        }
        #[allow(clippy::unnecessary_cast)] // time_t、suseconds_t 在 32 位平台上不是 i64
        let us = |tv: libc::timeval| {
            (tv.tv_sec as i64).max(0) as u64 * 1_000_000 + (tv.tv_usec as i64).max(0) as u64
        };
        let cpu = Duration::from_micros(us(ru.ru_utime) + us(ru.ru_stime));
        let (code, signal) = if libc::WIFEXITED(status) {
            (Some(libc::WEXITSTATUS(status)), None)
        } else if libc::WIFSIGNALED(status) {
            (None, Some(libc::WTERMSIG(status)))
        } else {
            (None, None)
        };
        return Ok(Exit { code, signal, cpu });
    }
}

/// poll(2) 等一组文件描述符可读（或对端关闭、出错）：返回每个描述符是否“有事”（可读、HUP、ERR）。超时时全是 false。
/// EINTR 时当作超时返回。
pub fn poll_readable(fds: &[RawFd], timeout: Duration) -> io::Result<Vec<bool>> {
    let mut pfds: Vec<libc::pollfd> = fds
        .iter()
        .map(|&fd| libc::pollfd {
            fd,
            events: libc::POLLIN,
            revents: 0,
        })
        .collect();
    let ms = timeout.as_millis().min(i32::MAX as u128) as libc::c_int;
    // SAFETY: pfds 是长度为 fds.len() 的有效数组，调用期间不移动。
    let rc = unsafe { libc::poll(pfds.as_mut_ptr(), pfds.len() as libc::nfds_t, ms) };
    if rc < 0 {
        let err = io::Error::last_os_error();
        if err.kind() == io::ErrorKind::Interrupted {
            return Ok(vec![false; fds.len()]);
        }
        return Err(err);
    }
    Ok(pfds
        .iter()
        .map(|p| p.revents & (libc::POLLIN | libc::POLLHUP | libc::POLLERR | libc::POLLNVAL) != 0)
        .collect())
}

/// 按有效 uid、gid 判断能不能执行 path（faccessat 加 AT_EACCESS，与 Go 的 exec.LookPath 一致）。
/// 内核不支持 AT_EACCESS（ENOSYS、EPERM，或老的 libc 报 EINVAL）时返回 None，由调用方按权限位判断。
pub fn eaccess_exec(path: &Path) -> Option<bool> {
    let c = cstr(path).ok()?;
    // SAFETY: c 是以 NUL 结尾的有效 C 字符串，调用期间一直有效。
    let rc = unsafe { libc::faccessat(libc::AT_FDCWD, c.as_ptr(), libc::X_OK, libc::AT_EACCESS) };
    if rc == 0 {
        return Some(true);
    }
    match io::Error::last_os_error().raw_os_error() {
        Some(libc::ENOSYS) | Some(libc::EPERM) | Some(libc::EINVAL) => None,
        _ => Some(false),
    }
}

/// cgroup2 文件系统的 magic（linux/magic.h 的 CGROUP2_SUPER_MAGIC）。
#[cfg(target_os = "linux")]
const CGROUP2_SUPER_MAGIC: u64 = 0x6367_7270;

/// path 所在的文件系统是不是 cgroup v2（fc-manager 用）。
#[cfg(target_os = "linux")]
pub fn is_cgroup2(path: &Path) -> io::Result<bool> {
    let c = cstr(path)?;
    // SAFETY: st 是本函数里的栈变量，按 libc::statfs 的布局清零；c 是以 NUL 结尾的有效 C 字符串，调用期间一直有效。
    let mut st: libc::statfs = unsafe { std::mem::zeroed() };
    check(unsafe { libc::statfs(c.as_ptr(), &mut st) })?;
    #[allow(clippy::unnecessary_cast)]
    Ok(st.f_type as u64 == CGROUP2_SUPER_MAGIC)
}

#[cfg(test)]
#[allow(clippy::zombie_processes)] // 由 wait4 回收，不经 Child::wait
mod tests {
    use super::*;
    use std::os::fd::AsRawFd;
    use std::process::Command;

    #[test]
    fn refuses_special_pids() {
        for pid in [-1, 0, 1] {
            assert!(kill(pid, 0).is_err());
            assert!(kill_group(pid, 0).is_err());
        }
        assert!(wait4(0).is_err());
    }

    #[test]
    fn wait4_reports_exit_codes_and_signals() {
        let child = Command::new("/bin/sh")
            .args(["-c", "exit 7"])
            .spawn()
            .unwrap();
        let e = wait4(child.id() as i32).unwrap();
        assert_eq!((e.code, e.signal, e.returncode()), (Some(7), None, 7));
        let child = Command::new("/bin/sh")
            .args(["-c", "kill -TERM $$; sleep 5"])
            .spawn()
            .unwrap();
        let e = wait4(child.id() as i32).unwrap();
        assert_eq!(e.returncode(), -SIGTERM);
    }

    #[test]
    fn kill_group_reaches_the_children() {
        use std::io::BufRead;
        use std::os::unix::process::CommandExt;
        let mut child = Command::new("/bin/sh")
            .args(["-c", "sleep 30 & echo $!; sleep 30; wait"])
            .stdout(std::process::Stdio::piped())
            .process_group(0)
            .spawn()
            .unwrap();
        let pid = child.id() as i32;
        let mut line = String::new();
        std::io::BufReader::new(child.stdout.take().unwrap())
            .read_line(&mut line)
            .unwrap();
        let grandchild: i32 = line.trim().parse().unwrap();
        kill_group(pid, SIGKILL).unwrap();
        assert_eq!(wait4(pid).unwrap().returncode(), -SIGKILL);
        // 孙进程也收到了：不在了，或者是等着被回收的僵尸（父进程已死，由 init 回收）。
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        loop {
            let state = std::fs::read_to_string(format!("/proc/{grandchild}/stat"))
                .ok()
                .and_then(|s| {
                    s.rsplit_once(')')
                        .and_then(|(_, r)| r.split_whitespace().next().map(str::to_string))
                });
            match state.as_deref() {
                None | Some("Z") | Some("X") => break,
                _ => assert!(
                    std::time::Instant::now() < deadline,
                    "孙进程还在跑：{state:?}"
                ),
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    #[test]
    fn poll_sees_data_and_eof() {
        let (a, b) = std::os::unix::net::UnixStream::pair().unwrap();
        assert_eq!(
            poll_readable(&[a.as_raw_fd()], Duration::from_millis(10)).unwrap(),
            vec![false]
        );
        drop(b);
        assert_eq!(
            poll_readable(&[a.as_raw_fd()], Duration::from_millis(10)).unwrap(),
            vec![true]
        );
    }

    #[test]
    fn eaccess() {
        assert_eq!(eaccess_exec(Path::new("/bin/sh")), Some(true));
        assert_eq!(eaccess_exec(Path::new("/nonexistent/x")), Some(false));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn dumpable_and_cgroup() {
        assert!(!is_cgroup2(Path::new("/proc")).unwrap());
        assert!(crate::is_dumpable().unwrap());
    }
}
