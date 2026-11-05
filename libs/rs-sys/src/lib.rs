//! 系统调用的薄封装（docs/plan/firecracker-backend.md 第 10 节）：Rust 服务里**唯一**允许 `unsafe` 的地方，单独评审。
//! 别的 crate 一律 `unsafe_code = "deny"`（根 Cargo.toml 的 workspace.lints）。
//!
//! 每个函数只做一件事，参数都是整数或以 NUL 结尾的路径，不持有借来的内存；出错时返回 `io::Error`（errno）。
//! 非 Unix 平台（Windows，只给本机开发测试）上只有能退化的几个，见各函数的说明。
#![allow(unsafe_code)]

use std::io;
use std::time::Duration;

#[cfg(unix)]
mod unix;
#[cfg(unix)]
pub use unix::*;

/// Firecracker 客户机里的 PID 1 与 vsock（D-20）。
#[cfg(target_os = "linux")]
pub mod guest;

/// 子进程的结束状态与它用掉的 CPU 时间（wait4 的 rusage：用户态加内核态，含它等待过的子进程）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Exit {
    /// 正常退出时的退出码。
    pub code: Option<i32>,
    /// 被信号杀掉时的信号编号。
    pub signal: Option<i32>,
    /// 用户态加内核态的 CPU 时间（微秒精度）。
    pub cpu: Duration,
}

impl Exit {
    /// 与 Python 的 returncode、Go 版 agent 的 exitCode 一致：被信号杀掉时是负的信号编号；都没有时是 -1。
    pub fn returncode(&self) -> i32 {
        match (self.code, self.signal) {
            (_, Some(sig)) => -sig,
            (Some(code), None) => code,
            (None, None) => -1,
        }
    }
}

/// 进程启动时去掉一个环境变量（std::env::remove_var 在 Rust 2024 里是 unsafe：别的线程同时读环境变量时不安全）。
///
/// 只能在 main 的开头、还没有起任何线程时调用；调用方负责这一点（函数里不检查）。
pub fn remove_env_before_threads(key: &str) {
    // SAFETY: 调用约定是进程里只有当前一个线程，没有并发读写环境变量的人。
    unsafe { std::env::remove_var(key) }
}

/// 让当前进程不可转储（PR_SET_DUMPABLE=0，AGENT-TOK-01）：/proc/<pid>/ 下的文件属主变成 root，同一个 uid 的其他进程
/// 读不到它的 environ（启动时的环境块，令牌在里面）与 mem，也 ptrace 不上。exec 之后内核把子进程恢复成可转储。
/// 只在 Linux 上有；别的平台什么都不做（与 Go 版 dumpable_other.go 一致）。
pub fn set_not_dumpable() -> io::Result<()> {
    #[cfg(target_os = "linux")]
    {
        // SAFETY: prctl(PR_SET_DUMPABLE, 0) 只读整数参数，不涉及内存。
        let rc = unsafe {
            libc::prctl(
                libc::PR_SET_DUMPABLE,
                0 as libc::c_ulong,
                0 as libc::c_ulong,
                0 as libc::c_ulong,
                0 as libc::c_ulong,
            )
        };
        if rc != 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(())
}

/// 当前进程是不是可转储的（只在 Linux 上有意义；别的平台返回 true）。
pub fn is_dumpable() -> io::Result<bool> {
    #[cfg(target_os = "linux")]
    {
        // SAFETY: prctl(PR_GET_DUMPABLE) 没有指针参数。
        let rc = unsafe {
            libc::prctl(
                libc::PR_GET_DUMPABLE,
                0 as libc::c_ulong,
                0 as libc::c_ulong,
                0 as libc::c_ulong,
                0 as libc::c_ulong,
            )
        };
        if rc < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(rc != 0)
    }
    #[cfg(not(target_os = "linux"))]
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn returncode_matches_python() {
        let e = |code, signal| Exit {
            code,
            signal,
            cpu: Duration::ZERO,
        };
        assert_eq!(e(Some(3), None).returncode(), 3);
        assert_eq!(e(None, Some(9)).returncode(), -9);
        assert_eq!(e(None, None).returncode(), -1);
    }
}
