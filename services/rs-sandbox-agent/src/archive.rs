//! 工作区的存档与恢复、清单、列目录与搜索（D-06a；语义同 Go 版 archive.go 与开发实现 agentruntime_core.dev.workdir）。
//!
//! ```text
//! GET  /v1/archive?max_bytes=N   工作目录打成 tar（普通文件、目录、符号链接；只留权限位）；文件总大小超过 N 时 413
//! PUT  /v1/archive?max_bytes=N   把 tar 解到工作目录：工作目录要是空的（409）；绝对路径、..、反斜杠、硬链接、设备文件、
//!                                指出工作目录的符号链接、经符号链接写进去的路径一律 400；超过 N 字节 413
//! GET  /v1/manifest              逐项的清单：路径、类型、权限位、大小、sha256、链接目标（按路径的字符串顺序排序）
//! GET  /v1/list/<相对路径>        列一个目录（不写为工作目录本身；不跟随符号链接），最多 1000 项
//! POST /v1/grep                  {"pattern", "path", "max_matches"}：按行搜索（Go 的 RE2 语法，goregex.rs），跳过任何一级 .git、
//!                                二进制与超过 1 MiB 的文件
//! ```
//!
//! 解包不交给 tar 库的默认行为（unpack）：逐项自己核对名字、类型、上级目录与链接目标，再自己建文件（D-28 难点与防错）。

use std::fs::{self, Metadata};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

use crate::agent::{App, bytes_path, fail, has_drive, resolve, write_json};
use crate::files::create_dirs;
use crate::gostr;
use crate::http::{Ctx, is_too_large};
use crate::json::{self, Obj};

/// agent 自己在工作目录里放的文件（适配器建环境时写的空文件），不进存档与清单。
const MARKER: &[u8] = b".agentruntime";
const MAX_GREP_FILE: u64 = 1 << 20;
const MAX_GREP_MATCHES: i64 = 1000;
const MAX_LIST_ENTRIES: usize = 1000;

// --- 遍历 ---

/// 本机的文件名 → 字节（Unix 上原样；Windows 上转成 UTF-8）。
fn name_bytes(name: &std::ffi::OsStr) -> Vec<u8> {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        name.as_bytes().to_vec()
    }
    #[cfg(not(unix))]
    {
        name.to_string_lossy().into_owned().into_bytes()
    }
}

/// 权限位（Go 的 FileMode.Perm()；Windows 上照 Go 的算法：只读 0444，否则 0666，目录另加 0111）。
fn perm(md: &Metadata) -> u32 {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        md.permissions().mode() & 0o777
    }
    #[cfg(not(unix))]
    {
        let base = if md.permissions().readonly() {
            0o444
        } else {
            0o666
        };
        if md.is_dir() { base | 0o111 } else { base }
    }
}

#[derive(PartialEq, Eq)]
pub enum Flow {
    Continue,
    SkipDir,
    Stop,
}

/// 一个目录按名字（字节序）排好的项。
fn read_dir_sorted(dir: &Path) -> io::Result<Vec<(Vec<u8>, PathBuf)>> {
    let mut items = Vec::new();
    for e in fs::read_dir(dir)? {
        let e = e?;
        items.push((name_bytes(&e.file_name()), e.path()));
    }
    items.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(items)
}

/// walk 的回调：(相对路径, 本机路径, lstat 的结果)。
type Visit<'a> = dyn FnMut(&[u8], &Path, &Metadata) -> io::Result<Flow> + 'a;

/// 按路径的字典序深度优先遍历工作目录（不跟随符号链接，与 Go 的 filepath.WalkDir 一样），跳过 agent 的标记文件与设备、
/// 管道、套接字。f 返回 SkipDir 时不进这个目录，Stop 时停下（walk 返回 Ok(true)）。
fn walk(root: &Path, f: &mut Visit<'_>) -> io::Result<bool> {
    fn inner(dir: &Path, prefix: &[u8], f: &mut Visit<'_>) -> io::Result<bool> {
        for (name, path) in read_dir_sorted(dir)? {
            let rel = if prefix.is_empty() {
                name.clone()
            } else {
                [prefix, b"/", &name].concat()
            };
            if rel == MARKER {
                continue;
            }
            let md = fs::symlink_metadata(&path)?;
            let ft = md.file_type();
            if !ft.is_file() && !ft.is_dir() && !ft.is_symlink() {
                continue;
            }
            match f(&rel, &path, &md)? {
                Flow::Stop => return Ok(true),
                Flow::SkipDir => continue,
                Flow::Continue => {}
            }
            if ft.is_dir() && inner(&path, &rel, f)? {
                return Ok(true);
            }
        }
        Ok(false)
    }
    inner(root, b"", f)
}

/// 链接目标 target 从 rel 那里看，是否还在工作目录里（与 Go 版 linkInside 相同）。
fn link_inside(rel: &[u8], target: &[u8]) -> bool {
    if target.is_empty()
        || target.starts_with(b"/")
        || target.contains(&b'\\')
        || target.contains(&0)
        || has_drive(target)
    {
        return false;
    }
    let (Ok(rel), Ok(target)) = (std::str::from_utf8(rel), std::str::from_utf8(target)) else {
        return link_inside_bytes(rel, target);
    };
    let joined = gostr::clean(&gostr::join(&gostr::dir(rel), target));
    joined != ".." && !joined.starts_with("../")
}

/// 名字里有不合法的 UTF-8 时：按字节做同样的规范化（Go 的 path 包本来就按字节处理）。
fn link_inside_bytes(rel: &[u8], target: &[u8]) -> bool {
    let to = |b: &[u8]| b.iter().map(|&c| char::from(c)).collect::<String>();
    let joined = gostr::clean(&gostr::join(&gostr::dir(&to(rel)), &to(target)));
    joined != ".." && !joined.starts_with("../")
}

fn read_link_bytes(p: &Path) -> io::Result<Vec<u8>> {
    let t = fs::read_link(p)?;
    let b = name_bytes(t.as_os_str());
    #[cfg(not(unix))]
    let b = b
        .into_iter()
        .map(|c| if c == b'\\' { b'/' } else { c })
        .collect();
    Ok(b)
}

fn query_limit(ctx: &Ctx<'_>) -> Result<u64, &'static str> {
    let Some(v) = gostr::query_get(&ctx.req.query, "max_bytes").filter(|v| !v.is_empty()) else {
        return Err("要给 max_bytes");
    };
    match v.parse::<i64>() {
        Ok(n) if n > 0 => Ok(n as u64),
        _ => Err("max_bytes 要是正整数"),
    }
}

// --- 导出 ---

pub fn export(app: &App, ctx: &mut Ctx<'_>) {
    let limit = match query_limit(ctx) {
        Ok(n) => n,
        Err(msg) => return fail(ctx, 400, "invalid_request", msg),
    };
    let root = app.c.workdir.clone();
    let mut total: u64 = 0;
    let counted = walk(&root, &mut |_, _, md| {
        if md.is_file() {
            total += md.len();
        }
        Ok(Flow::Continue)
    });
    if counted.is_err() {
        return fail(ctx, 500, "read_failed", "读不了工作目录");
    }
    if total > limit {
        return fail(
            ctx,
            413,
            "too_large",
            &format!("工作目录有 {total} 字节，超过上限 {limit}"),
        );
    }
    ctx.resp
        .send_stream(200, &[("Content-Type", "application/x-tar".into())], |w| {
            write_tar(&root, w)
        });
}

fn write_tar(root: &Path, w: &mut dyn Write) -> io::Result<()> {
    let mut b = tar::Builder::new(w);
    b.mode(tar::HeaderMode::Complete);
    walk(root, &mut |rel, path, md| {
        let mut h = tar::Header::new_gnu();
        h.set_mode(perm(md));
        h.set_mtime(
            md.modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map_or(0, |d| d.as_secs()),
        );
        h.set_uid(0);
        h.set_gid(0);
        let rel_path = bytes_path(rel);
        let ft = md.file_type();
        if ft.is_dir() {
            h.set_entry_type(tar::EntryType::Directory);
            h.set_size(0);
            b.append_data(&mut h, &rel_path, io::empty())?;
        } else if ft.is_symlink() {
            let target = match read_link_bytes(path) {
                Ok(t) if link_inside(rel, &t) => t,
                _ => return Ok(Flow::Continue), // 指出工作目录的链接不进存档
            };
            h.set_entry_type(tar::EntryType::Symlink);
            h.set_size(0);
            b.append_link(&mut h, &rel_path, bytes_path(&target))?;
        } else {
            let size = md.len();
            h.set_entry_type(tar::EntryType::Regular);
            h.set_size(size);
            let f = fs::File::open(path)?;
            // 导出中途文件变短：写不满就是错，连接断开，Worker 当作暂时性的失败重试。
            let mut src = ExactReader {
                inner: f.take(size),
                left: size,
            };
            b.append_data(&mut h, &rel_path, &mut src)?;
        }
        Ok(Flow::Continue)
    })?;
    b.finish()
}

/// 正好读 n 个字节，少了报错。
struct ExactReader<R> {
    inner: R,
    left: u64,
}

impl<R: Read> Read for ExactReader<R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if self.left == 0 || buf.is_empty() {
            return Ok(0);
        }
        let n = self.inner.read(buf)?;
        if n == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "文件在导出时变短了",
            ));
        }
        self.left -= n as u64;
        Ok(n)
    }
}

// --- 导入 ---

enum ImportError {
    TooLarge,
    Bad(&'static str, &'static str),
    Invalid,
}

impl From<io::Error> for ImportError {
    fn from(e: io::Error) -> Self {
        if is_too_large(&e) {
            ImportError::TooLarge
        } else {
            ImportError::Invalid
        }
    }
}

pub fn import(app: &App, ctx: &mut Ctx<'_>) {
    let limit = match query_limit(ctx) {
        Ok(n) => n,
        Err(msg) => return fail(ctx, 400, "invalid_request", msg),
    };
    ctx.body.set_limit(limit + limit / 8 + (1 << 20));
    // 拒绝时先把请求体读完（不超过上面的上限）再回：对方还在发的时候断开连接，它只看到连接断了，看不到 400。
    let entries = match fs::read_dir(&app.c.workdir) {
        Ok(e) => e,
        Err(_) => {
            ctx.body.drain();
            return fail(ctx, 500, "read_failed", "读不了工作目录");
        }
    };
    for e in entries {
        let name = e.map(|e| name_bytes(&e.file_name())).unwrap_or_default();
        if name != MARKER {
            ctx.body.drain();
            return fail(
                ctx,
                409,
                "not_empty",
                "工作目录不是空的：只能把存档导进新建的环境",
            );
        }
    }
    let res = extract(&app.c.workdir, &mut ctx.body, limit);
    let exceeded = ctx.body.exceeded;
    if res.is_err() {
        ctx.body.drain();
    }
    match res {
        Ok(()) => {
            ctx.body.drain();
            ctx.resp.no_content()
        }
        Err(_) if exceeded => fail(ctx, 413, "too_large", "存档超过上限"),
        Err(ImportError::TooLarge) => fail(ctx, 413, "too_large", "存档超过上限"),
        Err(ImportError::Bad(code, message)) => fail(ctx, 400, code, message),
        Err(ImportError::Invalid) => fail(ctx, 400, "invalid_archive", "不是合法的 tar"),
    }
}

/// tar 里的名字 → 相对工作目录的规范路径（与 Go 版 clean 相同）。
fn clean_name(name: &[u8]) -> Result<Vec<u8>, ImportError> {
    if name.is_empty()
        || name.contains(&b'\\')
        || name.contains(&0)
        || name.starts_with(b"/")
        || has_drive(name)
    {
        return Err(ImportError::Bad(
            "invalid_path",
            "存档里有绝对路径或不合法的名字",
        ));
    }
    let trimmed = name.strip_suffix(b"/").unwrap_or(name);
    let as_str: String = trimmed.iter().map(|&c| char::from(c)).collect();
    let rel = gostr::clean(&as_str);
    if rel == ".." || rel.starts_with("../") {
        return Err(ImportError::Bad("invalid_path", "存档里有出工作目录的路径"));
    }
    Ok(rel.chars().map(|c| c as u8).collect())
}

/// rel 的每一级上级目录要么不存在、要么是真目录（不是符号链接）：不能经链接写到工作目录外面。
fn parents_are_dirs(root: &Path, rel: &[u8]) -> Result<(), ImportError> {
    let Some(pos) = rel.iter().rposition(|&c| c == b'/') else {
        return Ok(());
    };
    let dir = &rel[..pos];
    let mut cur = root.to_path_buf();
    for part in dir.split(|&c| c == b'/') {
        cur.push(bytes_path(part));
        match fs::symlink_metadata(&cur) {
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                create_dirs(&root.join(bytes_path(dir))).map_err(|_| ImportError::Invalid)?;
                return Ok(());
            }
            Err(_) => return Err(ImportError::Invalid),
            Ok(md) if !md.is_dir() => {
                return Err(ImportError::Bad(
                    "invalid_path",
                    "存档里的路径经过了符号链接或文件",
                ));
            }
            Ok(_) => {}
        }
    }
    Ok(())
}

fn set_perm(path: &Path, mode: u32) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(path, fs::Permissions::from_mode(mode));
    }
    #[cfg(not(unix))]
    {
        let _ = (path, mode);
    }
}

fn extract(root: &Path, body: &mut dyn Read, limit: u64) -> Result<(), ImportError> {
    let mut archive = tar::Archive::new(body);
    let mut total: u64 = 0;
    for entry in archive.entries()? {
        let mut entry = entry?;
        let raw_type = entry.header().entry_type().as_byte();
        let name = entry.path_bytes().into_owned();
        let rel = clean_name(&name)?;
        if rel == b"." || rel == MARKER {
            continue;
        }
        parents_are_dirs(root, &rel)?;
        let target = root.join(bytes_path(&rel));
        let perm = entry.header().mode().map_err(|_| ImportError::Invalid)? & 0o777;
        // 老格式的普通文件（'\0'）名字以 / 结尾时是目录（Go 的 TypeRegA 规则）。
        let kind = match raw_type {
            b'\0' if name.ends_with(b"/") => b'5',
            b'\0' => b'0',
            k => k,
        };
        match kind {
            b'5' => {
                let made = mkdir(&target, perm | 0o700);
                if let Err(e) = made
                    && e.kind() != io::ErrorKind::AlreadyExists
                {
                    return Err(ImportError::Invalid);
                }
                match fs::symlink_metadata(&target) {
                    Ok(md) if md.is_dir() => {}
                    _ => {
                        return Err(ImportError::Bad(
                            "invalid_path",
                            "存档里的目录与已有的项重名",
                        ));
                    }
                }
                set_perm(&target, perm | 0o700);
            }
            b'0' => {
                let size = entry.size();
                total = total.saturating_add(size);
                if total > limit {
                    return Err(ImportError::TooLarge);
                }
                let mut f = create_new(&target, perm | 0o600)
                    .map_err(|_| ImportError::Bad("invalid_path", "存档里有重名的文件"))?;
                let copied = io::copy(&mut entry, &mut f)?;
                if copied != size {
                    return Err(ImportError::Invalid);
                }
                drop(f);
                set_perm(&target, perm);
            }
            b'2' => {
                let link = entry
                    .link_name_bytes()
                    .map(|l| l.into_owned())
                    .unwrap_or_default();
                if !link_inside(&rel, &link) {
                    return Err(ImportError::Bad(
                        "invalid_link",
                        "存档里的符号链接指出了工作目录",
                    ));
                }
                if symlink(&bytes_path(&link), &target).is_err() {
                    return Err(ImportError::Bad(
                        "symlink_unsupported",
                        "这里建不了符号链接",
                    ));
                }
            }
            _ => {
                return Err(ImportError::Bad(
                    "unsupported_entry",
                    "存档里只能有普通文件、目录与符号链接",
                ));
            }
        }
    }
    Ok(())
}

fn mkdir(path: &Path, mode: u32) -> io::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        fs::DirBuilder::new().mode(mode).create(path)
    }
    #[cfg(not(unix))]
    {
        let _ = mode;
        fs::create_dir(path)
    }
}

fn create_new(path: &Path, mode: u32) -> io::Result<fs::File> {
    let mut o = fs::OpenOptions::new();
    o.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        o.mode(mode);
    }
    #[cfg(not(unix))]
    let _ = mode;
    o.open(path)
}

fn symlink(target: &Path, link: &Path) -> io::Result<()> {
    #[cfg(unix)]
    {
        std::os::unix::fs::symlink(target, link)
    }
    #[cfg(windows)]
    {
        std::os::windows::fs::symlink_file(target, link)
    }
}

// --- 清单、列目录 ---

pub fn manifest(app: &App, ctx: &mut Ctx<'_>) {
    manifest_with_chunks(app, ctx, false);
}
pub fn snapshot(app: &App, ctx: &mut Ctx<'_>) {
    manifest_with_chunks(app, ctx, true);
}
fn manifest_with_chunks(app: &App, ctx: &mut Ctx<'_>, chunked: bool) {
    let mut out: Vec<(Vec<u8>, String)> = Vec::new();
    let res = walk(&app.c.workdir, &mut |rel, path, md| {
        let ft = md.file_type();
        let e = if ft.is_dir() {
            Obj::new()
                .b("path", rel)
                .s("type", "dir")
                .i("mode", perm(md) as i64)
                .done()
        } else if ft.is_symlink() {
            let target = read_link_bytes(path)?;
            let mut o = Obj::new().b("path", rel).s("type", "symlink").i("mode", 0);
            if !target.is_empty() {
                o = o.b("link", &target);
            }
            o.done()
        } else {
            let mut f = fs::File::open(path)?;
            let mut h = Sha256::new();
            let mut chunks = vec![];
            let mut n: u64 = 0;
            loop {
                let block_size = if chunked { 4 << 20 } else { 64 << 10 };
                let mut part = Vec::with_capacity(block_size);
                let k = (&mut f).take(block_size as u64).read_to_end(&mut part)?;
                if k == 0 {
                    break;
                }
                h.update(&part);
                n += k as u64;
                if chunked {
                    chunks.push(
                        serde_json::json!({"sha256":rs_transfer::digest(&part),"size_bytes":k}),
                    );
                }
            }
            let sum: String = h.finalize().iter().map(|b| format!("{b:02x}")).collect();
            let mut o = Obj::new()
                .b("path", rel)
                .s("type", "file")
                .i("mode", perm(md) as i64);
            if n > 0 {
                o = o.i("size", n as i64);
            }
            if !chunks.is_empty() {
                o = o.raw("chunks", &serde_json::Value::Array(chunks).to_string());
            }
            o.s("sha256", &sum).done()
        };
        out.push((rel.to_vec(), e));
        Ok(Flow::Continue)
    });
    if res.is_err() {
        return fail(ctx, 500, "read_failed", "读不了工作目录");
    }
    // 遍历是深度优先的（父目录在前）；清单按路径的字符串顺序排，与开发实现一致。
    out.sort_by(|a, b| a.0.cmp(&b.0));
    let body = Obj::new()
        .raw("entries", &json::array(out.into_iter().map(|(_, e)| e)))
        .done();
    write_json(ctx, 200, body);
}

pub fn list(app: &App, ctx: &mut Ctx<'_>, rel: &[u8]) {
    let mut target = app.c.workdir.clone();
    let at_root = rel.is_empty();
    if !at_root {
        match resolve(&app.c.workdir, rel) {
            Ok(t) => target = t,
            Err(msg) => return fail(ctx, 400, "invalid_path", msg),
        }
    }
    // 不跟随符号链接（与开发实现一致）：链接到别处的目录不当目录列。
    if !fs::symlink_metadata(&target).is_ok_and(|m| m.is_dir()) {
        return fail(ctx, 404, "dir_not_found", "没有这个目录");
    }
    let Ok(items) = read_dir_sorted(&target) else {
        return fail(ctx, 404, "dir_not_found", "没有这个目录");
    };
    let mut out: Vec<(Vec<u8>, String)> = Vec::new();
    for (name, path) in items {
        if at_root && name == MARKER {
            continue;
        }
        let Ok(md) = fs::symlink_metadata(&path) else {
            continue;
        };
        let ft = md.file_type();
        let e = if ft.is_dir() {
            Obj::new().b("name", &name).s("type", "dir").done()
        } else if ft.is_file() {
            let mut o = Obj::new().b("name", &name).s("type", "file");
            if md.len() > 0 {
                o = o.i("size", md.len() as i64);
            }
            o.done()
        } else if ft.is_symlink() {
            Obj::new().b("name", &name).s("type", "symlink").done()
        } else {
            continue;
        };
        out.push((name, e));
        if out.len() >= MAX_LIST_ENTRIES {
            break;
        }
    }
    out.sort_by(|a, b| a.0.cmp(&b.0));
    write_json(
        ctx,
        200,
        Obj::new()
            .raw("entries", &json::array(out.into_iter().map(|(_, e)| e)))
            .done(),
    );
}

// --- 搜索 ---

pub fn grep(app: &App, ctx: &mut Ctx<'_>) {
    // 与 Go 版一样只读前 64 KiB（LimitReader），读到的是不是完整的 JSON 由解析决定。
    let mut body = Vec::new();
    let _ = (&mut ctx.body).take(1 << 16).read_to_end(&mut body);
    let parsed = json::parse_first(&body).and_then(|v| {
        let m = json::object(&v).ok()?;
        Some((
            json::get_string(json::field(m, "pattern")).ok()?,
            json::get_string(json::field(m, "path")).ok()?,
            json::get_i64(json::field(m, "max_matches")).ok()?,
        ))
    });
    let Some((pattern, path, max)) = parsed.filter(|(p, _, _)| !p.is_empty()) else {
        return fail(
            ctx,
            400,
            "invalid_request",
            "要 {pattern, path, max_matches}",
        );
    };
    let Ok(re) = crate::goregex::compile(&pattern) else {
        return fail(ctx, 400, "invalid_pattern", "正则表达式不合法（RE2 语法）");
    };
    let limit = if max <= 0 {
        100
    } else {
        max.min(MAX_GREP_MATCHES)
    } as usize;
    let mut prefix = String::new();
    if !path.is_empty() && path != "." {
        if let Err(msg) = resolve(&app.c.workdir, path.as_bytes()) {
            return fail(ctx, 400, "invalid_path", msg);
        }
        prefix = gostr::clean(&path);
    }
    let prefix = prefix.into_bytes();
    let mut matches: Vec<String> = Vec::new();
    let mut truncated = false;
    let res = walk(&app.c.workdir, &mut |rel, p, md| {
        let ft = md.file_type();
        if ft.is_dir() && (p.file_name().is_some_and(|n| n == ".git") || rel == b".git") {
            return Ok(Flow::SkipDir);
        }
        if !prefix.is_empty()
            && rel != prefix.as_slice()
            && !rel.starts_with(&[prefix.as_slice(), b"/"].concat())
        {
            return Ok(Flow::Continue);
        }
        if !ft.is_file() || md.len() > MAX_GREP_FILE || rel.starts_with(b".git/") {
            return Ok(Flow::Continue);
        }
        let Ok(data) = fs::read(p) else {
            return Ok(Flow::Continue);
        };
        if data[..data.len().min(1024)].contains(&0) {
            return Ok(Flow::Continue); // 二进制文件
        }
        let mut lines: Vec<&[u8]> = data.split(|&c| c == b'\n').collect();
        if lines.last().is_some_and(|l| l.is_empty()) {
            lines.pop();
        }
        for (i, line) in lines.into_iter().enumerate() {
            let line = line.strip_suffix(b"\r").unwrap_or(line);
            let text = gostr::decode_lossy(line);
            if !re.is_match(text.as_bytes()) {
                continue;
            }
            let mut o = Obj::new().b("path", rel).i("line", i as i64 + 1);
            if text.chars().count() > 500 {
                let cut: String = text.chars().take(500).collect();
                o = o.s("text", &cut);
            } else {
                o = o.b("text", line);
            }
            matches.push(o.done());
            if matches.len() >= limit {
                truncated = true;
                return Ok(Flow::Stop);
            }
        }
        Ok(Flow::Continue)
    });
    if res.is_err() {
        return fail(ctx, 500, "read_failed", "读不了工作目录");
    }
    let body = Obj::new()
        .raw("matches", &json::array(matches))
        .bool("truncated", truncated)
        .done();
    write_json(ctx, 200, body);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn links_inside() {
        assert!(link_inside(b"a", b"b"));
        assert!(link_inside(b"d/ln", b"../x"));
        assert!(!link_inside(b"d/ln", b"../../x"));
        assert!(!link_inside(b"out", b"/etc"));
        assert!(!link_inside(b"out", b""));
        assert!(!link_inside(b"out", b"sub/../.."));
        assert!(!link_inside(b"out", b"a\\b"));
        assert!(link_inside(b"out", b"\xff"));
    }

    #[test]
    fn names_like_go() {
        assert_eq!(clean_name(b"./a/").ok(), Some(b"a".to_vec()));
        assert_eq!(clean_name(b"d/sub/../z").ok(), Some(b"d/z".to_vec()));
        assert_eq!(clean_name(b"./").ok(), Some(b".".to_vec()));
        for bad in [
            &b"/x"[..],
            b"../x",
            b"a/../../x",
            b"..",
            b"a\\b",
            b"C:/x",
            b"",
        ] {
            assert!(clean_name(bad).is_err(), "{bad:?}");
        }
        assert_eq!(
            clean_name(b"\xff/x").ok(),
            Some(b"\xff/x".to_vec()),
            "不合法的 UTF-8 按字节保留"
        );
    }
}

/// Shared secure extraction for the direct block restore path.
pub(crate) fn restore_tar(root: &Path, source: &mut dyn Read, limit: u64) -> io::Result<()> {
    for entry in fs::read_dir(root)? {
        if name_bytes(&entry?.file_name()) != MARKER {
            return Err(io::Error::other("workspace not empty"));
        }
    }
    extract(root, source, limit).map_err(|_| io::Error::other("invalid restore archive"))
}
