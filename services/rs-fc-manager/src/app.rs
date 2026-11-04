//! 子命令：`serve`（清扫上次留下的、起服务）、`sweep`（只清扫；systemd 的 ExecStopPost）、`check`（核对配置与宿主）。

use std::net::TcpListener;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::Path;
use std::sync::Arc;

use crate::config::Config;
use crate::manager::Manager;
use crate::secrets::TokenSource;
use crate::{info, server, sys};

pub fn run(cmd: &str) -> Result<(), String> {
    let cfg = Config::from_env(|k| std::env::var(k).ok())?;
    match cmd {
        "serve" => serve(cfg),
        "sweep" => {
            preflight(&cfg, false)?;
            let mgr = Manager::open(cfg)?;
            let n = mgr.sweep();
            info!("清扫完：{n} 台");
            Ok(())
        }
        "check" => {
            preflight(&cfg, true)?;
            TokenSource::load(&cfg.secrets_file)?;
            info!("配置与宿主核对通过");
            Ok(())
        }
        _ => Err(format!("不认识的子命令 {cmd}")),
    }
}

fn serve(cfg: Config) -> Result<(), String> {
    preflight(&cfg, true)?;
    let tokens = Arc::new(TokenSource::load(&cfg.secrets_file)?);
    let mgr = Arc::new(Manager::open(cfg)?);
    let n = mgr.sweep();
    if n > 0 {
        info!("清扫了上一个 fc-manager 留下的 {n} 台 VM");
    }
    mgr.prepare_cgroup()?;
    let listener = TcpListener::bind(&mgr.cfg.listen)
        .map_err(|e| format!("监听不了 AR_FC_LISTEN（{}）：{e}", mgr.cfg.listen))?;
    let addr = listener.local_addr().map_err(|e| e.to_string())?;
    info!("listening on {addr}");
    let reaper = mgr.clone();
    std::thread::spawn(move || {
        loop {
            std::thread::sleep(reaper.cfg.reap_interval);
            reaper.reap();
        }
    });
    server::serve(mgr, tokens, listener).map_err(|e| e.to_string())
}

fn executable(p: &Path) -> bool {
    p.metadata()
        .map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

/// 起服务前对宿主的核对；full 为假时（sweep）只查清扫要用的。
pub fn preflight(cfg: &Config, full: bool) -> Result<(), String> {
    if sys::geteuid() != 0 && !cfg.allow_non_root {
        return Err("fc-manager 要以 root 运行（jailer 要建 chroot、命名空间与 cgroup；测试时设 AR_FC_ALLOW_NON_ROOT=1）".into());
    }
    if !cfg.allow_non_root && !sys::is_cgroup2(&cfg.cgroup_root).unwrap_or(false) {
        return Err(format!(
            "AR_FC_CGROUP_ROOT（{}）不是 cgroup v2",
            cfg.cgroup_root.display()
        ));
    }
    let mut tools = vec![("AR_FC_IP_BIN", &cfg.ip_bin)];
    if full {
        tools.push(("AR_FC_JAILER_BIN", &cfg.jailer_bin));
        tools.push(("AR_FC_FIRECRACKER_BIN", &cfg.firecracker_bin));
        if let Some(m) = &cfg.mkfs_bin {
            tools.push(("AR_FC_MKFS_BIN", m));
        }
    }
    for (var, p) in tools {
        if !executable(p) {
            return Err(format!("{var}（{}）不存在或不可执行", p.display()));
        }
    }
    if !full {
        return Ok(());
    }
    if !cfg.kernel.is_file() {
        return Err(format!(
            "AR_FC_KERNEL（{}）指向的内核不存在",
            cfg.kernel.display()
        ));
    }
    if !cfg.images_dir.is_dir() {
        return Err(format!(
            "AR_FC_IMAGES_DIR（{}）不是目录",
            cfg.images_dir.display()
        ));
    }
    if !cfg.allow_non_root
        && std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open("/dev/kvm")
            .is_err()
    {
        return Err("/dev/kvm 不存在或读写不了".into());
    }
    std::fs::create_dir_all(&cfg.chroot_base).map_err(|e| {
        format!(
            "建不了 AR_FC_CHROOT_BASE（{}）：{e}",
            cfg.chroot_base.display()
        )
    })?;
    std::fs::create_dir_all(&cfg.netns_dir)
        .map_err(|e| format!("建不了 AR_FC_NETNS_DIR（{}）：{e}", cfg.netns_dir.display()))?;
    let dev = |p: &Path| p.metadata().map(|m| m.dev()).ok();
    let base = dev(&cfg.chroot_base);
    if base.is_none() || base != dev(&cfg.images_dir) || base != dev(&cfg.kernel) {
        return Err("AR_FC_CHROOT_BASE 要与 AR_FC_IMAGES_DIR、AR_FC_KERNEL 在同一个文件系统上（内核与 rootfs 是硬链接进 jail 的）".into());
    }
    Ok(())
}
