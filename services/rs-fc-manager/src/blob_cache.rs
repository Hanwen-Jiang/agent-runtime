//! WS-BLOB-01: bounded, content-addressed node read cache. Authorization is the caller's responsibility.
//! Cache contents are never a durability acknowledgement: writers must commit to ObjectStore first.
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, Weak};
use std::time::SystemTime;

static NEXT: AtomicU64 = AtomicU64::new(0);
pub struct BlobCache {
    root: PathBuf,
    _lock: File,
    capacity: u64,
    mutation: Mutex<()>,
    flights: Mutex<HashMap<String, Weak<Mutex<()>>>>,
    pub hits: AtomicU64,
    pub misses: AtomicU64,
    pub corruptions: AtomicU64,
    pub evictions: AtomicU64,
}
fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
pub fn valid_digest(sum: &str) -> bool {
    sum.len() == 64
        && sum
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
pub fn digest(data: &[u8]) -> String {
    Sha256::digest(data)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}
impl BlobCache {
    pub fn new(root: impl Into<PathBuf>, capacity: u64) -> io::Result<Self> {
        if capacity == 0 {
            return Err(invalid("cache capacity must be positive"));
        }
        let root = root.into();
        fs::create_dir_all(&root)?;
        if fs::symlink_metadata(&root)?.file_type().is_symlink() {
            return Err(invalid("cache root must not be a symlink"));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&root, fs::Permissions::from_mode(0o700))?;
        }
        let mut options = OpenOptions::new();
        options.read(true).write(true).create(true).truncate(false);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let lock = options.open(root.join(".cache.lock"))?;
        lock.try_lock()
            .map_err(|_| invalid("cache already has a writer"))?;
        // Only our numeric .pending-PID-counter regular files are abandoned
        // writes. The process lock prevents deleting an active writer's file.
        for entry in fs::read_dir(&root)? {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().into_owned();
            let owned = name
                .strip_prefix(".pending-")
                .and_then(|s| s.split_once('-'))
                .is_some_and(|(pid, seq)| {
                    !pid.is_empty()
                        && !seq.is_empty()
                        && pid.bytes().all(|b| b.is_ascii_digit())
                        && seq.bytes().all(|b| b.is_ascii_digit())
                });
            if owned && fs::symlink_metadata(entry.path())?.file_type().is_file() {
                fs::remove_file(entry.path())?;
            }
        }
        let cache = Self {
            root,
            _lock: lock,
            capacity,
            mutation: Mutex::new(()),
            flights: Mutex::new(HashMap::new()),
            hits: AtomicU64::new(0),
            misses: AtomicU64::new(0),
            corruptions: AtomicU64::new(0),
            evictions: AtomicU64::new(0),
        };
        cache.trim(0)?;
        Ok(cache)
    }
    fn read(&self, key: &str, size: u64) -> io::Result<Option<Vec<u8>>> {
        let path = self.root.join(key);
        let md = match fs::symlink_metadata(&path) {
            Ok(m) => m,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e),
        };
        if !md.is_file() || md.file_type().is_symlink() || md.len() != size {
            self.corruptions.fetch_add(1, Ordering::Relaxed);
            let _ = fs::remove_file(path);
            return Ok(None);
        }
        let f = match File::open(&path) {
            Ok(f) => f,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e),
        };
        let mut data = Vec::new();
        (&f).take(size.saturating_add(1)).read_to_end(&mut data)?;
        if data.len() as u64 != size || digest(&data) != key {
            self.corruptions.fetch_add(1, Ordering::Relaxed);
            let _ = fs::remove_file(path);
            return Ok(None);
        }
        let _ = f.set_modified(SystemTime::now());
        Ok(Some(data))
    }
    /// A single fetch per digest runs at once. Distinct digests fetch concurrently.
    /// The fetch callback must enforce its own request timeout and maximum response size.
    pub fn get_or_fetch(
        &self,
        key: &str,
        size: u64,
        fetch: impl FnOnce() -> io::Result<Vec<u8>>,
    ) -> io::Result<Vec<u8>> {
        if !valid_digest(key) {
            return Err(invalid("invalid sha256"));
        }
        let flight = {
            let mut flights = self.flights.lock().unwrap_or_else(|e| e.into_inner());
            flights.retain(|_, v| v.strong_count() > 0);
            if let Some(lock) = flights.get(key).and_then(Weak::upgrade) {
                lock
            } else {
                let lock = Arc::new(Mutex::new(()));
                flights.insert(key.into(), Arc::downgrade(&lock));
                lock
            }
        };
        let _single = flight.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(data) = self.read(key, size)? {
            self.hits.fetch_add(1, Ordering::Relaxed);
            return Ok(data);
        }
        self.misses.fetch_add(1, Ordering::Relaxed);
        let data = fetch()?;
        if data.len() as u64 != size || digest(&data) != key {
            return Err(invalid("blob size or sha256 mismatch"));
        }
        if size <= self.capacity {
            let _mutation = self.mutation.lock().unwrap_or_else(|e| e.into_inner());
            self.trim(size)?;
            let tmp = self.root.join(format!(
                ".pending-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            let result = (|| {
                let mut o = OpenOptions::new();
                o.write(true).create_new(true);
                #[cfg(unix)]
                {
                    use std::os::unix::fs::OpenOptionsExt;
                    o.mode(0o600);
                }
                let mut f = o.open(&tmp)?;
                f.write_all(&data)?;
                f.sync_all()?;
                fs::rename(&tmp, self.root.join(key))
            })();
            if result.is_err() {
                let _ = fs::remove_file(&tmp);
            }
            result?;
        }
        Ok(data)
    }
    fn trim(&self, incoming: u64) -> io::Result<()> {
        let mut entries = Vec::new();
        let mut total = 0u64;
        for e in fs::read_dir(&self.root)? {
            let e = e?;
            let name = e.file_name().to_string_lossy().into_owned();
            if !valid_digest(&name) {
                continue;
            }
            let m = fs::symlink_metadata(e.path())?;
            if !m.is_file() || m.file_type().is_symlink() {
                let _ = fs::remove_file(e.path());
                continue;
            }
            total = total.saturating_add(m.len());
            entries.push((
                m.modified().unwrap_or(SystemTime::UNIX_EPOCH),
                e.path(),
                m.len(),
            ));
        }
        entries.sort_by(|a, b| a.0.cmp(&b.0).then(a.1.cmp(&b.1)));
        for (_, path, size) in entries {
            if total.saturating_add(incoming) <= self.capacity {
                break;
            }
            match fs::remove_file(path) {
                Ok(()) => {
                    total = total.saturating_sub(size);
                    self.evictions.fetch_add(1, Ordering::Relaxed);
                }
                Err(e) if e.kind() == io::ErrorKind::NotFound => total = total.saturating_sub(size),
                Err(e) => return Err(e),
            }
        }
        Ok(())
    }
    pub fn root(&self) -> &Path {
        &self.root
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Dir(PathBuf);
    impl Dir {
        fn new() -> Self {
            Self(std::env::temp_dir().join(format!(
                "fc-blob-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            )))
        }
    }
    impl Drop for Dir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    #[test]
    fn hit_corruption_and_capacity() {
        let d = Dir::new();
        let cache = BlobCache::new(&d.0, 8).unwrap();
        let a = b"abcd";
        let b = b"efgh";
        let c = b"ijkl";
        cache
            .get_or_fetch(&digest(a), 4, || Ok(a.to_vec()))
            .unwrap();
        cache
            .get_or_fetch(&digest(a), 4, || panic!("cache miss"))
            .unwrap();
        fs::write(d.0.join(digest(a)), b"xxxx").unwrap();
        cache
            .get_or_fetch(&digest(a), 4, || Ok(a.to_vec()))
            .unwrap();
        cache
            .get_or_fetch(&digest(b), 4, || Ok(b.to_vec()))
            .unwrap();
        cache
            .get_or_fetch(&digest(c), 4, || Ok(c.to_vec()))
            .unwrap();
        assert_eq!(cache.corruptions.load(Ordering::Relaxed), 1);
        assert_eq!(cache.evictions.load(Ordering::Relaxed), 1);
        let total: u64 = fs::read_dir(&d.0)
            .unwrap()
            .map(|e| e.unwrap().metadata().unwrap().len())
            .sum();
        assert!(total <= 8);
        assert!(
            cache
                .get_or_fetch("../escape", 4, || Ok(a.to_vec()))
                .is_err()
        );
        assert!(
            cache
                .get_or_fetch(&digest(a), 4, || Ok(b.to_vec()))
                .is_err()
        );
    }
    #[test]
    fn concurrent_same_blob_fetches_once() {
        let d = Dir::new();
        let cache = Arc::new(BlobCache::new(&d.0, 1024).unwrap());
        let calls = Arc::new(AtomicU64::new(0));
        let mut threads = vec![];
        for _ in 0..12 {
            let cache = cache.clone();
            let calls = calls.clone();
            threads.push(std::thread::spawn(move || {
                cache
                    .get_or_fetch(&digest(b"same"), 4, || {
                        calls.fetch_add(1, Ordering::SeqCst);
                        std::thread::sleep(std::time::Duration::from_millis(10));
                        Ok(b"same".to_vec())
                    })
                    .unwrap()
            }));
        }
        for t in threads {
            assert_eq!(t.join().unwrap(), b"same")
        }
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }
    #[test]
    fn restart_reuses_and_large_blob_bypasses() {
        let d = Dir::new();
        let key = digest(b"small");
        BlobCache::new(&d.0, 8)
            .unwrap()
            .get_or_fetch(&key, 5, || Ok(b"small".to_vec()))
            .unwrap();
        let cache = BlobCache::new(&d.0, 8).unwrap();
        cache
            .get_or_fetch(&key, 5, || panic!("restart miss"))
            .unwrap();
        let big = b"more than eight";
        cache
            .get_or_fetch(&digest(big), big.len() as u64, || Ok(big.to_vec()))
            .unwrap();
        assert!(!d.0.join(digest(big)).exists());
    }
    #[test]
    fn startup_cleans_only_abandoned_owned_files_under_exclusive_lock() {
        let d = Dir::new();
        fs::create_dir_all(&d.0).unwrap();
        fs::write(d.0.join(".pending-123-4"), vec![0u8; 1024]).unwrap();
        fs::write(d.0.join(".pending-not-ours"), b"keep").unwrap();
        let cache = BlobCache::new(&d.0, 8).unwrap();
        assert!(!d.0.join(".pending-123-4").exists());
        assert!(d.0.join(".pending-not-ours").exists());
        fs::write(d.0.join(".pending-456-7"), b"active").unwrap();
        assert!(BlobCache::new(&d.0, 8).is_err());
        assert!(d.0.join(".pending-456-7").exists());
        drop(cache);
        let _next = BlobCache::new(&d.0, 8).unwrap();
        assert!(!d.0.join(".pending-456-7").exists());
    }
}
