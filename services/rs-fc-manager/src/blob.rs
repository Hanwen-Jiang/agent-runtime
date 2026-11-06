//! Firecracker per-VM block transport over vsock 1082 (guest loopback port 3130).
//! Only the authenticated control plane can issue grants. Guest requests cannot choose an upstream URL/key.
use crate::blob_cache::BlobCache;
use crate::http::{self, Conn};
use serde_json::{Value, json};
use std::collections::HashMap;
use std::io;
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

const TTL: Duration = Duration::from_secs(300);
const MAX_GRANTS: usize = 8192;
struct Grant {
    env: String,
    method: String,
    sum: String,
    size: u64,
    target: rs_transfer::Request,
    expires: Instant,
    uploaded: bool,
}
struct Service {
    cache: BlobCache,
    grants: Mutex<HashMap<String, Arc<Mutex<Grant>>>>,
}
static SERVICE: OnceLock<Service> = OnceLock::new();
pub fn initialize(root: PathBuf) -> io::Result<()> {
    if SERVICE.get().is_some() {
        return Ok(());
    }
    let cap = std::env::var("AR_FC_BLOB_CACHE_BYTES")
        .ok()
        .map(|v| v.parse::<u64>())
        .transpose()
        .map_err(|_| io::Error::other("invalid blob cache capacity"))?
        .unwrap_or(10 << 30);
    let service = Service {
        cache: BlobCache::new(root, cap)?,
        grants: Mutex::new(HashMap::new()),
    };
    let _ = SERVICE.set(service);
    Ok(())
}
pub fn register(env: &str, body: &Value) -> Result<Value, &'static str> {
    let service = SERVICE.get().ok_or("block service unavailable")?;
    let requests = body["objects"]
        .as_array()
        .filter(|v| !v.is_empty() && v.len() <= 256)
        .ok_or("objects must contain 1..256 grants")?;
    let mut pending = vec![];
    for item in requests {
        let method = item["method"]
            .as_str()
            .filter(|m| *m == "GET" || *m == "PUT")
            .ok_or("invalid grant method")?
            .to_string();
        let sum = item["sha256"]
            .as_str()
            .filter(|v| rs_transfer::valid_digest(v))
            .ok_or("invalid grant checksum")?
            .to_string();
        let size = item["size_bytes"]
            .as_u64()
            .filter(|s| *s <= rs_transfer::MAX_BLOB_BYTES)
            .ok_or("invalid grant size")?;
        let target: rs_transfer::Request =
            serde_json::from_value(item["target"].clone()).map_err(|_| "invalid grant target")?;
        if !target.url.starts_with("https://") || !target.proxy.is_empty() {
            return Err("host grants require direct HTTPS");
        }
        pending.push((
            rs_transfer::random_id().map_err(|_| "grant unavailable")?,
            Grant {
                env: env.into(),
                method,
                sum,
                size,
                target,
                expires: Instant::now() + TTL,
                uploaded: false,
            },
        ));
    }
    let mut grants = service.grants.lock().unwrap_or_else(|e| e.into_inner());
    grants.retain(|_, g| g.lock().unwrap_or_else(|e| e.into_inner()).expires > Instant::now());
    if grants.len() + pending.len() > MAX_GRANTS {
        return Err("block grant capacity reached");
    }
    let mut out = vec![];
    for (id, g) in pending {
        out.push(json!({"url":format!("http://127.0.0.1:3130/v1/blob/{id}"),"headers":{},"sha256":g.sum,"size_bytes":g.size}));
        grants.insert(id, Arc::new(Mutex::new(g)));
    }
    Ok(json!({"objects":out}))
}
pub fn serve(env: &str, stream: UnixStream) {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(30)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(120)));
    let mut conn = Conn::new(stream);
    let result = (|| -> Result<(u16, Vec<u8>), u16> {
        let head = conn.read_request().map_err(|_| 400u16)?;
        let id = head
            .path()
            .strip_prefix("/v1/blob/")
            .filter(|v| v.len() == 32 && v.bytes().all(|b| b.is_ascii_hexdigit()))
            .ok_or(404u16)?;
        let service = SERVICE.get().ok_or(503u16)?;
        let grant = service
            .grants
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(id)
            .cloned()
            .ok_or(404u16)?;
        let mut grant = grant.lock().unwrap_or_else(|e| e.into_inner());
        if grant.env != env || grant.expires <= Instant::now() || grant.method != head.method {
            return Err(403);
        }
        if head.method == "GET" {
            let data = service
                .cache
                .get_or_fetch(&grant.sum, grant.size, || {
                    rs_transfer::download(&grant.target, grant.size, &grant.sum)
                })
                .map_err(|_| 502u16)?;
            Ok((200, data))
        } else {
            let body = head.body().map_err(|_| 400u16)?;
            let data = conn
                .read_body(body, grant.size.saturating_add(1))
                .map_err(|_| 413u16)?;
            if data.len() as u64 != grant.size || rs_transfer::digest(&data) != grant.sum {
                return Err(400);
            }
            if !grant.uploaded {
                rs_transfer::upload(&grant.target, &data).map_err(|_| 502u16)?;
                grant.uploaded = true;
            }
            // Write-through: cache only after durable upload succeeded.
            let _ = service
                .cache
                .get_or_fetch(&grant.sum, grant.size, || Ok(data));
            Ok((204, vec![]))
        }
    })();
    let (status, data) =
        result.unwrap_or_else(|status| (status, b"block transfer unavailable".to_vec()));
    let _ = http::write_response(
        conn.get_mut(),
        status,
        &[("Content-Type", "application/octet-stream")],
        &data,
    );
}

pub fn stats() -> Value {
    use std::sync::atomic::Ordering;
    match SERVICE.get() {
        Some(s) => json!({"enabled":true,"hits":s.cache.hits.load(Ordering::Relaxed),
            "misses":s.cache.misses.load(Ordering::Relaxed),"corruptions":s.cache.corruptions.load(Ordering::Relaxed),
            "evictions":s.cache.evictions.load(Ordering::Relaxed)}),
        None => json!({"enabled":false}),
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    fn request(env: &str, method: &str, path: &str) -> Vec<u8> {
        let (mut client, server) = UnixStream::pair().unwrap();
        let env = env.to_string();
        let thread = std::thread::spawn(move || serve(&env, server));
        write!(
            client,
            "{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n"
        )
        .unwrap();
        client.shutdown(std::net::Shutdown::Write).unwrap();
        let mut out = vec![];
        client.read_to_end(&mut out).unwrap();
        thread.join().unwrap();
        out
    }
    #[test]
    fn scoped_grants_guard_cached_bytes_and_expiry() {
        let root =
            std::env::temp_dir().join(format!("blob-scope-{}", rs_transfer::random_id().unwrap()));
        initialize(root.clone()).unwrap();
        let sum = rs_transfer::digest(b"authorized bytes");
        let plan = json!({"objects":[{"method":"GET","sha256":sum,"size_bytes":16,
            "target":{"url":"https://invalid.example/never-fetched","headers":{}}}]});
        let out = register("env-a", &plan).unwrap();
        let url = out["objects"][0]["url"].as_str().unwrap();
        let path = url.strip_prefix("http://127.0.0.1:3130").unwrap();
        SERVICE
            .get()
            .unwrap()
            .cache
            .get_or_fetch(&sum, 16, || Ok(b"authorized bytes".to_vec()))
            .unwrap();
        assert!(request("env-b", "GET", path).starts_with(b"HTTP/1.1 403"));
        assert!(request("env-a", "PUT", path).starts_with(b"HTTP/1.1 403"));
        let hit = request("env-a", "GET", path);
        assert!(hit.starts_with(b"HTTP/1.1 200"));
        assert!(hit.ends_with(b"authorized bytes"));
        let id = path.strip_prefix("/v1/blob/").unwrap();
        SERVICE
            .get()
            .unwrap()
            .grants
            .lock()
            .unwrap()
            .get(id)
            .unwrap()
            .lock()
            .unwrap()
            .expires = Instant::now() - Duration::from_secs(1);
        assert!(request("env-a", "GET", path).starts_with(b"HTTP/1.1 403"));
        assert_eq!(stats()["hits"], 1);
        assert!(register("env-a",&json!({"objects":[{"method":"GET","sha256":sum,"size_bytes":16,"target":{"url":"http://127.0.0.1:1"}}]})).is_err());
        let _ = std::fs::remove_dir_all(root);
    }
}
