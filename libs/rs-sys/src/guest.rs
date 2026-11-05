//! Firecracker 客户机里要的系统调用（D-20 第二段）：sandbox-agent 以 PID 1（`--init`）起来时挂文件系统、设主机名、
//! 打开 lo、回收孤儿、降权、关机，以及 AF_VSOCK 的监听与连接。只在 Linux 上有。
//!
//! vsock 的流包成 `std::net::TcpStream` 交给调用方：读写、`shutdown`、`try_clone`、`set_read_timeout`（SO_RCVTIMEO）、
//! `peek`（MSG_PEEK）对任何流式 socket 都一样；只有 `peer_addr`、`local_addr`、`set_nodelay` 这类按 IP 解释的方法会报错，
//! agent 的 HTTP 层不依赖它们的结果。std 的 `TcpListener::accept` 解析不了 AF_VSOCK 的地址，所以 accept 在这里做。

use std::ffi::CString;
use std::io;
use std::net::TcpStream;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::path::Path;

use crate::Exit;

fn cstr(s: &str) -> io::Result<CString> {
    CString::new(s).map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "参数里有 NUL"))
}

fn path_cstr(p: &Path) -> io::Result<CString> {
    use std::os::unix::ffi::OsStrExt;
    CString::new(p.as_os_str().as_bytes())
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "路径里有 NUL"))
}

fn check(rc: libc::c_int) -> io::Result<()> {
    if rc == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

/// 宿主的 CID（firecracker 的 vsock 里客户机连宿主用它）。
pub const VMADDR_CID_HOST: u32 = 2;

fn vsock_addr(cid: u32, port: u32) -> libc::sockaddr_vm {
    // SAFETY: sockaddr_vm 是纯整数字段的结构体，全零是合法值。
    let mut a: libc::sockaddr_vm = unsafe { std::mem::zeroed() };
    a.svm_family = libc::AF_VSOCK as libc::sa_family_t;
    a.svm_cid = cid;
    a.svm_port = port;
    a
}

fn vsock_socket() -> io::Result<OwnedFd> {
    // SAFETY: socket(2) 只读整数参数；返回的描述符归 OwnedFd 所有。
    let fd = unsafe { libc::socket(libc::AF_VSOCK, libc::SOCK_STREAM | libc::SOCK_CLOEXEC, 0) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: fd 是刚建的、没有别人持有的描述符。
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

/// 在客户机的 vsock 端口上监听（任何 CID 都收）。端口小于 1024 要 root（CAP_NET_BIND_SERVICE）。
pub fn vsock_listen(port: u32, backlog: i32) -> io::Result<OwnedFd> {
    let fd = vsock_socket()?;
    let addr = vsock_addr(libc::VMADDR_CID_ANY, port);
    // SAFETY: addr 是本函数的栈变量，长度如实给出。
    check(unsafe {
        libc::bind(
            fd.as_raw_fd(),
            &addr as *const libc::sockaddr_vm as *const libc::sockaddr,
            std::mem::size_of::<libc::sockaddr_vm>() as libc::socklen_t,
        )
    })?;
    // SAFETY: listen(2) 只读整数参数。
    check(unsafe { libc::listen(fd.as_raw_fd(), backlog) })?;
    Ok(fd)
}

/// 接一个连接（EINTR 重试）。
pub fn vsock_accept(listener: &OwnedFd) -> io::Result<TcpStream> {
    loop {
        // SAFETY: 不要对端地址（两个指针都是空）；返回的描述符归 TcpStream 所有。
        let fd = unsafe {
            libc::accept4(
                listener.as_raw_fd(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                libc::SOCK_CLOEXEC,
            )
        };
        if fd < 0 {
            let e = io::Error::last_os_error();
            if e.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(e);
        }
        // SAFETY: fd 是 accept4 刚返回的流式 socket，没有别人持有。
        return Ok(unsafe { TcpStream::from_raw_fd(fd) });
    }
}

/// 连 `cid` 的 `port`（客户机连宿主时 cid 是 2）。
pub fn vsock_connect(cid: u32, port: u32) -> io::Result<TcpStream> {
    let fd = vsock_socket()?;
    let addr = vsock_addr(cid, port);
    loop {
        // SAFETY: addr 是本函数的栈变量，长度如实给出。
        let rc = unsafe {
            libc::connect(
                fd.as_raw_fd(),
                &addr as *const libc::sockaddr_vm as *const libc::sockaddr,
                std::mem::size_of::<libc::sockaddr_vm>() as libc::socklen_t,
            )
        };
        if rc == 0 {
            break;
        }
        let e = io::Error::last_os_error();
        if e.kind() != io::ErrorKind::Interrupted {
            return Err(e);
        }
    }
    Ok(TcpStream::from(fd))
}

/// mount(2)。`data` 是文件系统的选项（如 overlay 的 lowerdir=…），没有时传空串。
pub fn mount(source: &str, target: &Path, fstype: &str, flags: u64, data: &str) -> io::Result<()> {
    let (s, t, f, d) = (
        cstr(source)?,
        path_cstr(target)?,
        cstr(fstype)?,
        cstr(data)?,
    );
    // SAFETY: 四个都是本函数持有的以 NUL 结尾的字符串，调用期间有效。
    check(unsafe {
        libc::mount(
            s.as_ptr(),
            t.as_ptr(),
            f.as_ptr(),
            flags,
            d.as_ptr() as *const libc::c_void,
        )
    })
}

pub const MS_RDONLY: u64 = libc::MS_RDONLY;
pub const MS_NOSUID: u64 = libc::MS_NOSUID;
pub const MS_NODEV: u64 = libc::MS_NODEV;
pub const MS_NOEXEC: u64 = libc::MS_NOEXEC;
pub const MS_BIND: u64 = libc::MS_BIND;
pub const MS_REMOUNT: u64 = libc::MS_REMOUNT;

pub fn sethostname(name: &str) -> io::Result<()> {
    // SAFETY: 指针与长度来自同一个 &str，调用期间有效。
    check(unsafe { libc::sethostname(name.as_ptr() as *const libc::c_char, name.len()) })
}

/// 把 lo 设成 up（客户机没有别的网卡；D-22 的环回转发器要 127.0.0.1）。
pub fn loopback_up() -> io::Result<()> {
    // SAFETY: socket(2) 只读整数参数；描述符归 OwnedFd。
    let fd = unsafe { libc::socket(libc::AF_INET, libc::SOCK_DGRAM | libc::SOCK_CLOEXEC, 0) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: 同上。
    let fd = unsafe { OwnedFd::from_raw_fd(fd) };
    // SAFETY: ifreq 全零合法；名字按字节拷进 ifr_name（"lo" 远短于 IFNAMSIZ），两次 ioctl 都只读写这个栈变量。
    unsafe {
        let mut req: libc::ifreq = std::mem::zeroed();
        for (i, b) in b"lo".iter().enumerate() {
            req.ifr_name[i] = *b as libc::c_char;
        }
        if libc::ioctl(fd.as_raw_fd(), libc::SIOCGIFFLAGS as _, &mut req) < 0 {
            return Err(io::Error::last_os_error());
        }
        req.ifr_ifru.ifru_flags |= (libc::IFF_UP | libc::IFF_RUNNING) as libc::c_short;
        if libc::ioctl(fd.as_raw_fd(), libc::SIOCSIFFLAGS as _, &req) < 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(())
}

/// 回收任意一个已结束的子进程（PID 1 回收孤儿用）。阻塞等到有子进程结束；没有子进程时 ECHILD。
pub fn wait_any() -> io::Result<(i32, Exit)> {
    loop {
        let mut status: libc::c_int = 0;
        // SAFETY: status 是本函数的栈变量；不要 rusage（空指针）。
        let pid = unsafe { libc::wait4(-1, &mut status, 0, std::ptr::null_mut()) };
        if pid < 0 {
            let e = io::Error::last_os_error();
            if e.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(e);
        }
        let (code, signal) = if libc::WIFEXITED(status) {
            (Some(libc::WEXITSTATUS(status)), None)
        } else if libc::WIFSIGNALED(status) {
            (None, Some(libc::WTERMSIG(status)))
        } else {
            (None, None)
        };
        return Ok((
            pid,
            Exit {
                code,
                signal,
                cpu: std::time::Duration::ZERO,
            },
        ));
    }
}

/// 降到 uid、gid（附加组清空）。顺序是 setgroups、setgid、setuid：先放掉 uid 就改不了组了。
/// 之后内核把进程重置成可转储（suid_dumpable），调用方要再设一次 set_not_dumpable。
pub fn drop_privileges(uid: u32, gid: u32) -> io::Result<()> {
    // SAFETY: 三个调用都只读整数参数（setgroups 的列表为空，指针不被读）。
    unsafe {
        check(libc::setgroups(0, std::ptr::null()))?;
        check(libc::setgid(gid))?;
        check(libc::setuid(uid))?;
    }
    Ok(())
}

/// 把所有文件系统写回盘，然后关机（firecracker 进程随之退出）。只在 PID 1 里调用。
pub fn power_off() -> io::Error {
    // SAFETY: sync(2) 与 reboot(2) 只读整数参数；reboot 成功时不返回。
    unsafe {
        libc::sync();
        libc::reboot(libc::RB_POWER_OFF);
    }
    io::Error::last_os_error()
}

/// 本进程是不是 PID 1。
pub fn is_pid1() -> bool {
    // SAFETY: getpid(2) 没有参数，总是成功。
    unsafe { libc::getpid() == 1 }
}
