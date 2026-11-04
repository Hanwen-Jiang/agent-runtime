//! 最小的 HTTP/1.1（第 10 节定的“自己写最小的 HTTP/1.1”，不引入 HTTP 库与异步运行时）：读请求头与响应头、按
//! Content-Length 或 chunked 读请求体、写带 Content-Length 的响应。每个连接只处理一个请求（响应都带 `Connection: close`）。
//!
//! 解析从严：头最多 16 KiB、100 行；不收 obs-fold、裸的 CR 与 LF、控制字符；Content-Length 只能有一个且只能是数字；
//! Transfer-Encoding 只收 `chunked`，且不能与 Content-Length 同时出现（防请求走私，转发给 agent 的请求体是原样的字节）。

use std::io::{self, Read, Write};

pub const MAX_HEAD: usize = 16 << 10;
pub const MAX_HEADERS: usize = 100;
const MAX_TARGET: usize = 8 << 10;

#[derive(Debug)]
pub enum HttpError {
    /// 对方在发任何字节之前就关了连接。
    Closed,
    /// 头或体超过上限。
    TooLarge,
    Bad(&'static str),
    Io(io::Error),
}

impl From<io::Error> for HttpError {
    fn from(e: io::Error) -> Self {
        HttpError::Io(e)
    }
}

impl std::fmt::Display for HttpError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            HttpError::Closed => f.write_str("连接已关闭"),
            HttpError::TooLarge => f.write_str("太大"),
            HttpError::Bad(m) => f.write_str(m),
            HttpError::Io(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for HttpError {}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Body {
    Empty,
    Length(u64),
    Chunked,
}

/// 请求头或响应头。请求的 method、target 有值；响应的 status 有值。
#[derive(Debug, Clone, Default)]
pub struct Head {
    pub method: String,
    pub target: String,
    pub status: u16,
    pub headers: Vec<(String, String)>,
}

impl Head {
    pub fn get(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }

    pub fn all(&self, name: &str) -> Vec<&str> {
        self.headers
            .iter()
            .filter(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
            .collect()
    }

    pub fn path(&self) -> &str {
        self.target
            .split_once('?')
            .map_or(self.target.as_str(), |(p, _)| p)
    }

    pub fn body(&self) -> Result<Body, HttpError> {
        let te = self.all("transfer-encoding");
        let cl = self.all("content-length");
        if !te.is_empty() {
            if !cl.is_empty() {
                return Err(HttpError::Bad(
                    "Content-Length 与 Transfer-Encoding 不能同时出现",
                ));
            }
            if te.len() != 1 || !te[0].eq_ignore_ascii_case("chunked") {
                return Err(HttpError::Bad("Transfer-Encoding 只支持 chunked"));
            }
            return Ok(Body::Chunked);
        }
        match cl.as_slice() {
            [] => Ok(Body::Empty),
            [v] if !v.is_empty() && v.len() <= 18 && v.bytes().all(|b| b.is_ascii_digit()) => {
                Ok(Body::Length(v.parse().unwrap_or(0)))
            }
            [_] => Err(HttpError::Bad("Content-Length 不是数字")),
            _ => Err(HttpError::Bad("Content-Length 只能有一个")),
        }
    }
}

fn is_tchar(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&b)
}

/// 带缓冲的连接：读头时多读到的字节留在缓冲里，之后的 read 先给它们。
pub struct Conn<S> {
    stream: S,
    buf: Vec<u8>,
    pos: usize,
}

impl<S: Read> Conn<S> {
    pub fn new(stream: S) -> Self {
        Conn {
            stream,
            buf: Vec::with_capacity(4096),
            pos: 0,
        }
    }

    pub fn get_ref(&self) -> &S {
        &self.stream
    }

    pub fn get_mut(&mut self) -> &mut S {
        &mut self.stream
    }

    /// 交出底下的流（先用 take_buffered 取走缓冲里的字节）。
    pub fn into_inner(self) -> S {
        self.stream
    }

    /// 取走缓冲里还没读的字节。
    pub fn take_buffered(&mut self) -> Vec<u8> {
        let rest = self.buf[self.pos..].to_vec();
        self.buf.clear();
        self.pos = 0;
        rest
    }

    fn fill(&mut self) -> io::Result<usize> {
        if self.pos > 0 && self.pos == self.buf.len() {
            self.buf.clear();
            self.pos = 0;
        }
        let mut chunk = [0u8; 4096];
        let n = self.stream.read(&mut chunk)?;
        self.buf.extend_from_slice(&chunk[..n]);
        Ok(n)
    }

    fn read_head_bytes(&mut self) -> Result<Vec<u8>, HttpError> {
        loop {
            // 头最多 16 KiB，每次从头找一遍也不贵。
            if let Some(i) = self.buf[self.pos..]
                .windows(4)
                .position(|w| w == b"\r\n\r\n")
            {
                if i > MAX_HEAD {
                    return Err(HttpError::TooLarge);
                }
                let head = self.buf[self.pos..self.pos + i].to_vec();
                self.pos += i + 4;
                return Ok(head);
            }
            if self.buf.len() - self.pos > MAX_HEAD {
                return Err(HttpError::TooLarge);
            }
            let had = self.buf.len() - self.pos;
            if self.fill()? == 0 {
                return Err(if had == 0 {
                    HttpError::Closed
                } else {
                    HttpError::Bad("连接在头中途断开")
                });
            }
        }
    }

    pub fn read_request(&mut self) -> Result<Head, HttpError> {
        let raw = self.read_head_bytes()?;
        let text = std::str::from_utf8(&raw).map_err(|_| HttpError::Bad("请求头不是 ASCII"))?;
        let mut lines = text.split("\r\n");
        let first = lines.next().unwrap_or("");
        let mut parts = first.split(' ');
        let (Some(method), Some(target), Some(version), None) =
            (parts.next(), parts.next(), parts.next(), parts.next())
        else {
            return Err(HttpError::Bad("请求行不对"));
        };
        if method.is_empty() || method.len() > 16 || !method.bytes().all(is_tchar) {
            return Err(HttpError::Bad("方法不对"));
        }
        if !target.starts_with('/')
            || target.len() > MAX_TARGET
            || !target.bytes().all(|b| (0x21..=0x7e).contains(&b))
        {
            return Err(HttpError::Bad("请求目标不对"));
        }
        if version != "HTTP/1.1" && version != "HTTP/1.0" {
            return Err(HttpError::Bad("只支持 HTTP/1.1"));
        }
        let headers = parse_headers(lines)?;
        Ok(Head {
            method: method.to_string(),
            target: target.to_string(),
            status: 0,
            headers,
        })
    }

    pub fn read_response(&mut self) -> Result<Head, HttpError> {
        let raw = self.read_head_bytes()?;
        let text = std::str::from_utf8(&raw).map_err(|_| HttpError::Bad("响应头不是 ASCII"))?;
        let mut lines = text.split("\r\n");
        let first = lines.next().unwrap_or("");
        let mut parts = first.splitn(3, ' ');
        let (Some(version), Some(code)) = (parts.next(), parts.next()) else {
            return Err(HttpError::Bad("状态行不对"));
        };
        if !version.starts_with("HTTP/1.")
            || code.len() != 3
            || !code.bytes().all(|b| b.is_ascii_digit())
        {
            return Err(HttpError::Bad("状态行不对"));
        }
        if first.bytes().any(|b| b < 0x20 && b != b'\t') {
            return Err(HttpError::Bad("状态行不对"));
        }
        let headers = parse_headers(lines)?;
        Ok(Head {
            method: String::new(),
            target: String::new(),
            status: code.parse().unwrap_or(0),
            headers,
        })
    }

    /// 读一行（到 LF 为止，去掉结尾的 CRLF）。
    pub fn read_line(&mut self, max: usize) -> Result<Vec<u8>, HttpError> {
        loop {
            if let Some(i) = self.buf[self.pos..].iter().position(|b| *b == b'\n') {
                let mut line = self.buf[self.pos..self.pos + i].to_vec();
                self.pos += i + 1;
                if line.last() == Some(&b'\r') {
                    line.pop();
                }
                return Ok(line);
            }
            if self.buf.len() - self.pos > max {
                return Err(HttpError::TooLarge);
            }
            let had = self.buf.len() - self.pos;
            if self.fill()? == 0 {
                return Err(if had == 0 {
                    HttpError::Closed
                } else {
                    HttpError::Bad("连接在一行中途断开")
                });
            }
        }
    }

    fn read_exact_into(&mut self, n: usize, out: &mut Vec<u8>) -> Result<(), HttpError> {
        let mut left = n;
        while left > 0 {
            if self.pos == self.buf.len() && self.fill()? == 0 {
                return Err(HttpError::Bad("连接在请求体中途断开"));
            }
            let take = left.min(self.buf.len() - self.pos);
            out.extend_from_slice(&self.buf[self.pos..self.pos + take]);
            self.pos += take;
            left -= take;
        }
        Ok(())
    }

    /// 把请求体读进内存（API 的 JSON；上限 limit）。chunked 的扩展与尾部字段读掉不用。
    pub fn read_body(&mut self, body: Body, limit: u64) -> Result<Vec<u8>, HttpError> {
        let mut out = vec![];
        match body {
            Body::Empty => {}
            Body::Length(n) => {
                if n > limit {
                    return Err(HttpError::TooLarge);
                }
                self.read_exact_into(n as usize, &mut out)?;
            }
            Body::Chunked => loop {
                let line = self.read_line(4096)?;
                let text =
                    std::str::from_utf8(&line).map_err(|_| HttpError::Bad("分块长度不对"))?;
                let size_text = text.split(';').next().unwrap_or("").trim();
                if size_text.is_empty() || size_text.len() > 15 {
                    return Err(HttpError::Bad("分块长度不对"));
                }
                let size = u64::from_str_radix(size_text, 16)
                    .map_err(|_| HttpError::Bad("分块长度不对"))?;
                if size == 0 {
                    while !self.read_line(4096)?.is_empty() {}
                    break;
                }
                if out.len() as u64 + size > limit {
                    return Err(HttpError::TooLarge);
                }
                self.read_exact_into(size as usize, &mut out)?;
                if !self.read_line(2)?.is_empty() {
                    return Err(HttpError::Bad("分块后面缺 CRLF"));
                }
            },
        }
        Ok(out)
    }
}

impl<S: Read> Read for Conn<S> {
    fn read(&mut self, out: &mut [u8]) -> io::Result<usize> {
        if self.pos < self.buf.len() {
            let n = out.len().min(self.buf.len() - self.pos);
            out[..n].copy_from_slice(&self.buf[self.pos..self.pos + n]);
            self.pos += n;
            return Ok(n);
        }
        self.stream.read(out)
    }
}

fn parse_headers<'a>(
    lines: impl Iterator<Item = &'a str>,
) -> Result<Vec<(String, String)>, HttpError> {
    let mut headers = vec![];
    for line in lines {
        if line.starts_with(' ') || line.starts_with('\t') {
            return Err(HttpError::Bad("不收折行的头"));
        }
        let Some((name, value)) = line.split_once(':') else {
            return Err(HttpError::Bad("头的格式不对"));
        };
        if name.is_empty() || !name.bytes().all(is_tchar) {
            return Err(HttpError::Bad("头的名字不对"));
        }
        let value = value.trim_matches(|c| c == ' ' || c == '\t');
        if !value
            .bytes()
            .all(|b| b == b'\t' || (0x20..=0x7e).contains(&b))
        {
            return Err(HttpError::Bad("头的值里有控制字符"));
        }
        headers.push((name.to_string(), value.to_string()));
        if headers.len() > MAX_HEADERS {
            return Err(HttpError::TooLarge);
        }
    }
    Ok(headers)
}

pub fn reason(status: u16) -> &'static str {
    match status {
        200 => "OK",
        201 => "Created",
        204 => "No Content",
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        409 => "Conflict",
        411 => "Length Required",
        413 => "Content Too Large",
        431 => "Request Header Fields Too Large",
        500 => "Internal Server Error",
        502 => "Bad Gateway",
        503 => "Service Unavailable",
        _ => "",
    }
}

/// 写完整的响应（带 Content-Length 与 Connection: close）。
pub fn write_response<W: Write>(
    w: &mut W,
    status: u16,
    headers: &[(&str, &str)],
    body: &[u8],
) -> io::Result<()> {
    let mut head = format!("HTTP/1.1 {status} {}\r\n", reason(status));
    for (k, v) in headers {
        head.push_str(&format!("{k}: {v}\r\n"));
    }
    if status != 204 {
        head.push_str(&format!("Content-Length: {}\r\n", body.len()));
    }
    head.push_str("Connection: close\r\n\r\n");
    let mut wire = head.into_bytes();
    if status != 204 {
        wire.extend_from_slice(body);
    }
    w.write_all(&wire)?;
    w.flush()
}

pub fn write_json<W: Write>(
    w: &mut W,
    status: u16,
    value: &serde_json::Value,
    extra: &[(&str, &str)],
) -> io::Result<()> {
    let body = serde_json::to_vec(value).unwrap_or_default();
    let mut headers = vec![("Content-Type", "application/json")];
    headers.extend_from_slice(extra);
    write_response(w, status, &headers, &body)
}

pub fn error_json(code: &str, message: &str) -> serde_json::Value {
    serde_json::json!({"code": code, "message": message})
}

#[cfg(test)]
mod tests {
    use super::*;

    fn req(raw: &str) -> Result<Head, HttpError> {
        Conn::new(raw.as_bytes()).read_request()
    }

    #[test]
    fn parses_a_request_and_keeps_the_body_buffered() {
        let mut c = Conn::new(&b"POST /v1/envs?x=1 HTTP/1.1\r\nHost: a\r\nContent-Length: 4\r\nX-A:  b c \r\n\r\nbodyEXTRA"[..]);
        let h = c.read_request().unwrap();
        assert_eq!(
            (h.method.as_str(), h.target.as_str(), h.path()),
            ("POST", "/v1/envs?x=1", "/v1/envs")
        );
        assert_eq!(h.get("x-a"), Some("b c"));
        assert_eq!(h.body().unwrap(), Body::Length(4));
        assert_eq!(c.read_body(Body::Length(4), 10).unwrap(), b"body");
        assert_eq!(c.take_buffered(), b"EXTRA");
    }

    #[test]
    fn rejects_smuggling_shapes() {
        for raw in [
            "POST / HTTP/1.1\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n",
            "POST / HTTP/1.1\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\n",
            "POST / HTTP/1.1\r\nTransfer-Encoding: chunked, gzip\r\n\r\n",
            "POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\nTransfer-Encoding: chunked\r\n\r\n",
            "POST / HTTP/1.1\r\nContent-Length: +1\r\n\r\n",
            "POST / HTTP/1.1\r\nContent-Length: 1,1\r\n\r\n",
        ] {
            let h = req(raw).unwrap();
            assert!(h.body().is_err(), "{raw:?}");
        }
        for raw in [
            "GET / HTTP/1.1\r\nX: a\r\n b\r\n\r\n",
            "GET / HTTP/1.1\r\nX : a\r\n\r\n",
            "GET / HTTP/1.1\r\nX: a\rb\r\n\r\n",
            "GET / HTTP/1.1\r\nX: a\nY: b\r\n\r\n",
            "GET  / HTTP/1.1\r\n\r\n",
            "GET http://x/ HTTP/1.1\r\n\r\n",
            "GET / HTTP/2\r\n\r\n",
            "GET /a b HTTP/1.1\r\n\r\n",
        ] {
            assert!(matches!(req(raw), Err(HttpError::Bad(_))), "{raw:?}");
        }
    }

    #[test]
    fn limits_head_size_and_header_count() {
        let big = format!("GET / HTTP/1.1\r\nX: {}\r\n\r\n", "a".repeat(MAX_HEAD));
        assert!(matches!(req(&big), Err(HttpError::TooLarge)));
        let many = format!(
            "GET / HTTP/1.1\r\n{}\r\n",
            "X: y\r\n".repeat(MAX_HEADERS + 1)
        );
        assert!(matches!(req(&many), Err(HttpError::TooLarge)));
        assert!(matches!(req(""), Err(HttpError::Closed)));
        assert!(matches!(
            req("GET / HTTP/1.1\r\nX: y"),
            Err(HttpError::Bad(_))
        ));
    }

    #[test]
    fn decodes_chunked_bodies_with_a_limit() {
        let mut c =
            Conn::new(&b"5;ext=1\r\nhello\r\n6\r\n world\r\n0\r\nTrailer: x\r\n\r\nNEXT"[..]);
        assert_eq!(c.read_body(Body::Chunked, 100).unwrap(), b"hello world");
        assert_eq!(c.take_buffered(), b"NEXT");
        let mut c = Conn::new(&b"5\r\nhello\r\n0\r\n\r\n"[..]);
        assert!(matches!(
            c.read_body(Body::Chunked, 4),
            Err(HttpError::TooLarge)
        ));
        let mut c = Conn::new(&b"zz\r\n"[..]);
        assert!(c.read_body(Body::Chunked, 100).is_err());
        let mut c = Conn::new(&b"5\r\nhelloXX0\r\n\r\n"[..]);
        assert!(c.read_body(Body::Chunked, 100).is_err());
    }

    #[test]
    fn parses_responses() {
        let mut c = Conn::new(&b"HTTP/1.1 204 No Content\r\nServer: x\r\n\r\n"[..]);
        let h = c.read_response().unwrap();
        assert_eq!(h.status, 204);
        assert_eq!(h.get("server"), Some("x"));
        assert!(
            Conn::new(&b"HTTP/1.1 20 OK\r\n\r\n"[..])
                .read_response()
                .is_err()
        );
    }

    #[test]
    fn writes_responses_with_length_and_close() {
        let mut out = vec![];
        write_json(&mut out, 404, &error_json("unknown_env", "x"), &[]).unwrap();
        let text = String::from_utf8(out).unwrap();
        assert!(text.starts_with("HTTP/1.1 404 Not Found\r\n"));
        assert!(text.contains("Connection: close\r\n"));
        assert!(text.ends_with("{\"code\":\"unknown_env\",\"message\":\"x\"}"));
    }
}
