//! 最小的 HTTP/1.1 服务端（firecracker-backend.md 第 10 节：不引 HTTP 库与异步运行时，与 fc-manager 一样自己写）。
//! 行为照 Go 的 net/http（Go 版 agent 用的）：
//!
//! - 每个连接一个线程；keep-alive；请求头 10 秒内要读完（ReadHeaderTimeout），之后不限时；头最多 1 MiB。
//! - 请求体按 Content-Length 或 chunked 读（两者都有时按 chunked，Go 同样）；别的 Transfer-Encoding 501；
//!   `Expect: 100-continue` 在处理函数第一次读请求体时回 100，别的 Expect 417。
//! - 响应：正文不超过 2048 字节带 Content-Length，更长的分块（Go 的 bufferBeforeChunkingSize）；HEAD 不发正文；
//!   请求是 HTTP/1.0 或带 `Connection: close` 时回完就关。
//! - 处理函数没读完的请求体：剩下不超过 256 KiB 时读掉以复用连接，否则关连接（Go 的 maxPostHandlerReadBytes）。
//! - `peer_gone`：调用方断开（或只关了写的一半）时为真，命令执行用它杀进程组（docs/plan/running-task-interaction.md 第 2 节）。

use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const MAX_HEADER_BYTES: usize = (1 << 20) + 4096;
const READ_HEADER_TIMEOUT: Duration = Duration::from_secs(10);
const CHUNKING_THRESHOLD: usize = 2048;
const MAX_POST_HANDLER_READ: u64 = 256 << 10;

pub struct Request {
    pub method: String,
    /// 请求目标里的路径，保持转义（Go 的 EscapedPath）。
    pub path: String,
    /// `?` 之后的原样查询串。
    pub query: String,
    pub headers: Vec<(String, String)>,
    pub http10: bool,
}

impl Request {
    /// 第一个同名头的值（名字不分大小写，值去掉首尾空白，与 Go 的 Header.Get 一致）。
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Framing {
    Length(u64),
    Chunked,
}

/// 请求体。读超过 `limit` 时报 `BodyTooLarge`（Go 的 MaxBytesReader）。
pub struct Body<'a> {
    conn: &'a mut BufReader<TcpStream>,
    writer: &'a TcpStream,
    framing: Framing,
    remaining: u64,
    chunk_left: u64,
    done: bool,
    need_continue: bool,
    sent_continue: bool,
    limit: Option<u64>,
    read_total: u64,
    pub exceeded: bool,
    broken: bool,
}

/// 读请求体时超过了上限。
#[derive(Debug)]
pub struct BodyTooLarge;

impl std::fmt::Display for BodyTooLarge {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("http: request body too large")
    }
}

impl std::error::Error for BodyTooLarge {}

pub fn is_too_large(e: &io::Error) -> bool {
    e.get_ref().is_some_and(|inner| inner.is::<BodyTooLarge>())
}

impl Body<'_> {
    pub fn set_limit(&mut self, limit: u64) {
        self.limit = Some(limit);
    }

    fn raw_read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if self.done || buf.is_empty() {
            return Ok(0);
        }
        if self.need_continue && !self.sent_continue {
            self.sent_continue = true;
            let mut w = self.writer;
            w.write_all(b"HTTP/1.1 100 Continue\r\n\r\n")?;
        }
        match self.framing {
            Framing::Length(_) => {
                if self.remaining == 0 {
                    self.done = true;
                    return Ok(0);
                }
                let want = buf
                    .len()
                    .min(self.remaining.min(usize::MAX as u64) as usize);
                let n = self.conn.read(&mut buf[..want])?;
                if n == 0 {
                    self.broken = true;
                    return Err(io::Error::new(
                        io::ErrorKind::UnexpectedEof,
                        "unexpected EOF",
                    ));
                }
                self.remaining -= n as u64;
                if self.remaining == 0 {
                    self.done = true;
                }
                Ok(n)
            }
            Framing::Chunked => {
                if self.chunk_left == 0 {
                    let line = read_line(self.conn, 4096).inspect_err(|_| self.broken = true)?;
                    let size_str = line.split(';').next().unwrap_or("").trim();
                    let size = u64::from_str_radix(size_str, 16).map_err(|_| {
                        self.broken = true;
                        io::Error::new(io::ErrorKind::InvalidData, "malformed chunked encoding")
                    })?;
                    if size == 0 {
                        // 末尾的 trailer，到空行为止。
                        loop {
                            let l =
                                read_line(self.conn, 4096).inspect_err(|_| self.broken = true)?;
                            if l.is_empty() {
                                break;
                            }
                        }
                        self.done = true;
                        return Ok(0);
                    }
                    self.chunk_left = size;
                }
                let want = buf
                    .len()
                    .min(self.chunk_left.min(usize::MAX as u64) as usize);
                let n = self.conn.read(&mut buf[..want])?;
                if n == 0 {
                    self.broken = true;
                    return Err(io::Error::new(
                        io::ErrorKind::UnexpectedEof,
                        "unexpected EOF",
                    ));
                }
                self.chunk_left -= n as u64;
                if self.chunk_left == 0 {
                    let l = read_line(self.conn, 16).inspect_err(|_| self.broken = true)?;
                    if !l.is_empty() {
                        self.broken = true;
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidData,
                            "malformed chunked encoding",
                        ));
                    }
                }
                Ok(n)
            }
        }
    }

    /// 读完剩下的（不超过 limit），丢掉；返回是否读到了结尾。
    pub fn drain(&mut self) -> bool {
        let mut buf = [0u8; 16 << 10];
        loop {
            match self.read(&mut buf) {
                Ok(0) => return self.done,
                Ok(_) => {}
                Err(_) => return false,
            }
        }
    }

    pub fn read_to_end_limited(&mut self) -> io::Result<Vec<u8>> {
        let mut out = Vec::new();
        self.read_to_end(&mut out)?;
        Ok(out)
    }
}

impl Read for Body<'_> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if self.exceeded {
            return Err(io::Error::other(BodyTooLarge));
        }
        let Some(limit) = self.limit else {
            return self.raw_read(buf);
        };
        // 与 MaxBytesReader 一样多读一个字节，看是不是超了。
        let room = limit.saturating_sub(self.read_total);
        let want = buf
            .len()
            .min(room.saturating_add(1).min(usize::MAX as u64) as usize);
        let n = self.raw_read(&mut buf[..want])?;
        if self.read_total + n as u64 > limit {
            self.exceeded = true;
            let ok = (limit - self.read_total) as usize;
            self.read_total = limit;
            if ok > 0 {
                return Ok(ok);
            }
            return Err(io::Error::other(BodyTooLarge));
        }
        self.read_total += n as u64;
        Ok(n)
    }
}

fn read_line(r: &mut BufReader<TcpStream>, max: usize) -> io::Result<String> {
    let mut line = Vec::new();
    let n = r
        .by_ref()
        .take(max as u64 + 2)
        .read_until(b'\n', &mut line)?;
    if n == 0 {
        return Err(io::Error::new(
            io::ErrorKind::UnexpectedEof,
            "unexpected EOF",
        ));
    }
    if line.last() != Some(&b'\n') {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "line too long"));
    }
    line.pop();
    if line.last() == Some(&b'\r') {
        line.pop();
    }
    String::from_utf8(line).map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "not utf-8"))
}

/// 写响应。处理函数调一次 `send`、`send_stream`、`no_content` 之一；`abort` 让连接直接断开（已经开始写正文又写不下去时）。
pub struct Response<'a> {
    stream: &'a TcpStream,
    head_only: bool,
    http10: bool,
    pub close: bool,
    pub written: bool,
    pub aborted: bool,
}

fn status_text(code: u16) -> &'static str {
    match code {
        100 => "Continue",
        200 => "OK",
        204 => "No Content",
        307 => "Temporary Redirect",
        400 => "Bad Request",
        401 => "Unauthorized",
        404 => "Not Found",
        405 => "Method Not Allowed",
        409 => "Conflict",
        413 => "Request Entity Too Large",
        417 => "Expectation Failed",
        431 => "Request Header Fields Too Large",
        500 => "Internal Server Error",
        501 => "Not Implemented",
        505 => "HTTP Version Not Supported",
        _ => "",
    }
}

/// RFC 1123 的 GMT 时间（Go 的 http.TimeFormat）。
pub fn http_date(t: SystemTime) -> String {
    let secs = t
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0) as i64;
    let days = secs.div_euclid(86400);
    let rem = secs.rem_euclid(86400);
    let (h, m, s) = (rem / 3600, rem % 3600 / 60, rem % 60);
    // 1970-01-01 是星期四。
    let wd = ["Thu", "Fri", "Sat", "Sun", "Mon", "Tue", "Wed"][days.rem_euclid(7) as usize];
    // Howard Hinnant 的 civil_from_days。
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let mo = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(mo <= 2);
    let mon = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ][(mo - 1) as usize];
    format!("{wd}, {d:02} {mon} {y} {h:02}:{m:02}:{s:02} GMT")
}

impl<'a> Response<'a> {
    fn head(&self, status: u16, headers: &[(&str, String)], framing: Option<Framing>) -> String {
        let mut h = format!(
            "HTTP/1.{} {} {}\r\n",
            if self.http10 { 0 } else { 1 },
            status,
            status_text(status)
        );
        for (k, v) in headers {
            h.push_str(&format!("{k}: {v}\r\n"));
        }
        if self.close && !self.http10 {
            h.push_str("Connection: close\r\n");
        }
        h.push_str(&format!("Date: {}\r\n", http_date(SystemTime::now())));
        match framing {
            Some(Framing::Length(n)) => h.push_str(&format!("Content-Length: {n}\r\n")),
            Some(Framing::Chunked) => h.push_str("Transfer-Encoding: chunked\r\n"),
            None => {}
        }
        h.push_str("\r\n");
        h
    }

    /// 整个正文一次给出。
    pub fn send(&mut self, status: u16, headers: &[(&str, String)], body: &[u8]) {
        self.written = true;
        let mut w = self.stream;
        let body_allowed = status != 204 && status != 304 && status >= 200;
        if !body_allowed {
            let _ = w.write_all(self.head(status, headers, None).as_bytes());
            return;
        }
        let chunked = body.len() > CHUNKING_THRESHOLD && !self.head_only && !self.http10;
        if self.http10 && body.len() > CHUNKING_THRESHOLD {
            self.close = true;
        }
        if chunked {
            let mut out = self
                .head(status, headers, Some(Framing::Chunked))
                .into_bytes();
            out.extend_from_slice(format!("{:x}\r\n", body.len()).as_bytes());
            out.extend_from_slice(body);
            out.extend_from_slice(b"\r\n0\r\n\r\n");
            let _ = w.write_all(&out);
            return;
        }
        let framing = if self.head_only && body.is_empty() {
            None
        } else {
            Some(Framing::Length(body.len() as u64))
        };
        let mut out = self.head(status, headers, framing).into_bytes();
        if !self.head_only {
            out.extend_from_slice(body);
        }
        let _ = w.write_all(&out);
    }

    pub fn no_content(&mut self) {
        self.send(204, &[], b"");
    }

    /// 正文分块地写（长度事先不知道）；`f` 出错时断开连接，不写结尾的空块，对方看到的是不完整的正文。
    pub fn send_stream(
        &mut self,
        status: u16,
        headers: &[(&str, String)],
        f: impl FnOnce(&mut dyn Write) -> io::Result<()>,
    ) {
        self.written = true;
        if self.http10 {
            self.close = true;
        }
        let framing = if self.http10 || self.head_only {
            None
        } else {
            Some(Framing::Chunked)
        };
        let mut w = self.stream;
        if w.write_all(self.head(status, headers, framing).as_bytes())
            .is_err()
        {
            self.aborted = true;
            return;
        }
        if self.head_only {
            return;
        }
        let res = if self.http10 {
            let mut bw = io::BufWriter::with_capacity(32 << 10, w);
            f(&mut bw).and_then(|_| bw.flush())
        } else {
            let mut cw = Chunked {
                inner: io::BufWriter::with_capacity(32 << 10, w),
            };
            f(&mut cw).and_then(|_| cw.finish())
        };
        if res.is_err() {
            self.aborted = true;
        }
    }

    /// 带 Content-Length 的流式正文（读文件）：写不满 n 个字节时断开连接。
    pub fn send_sized(
        &mut self,
        status: u16,
        headers: &[(&str, String)],
        n: u64,
        src: &mut dyn Read,
    ) {
        self.written = true;
        let mut w = self.stream;
        if w.write_all(
            self.head(status, headers, Some(Framing::Length(n)))
                .as_bytes(),
        )
        .is_err()
        {
            self.aborted = true;
            return;
        }
        if self.head_only {
            return;
        }
        match io::copy(&mut src.take(n), &mut w) {
            Ok(copied) if copied == n => {}
            _ => self.aborted = true,
        }
    }
}

struct Chunked<'a> {
    inner: io::BufWriter<&'a TcpStream>,
}

impl Write for Chunked<'_> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        if buf.is_empty() {
            return Ok(0);
        }
        self.inner
            .write_all(format!("{:x}\r\n", buf.len()).as_bytes())?;
        self.inner.write_all(buf)?;
        self.inner.write_all(b"\r\n")?;
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}

impl Chunked<'_> {
    fn finish(mut self) -> io::Result<()> {
        self.inner.write_all(b"0\r\n\r\n")?;
        self.inner.flush()
    }
}

/// 处理函数拿到的上下文。
pub struct Ctx<'a> {
    pub req: Request,
    pub body: Body<'a>,
    pub resp: Response<'a>,
    peer: &'a TcpStream,
}

impl Ctx<'_> {
    /// 调用方断开了（或关了写的一半）。对方另发来的字节（流水线的下一个请求）不算断开。
    pub fn peer_gone(&self) -> bool {
        let mut b = [0u8; 1];
        if self
            .peer
            .set_read_timeout(Some(Duration::from_millis(1)))
            .is_err()
        {
            return true;
        }
        let gone = match self.peer.peek(&mut b) {
            Ok(0) => true,
            Ok(_) => false,
            Err(e) => !matches!(
                e.kind(),
                io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut | io::ErrorKind::Interrupted
            ),
        };
        let _ = self.peer.set_read_timeout(None);
        gone
    }
}

/// 一个连接上的请求循环。`handle` 处理一个请求；返回时响应要已经写完（没写的按 500 处理）。
pub fn serve_conn(stream: TcpStream, handle: &dyn Fn(&mut Ctx<'_>)) {
    let _ = stream.set_nodelay(true);
    let Ok(writer) = stream.try_clone() else {
        return;
    };
    let Ok(peer) = stream.try_clone() else { return };
    let mut reader = BufReader::with_capacity(16 << 10, stream);
    loop {
        // 空闲时不限时；第一个字节到了之后，头要在 10 秒内读完。
        if reader.get_ref().set_read_timeout(None).is_err() {
            return;
        }
        match reader.fill_buf() {
            Ok([]) | Err(_) => return,
            Ok(_) => {}
        }
        let _ = reader.get_ref().set_read_timeout(Some(READ_HEADER_TIMEOUT));
        let head = match read_head(&mut reader) {
            Ok(h) => h,
            Err(HeadError::Io) => return,
            Err(HeadError::Raw(resp)) => {
                let mut w = &writer;
                let _ = w.write_all(resp.as_bytes());
                return;
            }
        };
        let _ = reader.get_ref().set_read_timeout(None);
        let (req, framing, expect_continue) = head;
        let close_requested = req.http10
            && !req
                .header("Connection")
                .is_some_and(|v| v.eq_ignore_ascii_case("keep-alive"))
            || req
                .header("Connection")
                .is_some_and(|v| v.split(',').any(|t| t.trim().eq_ignore_ascii_case("close")));
        let head_only = req.method == "HEAD";
        let http10 = req.http10;
        let (remaining, done) = match framing {
            Framing::Length(n) => (n, n == 0),
            Framing::Chunked => (0, false),
        };
        let mut ctx = Ctx {
            req,
            body: Body {
                conn: &mut reader,
                writer: &writer,
                framing,
                remaining,
                chunk_left: 0,
                done,
                need_continue: expect_continue && !done,
                sent_continue: false,
                limit: None,
                read_total: 0,
                exceeded: false,
                broken: false,
            },
            resp: Response {
                stream: &writer,
                head_only,
                http10,
                close: close_requested,
                written: false,
                aborted: false,
            },
            peer: &peer,
        };
        handle(&mut ctx);
        if !ctx.resp.written {
            ctx.resp.send(500, &[], b"");
        }
        if ctx.resp.aborted || ctx.resp.close || ctx.body.exceeded || ctx.body.broken {
            let _ = writer.shutdown(std::net::Shutdown::Both);
            return;
        }
        // 没读完的请求体：没发过 100 的不读（对方可能不发了），剩下太多的不读，都直接关连接。
        if !ctx.body.done {
            if ctx.body.need_continue && !ctx.body.sent_continue {
                let _ = writer.shutdown(std::net::Shutdown::Both);
                return;
            }
            let small = matches!(ctx.body.framing, Framing::Length(_))
                && ctx.body.remaining <= MAX_POST_HANDLER_READ;
            ctx.body.limit = None;
            if !small || !ctx.body.drain() {
                let _ = writer.shutdown(std::net::Shutdown::Both);
                return;
            }
        }
    }
}

enum HeadError {
    Io,
    /// 直接写回去的整个响应（之后关连接），与 Go 的 net/http 在这几种情况下写的一样。
    Raw(String),
}

/// Go 对坏请求的回法：状态行与正文是同一句，`Connection: close`。
fn plain(text: &str) -> HeadError {
    HeadError::Raw(format!(
        "HTTP/1.1 {text}\r\nContent-Type: text/plain; charset=utf-8\r\nConnection: close\r\n\r\n{text}"
    ))
}

fn is_token(s: &str) -> bool {
    !s.is_empty()
        && s.bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&c))
}

fn read_head(r: &mut BufReader<TcpStream>) -> Result<(Request, Framing, bool), HeadError> {
    let bad = || plain("400 Bad Request");
    let mut total = 0usize;
    let mut lines: Vec<String> = Vec::new();
    loop {
        let mut line = Vec::new();
        let n = r
            .by_ref()
            .take((MAX_HEADER_BYTES - total + 1) as u64)
            .read_until(b'\n', &mut line)
            .map_err(|_| HeadError::Io)?;
        if n == 0 {
            return Err(HeadError::Io);
        }
        total += n;
        if line.last() != Some(&b'\n') {
            if total > MAX_HEADER_BYTES {
                return Err(plain("431 Request Header Fields Too Large"));
            }
            return Err(HeadError::Io);
        }
        line.pop();
        if line.last() == Some(&b'\r') {
            line.pop();
        }
        if line.is_empty() {
            if lines.is_empty() {
                continue; // 请求之间多出来的空行，Go 同样跳过
            }
            break;
        }
        lines.push(String::from_utf8(line).map_err(|_| bad())?);
    }
    let mut parts = lines[0].splitn(3, ' ');
    let (Some(method), Some(target), Some(proto)) = (parts.next(), parts.next(), parts.next())
    else {
        return Err(bad());
    };
    if !is_token(method) {
        return Err(bad());
    }
    let http10 = match proto {
        "HTTP/1.1" => false,
        "HTTP/1.0" => true,
        p if p.starts_with("HTTP/") => return Err(plain("505 HTTP Version Not Supported")),
        _ => return Err(bad()),
    };
    let mut headers = Vec::new();
    for l in &lines[1..] {
        let Some((k, v)) = l.split_once(':') else {
            return Err(bad());
        };
        if !is_token(k) || v.bytes().any(|c| c < 0x20 && c != b'\t' || c == 0x7f) {
            return Err(bad());
        }
        headers.push((k.to_string(), v.trim_matches([' ', '\t']).to_string()));
    }
    // 请求目标：origin-form、absolute-form（取路径）或 `*`。
    let (path, query) = if target == "*" {
        ("*".to_string(), String::new())
    } else {
        let t = if let Some(rest) = target
            .strip_prefix("http://")
            .or_else(|| target.strip_prefix("https://"))
        {
            rest.find('/').map_or("/", |i| &rest[i..])
        } else if target.starts_with('/') {
            target
        } else {
            return Err(bad());
        };
        let (p, q) = t.split_once('?').unwrap_or((t, ""));
        if crate::gostr::path_unescape(p).is_none() {
            return Err(bad());
        }
        (p.to_string(), q.to_string())
    };
    let req = Request {
        method: method.to_string(),
        path,
        query,
        headers,
        http10,
    };
    let hosts = req
        .headers
        .iter()
        .filter(|(k, _)| k.eq_ignore_ascii_case("Host"))
        .count();
    if !http10 && hosts == 0 {
        return Err(plain("400 Bad Request: missing required Host header"));
    }
    if hosts > 1 {
        return Err(plain("400 Bad Request: too many Host headers"));
    }
    let te: Vec<&str> = req
        .headers
        .iter()
        .filter(|(k, _)| k.eq_ignore_ascii_case("Transfer-Encoding"))
        .map(|(_, v)| v.as_str())
        .collect();
    let framing = if !te.is_empty() {
        if te.len() != 1 || !te[0].eq_ignore_ascii_case("chunked") || http10 {
            return Err(HeadError::Raw(
                "HTTP/1.1 501 Not Implemented\r\nContent-Type: text/plain; charset=utf-8\r\nConnection: close\r\n\r\nUnsupported transfer encoding"
                    .into(),
            ));
        }
        Framing::Chunked
    } else {
        let cls: Vec<&str> = req
            .headers
            .iter()
            .filter(|(k, _)| k.eq_ignore_ascii_case("Content-Length"))
            .map(|(_, v)| v.as_str())
            .collect();
        match cls.first() {
            None => Framing::Length(0),
            Some(first) => {
                if cls.iter().any(|c| c != first)
                    || first.is_empty()
                    || !first.bytes().all(|c| c.is_ascii_digit())
                {
                    return Err(bad());
                }
                Framing::Length(first.parse().map_err(|_| bad())?)
            }
        }
    };
    let expect = match req.header("Expect") {
        None | Some("") => false,
        Some(v) if v.eq_ignore_ascii_case("100-continue") && !http10 => true,
        Some(_) if http10 => false,
        Some(_) => {
            return Err(HeadError::Raw(format!(
                "HTTP/1.1 417 Expectation Failed\r\nConnection: close\r\nDate: {}\r\nContent-Length: 0\r\n\r\n",
                http_date(SystemTime::now())
            )));
        }
    };
    Ok((req, framing, expect))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dates_like_go() {
        assert_eq!(http_date(UNIX_EPOCH), "Thu, 01 Jan 1970 00:00:00 GMT");
        assert_eq!(
            http_date(UNIX_EPOCH + Duration::from_secs(1_793_819_536)),
            "Wed, 04 Nov 2026 19:12:16 GMT"
        );
        assert_eq!(
            http_date(UNIX_EPOCH + Duration::from_secs(951_782_400)),
            "Tue, 29 Feb 2000 00:00:00 GMT"
        );
    }
}
