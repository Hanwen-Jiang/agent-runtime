//! Two-phase uploads. Prepared bytes live in agent memory: user code cannot replace a file between hash/sign/upload.
use crate::agent::{App, fail, resolve, write_json};
use crate::http::Ctx;
use serde_json::{Value, json};
use std::collections::HashMap;
use std::io::{self, Read, Seek, SeekFrom};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
const TTL: Duration = Duration::from_secs(600);
const MAX_PENDING: usize = 32;
const MAX_PENDING_BYTES: usize = 256 << 20;
struct Prepared {
    data: Arc<Vec<u8>>,
    sum: String,
    expires: Instant,
    committed: Option<String>,
}
pub struct Transfers {
    entries: Arc<Mutex<HashMap<String, Prepared>>>,
}
impl Default for Transfers {
    fn default() -> Self {
        let entries = Arc::new(Mutex::new(HashMap::<String, Prepared>::new()));
        let weak = Arc::downgrade(&entries);
        std::thread::spawn(move || {
            loop {
                std::thread::sleep(Duration::from_secs(30));
                let Some(entries) = weak.upgrade() else { break };
                entries
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .retain(|_, v| v.expires > Instant::now());
            }
        });
        Self { entries }
    }
}
impl Transfers {
    pub fn prepare(&self, data: Vec<u8>) -> io::Result<Value> {
        if data.len() as u64 > rs_transfer::MAX_BLOB_BYTES {
            return Err(io::Error::other("transfer too large"));
        }
        let mut entries = self.entries.lock().unwrap_or_else(|e| e.into_inner());
        entries.retain(|_, v| v.expires > Instant::now());
        if entries.len() >= MAX_PENDING
            || entries.values().map(|v| v.data.len()).sum::<usize>() + data.len()
                > MAX_PENDING_BYTES
        {
            return Err(io::Error::other("transfer capacity reached"));
        }
        let id = rs_transfer::random_id()?;
        let sum = rs_transfer::digest(&data);
        let size = data.len();
        entries.insert(
            id.clone(),
            Prepared {
                data: Arc::new(data),
                sum: sum.clone(),
                expires: Instant::now() + TTL,
                committed: None,
            },
        );
        Ok(json!({"transfer_id":id,"sha256":sum,"size_bytes":size}))
    }
    pub fn bytes(&self, id: &str) -> io::Result<Arc<Vec<u8>>> {
        let entries = self.entries.lock().unwrap_or_else(|e| e.into_inner());
        entries
            .get(id)
            .filter(|v| v.expires > Instant::now())
            .map(|v| v.data.clone())
            .ok_or_else(|| io::Error::other("unknown or expired transfer"))
    }
    fn commit(&self, id: &str, key: &str, req: rs_transfer::Request) -> io::Result<Value> {
        // Hold lock across upload: repeat commits cannot race and use the same prepared bytes.
        let mut entries = self.entries.lock().unwrap_or_else(|e| e.into_inner());
        let v = entries
            .get_mut(id)
            .filter(|v| v.expires > Instant::now())
            .ok_or_else(|| io::Error::other("unknown or expired transfer"))?;
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
        t.entries.lock().unwrap().get_mut(id).unwrap().expires =
            Instant::now() - Duration::from_secs(1);
        assert!(t.bytes(id).is_err());
    }
}
