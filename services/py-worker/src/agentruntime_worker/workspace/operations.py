"""Workspace API 的操作（设计书改动稿 W2；D-06e，docs/plan/workspace-layer.md 第 5 节）。

task-service 的 WorkspaceService 起 WorkspaceOperationWorkflow（workflows/workspace.py），工作流只排一个 workspace_operation
activity（program 队列）；这里是它做的事：在承载工作区的沙箱里跑 git。

- 租户来自工作流输入里服务端构造的 TenantContext；workspace_id 格式同租户 ID。别的租户的工作区：行按租户分，查不到即 not_found。
- 凭据：sealed_credential 用 SecretStore 平台作用域的 workspace/credential-key 解开（agentruntime_core.sealing），只经 exec 的
  env 交给这一次的 git（http.extraHeader），不进 argv、日志、事件历史与错误信息；git 的输出在报错前把它遮掉。
- 联网：克隆与推送给这次操作记一条联网授权（task_id 是 wsop-<operation_id>，只放行 repo_url 的主机，按租户策略的
  sandbox.network 核对，批准人是发起的用户），借的环境按它放行，结束时收回。
- 读的操作（diff、推送、存档）先把别人闲着的可写环境存一份快照（存档模式），再在只读的环境里做，不打断会话；写的操作
  （建、提交）借可写的环境（会话为空），别的会话闲着的可写环境先存档再回收，正在跑命令时 denied（workspace_busy）。
- 推送：write_irreversible。分支要符合 workspace.push_branches；先写 gate 记录（workspace_pushes，幂等键见 push_key），
  先按远端分支的当前 sha 对账（git ls-remote），已经是这个 sha 就不推；不自动重试，超时或结果不明记 failed，下次调用先对账。
- 错误：transient 抛出（activity 按工作流的重试策略重试，推送不重试），别的错误写进结果的 error（统一错误模型）。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import re
import shutil
import stat
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from google.protobuf import json_format, struct_pb2
from temporalio import activity

from agentruntime.common.v1 import error_pb2
from agentruntime.workflow.v1 import workspace_pb2
from agentruntime_core.dev._common import PLATFORM_ID
from agentruntime_core.errors import ConflictError, DeniedError, InvalidError, NotFoundError, ProviderError, TransientError
from agentruntime_core.providers.sandbox import ExecResult, SandboxSpec
from agentruntime_core.sealing import open_sealed
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker import names
from agentruntime_worker.sandbox.broker import Lease, SandboxBroker
from agentruntime_worker.sandbox.grants import NetworkPolicy, check_request
from agentruntime_worker.sandbox.ledger import iso
from agentruntime_worker.workspace.archive import WorkspaceRecord

log = logging.getLogger(__name__)

Op = workspace_pb2.WorkspaceOperation
CREDENTIAL_SECRET = "workspace/credential-key"
MAX_DIFF_BYTES = 1 << 20
DIFF_FILE = ".git/agentruntime-diff.patch"
DIFF_INDEX = ".git/agentruntime-diff.index"
DEFAULT_AUTHOR = ("Agent Runtime", "agent-runtime@localhost")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_REF = re.compile(r"^[A-Za-z0-9._/@^~+-]{1,200}$")
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
# 不要 git 读系统与全局的配置、不要问人；safe.directory：沙箱里工作目录的属主可能与跑命令的用户不同。
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}


def push_key(tenant_id: str, workspace_id: str, branch: str, sha: str) -> str:
    """推送的幂等键：同一个提交推到同一个分支只算一次，与重试次数无关（附录 D.4）。"""
    return "wsp-" + hashlib.sha256(f"{tenant_id}\x00{workspace_id}\x00{branch}\x00{sha}".encode()).hexdigest()[:32]


def branch_allowed(patterns: tuple[str, ...], branch: str) -> bool:
    """glob：* 匹配不含 / 的一段，** 匹配任意多段（含 /）。"""
    for pattern in patterns:
        regex = ""
        i = 0
        while i < len(pattern):
            if pattern.startswith("**", i):
                regex += ".*"
                i += 2
            elif pattern[i] == "*":
                regex += "[^/]*"
                i += 1
            else:
                regex += re.escape(pattern[i])
                i += 1
        if re.fullmatch(regex, branch):
            return True
    return False


def check_branch(branch: str) -> str:
    """远端分支名按 git check-ref-format 的规则（不经 git 也能判断的那部分）。"""
    if (
        not isinstance(branch, str) or not _REF.match(branch) or branch.startswith(("-", "/", ".")) or branch.endswith(("/", ".", ".lock"))
        or ".." in branch or "//" in branch or "@{" in branch or "/." in branch or any(c in branch for c in "^~")
    ):
        raise InvalidError("invalid_branch", "分支名不合法")
    return branch


def check_rev(rev: str) -> str:
    if not _REF.match(rev) or rev.startswith("-"):
        raise InvalidError("invalid_base", "base 要是 commit、分支或标签")
    return rev


@dataclass(frozen=True)
class Remote:
    url: str
    host: str
    port: int
    local: bool


@dataclass(frozen=True)
class Credential:
    env: Mapping[str, str]
    # 要从输出里遮掉的东西（密码、Basic 头）。
    secrets: tuple[str, ...]


def struct(value: Mapping[str, Any]) -> struct_pb2.Struct:
    out = struct_pb2.Struct()
    out.update(dict(value))
    return out


class WorkspaceOperations:
    def __init__(
        self,
        broker: SandboxBroker | None,
        *,
        secret: Callable[[], Awaitable[bytes | None]],
        runtime: str = "python",
        allow_local_remotes: bool = False,
        git_timeout: timedelta = timedelta(minutes=15),
    ) -> None:
        self.broker = broker
        self._secret = secret
        self.runtime = runtime
        self.allow_local_remotes = allow_local_remotes
        self.git_timeout = git_timeout

    # --- 入口 ---

    async def run(self, inp: workspace_pb2.WorkspaceOperationInput) -> workspace_pb2.WorkspaceOperationResult:
        try:
            result = await self._dispatch(inp)
        except TransientError:
            raise  # 交给工作流的重试策略
        except ProviderError as err:
            log.info("工作区操作 %s 失败（%s）", Op.Name(inp.operation), err.code)
            return workspace_pb2.WorkspaceOperationResult(error=err.to_proto())
        return workspace_pb2.WorkspaceOperationResult(result=struct(result))

    async def _dispatch(self, inp: workspace_pb2.WorkspaceOperationInput) -> dict[str, Any]:
        if self.broker is None or self.broker.archiver is None:
            raise InvalidError("workspace_api_unavailable", "这个 Worker 没有配沙箱（AR_TOOL_SHELL_SANDBOX=sandbox），不能执行工作区的操作")
        owner = inp.tenant.tenant_id
        if not owner or owner == PLATFORM_ID or not _SLUG.match(owner):
            raise InvalidError("invalid_tenant", "工作区只属于租户")
        if not _SLUG.match(inp.workspace_id):
            raise InvalidError("invalid_workspace", "workspace_id 的格式不对")
        args = json_format.MessageToDict(inp.args) if inp.HasField("args") else {}
        handlers = {
            Op.WORKSPACE_OPERATION_CREATE: self.create,
            Op.WORKSPACE_OPERATION_DIFF: self.diff,
            Op.WORKSPACE_OPERATION_COMMIT: self.commit,
            Op.WORKSPACE_OPERATION_PUSH: self.push,
            Op.WORKSPACE_OPERATION_ARCHIVE: self.archive,
            Op.WORKSPACE_OPERATION_DELETE: self.delete,
        }
        handler = handlers.get(inp.operation)
        if handler is None:
            raise InvalidError("invalid_operation", "不认识的工作区操作")
        return await handler(inp, {str(k): v for k, v in args.items()})

    # --- 各操作 ---

    async def create(self, inp: workspace_pb2.WorkspaceOperationInput, args: dict[str, Any]) -> dict[str, Any]:
        owner, ws = inp.tenant.tenant_id, inp.workspace_id
        url = str(args.get("repo_url") or "")
        remote = self._remote(url) if url else None
        ref = str(args.get("ref") or "")
        if ref:
            check_branch(ref)
        credential = await self._credential(inp)
        limits = (await self._broker.sandboxes.policy_for(owner)).workspace
        await self._archiver.ensure(owner, ws, limits, repo_url=url, exclusive=True)
        try:
            async with self._lease(inp, writable=True, remote=remote) as lease:
                if remote is not None:
                    argv = ["clone", "--quiet", *(["--branch", ref] if ref else []), "--", remote.url, "."]
                    await self._git(lease, argv, credential=credential, what="克隆")
                else:
                    await self._git(lease, ["init", "--quiet", "-b", "main"], what="初始化")
                head = await self._head(lease)
                await self._archiver.update(owner, ws, head=head, repo_url=url)
        except BaseException:
            # 没建成：撤掉这一行（与挂卷时克隆了一半的目录），应用可以用同一个名字再建。
            with contextlib.suppress(ProviderError):
                await self._broker.destroy_workspace(owner, ws)
            with contextlib.suppress(ProviderError):
                await self._archiver.delete(owner, ws)
            self._remove_host_dir(owner, ws)
            raise
        record = await self._archiver.get(owner, ws)
        assert record is not None
        return {"workspace": workspace_dict(record)}

    async def diff(self, inp: workspace_pb2.WorkspaceOperationInput, args: dict[str, Any]) -> dict[str, Any]:
        await self._record(inp)
        base = str(args.get("base") or "")
        if base:
            check_rev(base)
        await self._current(inp)
        async with self._lease(inp, writable=False) as lease:
            head = await self._head(lease)
            # 临时的 index：HEAD 加上工作区里的全部改动（含未跟踪的新文件），不动工作区自己的 index。
            index = {"GIT_INDEX_FILE": DIFF_INDEX}
            await self._git(lease, ["read-tree", *(["HEAD"] if head else ["--empty"])], extra=index, what="准备 diff")
            await self._git(lease, ["add", "-A"], extra=index, what="准备 diff")
            target = [base, "--"] if base else (["HEAD", "--"] if head else [])
            await self._git(lease, ["diff", "--cached", "--no-color", "--no-ext-diff", "--binary", f"--output={DIFF_FILE}", *target], extra=index, what="diff")
            names_out = await self._git(lease, ["diff", "--cached", "--name-only", "-z", *target], extra=index, what="diff")
            data = await lease.box.read_file(lease.env_id, DIFF_FILE)
        files = [f for f in names_out.stdout.decode("utf-8", errors="replace").split("\x00") if f]
        truncated = len(data) > MAX_DIFF_BYTES
        return {"diff": data[:MAX_DIFF_BYTES].decode("utf-8", errors="replace"), "files": files, "truncated": truncated, "head": head}

    async def commit(self, inp: workspace_pb2.WorkspaceOperationInput, args: dict[str, Any]) -> dict[str, Any]:
        record = await self._record(inp)
        message = str(args.get("message") or "").strip()
        if not message or len(message) > 10_000:
            raise InvalidError("invalid_message", "提交说明不能为空，最多一万个字符")
        name = str(args.get("author_name") or DEFAULT_AUTHOR[0])
        email = str(args.get("author_email") or DEFAULT_AUTHOR[1])
        if any(c in name + email for c in "<>\n\x00") or len(name) > 200 or len(email) > 200:
            raise InvalidError("invalid_author", "作者的名字或邮箱不合法")
        async with self._lease(inp, writable=True) as lease:
            await self._git(lease, ["add", "-A"], what="暂存")
            staged = await self._git(lease, ["diff", "--cached", "--quiet"], what="检查改动", check=False)
            if staged.exit_code == 0:
                return {"sha": await self._head(lease), "committed": False}
            await self._git(lease, ["-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "--quiet", "--no-verify", "-F", "-"],
                            stdin=message.encode("utf-8"), what="提交")
            sha = await self._head(lease)
        await self._archiver.update(inp.tenant.tenant_id, record.workspace_id, head=sha)
        return {"sha": sha, "committed": True}

    async def push(self, inp: workspace_pb2.WorkspaceOperationInput, args: dict[str, Any]) -> dict[str, Any]:
        owner, ws = inp.tenant.tenant_id, inp.workspace_id
        record = await self._record(inp)
        branch = check_branch(str(args.get("branch") or ""))
        limits = (await self._broker.sandboxes.policy_for(owner)).workspace
        if not branch_allowed(limits.push_branches, branch):
            raise DeniedError("branch_not_allowed", f"租户策略只许推到 {'、'.join(limits.push_branches)}")
        if not record.repo_url:
            raise InvalidError("no_remote", "这个工作区不是从仓库克隆的，没有可以推送的远端")
        remote = self._remote(record.repo_url)
        credential = await self._credential(inp)
        await self._current(inp)
        async with self._lease(inp, writable=False, remote=remote) as lease:
            sha = await self._head(lease)
            if not sha:
                raise InvalidError("nothing_to_push", "工作区还没有提交")
            key = push_key(owner, ws, branch, sha)
            approved = inp.requested_by or "workspace-api"
            await self._gate(owner, key, ws=ws, branch=branch, sha=sha, remote=remote.url, approved_by=approved, operation_id=inp.operation_id)
            # 先对账：远端已经是这个 sha（重复调用，或上一次超时其实推成了）就不推。
            listed = await self._git(lease, ["ls-remote", "--", remote.url, f"refs/heads/{branch}"], credential=credential, what="查远端分支")
            current = listed.stdout.decode("utf-8", errors="replace").split("\t", 1)[0].strip()
            if current == sha:
                await self._finish(owner, key, "reconciled")
                return {"sha": sha, "branch": branch, "reconciled": True}
            try:
                await self._git(lease, ["push", "--quiet", "--", remote.url, f"HEAD:refs/heads/{branch}"], credential=credential, what="推送")
            except ProviderError as err:
                await self._finish(owner, key, "failed", err.code)
                if isinstance(err, TransientError):
                    # 结果不明（超时、连接断了）：不重试，下一次调用先对账。
                    raise TransientError("push_unknown", "推送的结果不明，稍后再调用一次（会先按远端分支的当前 sha 对账）") from None
                raise
            await self._finish(owner, key, "pushed")
        return {"sha": sha, "branch": branch, "reconciled": False}

    async def archive(self, inp: workspace_pb2.WorkspaceOperationInput, args: dict[str, Any]) -> dict[str, Any]:
        owner, ws = inp.tenant.tenant_id, inp.workspace_id
        await self._record(inp)
        if await self._broker.archive_mode_for(owner):
            snap = await self._current(inp)
            if snap is None:
                snap = await self._archiver.latest(owner, ws)
            if snap is None:
                # 还没存过（例如建了以后没用过）：借一个环境恢复（空的）再存一份。
                async with self._lease(inp, writable=True) as lease:
                    snap = await self._broker.snapshot(lease)
                    lease.writable = False  # 已经存过了，还的时候不再存
        else:
            async with self._lease(inp, writable=False) as lease:
                snap = await self._broker.snapshot(lease)
        return {"snapshot_id": snap.snapshot_id, "files": snap.files, "size_bytes": snap.size_bytes}

    async def delete(self, inp: workspace_pb2.WorkspaceOperationInput, args: dict[str, Any]) -> dict[str, Any]:
        owner, ws = inp.tenant.tenant_id, inp.workspace_id
        await self._record(inp)
        await self._broker.destroy_workspace(owner, ws)
        await self._archiver.delete(owner, ws)
        self._remove_host_dir(owner, ws)
        return {}

    def _remove_host_dir(self, owner: str, ws: str) -> None:
        """挂卷的工作区目录（<工作区根>/<租户>/<workspace>）删掉；存档模式下它本来就不在。"""
        root = self._broker.sandboxes.workspace_root
        if root is None:
            return
        target = (Path(root) / owner / ws).resolve()
        if target.is_relative_to(Path(root).resolve()) and target != Path(root).resolve() and target.is_dir():
            shutil.rmtree(target, onexc=_force_remove)

    # --- 环境 ---

    @property
    def _broker(self) -> SandboxBroker:
        assert self.broker is not None
        return self.broker

    @property
    def _archiver(self) -> Any:
        assert self.broker is not None and self.broker.archiver is not None
        return self.broker.archiver

    async def _record(self, inp: workspace_pb2.WorkspaceOperationInput) -> WorkspaceRecord:
        record = await self._archiver.get(inp.tenant.tenant_id, inp.workspace_id)
        if record is None or record.state == "deleted":
            raise NotFoundError("workspace_not_found", "工作区不存在")
        return record

    async def _current(self, inp: workspace_pb2.WorkspaceOperationInput) -> Any:
        """存档模式下把别人闲着的可写环境存一份快照（不销毁它），之后只读的环境看到的就是最新的；有命令在跑时
        workspace_busy。返回新存的快照（没有可写的环境时 None）。挂卷时什么也不做：只读的环境挂的就是同一处。"""
        owner, ws = inp.tenant.tenant_id, inp.workspace_id
        broker = self._broker
        if broker.ledger is None or not await broker.archive_mode_for(owner):
            return None
        snap = None
        for holder in await broker.ledger.writable_holders(owner, workspace_id=ws, session_id=None):
            if not broker.ledger.idle_now(holder):
                raise DeniedError("workspace_busy", "这个工作区正被一个可写的环境用着（命令还在跑），稍后再试")
            if holder.state != "ready" or not holder.backend_env_id:
                continue
            backend = await broker.sandboxes.backend(holder.backend)
            box = backend.provider.bind(TenantContext(tenant_id=owner, workspace_id=ws))
            # 存的时候占着它，别的命令不在半途改它。
            record = await broker.ledger.touch(holder, busy_for=timedelta(minutes=10))
            try:
                snap = await broker.snapshot(Lease(backend, box, holder.backend_env_id, record, owner=owner, workspace_id=ws, archive=True, writable=True))
            except NotFoundError:
                await broker.ledger.mark_lost(record, "存档时后端里已经没有这个环境")
            finally:
                if record.state == "ready":
                    await broker.ledger.touch(record)
        return snap

    @contextlib.asynccontextmanager
    async def _lease(self, inp: workspace_pb2.WorkspaceOperationInput, *, writable: bool, remote: Remote | None = None) -> Any:
        owner, ws = inp.tenant.tenant_id, inp.workspace_id
        broker = self._broker
        task_id = f"wsop-{inp.operation_id}"
        tenant = TenantContext(tenant_id=owner, user_id=inp.requested_by or inp.tenant.user_id, workspace_id=ws, session_id="")
        granted = False
        if remote is not None and not remote.local and broker.grants is not None:
            policy = NetworkPolicy.from_policy((await broker.sandboxes.policy_for(owner)).network)
            request = check_request(policy, hosts=[remote.host], ports=[remote.port], minutes=15, reason="Workspace API：克隆或推送")
            await broker.grants.create(owner, request, policy, task_id=task_id, approved_by=inp.requested_by or "workspace-api", workspace_id=ws)
            granted = True
        spec = SandboxSpec(runtime=self.runtime, network="none", idle_timeout=timedelta(minutes=10), workspace_id=ws)
        try:
            lease = await broker.acquire(tenant, spec, task_id=task_id, writable=writable, reuse=False, busy_for=self.git_timeout + timedelta(minutes=5))
            ok = False
            try:
                yield lease
                ok = True
            finally:
                if ok:
                    await broker.release(lease)
                else:
                    # 操作失败：不存档（不把做了一半的改动存进快照），环境直接销毁。
                    lease.writable = False
                    with contextlib.suppress(ProviderError):
                        await broker.release(lease)
        finally:
            if granted:
                with contextlib.suppress(ProviderError):
                    await broker.grants.revoke_task(owner, task_id)  # type: ignore[union-attr]

    async def _git(
        self, lease: Lease, argv: list[str], *, what: str, credential: Credential | None = None, extra: Mapping[str, str] | None = None,
        stdin: bytes = b"", check: bool = True,
    ) -> ExecResult:
        env = {**lease.env, **_GIT_ENV, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*", **(extra or {})}
        if credential is not None:
            env.update(credential.env)
        try:
            res = await lease.box.exec(lease.env_id, ["git", *argv], timeout=self.git_timeout, stdin=stdin, env=env)
        except OSError:
            raise InvalidError("git_unavailable", "沙箱里没有 git（运行环境要带 git，例如 universal）") from None
        if res.timed_out:
            raise TransientError("git_timeout", f"{what}超时")
        if check and res.exit_code != 0:
            raise _git_error(what, _redact(res.stderr.decode("utf-8", errors="replace"), credential))
        return res

    async def _head(self, lease: Lease) -> str:
        res = await self._git(lease, ["rev-parse", "--verify", "--quiet", "HEAD"], what="读 HEAD", check=False)
        sha = res.stdout.decode().strip() if res.exit_code == 0 else ""
        return sha if _SHA.match(sha) else ""

    # --- 凭据、远端与 gate ---

    def _remote(self, url: str) -> Remote:
        parts = urllib.parse.urlsplit(url)
        if parts.username or parts.password:
            raise InvalidError("credentials_in_url", "仓库地址里不能带凭据，凭据另外传")
        if parts.scheme == "https" and parts.hostname:
            return Remote(url=url, host=parts.hostname.lower(), port=parts.port or 443, local=False)
        if parts.scheme == "file" and self.allow_local_remotes:
            return Remote(url=url, host="", port=0, local=True)
        raise InvalidError("invalid_repo_url", "仓库地址只收 https")

    async def _credential(self, inp: workspace_pb2.WorkspaceOperationInput) -> Credential | None:
        if not inp.sealed_credential:
            return None
        key = await self._secret()
        if not key:
            raise InvalidError("missing_sealing_key", f"SecretStore 的平台作用域里没有 {CREDENTIAL_SECRET}")
        plain = open_sealed(key, inp.tenant.tenant_id, inp.operation_id, inp.sealed_credential)
        try:
            doc = json.loads(plain)
            username, password = str(doc.get("username") or ""), str(doc.get("password") or "")
        except (ValueError, AttributeError):
            raise DeniedError("invalid_sealed", "封起的凭据无效") from None
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        header = f"Authorization: Basic {token}"
        env = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_1": "http.extraHeader", "GIT_CONFIG_VALUE_1": header}
        return Credential(env=env, secrets=tuple(s for s in (password, token, header) if s))

    async def _gate(self, owner: str, key: str, *, ws: str, branch: str, sha: str, remote: str, approved_by: str, operation_id: str) -> None:
        """推送的 gate 记录：这次调用本身是调用者的批准（approved_by 是发起的用户）；同一个幂等键只记一行。"""
        archiver = self._archiver
        await archiver.migrate()
        now = iso(archiver._clock())  # noqa: SLF001
        async with archiver._db(owner).transaction() as tx:  # noqa: SLF001
            await tx.execute(
                "INSERT INTO workspace_pushes (push_key, workspace_id, branch, sha, remote, approved_by, operation_id, state, reason, created_at, finished_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, 'pushing', '', $8, '') "
                "ON CONFLICT (push_key) DO UPDATE SET state = 'pushing', operation_id = excluded.operation_id, approved_by = excluded.approved_by",
                (key, ws, branch, sha, remote, approved_by, operation_id, now),
            )

    async def _finish(self, owner: str, key: str, state: str, reason: str = "") -> None:
        archiver = self._archiver
        async with archiver._db(owner).transaction() as tx:  # noqa: SLF001
            await tx.execute(
                "UPDATE workspace_pushes SET state = $1, reason = $2, finished_at = $3 WHERE push_key = $4",
                (state, reason[:300], iso(archiver._clock()), key),  # noqa: SLF001
            )

    async def pushes(self, owner: str, workspace_id: str) -> list[dict[str, Any]]:
        """这个工作区的推送记录（测试与排查用）。"""
        archiver = self._archiver
        await archiver.migrate()
        async with archiver._db(owner).transaction(read_only=True) as tx:  # noqa: SLF001
            rows = await tx.query("SELECT push_key, branch, sha, approved_by, state, reason FROM workspace_pushes WHERE workspace_id = $1 ORDER BY created_at",
                                  (workspace_id,))
        return [dict(r) for r in rows]


def workspace_dict(record: WorkspaceRecord) -> dict[str, Any]:
    """与 workspace.v1.Workspace 的 JSON 写法一致（task-service 按 protojson 读）。"""
    out: dict[str, Any] = {
        "workspace_id": record.workspace_id, "state": "WORKSPACE_STATE_ACTIVE" if record.state == "active" else "WORKSPACE_STATE_DELETED",
        "repo_url": record.repo_url, "head": record.head, "snapshot_id": record.snapshot_id, "size_bytes": str(record.size_bytes), "files": record.files,
    }
    for name in ("created_at", "updated_at"):
        if getattr(record, name):
            out[name] = getattr(record, name)
    return out


def _redact(text: str, credential: Credential | None) -> str:
    if credential is not None:
        for secret in credential.secrets:
            text = text.replace(secret, "[已遮蔽]")
    text = re.sub(r"(?i)(authorization:\s*\w+\s+)\S+", r"\1[已遮蔽]", text)
    return text.strip()[-600:]


def _git_error(what: str, stderr: str) -> ProviderError:
    lower = stderr.lower()
    if any(s in lower for s in ("authentication failed", "permission denied", "403", "401", "could not read username")):
        return DeniedError("git_denied", f"{what}被远端拒绝（凭据不对或没有权限）：{stderr}")
    if any(s in lower for s in ("could not resolve host", "connection timed out", "connection refused", "failed to connect", "early eof", "rpc failed")):
        return TransientError("git_unavailable", f"{what}时连不上远端：{stderr}")
    if any(s in lower for s in ("[rejected]", "non-fast-forward", "fetch first", "stale info")):
        return ConflictError("push_rejected", f"{what}被拒绝（远端分支有别的提交）：{stderr}")
    if any(s in lower for s in ("not found", "does not exist", "couldn't find remote ref", "unknown revision", "bad revision")):
        return NotFoundError("git_not_found", f"{what}失败：{stderr}")
    return InvalidError("git_failed", f"{what}失败：{stderr}")


def _force_remove(function: Any, path: str, _exc: BaseException) -> None:
    """Windows 上 .git 里的只读文件：去掉只读再删。"""
    with contextlib.suppress(OSError):
        Path(path).chmod(stat.S_IWRITE)
        function(path)


class WorkspaceActivities:
    def __init__(self, operations: WorkspaceOperations | None) -> None:
        self.operations = operations

    @activity.defn(name=names.WORKSPACE_OPERATION)
    async def workspace_operation(self, inp: workspace_pb2.WorkspaceOperationInput) -> workspace_pb2.WorkspaceOperationResult:
        if self.operations is None:
            return workspace_pb2.WorkspaceOperationResult(error=error_pb2.Error(
                category=error_pb2.ERROR_CATEGORY_INVALID, code="workspace_api_unavailable",
                message="这个 Worker 没有配沙箱（AR_TOOL_SHELL_SANDBOX=sandbox），不能执行工作区的操作",
            ))
        return await self.operations.run(inp)

    def activities(self) -> dict[str, object]:
        return {names.WORKSPACE_OPERATION: self.workspace_operation}


__all__ = ["CREDENTIAL_SECRET", "WorkspaceActivities", "WorkspaceOperations", "branch_allowed", "check_branch", "push_key"]
