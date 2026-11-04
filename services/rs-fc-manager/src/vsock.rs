//! Firecracker 的 vsock 在宿主这一侧（firecracker 文档 vsock.md 的 hybrid vsock）：
//!
//! - 宿主连客户机：连 jail 里的 `<uds_path>`，写一行 `CONNECT <端口>\n`，firecracker 回 `OK <宿主侧端口>\n` 之后就是到客户机
//!   那个端口的字节流；客户机没在那个端口上监听时 firecracker 直接关连接。
//! - 客户机连宿主（CID 2 的某个端口）：firecracker 去连 `<uds_path>_<端口>`，没人监听就不通。fc-manager 只在启动时为
//!   令牌开一个（`_701`），交出令牌后删掉；别的端口一概没有（D-22 的转发以后再加，目标写死在登记里）。
//!
//! 全是普通的 Unix socket，不需要 unsafe。

use std::io::{self, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::Duration;

use crate::http::Conn;

/// 连客户机的 `port`：返回握手之后的流（缓冲里可能已经有客户机发来的字节，所以返回 Conn）。
pub fn connect(uds: &Path, port: u32, timeout: Duration) -> io::Result<Conn<UnixStream>> {
    let mut s = UnixStream::connect(uds)?;
    s.set_read_timeout(Some(timeout))?;
    s.set_write_timeout(Some(timeout))?;
    s.write_all(format!("CONNECT {port}\n").as_bytes())?;
    let mut conn = Conn::new(s);
    let line = conn.read_line(64).map_err(|e| {
        io::Error::new(
            io::ErrorKind::ConnectionRefused,
            format!("vsock 握手失败：{e}"),
        )
    })?;
    if !line.starts_with(b"OK ") {
        return Err(io::Error::new(
            io::ErrorKind::ConnectionRefused,
            "vsock 握手失败",
        ));
    }
    conn.get_mut().set_read_timeout(None)?;
    conn.get_mut().set_write_timeout(None)?;
    Ok(conn)
}

/// 客户机连宿主 `port` 时 firecracker 去连的 socket 路径。
pub fn host_port_path(uds: &Path, port: u32) -> PathBuf {
    let mut s = uds.as_os_str().to_owned();
    s.push(format!("_{port}"));
    PathBuf::from(s)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Read};
    use std::os::unix::net::UnixListener;

    fn sock(name: &str) -> PathBuf {
        let p = std::env::temp_dir().join(format!("fcm-vsock-{}-{name}", std::process::id()));
        let _ = std::fs::remove_file(&p);
        p
    }

    #[test]
    fn handshake_then_stream() {
        let p = sock("ok");
        let l = UnixListener::bind(&p).unwrap();
        let t = std::thread::spawn(move || {
            let (mut s, _) = l.accept().unwrap();
            let mut line = String::new();
            BufReader::new(s.try_clone().unwrap())
                .read_line(&mut line)
                .unwrap();
            assert_eq!(line, "CONNECT 700\n");
            s.write_all(b"OK 1073741824\nhello").unwrap();
        });
        let mut c = connect(&p, 700, Duration::from_secs(2)).unwrap();
        let mut got = String::new();
        c.read_to_string(&mut got).unwrap();
        assert_eq!(got, "hello");
        t.join().unwrap();
        let _ = std::fs::remove_file(p);
    }

    #[test]
    fn refused_when_the_guest_port_is_closed() {
        let p = sock("refused");
        let l = UnixListener::bind(&p).unwrap();
        let t = std::thread::spawn(move || drop(l.accept().unwrap()));
        assert!(connect(&p, 700, Duration::from_secs(2)).is_err());
        t.join().unwrap();
        assert!(connect(&sock("missing"), 700, Duration::from_secs(1)).is_err());
        let _ = std::fs::remove_file(p);
    }

    #[test]
    fn host_port_paths() {
        assert_eq!(
            host_port_path(Path::new("/j/root/run/v.sock"), 701),
            PathBuf::from("/j/root/run/v.sock_701")
        );
    }
}
