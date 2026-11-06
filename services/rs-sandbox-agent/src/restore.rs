//! WS-PULL-01 staged restore: batches fetch only absent content, then securely materialize a complete manifest.
use crate::agent::{App, fail, write_json};
use crate::http::Ctx;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

struct Session {
    root: PathBuf,
    expires: Instant,
    max_bytes: u64,
}
impl Drop for Session {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}
#[derive(Default)]
pub struct Restores {
    sessions: Mutex<HashMap<String, Arc<Mutex<Session>>>>,
}
fn bad() -> io::Error {
    io::Error::other("invalid restore plan")
}
fn id_ok(id: &str) -> bool {
    id.len() == 32 && id.bytes().all(|b| b.is_ascii_hexdigit())
}
fn request(ctx: &mut Ctx<'_>) -> Option<Value> {
    ctx.body.set_limit(16 << 20);
    serde_json::from_slice(&ctx.body.read_to_end_limited().ok()?).ok()
}
fn read_block(path: &Path, size: u64, sum: &str) -> bool {
    let Ok(file) = File::open(path) else {
        return false;
    };
    let mut data = Vec::new();
    file.take(size + 1).read_to_end(&mut data).is_ok()
        && data.len() as u64 == size
        && rs_transfer::digest(&data) == sum
}
impl Restores {
    fn session(&self, id: &str, max_bytes: u64) -> io::Result<Arc<Mutex<Session>>> {
        if !id_ok(id) || max_bytes == 0 || max_bytes > 64 << 30 {
            return Err(bad());
        }
        let mut sessions = self.sessions.lock().unwrap_or_else(|e| e.into_inner());
        sessions
            .retain(|_, s| s.lock().unwrap_or_else(|e| e.into_inner()).expires > Instant::now());
        if let Some(s) = sessions.get(id) {
            if s.lock().unwrap_or_else(|e| e.into_inner()).max_bytes != max_bytes {
                return Err(bad());
            };
            return Ok(s.clone());
        }
        if sessions.len() >= 4 {
            return Err(bad());
        }
        let root = std::env::temp_dir().join(format!("ar-restore-{}", rs_transfer::random_id()?));
        fs::create_dir(&root)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&root, fs::Permissions::from_mode(0o700))?;
        }
        let s = Arc::new(Mutex::new(Session {
            root,
            expires: Instant::now() + Duration::from_secs(900),
            max_bytes,
        }));
        sessions.insert(id.into(), s.clone());
        Ok(s)
    }
}
pub fn blocks(app: &App, ctx: &mut Ctx<'_>) {
    let Some(v) = request(ctx) else {
        return fail(ctx, 400, "invalid_request", "invalid restore batch");
    };
    let result = (|| -> io::Result<(usize, u64)> {
        let id = v["restore_id"].as_str().ok_or_else(bad)?;
        let limit = v["max_bytes"].as_u64().ok_or_else(bad)?;
        let session = app.restores.session(id, limit)?;
        let mut session = session.lock().unwrap_or_else(|e| e.into_inner());
        let blocks = v["blocks"]
            .as_array()
            .filter(|b| b.len() <= 128)
            .ok_or_else(bad)?;
        let mut todo = vec![];
        let mut seen = std::collections::HashSet::new();
        let mut used: u64 = fs::read_dir(&session.root)?
            .filter_map(Result::ok)
            .filter_map(|e| e.metadata().ok())
            .map(|m| m.len())
            .sum();
        let mut incoming = 0u64;
        for b in blocks {
            let sum = b["sha256"]
                .as_str()
                .filter(|s| rs_transfer::valid_digest(s))
                .ok_or_else(bad)?
                .to_string();
            let size = b["size_bytes"]
                .as_u64()
                .filter(|s| *s <= rs_transfer::MAX_BLOB_BYTES)
                .ok_or_else(bad)?;
            if !seen.insert(sum.clone()) || read_block(&session.root.join(&sum), size, &sum) {
                continue;
            }
            let staged = session.root.join(&sum);
            if let Ok(md) = fs::symlink_metadata(&staged) {
                fs::remove_file(&staged)?;
                used = used.saturating_sub(md.len());
            }
            incoming = incoming.checked_add(size).ok_or_else(bad)?;
            let target: rs_transfer::Request =
                serde_json::from_value(b["target"].clone()).map_err(|_| bad())?;
            todo.push((sum, size, target));
        }
        if used.saturating_add(incoming) > limit {
            return Err(bad());
        }
        let count = todo.len();
        let tasks = Mutex::new(todo);
        let errors = Mutex::new(false);
        std::thread::scope(|scope| {
            for _ in 0..4 {
                let tasks = &tasks;
                let errors = &errors;
                let root = &session.root;
                scope.spawn(move || {
                    loop {
                        let Some((sum, size, target)) = tasks.lock().unwrap().pop() else {
                            break;
                        };
                        let mut result = Err(bad());
                        for _ in 0..3 {
                            result = rs_transfer::download(&target, size, &sum);
                            if result.is_ok() {
                                break;
                            }
                        }
                        match result {
                            Ok(data) => {
                                if fs::write(root.join(&sum), data).is_err() {
                                    *errors.lock().unwrap() = true
                                }
                            }
                            Err(_) => *errors.lock().unwrap() = true,
                        }
                    }
                });
            }
        });
        if *errors.lock().unwrap() {
            return Err(io::Error::other("block download failed"));
        }
        session.expires = Instant::now() + Duration::from_secs(900);
        Ok((count, incoming))
    })();
    match result {
        Ok((count, bytes)) => write_json(
            ctx,
            200,
            serde_json::json!({"downloaded_blocks":count,"downloaded_bytes":bytes}).to_string(),
        ),
        Err(_) => fail(
            ctx,
            502,
            "workspace_restore_mismatch",
            "restore batch failed validation or transfer",
        ),
    }
}
// Assemble and verify every ordered block and the complete file before it enters
// the archive. The destination workspace remains untouched during this phase.
fn assembled_file(session: &Session, entry: &Value) -> io::Result<rs_transfer::TempFile> {
    let sum = entry["sha256"]
        .as_str()
        .filter(|s| rs_transfer::valid_digest(s))
        .ok_or_else(bad)?;
    let size = entry["size"].as_u64().unwrap_or(0);
    if size > session.max_bytes {
        return Err(bad());
    }
    let chunks: Vec<(String, u64)> = match entry.get("chunks") {
        Some(Value::Array(chunks)) if !chunks.is_empty() => chunks
            .iter()
            .map(|b| {
                let sha = b["sha256"]
                    .as_str()
                    .filter(|s| rs_transfer::valid_digest(s))
                    .ok_or_else(bad)?;
                let size = b["size_bytes"]
                    .as_u64()
                    .filter(|n| *n > 0 && *n <= 4 << 20)
                    .ok_or_else(bad)?;
                Ok((sha.to_string(), size))
            })
            .collect::<io::Result<_>>()?,
        None | Some(Value::Array(_)) => vec![(sum.to_string(), size)],
        _ => return Err(bad()),
    };
    let tmp = rs_transfer::TempFile::new()?;
    let mut out = File::create(&tmp.path)?;
    let mut hash = Sha256::new();
    let mut total = 0u64;
    let mut buf = vec![0u8; 64 << 10];
    for (sha, n) in chunks {
        total = total.checked_add(n).ok_or_else(bad)?;
        if total > size {
            return Err(bad());
        }
        let mut input = File::open(session.root.join(&sha))?;
        if input.metadata()?.len() != n {
            return Err(bad());
        }
        let mut part_hash = Sha256::new();
        let mut remaining = n;
        while remaining > 0 {
            let limit = remaining.min(buf.len() as u64) as usize;
            input.read_exact(&mut buf[..limit])?;
            hash.update(&buf[..limit]);
            part_hash.update(&buf[..limit]);
            out.write_all(&buf[..limit])?;
            remaining -= limit as u64;
        }
        let got: String = part_hash
            .finalize()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        if got != sha {
            return Err(bad());
        }
    }
    let got: String = hash.finalize().iter().map(|b| format!("{b:02x}")).collect();
    if total != size || got != sum {
        return Err(bad());
    }
    out.flush()?;
    Ok(tmp)
}

pub fn finish(app: &App, ctx: &mut Ctx<'_>) {
    let Some(v) = request(ctx) else {
        return fail(ctx, 400, "invalid_request", "invalid restore plan");
    };
    let id = v["restore_id"].as_str().unwrap_or("");
    let session = app
        .restores
        .sessions
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .remove(id);
    let Some(session) = session else {
        return fail(ctx, 404, "restore_not_found", "unknown restore session");
    };
    let result = (|| -> io::Result<()> {
        let session = session.lock().unwrap_or_else(|e| e.into_inner());
        if session.expires <= Instant::now() {
            return Err(bad());
        }
        let entries = v["entries"]
            .as_array()
            .filter(|e| e.len() <= 100_000)
            .ok_or_else(bad)?;
        let tmp = rs_transfer::TempFile::new()?;
        let mut builder = tar::Builder::new(File::create(&tmp.path)?);
        let mut total = 0u64;
        let mut paths = std::collections::HashSet::new();
        for e in entries {
            let path = e["path"].as_str().ok_or_else(bad)?;
            if !paths.insert(path.to_string()) {
                return Err(bad());
            }
            let mut header = tar::Header::new_gnu();
            header.set_mode(e["mode"].as_u64().unwrap_or(0) as u32 & 0o777);
            let kind = e["type"].as_str().ok_or_else(bad)?;
            match kind {
                "dir" => {
                    header.set_entry_type(tar::EntryType::Directory);
                    header.set_size(0);
                    header.set_cksum();
                    builder.append_data(&mut header, path, io::empty())?;
                }
                "symlink" => {
                    let link = e["link"].as_str().ok_or_else(bad)?;
                    header.set_entry_type(tar::EntryType::Symlink);
                    header.set_size(0);
                    header.set_link_name(link)?;
                    header.set_cksum();
                    builder.append_data(&mut header, path, io::empty())?;
                }
                "file" => {
                    let size = e["size"].as_u64().unwrap_or(0);
                    total = total.checked_add(size).ok_or_else(bad)?;
                    if total > session.max_bytes {
                        return Err(bad());
                    }
                    let file = assembled_file(&session, e)?;
                    header.set_entry_type(tar::EntryType::Regular);
                    header.set_size(size);
                    header.set_cksum();
                    builder.append_data(&mut header, path, File::open(&file.path)?)?;
                }
                _ => return Err(bad()),
            }
        }
        builder.finish()?;
        drop(builder);
        crate::archive::restore_tar(
            &app.c.workdir,
            &mut File::open(&tmp.path)?,
            session.max_bytes,
        )
    })();
    match result {
        Ok(()) => write_json(ctx, 200, "{}".into()),
        Err(_) => fail(
            ctx,
            400,
            "workspace_restore_mismatch",
            "restore rejected; discard this environment",
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn ordered_chunks_verify_each_block_and_complete_file() {
        let root = std::env::temp_dir().join(format!(
            "restore-test-{}",
            rs_transfer::random_id().unwrap()
        ));
        fs::create_dir(&root).unwrap();
        let session = Session {
            root,
            expires: Instant::now() + Duration::from_secs(60),
            max_bytes: 32,
        };
        let first = b"prefix";
        let second = b"suffix";
        let a = rs_transfer::digest(first);
        let b = rs_transfer::digest(second);
        fs::write(session.root.join(&a), first).unwrap();
        fs::write(session.root.join(&b), second).unwrap();
        let whole = [first.as_slice(), second.as_slice(), first.as_slice()].concat();
        let mut entry = json!({"size":whole.len(),"sha256":rs_transfer::digest(&whole),"chunks":[
            {"sha256":a,"size_bytes":first.len()},{"sha256":b,"size_bytes":second.len()},{"sha256":a,"size_bytes":first.len()}]});
        let file = assembled_file(&session, &entry).unwrap();
        assert_eq!(fs::read(file.path.clone()).unwrap(), whole);
        entry["chunks"][0]["sha256"] = json!(b);
        assert!(
            assembled_file(&session, &entry).is_err(),
            "ordered complete digest must be verified"
        );
        entry["chunks"][0]["sha256"] = json!(a);
        fs::write(session.root.join(&b), b"broken").unwrap();
        assert!(
            assembled_file(&session, &entry).is_err(),
            "same-size corrupted block must fail"
        );
    }

    #[test]
    fn legacy_and_empty_files_keep_their_integrity_checks() {
        let root = std::env::temp_dir().join(format!(
            "restore-test-{}",
            rs_transfer::random_id().unwrap()
        ));
        fs::create_dir(&root).unwrap();
        let session = Session {
            root,
            expires: Instant::now() + Duration::from_secs(60),
            max_bytes: 16,
        };
        let sha = rs_transfer::digest(b"");
        fs::write(session.root.join(&sha), b"").unwrap();
        let empty = json!({"size":0,"sha256":sha});
        let tmp = assembled_file(&session, &empty).unwrap();
        assert_eq!(fs::metadata(&tmp.path).unwrap().len(), 0);
        let invalid = json!({"size":1,"sha256":sha,"chunks":[{"sha256":sha,"size_bytes":0}]});
        assert!(assembled_file(&session, &invalid).is_err());
    }
}
