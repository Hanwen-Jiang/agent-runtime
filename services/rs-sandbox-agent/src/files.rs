//! PUT、GET /v1/files/<相对路径>（语义同 Go 版 writeFile、readFile）。

use std::io::ErrorKind;

use crate::agent::{App, fail, resolve};
use crate::http::{Ctx, is_too_large};

pub fn write_file(app: &App, ctx: &mut Ctx<'_>, rel: &[u8]) {
    let target = match resolve(&app.c.workdir, rel) {
        Ok(t) => t,
        Err(msg) => return fail(ctx, 400, "invalid_path", msg),
    };
    ctx.body.set_limit(app.c.file_limit);
    let data = match ctx.body.read_to_end_limited() {
        Ok(d) => d,
        Err(e) if is_too_large(&e) => return fail(ctx, 413, "too_large", "文件太大"),
        Err(_) => return fail(ctx, 400, "invalid_request", "读不完请求体"),
    };
    if let Some(parent) = target.parent()
        && create_dirs(parent).is_err()
    {
        return fail(ctx, 400, "invalid_path", "建不了上级目录");
    }
    if write_new_or_truncate(&target, &data).is_err() {
        return fail(ctx, 400, "invalid_path", "写不了这个文件");
    }
    ctx.resp.no_content();
}

/// os.MkdirAll(dir, 0o755)。
pub fn create_dirs(dir: &std::path::Path) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        std::fs::DirBuilder::new()
            .recursive(true)
            .mode(0o755)
            .create(dir)
    }
    #[cfg(not(unix))]
    std::fs::create_dir_all(dir)
}

/// os.WriteFile(path, data, 0o644)：新建时 0644（受 umask），已有的截断、权限不变。
fn write_new_or_truncate(path: &std::path::Path, data: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    let mut o = std::fs::OpenOptions::new();
    o.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        o.mode(0o644);
    }
    o.open(path)?.write_all(data)
}

pub fn read_file(app: &App, ctx: &mut Ctx<'_>, rel: &[u8]) {
    let target = match resolve(&app.c.workdir, rel) {
        Ok(t) => t,
        Err(msg) => return fail(ctx, 400, "invalid_path", msg),
    };
    let mut f = match std::fs::File::open(&target) {
        Ok(f) => f,
        Err(e) if e.kind() == ErrorKind::NotFound => {
            return fail(ctx, 404, "not_found", "文件不存在");
        }
        Err(_) => return fail(ctx, 500, "read_failed", "读不了这个文件"),
    };
    let md = match f.metadata() {
        Ok(m) if m.is_file() => m,
        _ => return fail(ctx, 404, "not_found", "文件不存在"),
    };
    let n = md.len();
    ctx.resp.send_sized(
        200,
        &[("Content-Type", "application/octet-stream".into())],
        n,
        &mut f,
    );
}
