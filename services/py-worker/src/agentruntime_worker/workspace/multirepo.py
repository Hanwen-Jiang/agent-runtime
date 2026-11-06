"""D-25: repository metadata and operations within one workspace lifecycle."""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import re
from typing import Any

from agentruntime_core.errors import ConflictError, DeniedError, InvalidError, NotFoundError
from agentruntime_core.providers.relational_store import Migration
from agentruntime_worker.sandbox.times import iso

MIGRATIONS = (Migration(213, "workspace_multirepo", (
    "ALTER TABLE workspaces ADD COLUMN layout TEXT NOT NULL DEFAULT 'root'",
    "ALTER TABLE workspace_pushes ADD COLUMN repo TEXT NOT NULL DEFAULT ''",
    """CREATE TABLE workspace_repos (
        workspace_id TEXT NOT NULL, name TEXT NOT NULL, url TEXT NOT NULL, ref TEXT NOT NULL,
        path TEXT NOT NULL, access TEXT NOT NULL, is_primary BIGINT NOT NULL DEFAULT 0,
        head TEXT NOT NULL DEFAULT '', branch TEXT NOT NULL DEFAULT '', pushed_head TEXT NOT NULL DEFAULT '', attached_at TEXT NOT NULL,
        operation_id TEXT NOT NULL DEFAULT '', PRIMARY KEY (workspace_id, name), UNIQUE (workspace_id, path)
    )""",
)),)
selected: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("workspace_repo", default=None)
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def path(rel: str) -> str:
    repo = selected.get()
    return f"{repo['path']}/{rel}" if repo else rel


def public(row: dict[str, Any]) -> dict[str, Any]:
    return {"name": row["name"], "url": row["url"], "ref": row["ref"], "path": row["path"],
            "access": "REPO_ACCESS_WRITE" if row["access"] == "write" else "REPO_ACCESS_READ",
            "primary": bool(row["is_primary"]), "head": row["head"], "branch": row["branch"], "attached_at": row["attached_at"]}


async def rows(ops: Any, owner: str, ws: str) -> list[dict[str, Any]]:
    await ops._archiver.migrate()
    async with ops._archiver._db(owner).transaction(read_only=True) as tx:
        return [dict(r) for r in await tx.query("SELECT * FROM workspace_repos WHERE workspace_id = $1 ORDER BY path", (ws,))]


async def workspace(ops: Any, owner: str, ws: str) -> dict[str, Any]:
    from .operations import workspace_dict
    record = await ops._record_of(owner, ws)
    result = workspace_dict(record)
    if record.layout == "multi":
        result["repos"] = [public(r) for r in await rows(ops, owner, ws)]
    return result


async def require_multi(ops: Any, inp: Any) -> Any:
    record = await ops._record_of(inp.tenant.tenant_id, inp.workspace_id)
    if record.layout != "multi":
        raise ConflictError("workspace_not_multi", "这个操作只适用于多仓库工作区")
    return record


async def choose(ops: Any, inp: Any, args: dict[str, Any]) -> dict[str, Any] | None:
    from .operations import Op
    record = await ops._record_of(inp.tenant.tenant_id, inp.workspace_id)
    name = str(args.get("repo") or "")
    if record.layout != "multi":
        if name:
            raise InvalidError("repo_not_applicable", "root 工作区不能指定 repo")
        return None
    if not name:
        raise InvalidError("repo_required", "多仓库操作必须指定 repo")
    repo = next((r for r in await rows(ops, inp.tenant.tenant_id, inp.workspace_id) if r["name"] == name), None)
    if repo is None:
        raise NotFoundError("repo_not_found", "工作区里没有这个仓库")
    if repo["access"] != "write" and inp.operation in (Op.WORKSPACE_OPERATION_COMMIT, Op.WORKSPACE_OPERATION_PUSH):
        raise DeniedError("repo_read_only", "这个仓库是只读的")
    return repo


async def list_repos(ops: Any, inp: Any, args: dict[str, Any]) -> dict[str, Any]:
    await require_multi(ops, inp)
    return {"repos": [public(r) for r in await rows(ops, inp.tenant.tenant_id, inp.workspace_id)]}


async def copy_metadata(ops: Any, owner: str, source: str, dest: str) -> None:
    source_rows = await rows(ops, owner, source)
    async with ops._archiver._db(owner).transaction() as tx:
        for row in source_rows:
            keys = list(row)
            values = [dest if k == "workspace_id" else "" if k == "operation_id" else row[k] for k in keys]
            await tx.execute(f"INSERT INTO workspace_repos ({', '.join(keys)}) VALUES ({', '.join(f'${i+1}' for i in range(len(keys)))})", values)


async def update_head(ops: Any, owner: str, ws: str, head: str) -> None:
    repo = selected.get()
    if not repo:
        await ops._archiver.update(owner, ws, head=head)
        return
    async with ops._archiver._db(owner).transaction() as tx:
        await tx.execute("UPDATE workspace_repos SET head = $1 WHERE workspace_id = $2 AND name = $3", (head, ws, repo["name"]))


async def attach(ops: Any, inp: Any, args: dict[str, Any]) -> dict[str, Any]:
    from .operations import _SHA, check_branch
    owner, ws = inp.tenant.tenant_id, inp.workspace_id
    await require_multi(ops, inp)
    requested = dict(args.get("repo") or {})
    name, target = str(requested.get("name") or ""), str(requested.get("path") or "")
    if not NAME.fullmatch(name):
        raise InvalidError("invalid_repo_name", "仓库名格式不合法")
    if not NAME.fullmatch(target) or target.casefold() in ("node_modules", "lost+found"):
        raise InvalidError("invalid_repo_path", "仓库路径必须是安全的单层目录名")
    access = requested.get("access") or "read"
    if access not in ("read", "write"):
        raise InvalidError("invalid_repo_access", "仓库权限必须是 read 或 write")
    remote = ops._remote(str(requested.get("url") or ""))
    ref = str(requested.get("ref") or "")
    if ref:
        check_branch(ref)
    credential = await ops._credential(inp, remote)
    limits = (await ops.policies.policy_for(owner)).workspace
    staging = ".repo-" + hashlib.sha256(inp.operation_id.encode()).hexdigest()[:20]
    async with ops._lease(inp, writable=True, remote=remote) as lease:
        existing = await rows(ops, owner, ws)
        same = next((r for r in existing if r["name"] == name), None)
        if same:
            if same["operation_id"] == inp.operation_id:
                return {"repo": public(same), "workspace": await workspace(ops, owner, ws)}
            raise ConflictError("repo_exists", "仓库名已存在")
        if any(r["path"].casefold() == target.casefold() for r in existing):
            raise ConflictError("repo_path_conflict", "仓库路径已被占用")
        if len(existing) >= limits.max_repos:
            raise DeniedError("max_repos", "工作区仓库数达到上限")
        primary = bool(requested.get("primary")) or not existing
        if requested.get("primary") and any(r["is_primary"] for r in existing):
            raise ConflictError("primary_exists", "已经有主仓库")
        items = await lease.box.list_dir(lease.env_id, "")
        if any(e.name.casefold() == target.casefold() for e in items):
            raise ConflictError("repo_path_conflict", "目标目录已经存在")
        # Check known usage before cloning; verify the complete layout before keeping it.
        manifest = await lease.box.manifest(lease.env_id)
        if limits.max_bytes and sum(e.size for e in manifest if e.type == "file") >= limits.max_bytes:
            raise DeniedError("max_bytes", "工作区大小达到上限")
        moved = False
        try:
            await lease.box.exec(lease.env_id, ["rm", "-rf", "--", staging], timeout=ops.git_timeout)
            if _SHA.fullmatch(ref):
                await ops._git(lease, ["init", "--quiet", "-b", "main", staging], what="初始化仓库")
                await ops._git(lease, ["-C", staging, "fetch", "--quiet", "--no-tags", "--", remote.url, ref], credential=credential, what="获取仓库")
                await ops._git(lease, ["-C", staging, "checkout", "--quiet", "--detach", "FETCH_HEAD"], what="检出仓库")
            else:
                await ops._git(lease, ["clone", "--quiet", *(["--branch", ref] if ref else []), "--", remote.url, staging], credential=credential, what="挂载仓库")
            manifest = await lease.box.manifest(lease.env_id)
            if limits.max_bytes and sum(e.size for e in manifest if e.type == "file") > limits.max_bytes:
                raise DeniedError("max_bytes", "仓库加入后超过工作区大小上限")
            moved_result = await lease.box.exec(lease.env_id, ["mv", "--", staging, target], timeout=ops.git_timeout)
            if moved_result.exit_code != 0:
                raise ConflictError("repo_path_conflict", "无法安装仓库目录")
            moved = True
            row = {"name": name, "url": remote.url, "ref": ref, "path": target, "access": access,
                   "is_primary": int(primary), "attached_at": iso(ops._archiver._clock()), "operation_id": inp.operation_id}
            token = selected.set(row)
            try:
                row.update(head=await ops._head(lease), branch=await ops._branch(lease))
            finally:
                selected.reset(token)
            async with ops._archiver._db(owner).transaction() as tx:
                keys = ["workspace_id", *row]
                await tx.execute(f"INSERT INTO workspace_repos ({', '.join(keys)}) VALUES ({', '.join(f'${i+1}' for i in range(len(keys)))})", [ws, *row.values()])
        except BaseException:
            with contextlib.suppress(Exception):
                await lease.box.exec(lease.env_id, ["rm", "-rf", "--", target if moved else staging], timeout=ops.git_timeout)
            raise
    return {"repo": public(row), "workspace": await workspace(ops, owner, ws)}


async def detach(ops: Any, inp: Any, args: dict[str, Any]) -> dict[str, Any]:
    owner, ws = inp.tenant.tenant_id, inp.workspace_id
    await require_multi(ops, inp)
    repo = await choose(ops, inp, args)
    assert repo is not None
    token = selected.set(repo)
    try:
        async with ops._lease(inp, writable=True) as lease:
            status = await ops._git(lease, ["status", "--porcelain", "-z", "--untracked-files=all"], what="检查仓库改动")
            files = len([p for p in status.stdout.split(b"\0") if p])
            # A broken ref/object must never turn an unknown commit count into a
            # clean repository: doing so could discard the only copy of a commit.
            # Include other branches, tags, stash and detached HEAD; checking
            # HEAD alone loses unpublished commits after switching branches.
            # --all also handles a genuinely empty repository without an error.
            count = await ops._git(lease, ["rev-list", "--count", "--all", "--not", "--remotes", *([repo["pushed_head"]] if repo.get("pushed_head") else [])], what="检查未推送提交")
            commits = int(count.stdout.strip() or b"0")
            if (files or commits) and not args.get("discard_changes"):
                raise ConflictError("repo_dirty", "仓库有未提交改动或未推送提交")
            result = await lease.box.exec(lease.env_id, ["rm", "-rf", "--", repo["path"]], timeout=ops.git_timeout)
            if result.exit_code != 0:
                raise InvalidError("repo_remove_failed", "无法卸载仓库")
            async with ops._archiver._db(owner).transaction() as tx:
                await tx.execute("DELETE FROM workspace_repos WHERE workspace_id = $1 AND name = $2", (ws, repo["name"]))
                if repo["is_primary"]:
                    remaining = await tx.query("SELECT name FROM workspace_repos WHERE workspace_id = $1 ORDER BY attached_at, name", (ws,))
                    if remaining:
                        await tx.execute("UPDATE workspace_repos SET is_primary = 1 WHERE workspace_id = $1 AND name = $2", (ws, remaining[0]["name"]))
    finally:
        selected.reset(token)
    return {"workspace": await workspace(ops, owner, ws), "discarded_files": files, "discarded_commits": commits}
