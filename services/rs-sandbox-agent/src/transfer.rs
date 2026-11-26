//! Two-phase uploads. Prepared bytes live in agent memory: user code cannot replace a file between hash/sign/upload.
use crate::agent::{App, fail, resolve, write_json};
use crate::http::Ctx;
use serde_json::{Value, json};
use std::collections::HashMap;
use std::io::{self, Read, Seek, SeekFrom};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
const TTL: Duration = Duration::from_secs(600);
const MAX_PENDING: usize = 64;
const MAX_PENDING_BYTES: usize = 256 << 20;
struct Prepared {
    data: Arc<Vec<u8>>,
    reserved: usize,
    sum: String,
    expires: Instant,
    committed: Option<String>,
}
pub struct Transfers {
    entries: Arc<Mutex<HashMap<String, Arc<Mutex<Prepared>>>>>,
}
impl Default for Transfers {
    fn default() -> Self {
        let entries = Arc::new(Mutex::new(HashMap::<String, Arc<Mutex<Prepared>>>::new()));
        let weak = Arc::downgrade(&entries);
        std::thread::spawn(move || {
            loop {
                std::thread::sleep(Duration::from_secs(30));
                let Some(entries) = weak.upgrade() else { break };
                entries.lock().unwrap_or_else(|e| e.into_inner()).retain(|_, v| {
                    v.lock().unwrap_or_else(|e| e.into_inner()).expires > Instant::now()
                });
            }
        });
        Self { entries }
    }
}
pub struct OutputReservation {
    entries: Arc<Mutex<HashMap<String, Arc<Mutex<Prepared>>>>>,
    ids: [String; 2],
}
impl OutputReservation {
    pub fn fill(&self, index: usize, data: Vec<u8>) -> io::Result<Value> {
        let id = &self.ids[index];
        let entry = {
            let entries = self.entries.lock().unwrap_or_else(|e| e.into_inner());
            entries
                .get(id)
                .cloned()
                .ok_or_else(|| io::Error::other("output reservation missing"))?
        };
        let mut p = entry.lock().unwrap_or_else(|e| e.into_inner());
        if data.len() > p.reserved {
            return Err(io::Error::other("output reservation exceeded"));
        }
        p.sum = rs_transfer::digest(&data);
        p.data = Arc::new(data);
        p.reserved = 0;
        p.expires = Instant::now() + TTL;
        Ok(json!({"transfer_id":id,"sha256":p.sum,"size_bytes":p.data.len()}))
    }
}
impl Drop for OutputReservation {
    fn drop(&mut self) {
        let mut entries = self.entries.lock().unwrap_or_else(|e| e.into_inner());
        for id in &self.ids {
            if entries
                .get(id)
                .is_some_and(|p| p.lock().unwrap_or_else(|e| e.into_inner()).reserved > 0)
            {
                entries.remove(id);
            }
        }
    }
}
impl Transfers {
    pub fn reserve_outputs(
        &self,
        bytes: usize,
        run_for: Duration,
    ) -> io::Result<OutputReservation> {
        let mut entries = self.entries.lock().unwrap_or_else(|e| e.into_inner());
        let now = Instant::now();
        entries.retain(|_, p| p.lock().unwrap_or_else(|e| e.into_inner()).expires > now);
        let used: usize = entries
            .values()
            .map(|p| {
                let g = p.lock().unwrap_or_else(|e| e.into_inner());
                g.data.len() + g.reserved
            })
            .sum();
        if entries.len() + 2 > MAX_PENDING
            || used.saturating_add(bytes.saturating_mul(2)) > MAX_PENDING_BYTES
        {
            return Err(io::Error::other("output capacity reached before execution"));
        }
        let ids = [rs_transfer::random_id()?, rs_transfer::random_id()?];
        for id in &ids {
            entries.insert(
                id.clone(),
                Arc::new(Mutex::new(Prepared {
                    data: Arc::new(vec![]),
                    reserved: bytes,
                    sum: String::new(),
                    expires: now + run_for + TTL,
                    committed: None,
                })),
            );
        }
        Ok(OutputReservation {
            entries: self.entries.clone(),
            ids,
        })
    }
    pub fn prepare(&self, data: Vec<u8>) -> io::Result<Value> {
        if data.len() as u64 > rs_transfer::MAX_BLOB_BYTES {
            return Err(io::Error::other("transfer too large"));
        }
        let mut entries = self.entries.lock().unwrap_or_else(|e| e.into_inner());
        let now = Instant::now();
        entries.retain(|_, v| v.lock().unwrap_or_else(|e| e.into_inner()).expires > now);
        let used: usize = entries
            .values()
            .map(|v| {
                let g = v.lock().unwrap_or_else(|e| e.into_inner());
                g.data.len() + g.reserved
            })
            .sum();
        if entries.len() >= MAX_PENDING || used + data.len() > MAX_PENDING_BYTES {
            return Err(io::Error::other("transfer capacity reached"));
        }
        let id = rs_transfer::random_id()?;
        let sum = rs_transfer::digest(&data);
        let size = data.len();
        entries.insert(
            id.clone(),
            Arc::new(Mutex::new(Prepared {
                data: Arc::new(data),
                reserved: 0,
                sum: sum.clone(),
                expires: now + TTL,
                committed: None,
            })),
        );
        Ok(json!({"transfer_id":id,"sha256":sum,"size_bytes":size}))
    }
    pub fn bytes(&self, id: &str) -> io::Result<Arc<Vec<u8>>> {
        let entry = {
            let entries = self.entries.lock().unwrap_or_else(|e| e.into_inner());
            entries.get(id).cloned()
        };
        let entry = entry.ok_or_else(|| io::Error::other("unknown or expired transfer"))?;
        let v = entry.lock().unwrap_or_else(|e| e.into_inner());
        if v.expires <= Instant::now() {
            return Err(io::Error::other("unknown or expired transfer"));
        }
        Ok(v.data.clone())
    }
    fn commit(&self, id: &str, key: &str, req: rs_transfer::Request) -> io::Result<Value> {
        let entry = {
            let entries = self.entries.lock().unwrap_or_else(|e| e.into_inner());
            entries.get(id).cloned()
        };
        let entry = entry.ok_or_else(|| io::Error::other("unknown or expired transfer"))?;
        // Hold per-transfer lock across upload: parallel transfers proceed concurrently while repeat commits on one transfer stay serialized.
        let mut v = entry.lock().unwrap_or_else(|e| e.into_inner());
        if v.expires <= Instant::now() {
            return Err(io::Error::other("unknown or expired transfer"));
        }
        if key.is_empty() || key.len() > 2048 {
            return Err(io::Error::other("invalid object key"));
        }
        if let Some(done) = &v.committed {
            if done != key {
                return Err(io::Error::other("transfer already committed"));
            }
        } else {
            rs_transfer::upload(&req, &v.data)?;
            v.committed = Some(key.into());
        }
        Ok(json!({"object_key":key,"size_bytes":v.data.len(),"sha256":v.sum}))
    }
}
fn request(ctx: &mut Ctx<'_>) -> Option<Value> {
    ctx.body.set_limit(64 << 10);
    let data = ctx.body.read_to_end_limited().ok()?;
    serde_json::from_slice(&data).ok()
}
pub fn prepare(app: &App, ctx: &mut Ctx<'_>) {
    let Some(v) = request(ctx) else {
        return fail(ctx, 400, "invalid_request", "invalid transfer request");
    };
    let Some(path) = v["path"].as_str() else {
        return fail(ctx, 400, "invalid_path", "path required");
    };
    let result = (|| {
        let root = app.c.workdir.canonicalize()?;
        let path = resolve(&root, path.as_bytes())
            .map_err(io::Error::other)?
            .canonicalize()?;
        if !path.starts_with(&root) {
            return Err(io::Error::other("path outside workspace"));
        }
        let mut file = std::fs::File::open(&path)?;
        let md = file.metadata()?;
        let range = if v.get("offset").is_some() || v.get("length").is_some() {
            let offset = v["offset"]
                .as_u64()
                .ok_or_else(|| io::Error::other("invalid range"))?;
            let length = v["length"]
                .as_u64()
                .filter(|n| *n <= 4 << 20)
                .ok_or_else(|| io::Error::other("invalid range"))?;
            if offset > md.len() || length > md.len() - offset {
                return Err(io::Error::other("range outside file"));
            }
            Some((offset, length))
        } else {
            None
        };
        if !md.is_file()
            || (range.is_none() && md.len() > app.c.file_limit.min(rs_transfer::MAX_BLOB_BYTES))
        {
            return Err(io::Error::other("invalid file or file too large"));
        }
        #[cfg(target_os = "linux")]
        {
            use std::os::fd::AsRawFd;
            let actual = std::fs::read_link(format!("/proc/self/fd/{}", file.as_raw_fd()))?;
            if !actual.starts_with(&root) {
                return Err(io::Error::other("path outside workspace"));
            }
        }
        let mut data = Vec::new();
        if let Some((offset, length)) = range {
            file.seek(SeekFrom::Start(offset))?;
            file.take(length).read_to_end(&mut data)?;
            if data.len() as u64 != length {
                return Err(io::Error::other("file changed"));
            }
        } else {
            file.take(app.c.file_limit.min(rs_transfer::MAX_BLOB_BYTES) + 1)
                .read_to_end(&mut data)?;
        }
        if range.is_none() && data.len() as u64 > app.c.file_limit.min(rs_transfer::MAX_BLOB_BYTES)
        {
            return Err(io::Error::other("file grew beyond limit"));
        }
        app.transfers.prepare(data)
    })();
    match result {
        Ok(v) => write_json(ctx, 200, v.to_string()),
        Err(_) => fail(
            ctx,
            400,
            "transfer_prepare_failed",
            "cannot prepare bounded workspace file",
        ),
    }
}
pub fn commit(app: &App, ctx: &mut Ctx<'_>) {
    let Some(v) = request(ctx) else {
        return fail(ctx, 400, "invalid_request", "invalid transfer request");
    };
    let (Some(id), Some(key)) = (v["transfer_id"].as_str(), v["object_key"].as_str()) else {
        return fail(
            ctx,
            400,
            "invalid_request",
            "transfer_id and object_key required",
        );
    };
    let req = match serde_json::from_value::<rs_transfer::Request>(v["target"].clone()) {
        Ok(r) => r,
        Err(_) => return fail(ctx, 400, "invalid_request", "target required"),
    };
    match app.transfers.commit(id, key, req) {
        Ok(v) => write_json(ctx, 200, v.to_string()),
        Err(_) => fail(
            ctx,
            502,
            "transfer_upload_failed",
            "upload failed; prepared bytes retained for retry",
        ),
    }
}
pub fn release(app: &App, ctx: &mut Ctx<'_>) {
    let Some(v) = request(ctx) else {
        return fail(ctx, 400, "invalid_request", "invalid transfer request");
    };
    app.transfers
        .entries
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .remove(v["transfer_id"].as_str().unwrap_or(""));
    write_json(ctx, 200, "{}".into());
}
pub fn read(app: &App, ctx: &mut Ctx<'_>) {
    let Some(v) = request(ctx) else {
        return fail(ctx, 400, "invalid_request", "invalid transfer request");
    };
    match app.transfers.bytes(v["transfer_id"].as_str().unwrap_or("")) {
        Ok(data) => ctx.resp.send(
            200,
            &[("Content-Type", "application/octet-stream".into())],
            &data,
        ),
        Err(_) => fail(
            ctx,
            404,
            "transfer_not_found",
            "unknown or expired transfer",
        ),
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn prepared_bytes_are_immutable_and_bounded() {
        let transfers = Transfers::default();
        let data = b"original".to_vec();
        let prepared = transfers.prepare(data.clone()).unwrap();
        let id = prepared["transfer_id"].as_str().unwrap();
        let mut changed = data;
        changed[0] = b'X';
        assert_eq!(&**transfers.bytes(id).unwrap(), b"original");
        assert!(transfers.bytes("other").is_err());
        for _ in 1..MAX_PENDING {
            transfers.prepare(vec![]).unwrap();
        }
        assert!(transfers.prepare(vec![]).is_err());
    }
    #[test]
    fn expired_transfer_is_unavailable() {
        let t = Transfers::default();
        let p = t.prepare(vec![1]).unwrap();
        let id = p["transfer_id"].as_str().unwrap();
        let entry = t.entries.lock().unwrap().get(id).unwrap().clone();
        entry.lock().unwrap().expires = Instant::now() - Duration::from_secs(1);
        assert!(t.bytes(id).is_err());
    }
}
