//! Host-local, bounded snapshots of the pre-credential bootstrap boundary.
//! Only the manager can capture here: there is no API to snapshot a tenant VM.
//! Files installed in a live jail are hard links, pinning their cache entry.

use std::collections::BTreeSet;
use std::fs::{self, File};
use std::io::Read;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};

use crate::config::Config;
use crate::spec::{SCRATCH_IN_JAIL, VmSpec};
use crate::vm::{self, Paths, VmError};

const FORMAT: u32 = 3;
const FILES: [&str; 3] = ["state", "memory", "scratch"];

#[derive(Serialize, Deserialize)]
struct Manifest {
    format: u32,
    key: String,
    lengths: [u64; 3],
    sha256: [String; 3],
}

type Stamp = (u64, u64, u64, i64, i64, i64, i64);

pub struct Cache {
    root: PathBuf,
    budget: u64,
}

fn failure(e: impl std::fmt::Display) -> VmError {
    VmError::Host(format!("snapshot cache: {e}"))
}

fn stamp(m: &fs::Metadata) -> Stamp {
    (
        m.dev(),
        m.ino(),
        m.len(),
        m.mtime(),
        m.mtime_nsec(),
        m.ctime(),
        m.ctime_nsec(),
    )
}

fn key_valid(key: &str) -> bool {
    key.len() == 64
        && key
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn sparse_copy(from: &Path, to: &Path) -> Result<(), VmError> {
    let status = Command::new("/usr/bin/cp")
        .env_clear()
        .args(["--reflink=auto", "--sparse=always", "--"])
        .arg(from)
        .arg(to)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map_err(failure)?;
    if status.success() {
        Ok(())
    } else {
        Err(failure("copy failed"))
    }
}

impl Cache {
    pub fn new(cfg: &Config) -> Result<Self, String> {
        let budget = cfg
            .snapshot_cache_mib
            .checked_mul(1 << 20)
            .ok_or("snapshot cache limit overflow")?;
        let root = cfg.state_dir.join("base-snapshots");
        if budget > 0 {
            fs::DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(&root)
                .map_err(|e| e.to_string())?;
            fs::set_permissions(&root, fs::Permissions::from_mode(0o700))
                .map_err(|e| e.to_string())?;
            // The manager holds the state-directory lock before opening this
            // cache. A crashed capture cannot leave unbounded partial files.
            for entry in fs::read_dir(&root).map_err(|e| e.to_string())?.flatten() {
                let name = entry.file_name().to_string_lossy().into_owned();
                if name.strip_prefix(".partial-").is_some_and(key_valid) {
                    fs::remove_dir_all(entry.path()).map_err(|e| e.to_string())?;
                }
            }
        }
        Ok(Self { root, budget })
    }

    fn hash_file(&mut self, path: &Path) -> Result<String, VmError> {
        let mut file = File::open(path).map_err(failure)?;
        let identity = stamp(&file.metadata().map_err(failure)?);
        // Metadata is a race detector, not proof of unchanged contents: two
        // same-size writes can share a filesystem timestamp. Always validate
        // bytes before a VM consumes an image or snapshot.
        let mut hash = Sha256::new();
        let mut buf = vec![0u8; 1 << 20];
        loop {
            let n = file.read(&mut buf).map_err(failure)?;
            if n == 0 {
                break;
            }
            hash.update(&buf[..n]);
        }
        if stamp(&file.metadata().map_err(failure)?) != identity {
            return Err(failure("image changed while hashing"));
        }
        let digest = hex(&hash.finalize());
        Ok(digest)
    }

    /// Real content digests invalidate the cache after kernel/VMM/agent/rootfs
    /// updates. CPU identity is stable across frequency changes on this host.
    pub fn key(&mut self, cfg: &Config, spec: &VmSpec, rootfs: &Path) -> Result<String, VmError> {
        let cpu = fs::read_to_string("/proc/cpuinfo").map_err(failure)?;
        let cpu: BTreeSet<_> = cpu
            .lines()
            .filter_map(|line| {
                let (key, value) = line.split_once(':')?;
                [
                    "vendor_id",
                    "cpu family",
                    "model",
                    "stepping",
                    "microcode",
                    "flags",
                ]
                .contains(&key.trim())
                .then(|| format!("{}:{}", key.trim(), value.trim()))
            })
            .collect();
        let host_kernel = fs::read_to_string("/proc/sys/kernel/osrelease").map_err(failure)?;
        let key = json!({"format":FORMAT, "rootfs":self.hash_file(rootfs)?, "kernel":self.hash_file(&cfg.kernel)?,
            "vmm":self.hash_file(&cfg.firecracker_bin)?, "host_kernel":host_kernel.trim(), "cpu":cpu, "cpu_template":"none", "spec":spec, "init":cfg.guest_init});
        Ok(hex(&Sha256::digest(key.to_string().as_bytes())))
    }

    fn manifest(&self, key: &str) -> Result<Manifest, VmError> {
        if !key_valid(key) {
            return Err(failure("invalid cache key"));
        }
        let dir = self.root.join(key);
        let manifest: Manifest =
            serde_json::from_slice(&fs::read(dir.join("manifest.json")).map_err(failure)?)
                .map_err(failure)?;
        if manifest.format != FORMAT || manifest.key != key {
            return Err(failure("cache version mismatch"));
        }
        for (i, name) in FILES.iter().enumerate() {
            let meta = fs::symlink_metadata(dir.join(name)).map_err(failure)?;
            if !meta.is_file()
                || meta.len() != manifest.lengths[i]
                || meta.permissions().mode() & 0o222 != 0
            {
                return Err(failure("cache file metadata mismatch"));
            }
        }
        Ok(manifest)
    }

    pub fn contains(&self, key: &str) -> bool {
        if self.manifest(key).is_ok() {
            true
        } else {
            self.invalidate(key);
            false
        }
    }

    pub fn invalidate(&self, key: &str) {
        if key_valid(key) {
            let _ = fs::remove_dir_all(self.root.join(key));
        }
    }

    fn make_room(&self, needed: u64) -> Result<(), VmError> {
        if needed > self.budget {
            return Err(failure("base snapshot exceeds configured cache capacity"));
        }
        let mut entries = Vec::new();
        let mut total = 0u64;
        for entry in fs::read_dir(&self.root).map_err(failure)?.flatten() {
            let key = entry.file_name().to_string_lossy().into_owned();
            if !key_valid(&key) {
                continue;
            }
            let Ok(manifest) = self.manifest(&key) else {
                self.invalidate(&key);
                continue;
            };
            let size = manifest
                .lengths
                .iter()
                .try_fold(0u64, |sum, n| sum.checked_add(*n))
                .ok_or_else(|| failure("snapshot size overflow"))?;
            total = total
                .checked_add(size)
                .ok_or_else(|| failure("cache size overflow"))?;
            let meta = fs::metadata(entry.path().join("memory")).map_err(failure)?;
            let last = fs::metadata(entry.path().join("manifest.json"))
                .and_then(|m| m.modified())
                .unwrap_or(UNIX_EPOCH);
            entries.push((last, key, size, meta.nlink() == 1));
        }
        entries.sort_by_key(|e| e.0);
        for (_, key, size, unpinned) in entries {
            if total <= self.budget - needed {
                break;
            }
            if unpinned {
                self.invalidate(&key);
                total -= size;
            }
        }
        if total > self.budget - needed {
            return Err(failure("cache capacity pinned by active VMs"));
        }
        Ok(())
    }

    pub fn install(&mut self, cfg: &Config, key: &str, p: &Paths, uid: u32) -> Result<(), VmError> {
        let manifest = self.manifest(key)?;
        let dir = self.root.join(key);
        for (i, name) in FILES.iter().enumerate() {
            if self.hash_file(&dir.join(name))? != manifest.sha256[i] {
                return Err(failure("snapshot content digest mismatch"));
            }
        }
        fs::create_dir_all(p.in_root("/snapshots")).map_err(failure)?;
        for name in ["memory", "state"] {
            fs::hard_link(dir.join(name), p.in_root(&format!("/snapshots/{name}")))
                .map_err(failure)?;
        }
        sparse_copy(&dir.join("scratch"), &p.in_root(SCRATCH_IN_JAIL))?;
        fs::set_permissions(
            p.in_root(SCRATCH_IN_JAIL),
            fs::Permissions::from_mode(0o600),
        )
        .map_err(failure)?;
        vm::chown(cfg, &p.in_root(SCRATCH_IN_JAIL), uid)?;
        File::open(dir.join("manifest.json"))
            .and_then(|f| f.set_times(fs::FileTimes::new().set_modified(SystemTime::now())))
            .map_err(failure)
    }

    /// Called only while PID 1 waits for its first token. Always resumes the
    /// original VM even when capturing fails, so it can continue cold boot.
    pub fn capture(
        &mut self,
        cfg: &Config,
        key: &str,
        p: &Paths,
        spec: &VmSpec,
        uid: u32,
    ) -> Result<(), VmError> {
        if self.contains(key) {
            return Ok(());
        }
        let estimate = spec
            .memory_mib
            .checked_add(spec.disk_mib)
            .and_then(|m| m.checked_mul(1 << 20))
            .and_then(|b| b.checked_add(1 << 20))
            .ok_or_else(|| failure("snapshot size overflow"))?;
        self.make_room(estimate)?;
        let work = p.in_root("/snapshots");
        fs::create_dir_all(&work).map_err(failure)?;
        fs::set_permissions(&work, fs::Permissions::from_mode(0o700)).map_err(failure)?;
        vm::chown(cfg, &work, uid)?;
        vm::api_request(p, "PATCH", "/vm", &json!({"state":"Paused"}))?;
        let temporary = self.root.join(format!(".partial-{key}"));
        let result = (|| {
            vm::api_request(
                p,
                "PUT",
                "/snapshot/create",
                &json!({"snapshot_type":"Full", "snapshot_path":"/snapshots/state", "mem_file_path":"/snapshots/memory"}),
            )?;
            if temporary.exists() {
                fs::remove_dir_all(&temporary).map_err(failure)?;
            }
            fs::DirBuilder::new()
                .mode(0o700)
                .create(&temporary)
                .map_err(failure)?;
            for name in ["state", "memory"] {
                fs::rename(work.join(name), temporary.join(name)).map_err(failure)?;
            }
            sparse_copy(&p.in_root(SCRATCH_IN_JAIL), &temporary.join("scratch"))?;
            let mut lengths = [0; 3];
            let mut sha256 = std::array::from_fn(|_| String::new());
            for (i, name) in FILES.iter().enumerate() {
                let file = temporary.join(name);
                lengths[i] = fs::metadata(&file).map_err(failure)?.len();
                fs::set_permissions(&file, fs::Permissions::from_mode(0o444)).map_err(failure)?;
                vm::chown(cfg, &file, 0)?;
                File::open(&file)
                    .and_then(|f| f.sync_all())
                    .map_err(failure)?;
                sha256[i] = self.hash_file(&file)?;
            }
            let manifest = Manifest {
                format: FORMAT,
                key: key.to_string(),
                lengths,
                sha256,
            };
            let actual = lengths
                .iter()
                .try_fold(0u64, |sum, size| sum.checked_add(*size))
                .ok_or_else(|| failure("snapshot size overflow"))?;
            self.make_room(actual)?;
            fs::write(
                temporary.join("manifest.json"),
                serde_json::to_vec(&manifest).map_err(failure)?,
            )
            .map_err(failure)?;
            File::open(temporary.join("manifest.json"))
                .and_then(|f| f.sync_all())
                .map_err(failure)?;
            File::open(&temporary)
                .and_then(|f| f.sync_all())
                .map_err(failure)?;
            fs::rename(&temporary, self.root.join(key)).map_err(failure)?;
            File::open(&self.root)
                .and_then(|f| f.sync_all())
                .map_err(failure)
        })();
        let resumed = vm::resume(p);
        if result.is_err() {
            let _ = fs::remove_dir_all(&temporary);
        }
        resumed?;
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct TestCache {
        cache: Cache,
        root: PathBuf,
    }
    impl TestCache {
        fn new(budget: u64) -> Self {
            let root =
                std::env::temp_dir().join(format!("fc-snapshot-{}", vm::random_hex(12).unwrap()));
            fs::create_dir_all(&root).unwrap();
            Self {
                cache: Cache {
                    root: root.clone(),
                    budget,
                },
                root,
            }
        }
        fn entry(&self, ch: &str, length: u64) -> String {
            let key = ch.repeat(64);
            let dir = self.root.join(&key);
            fs::create_dir(&dir).unwrap();
            for name in FILES {
                let file = File::create(dir.join(name)).unwrap();
                file.set_len(length).unwrap();
                file.set_permissions(fs::Permissions::from_mode(0o444))
                    .unwrap();
            }
            fs::write(
                dir.join("manifest.json"),
                serde_json::to_vec(&Manifest {
                    format: FORMAT,
                    key: key.clone(),
                    lengths: [length; 3],
                    sha256: std::array::from_fn(|_| hex(&Sha256::digest(vec![0; length as usize]))),
                })
                .unwrap(),
            )
            .unwrap();
            key
        }
    }
    impl Drop for TestCache {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn pinned_memory_survives_eviction_and_invalid_entries_are_rejected() {
        let test = TestCache::new(60);
        let first = test.entry("a", 10);
        let second = test.entry("b", 10);
        fs::hard_link(
            test.root.join(&first).join("memory"),
            test.root.join("active-memory"),
        )
        .unwrap();
        test.cache.make_room(30).unwrap();
        assert!(test.cache.contains(&first));
        assert!(!test.cache.contains(&second));
        assert!(test.cache.make_room(31).is_err());
        // Same-length symlinks must not qualify as trusted snapshot files.
        fs::remove_file(test.root.join(&first).join("state")).unwrap();
        std::os::unix::fs::symlink("memory", test.root.join(&first).join("state")).unwrap();
        assert!(!test.cache.contains(&first));
        assert!(!test.cache.contains("../outside"));
    }

    #[test]
    fn content_changes_invalidate_cached_hash_even_at_same_size() {
        let mut test = TestCache::new(60);
        let path = test.root.join("image");
        fs::write(&path, b"rootfs-v1").unwrap();
        let before = test.cache.hash_file(&path).unwrap();
        fs::write(&path, b"rootfs-v2").unwrap();
        let after = test.cache.hash_file(&path).unwrap();
        assert_ne!(before, after);
        assert_eq!(after, test.cache.hash_file(&path).unwrap());
    }

    #[test]
    fn restore_checks_content_and_copies_a_private_writable_disk() {
        let mut test = TestCache::new(1024);
        let key = test.entry("a", 16);
        let mut cfg = Config::from_env(|_| None).unwrap();
        cfg.chroot_base = test.root.join("j");
        let a = Paths::new(&cfg, "fc-0000000000000001");
        let b = Paths::new(&cfg, "fc-0000000000000002");
        test.cache
            .install(&cfg, &key, &a, crate::sys::geteuid())
            .unwrap();
        test.cache
            .install(&cfg, &key, &b, crate::sys::geteuid())
            .unwrap();
        fs::write(a.in_root(SCRATCH_IN_JAIL), b"tenant-a-private").unwrap();
        assert_eq!(fs::read(b.in_root(SCRATCH_IN_JAIL)).unwrap(), vec![0; 16]);
        assert_eq!(
            fs::read(test.root.join(&key).join("scratch")).unwrap(),
            vec![0; 16]
        );
        let state = test.root.join(&key).join("state");
        fs::set_permissions(&state, fs::Permissions::from_mode(0o644)).unwrap();
        fs::write(&state, b"same-size-damage").unwrap();
        fs::set_permissions(&state, fs::Permissions::from_mode(0o444)).unwrap();
        let c = Paths::new(&cfg, "fc-0000000000000003");
        assert!(
            test.cache
                .install(&cfg, &key, &c, crate::sys::geteuid())
                .is_err()
        );
        assert!(!c.in_root(SCRATCH_IN_JAIL).exists());
    }
}
