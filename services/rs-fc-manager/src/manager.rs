//! VM 表、容量、建与删、VM 死了之后的清理，以及启动时的清扫。
//!
//! **重启时终止，不接管**（D-20 的“二者必居其一”，定为终止）：
//! - 接管要把每台 VM 的 agent 令牌落盘，重启后的 fc-manager 才能接着替调用方转发；终止就什么机密都不用落盘。
//! - 宿主上权限最高的组件代码要小：接管要核对一台无人看管时跑过的 VM 现在的状态，终止只要清扫。
//! - 环境没了，Worker 已有现成的处理：环境表记 lost，下一条命令透明换新（D-13）；存档模式的工作区从上一次的存档恢复。
//!
//! 代价：fc-manager 一重启，所有 VM 里上次存档之后的改动都没了。所以升级 fc-manager 之前先把 firecracker 后端设成
//! `enabled: false` 排空（D-19），而不是直接重启。清扫的入口有三个：`serve` 启动时、systemd 的 ExecStopPost
//! （`fc-manager sweep`，主进程怎么退出都会跑）、手工的 `fc-manager sweep`；同一个 state 目录同时只能有一个
//! fc-manager 进程（锁文件），所以 sweep 不会误杀正在服务的 VM。
//!
//! 建环境的顺序：先占容量与 uid，再写记录，之后才动宿主（jail、netns、jailer）；任何一步失败就拆干净、还回名额。

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard};
use std::time::{Instant, SystemTime};

use serde_json::{Value, json};

use crate::config::{Config, Fault};
use crate::net::{self, Links};
use crate::secrets::Token;
use crate::snapshot;
use crate::spec::{self, CreateRequest, VmSpec};
use crate::store::{Record, Store};
use crate::vm::{self, Paths, VmError};
use crate::{info, timefmt, warn};

#[derive(Debug)]
pub struct ApiError {
    pub status: u16,
    pub code: &'static str,
    pub message: String,
    pub retry_after: Option<u32>,
}

impl ApiError {
    pub fn new(status: u16, code: &'static str, message: impl Into<String>) -> ApiError {
        ApiError {
            status,
            code,
            message: message.into(),
            retry_after: None,
        }
    }

    pub fn transient(code: &'static str, message: impl Into<String>, retry_after: u32) -> ApiError {
        ApiError {
            status: 503,
            code,
            message: message.into(),
            retry_after: Some(retry_after),
        }
    }

    pub fn unknown_env() -> ApiError {
        ApiError::new(404, "unknown_env", "环境不存在")
    }
}

/// 预热池里的 VM 记在这个作用域下（D-26）。它不是合法的 X-Sandbox-Owner（spec::valid_owner 不收），调用方冒充不了，
/// 列表也不给（对账看不到它们，不会当孤儿删）。交给调用方时改成调用方的作用域，之后与新建的一样。
pub const POOL_OWNER: &str = "_pool";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum State {
    Creating,
    Ready,
    Destroying,
}

type RootfsStamp = (u64, u64, u64, i64, i64);

struct Vm {
    rec: Record,
    state: State,
    token: Option<Token>,
    /// 建的过程中被 DELETE 了：建的线程在下一个检查点拆掉它。
    cancelled: bool,
    /// 有线程正在拆它（销毁、或者 reaper 发现它死了）。
    busy: bool,
    conns: usize,
    links: std::sync::Arc<Links>,
    rootfs_stamp: Option<RootfsStamp>,
}

#[derive(Default)]
struct Inner {
    vms: BTreeMap<String, Vm>,
    slots: BTreeSet<u32>,
    vcpus: u64,
    memory: u64,
    disk: u64,
}

pub struct Manager {
    pub cfg: Config,
    store: Store,
    inner: Mutex<Inner>,
    snapshots: Mutex<snapshot::Cache>,
    _lock: fs::File,
}

pub struct Created {
    pub env_id: String,
    pub created_at: String,
    pub image: String,
    pub spec: VmSpec,
}

/// 转发一次 agent 请求要的东西；活着的期间算这台 VM 的一个连接。
pub struct AgentRoute<'a> {
    mgr: &'a Manager,
    pub env_id: String,
    pub vsock: PathBuf,
    pub token: Token,
}

impl Drop for AgentRoute<'_> {
    fn drop(&mut self) {
        if let Some(vm) = self.mgr.lock().vms.get_mut(&self.env_id) {
            vm.conns = vm.conns.saturating_sub(1);
        }
    }
}

fn image_digest(rootfs: &Path) -> String {
    let mut side = rootfs.as_os_str().to_owned();
    side.push(".sha256");
    let text = fs::read_to_string(PathBuf::from(side)).unwrap_or_default();
    let hex = text.split_whitespace().next().unwrap_or("");
    if hex.len() == 64
        && hex
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        format!("sha256:{hex}")
    } else {
        String::new()
    }
}

fn rootfs_stamp(rootfs: &Path) -> Option<RootfsStamp> {
    fs::metadata(rootfs)
        .ok()
        .map(|m| (m.dev(), m.ino(), m.len(), m.mtime(), m.mtime_nsec()))
}

impl Manager {
    /// 打开 state 目录并拿到它的锁：同一个目录同时只有一个 fc-manager（serve 或 sweep）。
    pub fn open(cfg: Config) -> Result<Manager, String> {
        let store = Store::open(&cfg.state_dir)
            .map_err(|e| format!("建不了 AR_FC_STATE_DIR（{}）：{e}", cfg.state_dir.display()))?;
        fs::create_dir_all(cfg.state_dir.join("logs"))
            .map_err(|e| format!("建不了日志目录：{e}"))?;
        let lock = fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(cfg.state_dir.join("fc-manager.lock"))
            .map_err(|e| format!("打不开锁文件：{e}"))?;
        lock.try_lock().map_err(|_| {
            format!(
                "另一个 fc-manager 正在用 AR_FC_STATE_DIR（{}）",
                cfg.state_dir.display()
            )
        })?;
        let snapshots = Mutex::new(snapshot::Cache::new(&cfg)?);
        Ok(Manager {
            cfg,
            store,
            inner: Mutex::new(Inner::default()),
            snapshots,
            _lock: lock,
        })
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// 在父 cgroup（与它的上级）打开 VM 要用的控制器。
    pub fn prepare_cgroup(&self) -> Result<(), String> {
        let parent = self.cfg.cgroup_root.join(&self.cfg.parent_cgroup);
        fs::create_dir_all(&parent)
            .map_err(|e| format!("建不了 VM 的父 cgroup（{}）：{e}", parent.display()))?;
        let mut dir = self.cfg.cgroup_root.clone();
        let mut chain = vec![dir.clone()];
        for seg in self.cfg.parent_cgroup.split('/') {
            dir = dir.join(seg);
            chain.push(dir.clone());
        }
        for d in chain {
            fs::write(d.join("cgroup.subtree_control"), "+cpu +memory +pids +io").map_err(|e| {
                format!(
                    "打不开 {} 的 cpu、memory、pids、io 控制器：{e}",
                    d.display()
                )
            })?;
        }
        Ok(())
    }

    /// 清掉上一个 fc-manager 留下的一切：记录里的、jail 目录、VM 的 cgroup、netns、日志里能认出环境 ID 的。返回清掉了几个。
    pub fn sweep(&self) -> usize {
        let mut found: BTreeMap<String, (Option<i32>, Option<u64>)> = BTreeMap::new();
        for (id, rec) in self.store.load_all() {
            found.insert(
                id,
                rec.map(|r| (r.pid, r.pid_start)).unwrap_or((None, None)),
            );
        }
        for id in self.residue_ids() {
            found.entry(id).or_insert((None, None));
        }
        let mut cleaned = 0;
        for (id, (pid, start)) in found {
            if self.lock().vms.contains_key(&id) {
                continue;
            }
            match vm::teardown(&self.cfg, &id, pid, start) {
                Ok(()) => {
                    if let Err(e) = self.store.remove(&id) {
                        warn!("删不掉 {id} 的记录：{e}");
                    }
                    info!("清扫了 {id}");
                    cleaned += 1;
                }
                Err(e) => warn!("清扫 {id} 没完成：{e}"),
            }
        }
        cleaned
    }

    fn residue_ids(&self) -> BTreeSet<String> {
        let cfg = &self.cfg;
        let mut ids = BTreeSet::new();
        let mut scan = |dir: PathBuf, parse: &dyn Fn(&str) -> Option<String>| {
            if let Ok(rd) = fs::read_dir(&dir) {
                for e in rd.flatten() {
                    if let Some(id) = parse(&e.file_name().to_string_lossy()) {
                        ids.insert(id);
                    }
                }
            }
        };
        let plain = |n: &str| spec::valid_env_id(n).then(|| n.to_string());
        scan(cfg.chroot_base.join(cfg.exec_name()), &plain);
        scan(cfg.cgroup_root.join(&cfg.parent_cgroup), &plain);
        // /run/netns is shared by independent managers. The durable record is
        // written before netns creation; never infer ownership from arfc-* alone.
        scan(cfg.state_dir.join("logs"), &|n: &str| {
            n.strip_suffix(".log").and_then(plain)
        });
        ids
    }

    fn fault(&self, at: Fault) {
        if self.cfg.fault == Some(at) {
            warn!("fault 注入：{at:?}，进程中止");
            std::process::abort();
        }
    }

    fn checkpoint(&self, env_id: &str) -> Result<(), VmError> {
        if self.lock().vms.get(env_id).is_none_or(|v| v.cancelled) {
            Err(VmError::Cancelled)
        } else {
            Ok(())
        }
    }

    pub fn create(&self, owner: &str, req: CreateRequest) -> Result<Created, ApiError> {
        let (spec, labels) = spec::validate(req, &self.cfg.limits)
            .map_err(|m| ApiError::new(400, "invalid_argument", m))?;
        let rootfs = self.cfg.images_dir.join(format!("{}.ext4", spec.runtime));
        if !rootfs.is_file() {
            return Err(ApiError::new(
                400,
                "unsupported_runtime",
                format!("没有 runtime {} 的镜像", spec.runtime),
            ));
        }
        if owner != POOL_OWNER
            && let Some(c) = self.take_warm(owner, &spec, &labels)
        {
            return Ok(c);
        }
        let image = image_digest(&rootfs);
        let env_id = vm::new_env_id().map_err(|e| {
            warn!("取随机数失败：{e}");
            ApiError::new(500, "internal", "fc-manager 内部错误")
        })?;
        let links = Links::new(self.cfg.max_forward_conns);
        let mut rec = {
            let mut g = self.lock();
            let c = &self.cfg;
            let full = if g.vms.len() as u64 >= u64::from(c.max_vms) {
                Some(format!("这台宿主同时最多 {} 台 VM", c.max_vms))
            } else if g.vcpus + u64::from(spec.vcpus) > c.max_vcpus {
                Some(format!("vCPU 只剩 {}", c.max_vcpus - g.vcpus))
            } else if g.memory + spec.memory_mib > c.max_memory_mib {
                Some(format!("内存只剩 {} MiB", c.max_memory_mib - g.memory))
            } else if g.disk + spec.disk_mib > c.max_scratch_mib {
                Some(format!("盘只剩 {} MiB", c.max_scratch_mib - g.disk))
            } else {
                None
            };
            if let Some(why) = full {
                return Err(ApiError::transient(
                    "host_capacity",
                    format!("宿主的容量满了：{why}"),
                    5,
                ));
            }
            let slot = (0..c.max_vms)
                .find(|s| !g.slots.contains(s))
                .expect("VM 数没满时一定有空的 uid");
            g.slots.insert(slot);
            g.vcpus += u64::from(spec.vcpus);
            g.memory += spec.memory_mib;
            g.disk += spec.disk_mib;
            let rec = Record {
                env_id: env_id.clone(),
                owner: owner.to_string(),
                labels,
                created_at: timefmt::rfc3339(SystemTime::now()),
                slot,
                uid: c.uid_base + slot,
                spec: spec.clone(),
                image,
                pid: None,
                pid_start: None,
            };
            let vm = Vm {
                rec: rec.clone(),
                state: State::Creating,
                token: None,
                cancelled: false,
                busy: true,
                conns: 0,
                links: links.clone(),
                rootfs_stamp: rootfs_stamp(&rootfs),
            };
            g.vms.insert(env_id.clone(), vm);
            rec
        };
        let started = Instant::now();
        match self.boot(&mut rec, &rootfs, &links) {
            Ok(token) => {
                let mut g = self.lock();
                let vm = g.vms.get_mut(&env_id).expect("建的期间只有本线程会删它");
                if vm.cancelled {
                    drop(g);
                    self.discard(&rec);
                    return Err(ApiError::new(
                        409,
                        "env_destroyed",
                        "环境在建好之前被销毁了",
                    ));
                }
                vm.rec = rec.clone();
                vm.state = State::Ready;
                vm.token = Some(token);
                vm.busy = false;
                info!(
                    "建好了 {env_id}（{owner}，{} vCPU，{} MiB，盘 {} MiB，{} ms）",
                    spec.vcpus,
                    spec.memory_mib,
                    spec.disk_mib,
                    started.elapsed().as_millis()
                );
                Ok(Created {
                    env_id,
                    created_at: rec.created_at,
                    image: rec.image,
                    spec,
                })
            }
            Err(e) => {
                warn!("建 {env_id} 失败：{}", e.detail());
                self.discard(&rec);
                Err(match e {
                    VmError::Boot(_) => ApiError::transient("boot_failed", "VM 没能启动", 1),
                    VmError::Rejected(m) => ApiError::new(500, "vm_config_rejected", m),
                    VmError::Host(_) => ApiError::new(500, "internal", "宿主上的操作失败"),
                    VmError::Cancelled => {
                        ApiError::new(409, "env_destroyed", "环境在建好之前被销毁了")
                    }
                })
            }
        }
    }

    fn boot(
        &self,
        rec: &mut Record,
        rootfs: &Path,
        links: &std::sync::Arc<Links>,
    ) -> Result<Token, VmError> {
        // Pin the actual kernel/rootfs in this jail before hashing. Hashing the
        // public image pathname first both doubled IO and raced publication.
        self.store
            .write(rec)
            .map_err(|e| VmError::Host(format!("写记录：{e}")))?;
        self.fault(Fault::AfterRecord);
        let paths = Paths::new(&self.cfg, &rec.env_id);
        vm::prepare(&self.cfg, &paths, &rec.spec, rec.uid, rootfs)?;
        let mut pinned = self.cfg.clone();
        pinned.kernel = paths.in_root(crate::spec::KERNEL_IN_JAIL);
        let key = if self.cfg.snapshot_cache_mib > 0 {
            match self
                .snapshots
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .key(
                    &pinned,
                    &rec.spec,
                    &paths.in_root(crate::spec::ROOTFS_IN_JAIL),
                ) {
                Ok(key) => Some(key),
                Err(e) => {
                    warn!("snapshot key unavailable; cold boot: {}", e.detail());
                    None
                }
            }
        } else {
            None
        };
        let restoring = key.as_deref().is_some_and(|key| {
            self.snapshots
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .contains(key)
        });
        let result = self.boot_attempt(rec, rootfs, links, key.as_deref(), restoring, true);
        if key.is_none() || result.is_ok() || matches!(result, Err(VmError::Cancelled)) {
            return result;
        }
        if let Err(ref e) = result {
            warn!(
                "snapshot bootstrap failed for {}; cold fallback: {}",
                rec.env_id,
                e.detail()
            );
        }
        // A restored VMM and its listeners must be fully removed before trying
        // a cold VM under the same reserved slot. Never reuse a stopped Links.
        links.stop();
        vm::teardown(&self.cfg, &rec.env_id, rec.pid, rec.pid_start).map_err(VmError::Host)?;
        self.checkpoint(&rec.env_id)?;
        self.snapshots
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .invalidate(key.as_deref().expect("snapshot key"))?;
        rec.pid = None;
        rec.pid_start = None;
        let replacement = Links::new(self.cfg.max_forward_conns);
        if let Some(vm) = self.lock().vms.get_mut(&rec.env_id) {
            vm.rec = rec.clone();
            vm.links = replacement.clone();
        }
        self.boot_attempt(rec, rootfs, &replacement, None, false, false)
    }

    fn boot_attempt(
        &self,
        rec: &mut Record,
        rootfs: &Path,
        links: &std::sync::Arc<Links>,
        snapshot_key: Option<&str>,
        restoring: bool,
        prepared: bool,
    ) -> Result<Token, VmError> {
        let write = |r: &Record| {
            self.store
                .write(r)
                .map_err(|e| VmError::Host(format!("写记录：{e}")))
        };
        let p = Paths::new(&self.cfg, &rec.env_id);
        if !prepared {
            write(rec)?;
            self.fault(Fault::AfterRecord);
            vm::prepare(&self.cfg, &p, &rec.spec, rec.uid, rootfs)?;
        }
        if restoring {
            self.snapshots
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .install(&self.cfg, snapshot_key.expect("restore key"), &p, rec.uid)?;
        }
        self.checkpoint(&rec.env_id)?;
        let (pid, start) = vm::spawn(&self.cfg, &p, &rec.env_id, &rec.spec, rec.uid)?;
        rec.pid = Some(pid);
        rec.pid_start = start;
        write(rec)?;
        if let Some(vm) = self.lock().vms.get_mut(&rec.env_id) {
            vm.rec = rec.clone();
        }
        self.fault(Fault::AfterSpawn);
        self.checkpoint(&rec.env_id)?;
        if restoring {
            vm::load_snapshot(&p)?;
        } else {
            vm::configure_bootstrap(&self.cfg, &p, &rec.spec, snapshot_key.is_some())?;
        }
        let token =
            Token::new(vm::random_hex(32).map_err(|e| VmError::Host(format!("取随机数：{e}")))?);
        let listener = vm::token_listener(&self.cfg, &p, rec.uid)?;
        net::open_outbound(&self.cfg, &p, rec.uid, links, |path, uid| {
            vm::chown(&self.cfg, path, uid).map_err(|e| e.detail())
        })
        .map_err(VmError::Host)?;
        if restoring {
            vm::resume(&p)?;
        } else {
            vm::start(&p)?;
        }
        self.fault(Fault::AfterStart);
        let deadline = Instant::now() + self.cfg.boot_timeout;
        if let Some(key) = snapshot_key {
            if !restoring {
                let stream = vm::await_bootstrap(&listener, deadline)?;
                let result = self
                    .snapshots
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .capture(&self.cfg, key, &p, &rec.spec, rec.uid);
                if let Err(e) = result {
                    warn!(
                        "base snapshot capture unavailable; continue cold: {}",
                        e.detail()
                    );
                }
                // Saving and hashing memory can outlive the guest's socket
                // timeout. Never send credentials over the captured stream:
                // make the original VM reconnect just as a restored VM does.
                drop(stream);
            }
            vm::complete_bootstrap(&listener, &token, Instant::now() + self.cfg.boot_timeout)?;
            drop(listener);
            let _ = fs::remove_file(p.token_sock());
            info!(
                "{} bootstrap path: {}",
                rec.env_id,
                if restoring {
                    "memory-snapshot"
                } else {
                    "cold-base-capture"
                }
            );
        } else {
            vm::deliver_token(&p, listener, &token, deadline)?;
            info!("{} bootstrap path: cold", rec.env_id);
        }
        vm::wait_healthy(&p, Instant::now() + self.cfg.boot_timeout)?;
        self.checkpoint(&rec.env_id)?;
        Ok(token)
    }

    /// 拆掉并还回名额；拆不干净就留在表里（Destroying），reaper 接着试，记录也留着给清扫。
    fn discard(&self, rec: &Record) {
        match vm::teardown(&self.cfg, &rec.env_id, rec.pid, rec.pid_start) {
            Ok(()) => {
                if let Err(e) = self.store.remove(&rec.env_id) {
                    warn!("删不掉 {} 的记录：{e}", rec.env_id);
                }
                self.release(&rec.env_id);
            }
            Err(e) => {
                warn!("拆 {} 没完成：{e}；稍后再试", rec.env_id);
                if let Some(vm) = self.lock().vms.get_mut(&rec.env_id) {
                    vm.rec = rec.clone();
                    vm.state = State::Destroying;
                    vm.busy = false;
                }
            }
        }
    }

    fn release(&self, env_id: &str) {
        let mut g = self.lock();
        if let Some(vm) = g.vms.remove(env_id) {
            vm.links.stop();
            g.slots.remove(&vm.rec.slot);
            g.vcpus -= u64::from(vm.rec.spec.vcpus);
            g.memory -= vm.rec.spec.memory_mib;
            g.disk -= vm.rec.spec.disk_mib;
        }
    }

    /// 销毁：没有这个环境也算成功；别的作用域的是 unknown_env。
    pub fn destroy(&self, owner: &str, env_id: &str) -> Result<(), ApiError> {
        let rec = {
            let mut g = self.lock();
            let Some(vm) = g.vms.get_mut(env_id) else {
                return Ok(());
            };
            if vm.rec.owner != owner {
                return Err(ApiError::unknown_env());
            }
            match vm.state {
                State::Creating => {
                    vm.cancelled = true;
                    return Ok(());
                }
                State::Destroying if vm.busy => return Ok(()),
                _ => {
                    vm.state = State::Destroying;
                    vm.busy = true;
                    vm.rec.clone()
                }
            }
        };
        match vm::teardown(&self.cfg, env_id, rec.pid, rec.pid_start) {
            Ok(()) => {
                let _ = self.store.remove(env_id);
                self.release(env_id);
                info!("销毁了 {env_id}");
                Ok(())
            }
            Err(e) => {
                warn!("销毁 {env_id} 没完成：{e}");
                if let Some(vm) = self.lock().vms.get_mut(env_id) {
                    vm.busy = false;
                }
                Err(ApiError::new(
                    500,
                    "destroy_incomplete",
                    "销毁没完成，fc-manager 会接着试",
                ))
            }
        }
    }

    pub fn list(&self) -> Vec<Value> {
        self.lock()
            .vms
            .values()
            .filter(|v| v.state == State::Ready && v.rec.owner != POOL_OWNER)
            .map(|v| {
                json!({
                    "env_id": v.rec.env_id,
                    "owner": v.rec.owner,
                    "labels": v.rec.labels,
                    "created_at": v.rec.created_at,
                    "state": "running",
                    "runtime": v.rec.spec.runtime,
                    "image": v.rec.image,
                    "vcpus": v.rec.spec.vcpus,
                    "memory_mb": v.rec.spec.memory_mib,
                    "disk_mb": v.rec.spec.disk_mib,
                })
            })
            .collect()
    }

    /// 从预热池里拿一台同规格、同镜像的 VM 交给 owner（D-26）。池里的 VM 只起了基础镜像，没有任何租户的数据与变量。
    fn take_warm(
        &self,
        owner: &str,
        spec: &VmSpec,
        labels: &BTreeMap<String, String>,
    ) -> Option<Created> {
        let rootfs = self.cfg.images_dir.join(format!("{}.ext4", spec.runtime));
        let current = image_digest(&rootfs);
        let stamp = rootfs_stamp(&rootfs);
        let rec = {
            let mut g = self.lock();
            let vm = g.vms.values_mut().find(|v| {
                v.rec.owner == POOL_OWNER
                    && v.state == State::Ready
                    && !v.busy
                    && v.rec.spec == *spec
                    && v.rec.image == current
                    && v.rootfs_stamp == stamp
            })?;
            vm.rec.owner = owner.to_string();
            vm.rec.labels = labels.clone();
            vm.rec.created_at = timefmt::rfc3339(SystemTime::now());
            vm.rec.clone()
        };
        if let Err(e) = self.store.write(&rec) {
            warn!("写 {} 的记录失败：{e}", rec.env_id);
        }
        info!("从预热池交出 {}（{owner}）", rec.env_id);
        Some(Created {
            env_id: rec.env_id,
            created_at: rec.created_at,
            image: rec.image,
            spec: rec.spec,
        })
    }

    /// 把预热池补满（定时跑）：每项按配置的台数补；镜像换了的旧待命 VM 拆掉。补不上（容量满）就等下次。
    pub fn fill_pool(&self) {
        for w in &self.cfg.warm_pool {
            let rootfs = self.cfg.images_dir.join(format!("{}.ext4", w.runtime));
            let current = image_digest(&rootfs);
            let stamp = rootfs_stamp(&rootfs);
            let (have, stale): (u32, Vec<String>) = {
                let g = self.lock();
                let mine: Vec<&Vm> = g
                    .vms
                    .values()
                    .filter(|v| {
                        v.rec.owner == POOL_OWNER
                            && v.rec.spec.runtime == w.runtime
                            && v.rec.spec.cpu_millis == w.cpu_millis
                            && v.rec.spec.memory_mib == w.memory_mb
                    })
                    .collect();
                let stale = mine
                    .iter()
                    .filter(|v| {
                        v.state == State::Ready
                            && !v.busy
                            && (v.rec.image != current || v.rootfs_stamp != stamp)
                    })
                    .map(|v| v.rec.env_id.clone())
                    .collect();
                (mine.len() as u32, stale)
            };
            for id in &stale {
                let _ = self.destroy(POOL_OWNER, id);
            }
            for _ in (have - stale.len() as u32)..w.count {
                let req = CreateRequest {
                    runtime: w.runtime.clone(),
                    cpu_millis: w.cpu_millis,
                    memory_mb: w.memory_mb,
                    disk_mb: None,
                    labels: BTreeMap::new(),
                };
                if let Err(e) = self.create(POOL_OWNER, req) {
                    warn!("补预热池（{}）失败：{}", w.runtime, e.message);
                    break;
                }
            }
        }
    }

    /// 客户机端口 `port` 的预览入口（D-22）：在 fc-manager 的监听 IP 上开一个端口，返回 `IP:端口`。
    pub fn preview(&self, owner: &str, env_id: &str, port: u16) -> Result<String, ApiError> {
        let links = {
            let g = self.lock();
            let Some(vm) = g.vms.get(env_id) else {
                return Err(ApiError::unknown_env());
            };
            if vm.rec.owner != owner || vm.state != State::Ready {
                return Err(ApiError::unknown_env());
            }
            vm.links.clone()
        };
        let ip = self
            .cfg
            .listen
            .parse::<std::net::SocketAddr>()
            .map(|a| a.ip())
            .map_err(|_| ApiError::new(500, "internal", "fc-manager 内部错误"))?;
        let p = Paths::new(&self.cfg, env_id);
        net::open_preview(ip, &p, &links, port)
            .map(|a| a.to_string())
            .map_err(|m| ApiError::new(409, "preview_unavailable", m))
    }

    pub fn agent_route(&self, owner: &str, env_id: &str) -> Result<AgentRoute<'_>, ApiError> {
        let mut g = self.lock();
        let Some(vm) = g.vms.get_mut(env_id) else {
            return Err(ApiError::unknown_env());
        };
        if vm.rec.owner != owner || vm.state != State::Ready {
            return Err(ApiError::unknown_env());
        }
        if vm.conns >= self.cfg.max_agent_connections {
            return Err(ApiError::transient(
                "too_many_connections",
                "这个环境的并发请求太多",
                1,
            ));
        }
        let Some(token) = vm.token.clone() else {
            return Err(ApiError::unknown_env());
        };
        vm.conns += 1;
        Ok(AgentRoute {
            mgr: self,
            env_id: env_id.to_string(),
            vsock: Paths::new(&self.cfg, env_id).vsock(),
            token,
        })
    }

    /// 定时跑：VM 进程没了（被手工杀掉、客户机内核 panic）就拆掉它的东西、从表里去掉，之后的请求是 unknown_env；
    /// 上次没拆干净的再拆一次。
    pub fn reap(&self) {
        let todo: Vec<(Record, bool)> = {
            let mut g = self.lock();
            let mut out = vec![];
            for vm in g.vms.values_mut() {
                let dead = vm.state == State::Ready
                    && !vm::running(&vm.rec.env_id, vm.rec.pid, vm.rec.pid_start);
                let retry = vm.state == State::Destroying && !vm.busy;
                if dead || retry {
                    vm.state = State::Destroying;
                    vm.busy = true;
                    out.push((vm.rec.clone(), dead));
                }
            }
            out
        };
        for (rec, dead) in todo {
            if dead {
                warn!(
                    "{} 的 VM 进程退出了（pid {}），清理",
                    rec.env_id,
                    rec.pid.unwrap_or(0)
                );
            }
            self.discard(&rec);
        }
    }
}
