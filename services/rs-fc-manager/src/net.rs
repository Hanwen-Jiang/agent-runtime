//! 客户机的出网与预览（D-22）。客户机没有网卡，只有 vsock：
//!
//! - **出网**：客户机里 agent（PID 1）的环回转发器把 127.0.0.1:3128、127.0.0.1:3129 的连接送到宿主（CID 2）的
//!   vsock 1080、1081 端口；firecracker 去连 jail 里的 `<uds>_1080`、`<uds>_1081`，这里在那两个 Unix socket 上听，
//!   把字节转给配置里写死的出网网关与包镜像（`AR_FC_EGRESS_TARGET`、`AR_FC_MIRROR_TARGET`，IP:端口）。不解析客户机给的
//!   任何地址：客户机说什么都只能到这两个目标，凭据与授权照旧由网关与镜像校验（D-16、D-15）。没配目标的端口不开，
//!   客户机连过来 firecracker 直接断开。
//! - **预览**：`POST /v1/envs/<id>/preview {"port": n}` 在 fc-manager 的监听地址上开一个 TCP 端口（每台 VM 每个客户机
//!   端口一个，重复要同一个就回同一个），进来的连接经 vsock 702 端口送进客户机，第一行 `PORT <n>`，agent 转到客户机里的
//!   127.0.0.1:<n>。出网网关的预览入口（令牌、授权、审计都在网关）连这个地址，与 K8s 后端连 Pod IP 一样。
//!
//! 每台 VM 转发的连接数有上限（`AR_FC_MAX_FORWARD_CONNS`）；VM 销毁时停掉这台 VM 的全部监听。这里不限带宽：
//! 出网的用量与上限在网关里按凭据记（D-16）。

use std::collections::BTreeMap;
use std::io::{self, Read, Write};
use std::net::{Shutdown, SocketAddr, TcpListener, TcpStream};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::config::Config;
use crate::spec::{EGRESS_PORT, MIRROR_PORT, PREVIEW_PORT};
use crate::vm::Paths;
use crate::{vsock, warn};

/// 每台 VM 最多开几个预览端口。
pub const MAX_PREVIEWS: usize = 8;

/// 一台 VM 的转发：停止标记、连接计数、开着的预览端口。
pub struct Links {
    stop: AtomicBool,
    conns: AtomicUsize,
    max_conns: usize,
    previews: Mutex<BTreeMap<u16, SocketAddr>>,
}

impl Links {
    pub fn new(max_conns: usize) -> Arc<Links> {
        Arc::new(Links {
            stop: AtomicBool::new(false),
            conns: AtomicUsize::new(0),
            max_conns,
            previews: Mutex::new(BTreeMap::new()),
        })
    }

    pub fn stop(&self) {
        self.stop.store(true, Ordering::SeqCst);
    }

    fn stopped(&self) -> bool {
        self.stop.load(Ordering::SeqCst)
    }

    fn take(self: &Arc<Self>) -> Option<Slot> {
        if self.conns.fetch_add(1, Ordering::SeqCst) >= self.max_conns {
            self.conns.fetch_sub(1, Ordering::SeqCst);
            return None;
        }
        Some(Slot(self.clone()))
    }
}

struct Slot(Arc<Links>);

impl Drop for Slot {
    fn drop(&mut self) {
        self.0.conns.fetch_sub(1, Ordering::SeqCst);
    }
}

trait Stream: Read + Write + Send + Sized + 'static {
    fn dup(&self) -> io::Result<Self>;
    fn close_write(&self);
}

impl Stream for TcpStream {
    fn dup(&self) -> io::Result<Self> {
        self.try_clone()
    }
    fn close_write(&self) {
        let _ = self.shutdown(Shutdown::Write);
    }
}

impl Stream for UnixStream {
    fn dup(&self) -> io::Result<Self> {
        self.try_clone()
    }
    fn close_write(&self) {
        let _ = self.shutdown(Shutdown::Write);
    }
}

/// 双向搬字节，一边读完就半关另一边的写；两个方向都结束才返回。
fn splice<A: Stream, B: Stream>(a: A, b: B, prefix_to_a: &[u8]) {
    let (Ok(a2), Ok(b2)) = (a.dup(), b.dup()) else {
        return;
    };
    let mut a_w = a2;
    if !prefix_to_a.is_empty() && a_w.write_all(prefix_to_a).is_err() {
        return;
    }
    let t = std::thread::Builder::new()
        .stack_size(64 << 10)
        .spawn(move || {
            let (mut r, mut w) = (b2, a_w);
            let _ = io::copy(&mut r, &mut w);
            w.close_write();
        });
    let (mut r, mut w) = (a, b);
    let _ = io::copy(&mut r, &mut w);
    w.close_write();
    if let Ok(t) = t {
        let _ = t.join();
    }
}

fn spawn(links: &Arc<Links>, f: impl FnOnce() + Send + 'static) {
    let Some(slot) = links.take() else {
        return;
    };
    let _ = std::thread::Builder::new()
        .name("fwd".into())
        .stack_size(64 << 10)
        .spawn(move || {
            f();
            drop(slot);
        });
}

/// 在 InstanceStart 之前开好出网的两个端口（只开配了目标的）。socket 属于这台 VM 的 uid、0600。
pub fn open_outbound(
    cfg: &Config,
    p: &Paths,
    uid: u32,
    links: &Arc<Links>,
    chown: impl Fn(&std::path::Path, u32) -> Result<(), String>,
) -> Result<(), String> {
    for (port, target) in [
        (EGRESS_PORT, cfg.egress_target),
        (MIRROR_PORT, cfg.mirror_target),
    ] {
        let Some(target) = target else { continue };
        let path = vsock::host_port_path(&p.vsock(), port);
        let _ = std::fs::remove_file(&path);
        let l = UnixListener::bind(&path).map_err(|e| format!("开 vsock {port} 的转发：{e}"))?;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))
            .map_err(|e| format!("vsock {port} 的权限：{e}"))?;
        chown(&path, uid)?;
        l.set_nonblocking(true)
            .map_err(|e| format!("vsock {port}：{e}"))?;
        let links = links.clone();
        std::thread::Builder::new()
            .name(format!("out{port}"))
            .spawn(move || accept_outbound(l, target, links))
            .map_err(|e| format!("起不了转发线程：{e}"))?;
    }
    Ok(())
}

fn accept_outbound(l: UnixListener, target: SocketAddr, links: Arc<Links>) {
    while !links.stopped() {
        match l.accept() {
            Ok((guest, _)) => {
                let _ = guest.set_nonblocking(false);
                spawn(&links, move || {
                    match TcpStream::connect_timeout(&target, Duration::from_secs(10)) {
                        Ok(up) => {
                            let _ = up.set_nodelay(true);
                            splice(guest, up, b"");
                        }
                        Err(e) => {
                            warn!("连不上转发的目标 {target}：{}", e.kind());
                            let _ = guest.shutdown(Shutdown::Both);
                        }
                    }
                });
            }
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(20));
            }
            Err(_) => std::thread::sleep(Duration::from_millis(100)),
        }
    }
}

/// 开（或取回）这台 VM 客户机端口 `port` 的预览入口，返回宿主上的地址。
pub fn open_preview(
    bind_ip: std::net::IpAddr,
    p: &Paths,
    links: &Arc<Links>,
    port: u16,
) -> Result<SocketAddr, String> {
    let mut previews = links.previews.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(addr) = previews.get(&port) {
        return Ok(*addr);
    }
    if previews.len() >= MAX_PREVIEWS {
        return Err(format!("每个环境最多 {MAX_PREVIEWS} 个预览端口"));
    }
    let l = TcpListener::bind((bind_ip, 0)).map_err(|e| format!("开预览端口：{e}"))?;
    let addr = l.local_addr().map_err(|e| e.to_string())?;
    l.set_nonblocking(true).map_err(|e| e.to_string())?;
    let vsock_path = p.vsock();
    let links2 = links.clone();
    std::thread::Builder::new()
        .name("preview".into())
        .spawn(move || {
            while !links2.stopped() {
                match l.accept() {
                    Ok((client, _)) => {
                        let _ = client.set_nonblocking(false);
                        let path = vsock_path.clone();
                        spawn(&links2, move || preview_conn(client, &path, port));
                    }
                    Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(20));
                    }
                    Err(_) => std::thread::sleep(Duration::from_millis(100)),
                }
            }
        })
        .map_err(|e| format!("起不了预览线程：{e}"))?;
    previews.insert(port, addr);
    Ok(addr)
}

fn preview_conn(client: TcpStream, vsock_path: &std::path::Path, port: u16) {
    let _ = client.set_nodelay(true);
    let Ok(mut conn) = vsock::connect(vsock_path, PREVIEW_PORT, Duration::from_secs(5)) else {
        let _ = client.shutdown(Shutdown::Both);
        return;
    };
    if conn
        .get_mut()
        .write_all(format!("PORT {port}\n").as_bytes())
        .is_err()
    {
        return;
    }
    let _ = conn
        .get_mut()
        .set_read_timeout(Some(Duration::from_secs(5)));
    match conn.read_line(16) {
        Ok(line) if line.starts_with(b"OK") => {}
        _ => {
            // 客户机里那个端口没有服务：给网关一个干净的断开（它回 502）。
            let _ = client.shutdown(Shutdown::Both);
            return;
        }
    }
    let _ = conn.get_mut().set_read_timeout(None);
    let buffered = conn.take_buffered();
    let guest = conn.into_inner();
    // 客户机先发来的字节（握手之后已经读进缓冲的）先交给调用方。
    splice(client, guest, &buffered);
}
