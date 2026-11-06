//! Single-object HTTPS transfer shared by sandbox-agent and fc-manager.
//! curl supplies system TLS verification, proxy support and bounded transfer deadlines.
//! Signed URLs/headers are passed through stdin configuration, never argv, logs or errors.
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

pub const MAX_BLOB_BYTES: u64 = 64 << 20;
#[derive(Clone, Serialize, Deserialize)]
pub struct Request {
    pub url: String,
    #[serde(default)]
    pub headers: BTreeMap<String, String>,
    #[serde(default)]
    pub proxy: String,
}
impl std::fmt::Debug for Request {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Request { credentials: [REDACTED] }")
    }
}
fn err(message: &str) -> io::Error {
    io::Error::other(message)
}
pub fn digest(data: &[u8]) -> String {
    Sha256::digest(data)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}
pub fn valid_digest(sum: &str) -> bool {
    sum.len() == 64
        && sum
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
pub fn random_id() -> io::Result<String> {
    let mut bytes = [0u8; 16];
    File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}
pub struct TempFile {
    pub path: PathBuf,
}
impl TempFile {
    pub fn new() -> io::Result<Self> {
        for _ in 0..8 {
            let path = std::env::temp_dir().join(format!("ar-transfer-{}", random_id()?));
            let mut o = OpenOptions::new();
            o.write(true).create_new(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                o.mode(0o600);
            }
            match o.open(&path) {
                Ok(_) => return Ok(Self { path }),
                Err(e) if e.kind() == io::ErrorKind::AlreadyExists => continue,
                Err(e) => return Err(e),
            }
        }
        Err(err("cannot allocate transfer file"))
    }
}
impl Drop for TempFile {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}
fn escaped(v: &str) -> io::Result<String> {
    if v.bytes().any(|b| b < 32 || b == 127) {
        return Err(err("invalid transfer parameter"));
    }
    Ok(v.replace('\\', "\\\\").replace('"', "\\\""))
}
fn validate_url(url: &str) -> io::Result<()> {
    if url.len() > 16_384 || url.contains('#') {
        return Err(err("invalid transfer URL"));
    }
    let (scheme, rest) = url
        .split_once("://")
        .ok_or_else(|| err("invalid transfer URL"))?;
    let authority = rest.split(['/', '?']).next().unwrap_or("");
    if authority.is_empty() || authority.contains('@') || authority.contains('\\') {
        return Err(err("invalid transfer URL"));
    }
    if scheme != "https"
        && !(scheme == "http"
            && (authority.starts_with("127.0.0.1:") || authority.starts_with("[::1]:")))
    {
        return Err(err("HTTPS required for object transfers"));
    }
    escaped(url)?;
    Ok(())
}
fn config(req: &Request) -> io::Result<String> {
    validate_url(&req.url)?;
    let mut cfg = format!("url = \"{}\"\n", escaped(&req.url)?);
    if req.headers.len() > 32 {
        return Err(err("too many transfer headers"));
    }
    for (k, v) in &req.headers {
        if k.is_empty()
            || !k.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
            || k.eq_ignore_ascii_case("host")
            || k.eq_ignore_ascii_case("transfer-encoding")
        {
            return Err(err("invalid transfer header"));
        }
        cfg.push_str(&format!("header = \"{}: {}\"\n", k, escaped(v)?));
    }
    if !req.proxy.is_empty() {
        if !req.proxy.starts_with("http://") {
            return Err(err("invalid transfer proxy"));
        }
        cfg.push_str(&format!("proxy = \"{}\"\n", escaped(&req.proxy)?));
    }
    Ok(cfg)
}
fn perform(req: &Request, method: &str, file: &Path, size: u64) -> io::Result<()> {
    if size > MAX_BLOB_BYTES {
        return Err(err("transfer too large"));
    }
    let cfg = config(req)?;
    let mut cmd = Command::new("/usr/bin/curl");
    cmd.env_clear()
        .env("PATH", "/usr/bin:/bin")
        .args([
            "--disable",
            "--silent",
            "--globoff",
            "--fail",
            "--proto",
            "=http,https",
            "--connect-timeout",
            "10",
            "--max-time",
            "120",
            "--config",
            "-",
            "--request",
            method,
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    if method == "PUT" {
        cmd.arg("--upload-file")
            .arg(file)
            .args(["--output", "/dev/null"]);
    } else {
        cmd.arg("--output")
            .arg(file)
            .arg("--max-filesize")
            .arg(size.max(1).to_string());
    }
    let mut child = cmd
        .spawn()
        .map_err(|_| err("transfer client unavailable"))?;
    let written = child
        .stdin
        .take()
        .ok_or_else(|| err("transfer pipe unavailable"))?
        .write_all(cfg.as_bytes());
    if written.is_err() {
        let _ = child.kill();
        let _ = child.wait();
        return Err(err("transfer setup failed"));
    }
    let status = child.wait().map_err(|_| err("transfer wait failed"))?;
    if !status.success() {
        return Err(err("object transfer rejected or unavailable"));
    }
    Ok(())
}
pub fn download(req: &Request, size: u64, sum: &str) -> io::Result<Vec<u8>> {
    if !valid_digest(sum) {
        return Err(err("invalid sha256"));
    }
    let tmp = TempFile::new()?;
    perform(req, "GET", &tmp.path, size)?;
    let mut data = Vec::new();
    File::open(&tmp.path)?
        .take(size.saturating_add(1))
        .read_to_end(&mut data)?;
    if data.len() as u64 != size || digest(&data) != sum {
        return Err(err("download size or sha256 mismatch"));
    }
    Ok(data)
}
pub fn upload(req: &Request, data: &[u8]) -> io::Result<()> {
    let tmp = TempFile::new()?;
    fs::write(&tmp.path, data)?;
    perform(req, "PUT", &tmp.path, data.len() as u64)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_credential_leaks_and_unsafe_targets() {
        let mut r = Request {
            url: "https://example.test/blob?secret=123".into(),
            headers: BTreeMap::new(),
            proxy: String::new(),
        };
        assert!(!format!("{r:?}").contains("123"));
        assert!(config(&r).is_ok());
        for url in [
            "http://169.254.169.254/a",
            "file:///etc/passwd",
            "https://x@host/a",
            "https://host/a\nheader = bad",
        ] {
            r.url = url.into();
            assert!(config(&r).is_err())
        }
        r.url = "https://example.test/x".into();
        r.headers
            .insert("X-Value".into(), "value\r\nX-Evil: 1".into());
        assert!(config(&r).is_err());
    }
    #[test]
    fn actual_http_bytes_upload_and_hash_checked_download() {
        use std::io::BufRead;
        use std::net::TcpListener;
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/blob", listener.local_addr().unwrap());
        let server = std::thread::spawn(move || {
            for method in ["PUT", "GET", "GET"] {
                let (mut stream, _) = listener.accept().unwrap();
                stream
                    .set_read_timeout(Some(std::time::Duration::from_secs(5)))
                    .unwrap();
                let mut reader = io::BufReader::new(stream.try_clone().unwrap());
                let mut first = String::new();
                reader.read_line(&mut first).unwrap();
                assert!(first.starts_with(method));
                let mut size = 0;
                loop {
                    let mut line = String::new();
                    reader.read_line(&mut line).unwrap();
                    if line == "\r\n" {
                        break;
                    }
                    if line.to_ascii_lowercase().starts_with("content-length:") {
                        size = line
                            .split(':')
                            .nth(1)
                            .unwrap()
                            .trim()
                            .parse::<usize>()
                            .unwrap()
                    }
                }
                if method == "PUT" {
                    let mut data = vec![0; size];
                    reader.read_exact(&mut data).unwrap();
                    assert_eq!(data, b"object bytes");
                    stream
                        .write_all(
                            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                        )
                        .unwrap();
                } else {
                    stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\nConnection: close\r\n\r\nobject bytes").unwrap();
                }
            }
        });
        let req = Request {
            url,
            headers: BTreeMap::new(),
            proxy: String::new(),
        };
        upload(&req, b"object bytes").unwrap();
        assert_eq!(
            download(&req, 12, &digest(b"object bytes")).unwrap(),
            b"object bytes"
        );
        assert!(download(&req, 12, &digest(b"other bytes!")).is_err());
        server.join().unwrap();
    }
}
