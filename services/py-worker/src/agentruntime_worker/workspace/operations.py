"""Workspace API 的操作（设计书改动稿 W2；D-06e、D-06f，docs/plan/workspace-layer.md 第 5、8 节）。

task-service 的 WorkspaceService 起 WorkspaceOperationWorkflow（workflows/workspace.py），工作流只排一个 workspace_operation
activity（program 队列）；这里是它做的事：在承载工作区的沙箱里跑 git、列目录、读文件。

- 租户来自工作流输入里服务端构造的 TenantContext；workspace_id 格式同租户 ID。别的租户的工作区：行按租户分，查不到即 not_found。
- 凭据：sealed_credential 用 SecretStore 平台作用域的 workspace/credential-key 解开（agentruntime_core.sealing），只经 exec 的
  env 交给这一次的 git：只对远端那台主机生效的 http.<https://主机/>.extraHeader，不进 argv、日志、事件历史与错误信息；
  git 的输出在报错前把它遮掉。git 一律不跑仓库里的 hooks（core.hooksPath 指向不存在的地方）。
- 联网：克隆、推送与同步要连远端。后端对沙箱的网络有约束时（经出网网关，或托管后端按域名放行），给这次操作记一条联网授权
  （task_id 是 wsop-<operation_id>，只放行远端的主机，按租户策略的 sandbox.network 核对，批准人是发起的用户），结束时收回；
  本地后端不隔离网络，不记。
- 读的操作（diff、列目录、读文件、log、推送、存档、被复制的源）先把别人闲着的可写环境存一份快照（存档模式），再在只读的
  环境里做，不打断会话；写的操作（建、提交、丢弃改动、同步）借可写的环境（会话为空），别的会话闲着的可写环境先存档再回收，
  正在跑命令时 denied（workspace_busy）。
- 推送：write_irreversible。分支要符合 workspace.push_branches；先写 gate 记录（workspace_pushes，幂等键见 push_key），
  先按远端分支的当前 sha 对账（git ls-remote），已经是这个 sha 就不推；不自动重试，超时或结果不明记 failed，下次调用先对账。
- 远端一律是业务库里记的 repo_url（建工作区时给的，复制的沿用源的），不读沙箱里 .git/config 的 origin：沙箱里的代码改得了它。
  连远端的 git（推送前的对账、推送、同步取远端）也不读工作副本的 .git/config（G-03 第 9 条）：在环境里一个临时的裸仓库下跑，
  对象库指回工作副本（见 _DETACHED_ENV）。建工作区时的克隆与取提交是在刚建的空目录里，配置是这次操作自己写的。
- 没配沙箱（AR_TOOL_SHELL_SANDBOX 是 host 或 docker）时 Worker 用一个只登记本地后端的 SandboxBroker（tools.workspace_operations），
  工作区就是 <AR_TOOL_WORKSPACE_ROOT>/<租户>/<workspace>，与 shell_run、repo_* 是同一处。
- 错误：transient 抛出（activity 按工作流的重试策略重试，推送不重试），别的错误写进结果的 error（统一错误模型）。
- 计时（PERF-00，见 workspace.timing）：给了 telemetry 时每次操作记一个 runtime.workspace_op span（排队、借环境、恢复、命令、
  存档、归还的独占耗时与进出 Worker 的字节数），另记直方图 runtime.workspace_op.duration_ms 与计数器 runtime.worker.bytes。
  不记路径与内容。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import posixpath
import re
import secrets
import shutil
import stat
import tempfile
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
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
from agentruntime_core.providers.telemetry import TelemetryProvider
from agentruntime_core.sealing import open_sealed
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker import names
from agentruntime_worker.sandbox.broker import Lease, SandboxBroker
from agentruntime_worker.sandbox.env_config import EnvironmentConfig
from agentruntime_worker.sandbox.grants import NetworkPolicy, check_request
from agentruntime_worker.sandbox.ledger import iso
from agentruntime_worker.workspace import timing
from agentruntime_worker.workspace.archive import WorkspaceRecord
from agentruntime_worker.workspace.copy import copy_working_copy

log = logging.getLogger(__name__)

Op = workspace_pb2.WorkspaceOperation
# 只读的操作：不动工作区的版本号（WS-READ-03），task-service 可以按版本缓存它们的结果。
READ_OPERATIONS = frozenset({
    Op.WORKSPACE_OPERATION_LIST_FILES, Op.WORKSPACE_OPERATION_READ_FILE, Op.WORKSPACE_OPERATION_LOG, Op.WORKSPACE_OPERATION_DIFF,
})
CREDENTIAL_SECRET = "workspace/credential-key"
MAX_DIFF_BYTES = 1 << 20
MAX_READ_BYTES = 512 << 10
BINARY_PROBE = 8192
MAX_LIST = 1000
DEFAULT_LOG, MAX_LOG = 30, 200
MAX_INITIAL_FILES, MAX_INITIAL_BYTES = 200, 1 << 20
DIFF_FILE = ".git/agentruntime-diff.patch"
DIFF_INDEX = ".git/agentruntime-diff.index"
DEFAULT_AUTHOR = ("Agent Runtime", "agent-runtime@localhost")
# 不存在的 hooks 目录：仓库带的、沙箱里写进去的 hooks 都不跑。
NO_HOOKS = "/dev/null"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_REF = re.compile(r"^[A-Za-z0-9._/@^~+-]{1,200}$")
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_DRIVE = re.compile(r"^[A-Za-z]:")
# git log 的格式：sha、作者、邮箱、作者时间（严格 ISO 8601）、说明的第一行，以 \x1f 分隔。
_LOG_FORMAT = "%H%x1f%an%x1f%ae%x1f%aI%x1f%s"
# 不要 git 读系统的配置、不要问人。
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
# 连远端的 git（对账、推送、同步取远端）在环境里一个临时的裸仓库下跑（G-03 第 9 条）：工作副本的 .git/config 是 Agent 改得了的，
# insteadOf 改写远端、ext 传输、credential.helper、http.<url>.sslVerify 等都能让带凭据的 git 执行它写的命令或把请求头交给别处，
# 逐项覆盖靠不住，所以根本不读它；全局配置也不读。对象库经 GIT_OBJECT_DIRECTORY 指回工作副本；临时仓库没有引用，
# gc 与 maintenance 一律关掉，免得按它的引用清掉工作副本的对象。
_DETACHED_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null"}
_DETACHED_CONFIG = (("gc.auto", "0"), ("maintenance.auto", "false"))
# 远端的沙箱里临时仓库放在环境自己的 /tmp（不在工作区的卷上：挂卷时会话的环境也看得到卷）；本地后端放在 Worker 的临时目录。
DETACHED_PREFIX = "/tmp/agentruntime-git-"
_FILE_TYPES = {"file": "FILE_TYPE_FILE", "dir": "FILE_TYPE_DIRECTORY", "symlink": "FILE_TYPE_SYMLINK"}


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
    if not _REF.match(rev) or rev.startswith("-") or ".." in rev:
        raise InvalidError("invalid_base", "base 要是 commit、分支或标签")
    return rev


def check_path(path: Any, *, allow_root: bool = False) -> str:
    """应用给的路径（ListFiles、ReadFile、初始文件）：相对工作区根、/ 分隔，不能有绝对路径、盘符、空段、. 与 ..、反斜杠、NUL，
    任何一级都不能是 .git。返回规范的写法（根是空串）。"""
    if not isinstance(path, str) or "\\" in path or "\x00" in path or path.startswith("/") or _DRIVE.match(path):
        raise InvalidError("invalid_path", "路径要相对工作区根，用 / 分隔")
    if path in ("", "."):
        if allow_root:
            return ""
        raise InvalidError("invalid_path", "要给工作区里的一个文件")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise InvalidError("invalid_path", "路径不得含空段、. 或 ..")
    if ".git" in parts:
        raise InvalidError("invalid_path", "不能直接访问 .git（经 GetLog、GetDiff 看 git 的信息）")
    return path


@dataclass(frozen=True)
class Remote:
    url: str
    host: str
    port: int
    local: bool

    @property
    def prefix(self) -> str:
        """http.<prefix>.extraHeader 的前缀：只对这台主机生效。"""
        parts = urllib.parse.urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}/"


@dataclass(frozen=True)
class Detached:
    """临时的裸仓库：git_dir 是它，objects 与 shallow 是工作副本的对象库与 shallow 文件（环境里的绝对路径）。"""

    git_dir: str
    objects: str
    shallow: str


@dataclass(frozen=True)
class Credential:
    # 追加给 git 的配置（键、值）；里面有凭据，不进 repr。
    config: tuple[tuple[str, str], ...]
    # 要从输出里遮掉的东西（密码、Basic 头）。
    secrets: tuple[str, ...]

    def __repr__(self) -> str:
        return "Credential(***)"


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
        telemetry: TelemetryProvider | None = None,
    ) -> None:
        self.broker = broker
        self._secret = secret
        self.runtime = runtime
        self.allow_local_remotes = allow_local_remotes
        self.git_timeout = git_timeout
        self.telemetry = telemetry

    # --- 入口 ---

    async def run(self, inp: workspace_pb2.WorkspaceOperationInput, *, queued_ms: float | None = None) -> workspace_pb2.WorkspaceOperationResult:
        """queued_ms：这次尝试从排进队列到开始执行的毫秒数（activity 一侧算好传进来，PERF-00）。"""
        with timing.metering() as meter:
            if queued_ms is not None:
                meter.add_ms("queue", queued_ms)
            try:
                async with self._span(inp) as span:
                    outcome = "internal"
                    try:
                        result = await self._dispatch(inp)
                        outcome = "ok"
                    except TransientError as err:
                        outcome = err.code
                        raise  # 交给工作流的重试策略
                    except ProviderError as err:
                        outcome = err.code
                        log.info("工作区操作 %s 失败（%s）", Op.Name(inp.operation), err.code)
                        return workspace_pb2.WorkspaceOperationResult(error=err.to_proto())
                    finally:
                        await self._report(inp, meter, span, outcome)
            finally:
                # 与 Node Executor 一样每次结束时导出；导出失败不影响操作的结果。
                if self._telemetry_for(inp) is not None:
                    with contextlib.suppress(ProviderError):
                        await self._telemetry_for(inp).flush()  # type: ignore[union-attr]
        return workspace_pb2.WorkspaceOperationResult(result=struct(result))

    def _telemetry_for(self, inp: workspace_pb2.WorkspaceOperationInput) -> Any:
        """只绑租户的 Telemetry（指标与导出用）；没给 telemetry 或租户不合法时 None。"""
        owner = inp.tenant.tenant_id
        if self.telemetry is None or not _SLUG.match(owner) or owner == PLATFORM_ID:
            return None
        return self.telemetry.bind(TenantContext(tenant_id=owner))

    def _span(self, inp: workspace_pb2.WorkspaceOperationInput) -> Any:
        if self._telemetry_for(inp) is None:
            return contextlib.nullcontext()
        assert self.telemetry is not None
        ws = inp.workspace_id if _SLUG.match(inp.workspace_id) else ""
        return self.telemetry.bind(TenantContext(tenant_id=inp.tenant.tenant_id, workspace_id=ws)).start_span("runtime.workspace_op", attributes={
            "workspace.operation": _op_name(inp.operation),
        })

    async def _report(self, inp: workspace_pb2.WorkspaceOperationInput, meter: timing.Meter, span: Any, outcome: str) -> None:
        """PERF-00：span 的属性与指标。记不下来只写日志，不影响操作本身。"""
        metrics = self._telemetry_for(inp)
        if span is None or metrics is None:
            return
        labels = {"workspace.operation": _op_name(inp.operation), "workspace.mode": meter.labels.get("workspace.mode", "none"), "workspace.outcome": outcome}
        attributes = meter.attributes()
        try:
            span.set_attributes({**labels, **attributes, **{k: v for k, v in meter.labels.items() if k != "workspace.mode"}})
            # 指标只绑租户：标签不带工作区与会话，不随工作区膨胀。
            await timing.record_durations(metrics, attributes, labels)
            await timing.record_bytes(metrics, meter, activity="workspace_operation", labels={"workspace.operation": labels["workspace.operation"]})
        except Exception:  # noqa: BLE001
            log.warning("工作区操作 %s 的计时没记下来", Op.Name(inp.operation), exc_info=True)

    async def _dispatch(self, inp: workspace_pb2.WorkspaceOperationInput) -> dict[str, Any]:
        if self.broker is None or self.broker.archiver is None:
            raise InvalidError("workspace_api_unavailable", "这个 Worker 没有配沙箱，也没有配工作区目录（AR_TOOL_WORKSPACE_ROOT），不能执行工作区的操作")
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
            Op.WORKSPACE_OPERATION_LIST_FILES: self.list_files,
            Op.WORKSPACE_OPERATION_READ_FILE: self.read_file,
            Op.WORKSPACE_OPERATION_LOG: self.log,
            Op.WORKSPACE_OPERATION_DISCARD: self.discard,
            Op.WORKSPACE_OPERATION_SYNC: self.sync,
            Op.WORKSPACE_OPERATION_SET_ENVIRONMENT: self.set_environment,
        }
        handler = handlers.get(inp.operation)
        if handler is None:
            raise InvalidError("invalid_operation", "不认识的工作区操作")
        result = await handler(inp, {str(k): v for k, v in args.items()})
        if inp.operation not in READ_OPERATIONS:
            # WS-READ-03：改动过工作区，task-service 按旧版本缓存的只读结果不再命中。加不上只少了一次失效，TTL 兜底。
            try:
                await self.broker.archiver.bump_generation(owner, inp.workspace_id)
            except ProviderError as err:
                log.warning("工作区 %s 的版本号没加上（%s）", inp.workspace_id, err.code)
        return result

    # --- 建 ---

    async def create(self, inp: workspace_pb2.WorkspaceOperationInput, args: dict[str, Any]) -> dict[str, Any]:
        owner, ws = inp.tenant.tenant_id, inp.workspace_id
        url = str(args.get("repo_url") or "")
        source = str(args.get("from_workspace_id") or "")
        files = _initial_files(args.get("files"))
        if source and (url or files):
            raise InvalidError("conflicting_sources", "from_workspace_id 不能与 repo_url、files 同给")
        remote = self._remote(url) if url else None
        ref = str(args.get("ref") or "")
        if ref:
            check_branch(ref)
        # ref 是完整的提交时只取到它为止的历史（TMP 计划 eval-observability-and-runtime-fixes 11.10）。
        commit = ref if remote is not None and _SHA.match(ref) else ""
        author = _author(args)
        # ENV-02：选用的环境配置（task-service 校验过，这里再核一遍）；不对时不建工作区。
        environment = EnvironmentConfig.from_args(args["environment"]) if args.get("environment") else None
        credential = await self._credential(inp, remote) if remote is not None else None
        origin: WorkspaceRecord | None = None
        if source:
            if source == ws or not _SLUG.match(source):
                raise InvalidError("invalid_source", "from_workspace_id 要是同租户里另一个工作区")
            origin = await self._record_of(owner, source)
        limits = (await self._broker.sandboxes.policy_for(owner)).workspace
        await self._archiver.ensure(owner, ws, limits, repo_url=url or (origin.repo_url if origin else ""), exclusive=True)
        try:
            if environment is not None:
                await self._archiver.set_environment(owner, ws, environment, owner_user=inp.requested_by or inp.tenant.user_id)
            if origin is not None:
                head, branch = await self._copy(inp, source, max_bytes=limits.max_bytes)
            else:
                async with self._lease(inp, writable=True, remote=remote) as lease:
                    if commit and remote is not None:
                        # 之后的提交（例如上游的修复）不进工作区，远端的分支与标签也不取；HEAD 分离在这个提交上。
                        await self._git(lease, ["init", "--quiet", "-b", "main"], what="初始化")
                        await self._git(lease, ["fetch", "--quiet", "--no-tags", "--", remote.url, commit], credential=credential, what="取提交")
                        await self._git(lease, ["checkout", "--quiet", "--detach", "FETCH_HEAD"], what="检出")
                    elif remote is not None:
                        argv = ["clone", "--quiet", *(["--branch", ref] if ref else []), "--", remote.url, "."]
                        await self._git(lease, argv, credential=credential, what="克隆")
                    else:
                        await self._git(lease, ["init", "--quiet", "-b", "main"], what="初始化")
                    if files:
                        await self._write_and_commit(lease, files, author, cloned=remote is not None)
                    head, branch = await self._head(lease), await self._branch(lease)
            await self._archiver.update(owner, ws, head=head)
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
        return {"workspace": workspace_dict(record, branch=branch)}

    async def _write_and_commit(self, lease: Lease, files: list[tuple[str, str]], author: tuple[str, str], *, cloned: bool) -> None:
        """写入建工作区时给的文件并提交：空工作区是初始提交；克隆之后另做一次提交（同名的覆盖）。

        只暂存写入的这几个文件，并且 --force：仓库的 .gitignore 忽略了它们也要进这次提交（不然补丁的基准里没有它们）；
        内容与仓库里的一样时照样提交（空提交），head 一定是这次提交。
        """
        for path, content in files:
            data = content.encode("utf-8")
            with timing.phase("command"):
                await lease.box.write_file(lease.env_id, path, data)
            timing.add_bytes("sandbox", "out", len(data))
        await self._git(lease, ["add", "--force", "--", *(path for path, _ in files)], what="暂存")
        message = "Add workspace files" if cloned else "Initial commit"
        argv = [*_author_config(author), "commit", "--quiet", "--no-verify", *(["--allow-empty"] if cloned else []), "-m", message]
        await self._git(lease, argv, what="提交写入的文件" if cloned else "初始提交")

    async def _copy(self, inp: workspace_pb2.WorkspaceOperationInput, source: str, *, max_bytes: int) -> tuple[str, str]:
        """把同租户的 source 工作区复制进新建的（空的）工作区：从源的只读环境导出，再导进新的可写环境；两种模式一样。

        复制之后核对（TMP 计划 eval-observability-and-runtime-fixes 第 2 节）：新工作区（不算 .git）一个文件也没有、而源有文件时
        报 workspace_empty_after_copy，由 create 撤掉这一行与环境。源的文件数取源的只读环境里的清单（存档记录的 files 连 .git 一起数，不能用）。
        """
        owner = inp.tenant.tenant_id
        await self._current(owner, source)
        # N-10：不经环境复制整个仓库。存档模式引用源的快照（块不搬）；挂卷时在卷上复制，.git/objects 用硬链接。
        if await self._broker.archive_mode_for(owner):
            copied = await self._archiver.copy_snapshot(owner, source, inp.workspace_id, max_bytes=max_bytes)
            if copied is not None:
                return copied[1], copied[2]
        else:
            root = self._broker.sandboxes.workspace_root
            if root is not None and (Path(root) / owner / source).is_dir():
                src, dst = Path(root) / owner / source, Path(root) / owner / inp.workspace_id
                stats = await asyncio.to_thread(copy_working_copy, src, dst, max_bytes=max_bytes)
                if not stats.worktree_files and await asyncio.to_thread(_has_worktree_files, src):
                    raise ConflictError("workspace_empty_after_copy", f"从 {source} 复制过来的工作区是空的：不留空的副本")
                log.info("工作区 %s 从 %s 复制：复制 %d 字节，硬链接 %d 字节", inp.workspace_id, source, stats.copied_bytes, stats.linked_bytes)
                return await asyncio.to_thread(_host_head, dst)
        with tempfile.TemporaryFile() as tmp:
            async with self._lease(inp, writable=False, workspace_id=source) as src:
                _require(src.box, "export_archive")
                with timing.phase("command"):
                    await src.box.export_archive(src.env_id, tmp, max_bytes=max_bytes)
                expected = await _file_count(src)
            size = tmp.tell()
            timing.add_bytes("sandbox", "in", size)
            tmp.seek(0)
            async with self._lease(inp, writable=True) as dst:
                _require(dst.box, "import_archive")
                with timing.phase("command"):
                    await dst.box.import_archive(dst.env_id, tmp, size=size, max_bytes=max_bytes)
                timing.add_bytes("sandbox", "out", size)
                if expected and not await _file_count(dst):
                    raise ConflictError("workspace_empty_after_copy", f"从 {source} 复制过来的工作区是空的（源有 {expected} 个文件）：不留空的副本")
                return await self._head(dst), await self._branch(dst)

    # --- 看 ---

    async def diff(self, inp: workspace_pb2.WorkspaceOperationInput, args: dict[str, Any]) -> dict[str, Any]:
        await self._record(inp)
        base = str(args.get("base") or "")
        if base:
            check_rev(base)
        renames = ["--find-renames"] if args.get("find_renames") else ["--no-renames"]
        await self._current(inp.tenant.tenant_id, inp.workspace_id)
        async with self._lease(inp, writable=False) as lease:
            head, branch = await self._head(lease), await self._branch(lease)
            # 临时的 index：HEAD 加上工作区里的全部改动（含未跟踪的新文件），不动工作区自己的 index。
            index = {"GIT_INDEX_FILE": DIFF_INDEX}
            await self._git(lease, ["read-tree", *(["HEAD"] if head else ["--empty"])], extra=index, what="准备 diff")
            await self._git(lease, ["add", "-A"], extra=index, what="准备 diff")
            target = [base, "--"] if base else (["HEAD", "--"] if head else [])
            common = ["diff", "--cached", "--no-color", "--no-ext-diff", *renames]
            await self._git(lease, [*common, "--binary", f"--output={DIFF_FILE}", *target], extra=index, what="diff")
            numstat = await self._git(lease, [*common, "--numstat", "-z", *target], extra=index, what="diff")
            with timing.phase("command"):
                data = await lease.box.read_file(lease.env_id, DIFF_FILE)
            timing.add_bytes("sandbox", "in", len(data))
        stats = parse_numstat(numstat.stdout.decode("utf-8", errors="replace"))
        return {
            "diff": data[:MAX_DIFF_BYTES].decode("utf-8", errors="replace"), "files": [s["path"] for s in stats], "stats": stats,
            "truncated": len(data) > MAX_DIFF_BYTES, "head": head, "branch": branch,
        }

    async def list_files(self, inp: workspace_pb2.WorkspaceOperationInput, args: dict[str, Any]) -> dict[str, Any]:
        await self._record(inp)
        rel = check_path(str(args.get("path") or ""), allow_root=True)
        await self._current(inp.tenant.tenant_id, inp.workspace_id)
        async with self._lease(inp, writable=False) as lease:
            _require(lease.box, "list_dir")
            try:
                with timing.phase("command"):
                    items = await lease.box.list_dir(lease.env_id, rel)
            except NotFoundError as err:
                if err.code == "unknown_env":
                    raise
                raise NotFoundError("dir_not_found", "工作区里没有这个目录") from None
        entries = []
        for item in items:
            if item.name == ".git":
                continue
            entry: dict[str, Any] = {"name": item.name, "type": _FILE_TYPES.get(item.type, "FILE_TYPE_UNSPECIFIED")}
            if item.type == "file":
                entry["size"] = str(item.size)
            entries.append(entry)
        return {"entries": entries, "truncated": len(items) >= MAX_LIST}

    async def read_file(self, inp: workspace_pb2.WorkspaceOperationInput, args: dict[str, Any]) -> dict[str, Any]:
        await self._record(inp)
        rel = check_path(str(args.get("path") or ""))
        parent, name = posixpath.split(rel)
        await self._current(inp.tenant.tenant_id, inp.workspace_id)
        async with self._lease(inp, writable=False) as lease:
            _require(lease.box, "list_dir")
            # 先从上级目录看大小与类型：大文件不读进来。
            try:
                with timing.phase("command"):
                    listing = await lease.box.list_dir(lease.env_id, parent)
            except NotFoundError as err:
                if err.code == "unknown_env":
                    raise
                listing = []
            entry = next((e for e in listing if e.name == name), None)
            if (entry is None and len(listing) < MAX_LIST) or (entry is not None and entry.type != "file"):
                raise NotFoundError("file_not_found", f"工作区里没有文件 {rel}")
            if entry is not None and entry.size > MAX_READ_BYTES:
                return {"size": str(entry.size), "too_large": True}
            # 列目录只给前 1000 项：大目录里没列到的，直接读（agent 对单个文件另有上限）。
            try:
                with timing.phase("command"):
                    data = await lease.box.read_file(lease.env_id, rel)
                timing.add_bytes("sandbox", "in", len(data))
            except NotFoundError as err:
                if err.code == "unknown_env":
                    raise
                raise NotFoundError("file_not_found", f"工作区里没有文件 {rel}") from None
            except InvalidError as err:
                if err.code != "too_large":
                    raise
                return {"size": "0", "too_large": True}
            if len(data) > MAX_READ_BYTES:
                return {"size": str(len(data)), "too_large": True}
        if b"\x00" in data[:BINARY_PROBE]:
            return {"size": str(len(data)), "binary": True}
        return {"size": str(len(data)), "content": data.decode("utf-8", errors="replace")}

    async def log(self, inp: workspace_pb2.WorkspaceOperationInput, args: dict[str, Any]) -> dict[str, Any]:
        await self._record(inp)
        limit = int(args.get("limit") or 0) or DEFAULT_LOG
        if not 1 <= limit <= MAX_LOG:
            raise InvalidError("invalid_limit", f"limit 最多 {MAX_LOG}")
        base = str(args.get("base") or "")
        if base:
            check_rev(base)
        await self._current(inp.tenant.tenant_id, inp.workspace_id)
        async with self._lease(inp, writable=False) as lease:
            if not await self._head(lease):
                return {"commits": []}
            rev = [f"{base}..HEAD"] if base else ["HEAD"]
            out = await self._git(lease, ["log", f"-n{limit}", f"--format={_LOG_FORMAT}", "--end-of-options", *rev], what="读提交记录")
        commits = []
        for line in out.stdout.decode("utf-8", errors="replace").splitlines():
            fields = line.split("\x1f")
            if len(fields) == 5 and _SHA.match(fields[0]):
                commits.append({"sha": fields[0], "author_name": fields[1], "author_email": fields[2], "authored_at": fields[3], "subject": fields[4]})
        return {"commits": commits}

    # --- 改 ---

    async def commit(self, inp: workspace_pb2.WorkspaceOperationInput, args: dict[str, Any]) -> dict[str, Any]:
        record = await self._record(inp)
        message = str(args.get("message") or "").strip()
        if not message or len(message) > 10_000:
            raise InvalidError("invalid_message", "提交说明不能为空，最多一万个字符")
        author = _author(args)
        async with self._lease(inp, writable=True) as lease:
            await self._git(lease, ["add", "-A"], what="暂存")
            staged = await self._git(lease, ["diff", "--cached", "--quiet"], what="检查改动", check=False)
            if staged.exit_code == 0:
                return {"sha": await self._head(lease), "committed": False}
            await self._git(lease, [*_author_config(author), "commit", "--quiet", "--no-verify", "-F", "-"], stdin=message.encode("utf-8"), what="提交")
            sha = await self._head(lease)
        await self._archiver.update(inp.tenant.tenant_id, record.workspace_id, head=sha)
        return {"sha": sha, "committed": True}

    async def discard(self, inp: workspace_pb2.WorkspaceOperationInput, args: dict[str, Any]) -> dict[str, Any]:
        await self._record(inp)
        async with self._lease(inp, writable=True) as lease:
            head = await self._head(lease)
            if head:
                await self._git(lease, ["reset", "--quiet", "--hard", "HEAD"], what="丢弃改动")
            else:
                await self._git(lease, ["read-tree", "--empty"], what="丢弃改动")  # 还没有提交：清空暂存
            # 未跟踪的文件与目录；.gitignore 忽略的不删（没有 -x）。
            await self._git(lease, ["clean", "--quiet", "-f", "-d"], what="丢弃改动")
        return {"head": head}

    async def sync(self, inp: workspace_pb2.WorkspaceOperationInput, args: dict[str, Any]) -> dict[str, Any]:
        owner, ws = inp.tenant.tenant_id, inp.workspace_id
        record = await self._record(inp)
        branch = check_branch(str(args.get("branch") or ""))
        if not record.repo_url:
            raise InvalidError("no_remote", "这个工作区不是从仓库克隆的，没有可以同步的远端")
        remote = self._remote(record.repo_url)
        credential = await self._credential(inp, remote)
        async with self._lease(inp, writable=True, remote=remote) as lease:
            status = await self._git(lease, ["status", "--porcelain"], what="检查改动")
            if status.stdout.strip():
                raise ConflictError("workspace_dirty", "工作区还有未提交的改动：先提交或丢弃")
            # 远端用业务库里记的地址，远端分支记在 origin/<分支> 下（之后 GetLog 的 base 可以写 origin/<分支>）。
            # 取的时候在临时仓库下（对象直接进工作副本的对象库），之后再把引用与 shallow 交给工作副本。
            ref = f"refs/remotes/origin/{branch}"
            async with self._detached(lease) as d:
                await self._remote_git(lease, d, ["fetch", "--quiet", "--depth", "50", "--", remote.url, f"+refs/heads/{branch}:{ref}"],
                                       credential=credential, what="取远端分支")
                fetched = (await self._remote_git(lease, d, ["rev-parse", "--verify", "--quiet", ref], what="读远端分支")).stdout.decode().strip()
                if not _SHA.match(fetched):
                    raise NotFoundError("git_not_found", f"远端没有分支 {branch}")
                # --depth 更新的是临时仓库的 shallow（进来时按工作副本的填好了），原样交回工作副本。
                if await self._is_shallow(lease, d):
                    await self._copy_file(lease, f"{d.git_dir}/shallow", d.shallow)
            await self._git(lease, ["update-ref", ref, fetched], what="记远端分支")
            await self._git(lease, ["checkout", "--quiet", "-B", branch, ref], what="切到远端分支")
            head = await self._head(lease)
        await self._archiver.update(owner, ws, head=head)
        return {"head": head, "branch": branch}

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
        credential = await self._credential(inp, remote)
        await self._current(owner, ws)
        async with self._lease(inp, writable=False, remote=remote) as lease:
            sha = await self._head(lease)
            if not sha:
                raise InvalidError("nothing_to_push", "工作区还没有提交")
            key = push_key(owner, ws, branch, sha)
            approved = inp.requested_by or "workspace-api"
            await self._gate(owner, key, ws=ws, branch=branch, sha=sha, remote=remote.url, approved_by=approved, operation_id=inp.operation_id)
            async with self._detached(lease) as d:
                # 先对账：远端已经是这个 sha（重复调用，或上一次超时其实推成了）就不推。
                listed = await self._remote_git(lease, d, ["ls-remote", "--", remote.url, f"refs/heads/{branch}"], credential=credential, what="查远端分支")
                current = listed.stdout.decode("utf-8", errors="replace").split("\t", 1)[0].strip()
                if current == sha:
                    await self._finish(owner, key, "reconciled")
                    return {"sha": sha, "branch": branch, "reconciled": True}
                try:
                    # 临时仓库没有 HEAD：推的是上面读到的那个提交。
                    await self._remote_git(lease, d, ["push", "--quiet", "--", remote.url, f"{sha}:refs/heads/{branch}"], credential=credential, what="推送")
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
            snap = await self._current(owner, ws)
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
        return {"snapshot_id": snap.snapshot_id, "files": snap.files, "size_bytes": str(snap.size_bytes)}

    async def set_environment(self, inp: workspace_pb2.WorkspaceOperationInput, args: dict[str, Any]) -> dict[str, Any]:
        """设置或清除选用的环境配置（ENV-02）：只影响之后新准备的环境；不起环境。"""
        owner, ws = inp.tenant.tenant_id, inp.workspace_id
        await self._record(inp)
        if args.get("environment"):
            config = EnvironmentConfig.from_args(args["environment"])
            await self._archiver.set_environment(owner, ws, config, owner_user=inp.requested_by or inp.tenant.user_id)
        else:
            await self._archiver.clear_environment(owner, ws)
        return {"workspace": workspace_dict(await self._record(inp))}

    async def delete(self, inp: workspace_pb2.WorkspaceOperationInput, args: dict[str, Any]) -> dict[str, Any]:
        owner, ws = inp.tenant.tenant_id, inp.workspace_id
        await self._record(inp)
        await self._broker.destroy_workspace(owner, ws)
        await self._archiver.delete(owner, ws)
        self._remove_host_dir(owner, ws)
        return {}

    async def _ensure_host_dir(self, owner: str, ws: str) -> None:
        """挂卷的后端：借环境之前以 Worker 的身份建好 <工作区根>/<租户>/<workspace>。K8s 后端按 subPath 挂，目录不在时
        kubelet 以 root 建，沙箱与 Worker 就都写不进去（工具一侧由 PlatformTools._workspace 建）。存档模式下不建。"""
        root = self._broker.sandboxes.workspace_root
        if root is None or not _SLUG.match(owner) or not _SLUG.match(ws) or await self._broker.archive_mode_for(owner):
            return
        (Path(root) / owner / ws).mkdir(parents=True, exist_ok=True)

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
        return await self._record_of(inp.tenant.tenant_id, inp.workspace_id)

    async def _record_of(self, owner: str, ws: str) -> WorkspaceRecord:
        record = await self._archiver.get(owner, ws)
        if record is None or record.state == "deleted":
            raise NotFoundError("workspace_not_found", "工作区不存在")
        return record

    async def _current(self, owner: str, ws: str) -> Any:
        """存档模式下把别人闲着的可写环境存一份快照（不销毁它），之后只读的环境看到的就是最新的；有命令在跑时
        workspace_busy。返回新存的快照（没有可写的环境时 None）。挂卷时什么也不做：只读的环境挂的就是同一处。"""
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

    async def _network_enforced(self, owner: str) -> bool:
        """后端约束沙箱的网络（经出网网关，或托管后端按域名放行）时才要联网授权；本地后端不隔离网络。"""
        broker = self._broker
        name = (await broker.sandboxes.policy_for(owner)).backend
        return broker.has_gateway(name) or broker.sandboxes.spec(name).get("kind") not in ("local", "k8s")

    @contextlib.asynccontextmanager
    async def _lease(
        self, inp: workspace_pb2.WorkspaceOperationInput, *, writable: bool, remote: Remote | None = None, workspace_id: str = "",
    ) -> Any:
        owner, ws = inp.tenant.tenant_id, workspace_id or inp.workspace_id
        broker = self._broker
        task_id = f"wsop-{inp.operation_id}"
        tenant = TenantContext(tenant_id=owner, user_id=inp.requested_by or inp.tenant.user_id, workspace_id=ws, session_id="")
        granted = False
        if remote is not None and not remote.local and broker.grants is not None and await self._network_enforced(owner):
            policy = NetworkPolicy.from_policy((await broker.sandboxes.policy_for(owner)).network)
            request = check_request(policy, hosts=[remote.host], ports=[remote.port], minutes=15, reason="Workspace API：克隆、推送或同步")
            await broker.grants.create(owner, request, policy, task_id=task_id, approved_by=inp.requested_by or "workspace-api", workspace_id=ws)
            granted = True
        spec = SandboxSpec(runtime=self.runtime, network="none", idle_timeout=timedelta(minutes=10), workspace_id=ws)
        await self._ensure_host_dir(owner, ws)
        try:
            with timing.phase("acquire"):
                lease = await broker.acquire(tenant, spec, task_id=task_id, writable=writable, reuse=False, busy_for=self.git_timeout + timedelta(minutes=5))
            meter = timing.current()
            if meter is not None:
                meter.labels.update({"workspace.mode": "archive" if lease.archive else "mount", "sandbox.backend": lease.backend.name})
            ok = False
            try:
                yield lease
                ok = True
            finally:
                with timing.phase("release"):
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
        stdin: bytes = b"", check: bool = True, config: tuple[tuple[str, str], ...] = (),
    ) -> ExecResult:
        config = [("safe.directory", "*"), ("core.hooksPath", NO_HOOKS), ("core.autocrlf", "false"), *config, *(credential.config if credential else ())]
        env = {**lease.env, **_GIT_ENV, "GIT_CONFIG_COUNT": str(len(config)), **(extra or {})}
        for i, (key, value) in enumerate(config):
            env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
        try:
            with timing.phase("command"):
                res = await lease.box.exec(lease.env_id, ["git", *argv], timeout=self.git_timeout, stdin=stdin, env=env)
            timing.exec_bytes(stdin, res)
        except OSError:
            raise InvalidError("git_unavailable", "沙箱里没有 git（运行环境要带 git，例如 universal）") from None
        if res.timed_out:
            raise TransientError("git_timeout", f"{what}超时")
        if check and res.exit_code != 0:
            raise _git_error(what, _redact(res.stderr.decode("utf-8", errors="replace"), credential))
        return res

    @contextlib.asynccontextmanager
    async def _detached(self, lease: Lease) -> AsyncIterator[Detached]:
        """建一个临时的裸仓库给连远端的 git 用（见 _DETACHED_ENV 上面的说明），用完删掉。工作副本是浅的时把 shallow 拷进去。"""
        paths = await self._git(lease, ["rev-parse", "--path-format=absolute", "--git-path", "objects", "--git-path", "shallow"], what="找对象库")
        objects, shallow = (paths.stdout.decode("utf-8", errors="replace").splitlines() + ["", ""])[:2]
        if not objects or not shallow:
            raise InvalidError("git_failed", "找不到工作副本的对象库")
        local = lease.backend.kind == "local"
        git_dir = Path(tempfile.mkdtemp(prefix="agentruntime-git-")).as_posix() if local else DETACHED_PREFIX + secrets.token_hex(8)
        try:
            await self._git(lease, ["init", "--quiet", "--bare", "--template=", git_dir], what="准备临时仓库", extra=_DETACHED_ENV)
            d = Detached(git_dir=git_dir, objects=objects, shallow=shallow)
            if await self._is_shallow(lease):
                await self._copy_file(lease, shallow, f"{git_dir}/shallow")
            yield d
        finally:
            if local:
                shutil.rmtree(git_dir, ignore_errors=True)
            else:
                with contextlib.suppress(ProviderError, OSError):
                    await lease.box.exec(lease.env_id, ["rm", "-rf", "--", git_dir], timeout=self.git_timeout, env=lease.env)

    async def _remote_git(self, lease: Lease, d: Detached, argv: list[str], *, what: str, credential: Credential | None = None) -> ExecResult:
        """在临时的裸仓库下跑 git：不读工作副本与全局的配置，对象库是工作副本的。"""
        extra = {**_DETACHED_ENV, "GIT_OBJECT_DIRECTORY": d.objects}
        return await self._git(lease, ["--git-dir", d.git_dir, *argv], what=what, credential=credential, extra=extra, config=_DETACHED_CONFIG)

    async def _is_shallow(self, lease: Lease, d: Detached | None = None) -> bool:
        """工作副本（或给了 d 时临时仓库）是不是浅的。"""
        argv = ["rev-parse", "--is-shallow-repository"]
        res = await (self._remote_git(lease, d, argv, what="查浅仓库") if d is not None else self._git(lease, argv, what="查浅仓库"))
        return res.stdout.strip() == b"true"

    async def _copy_file(self, lease: Lease, src: str, dst: str) -> None:
        """在环境里拷一个文件（两边都是环境里的绝对路径）。本地后端的命令就在本机跑，直接拷。"""
        if lease.backend.kind == "local":
            await asyncio.to_thread(shutil.copyfile, src, dst)
            return
        res = await lease.box.exec(lease.env_id, ["cp", "-f", "--", src, dst], timeout=self.git_timeout, env=lease.env)
        if res.exit_code != 0:
            raise InvalidError("git_failed", "拷贝 shallow 文件失败")

    async def _head(self, lease: Lease) -> str:
        res = await self._git(lease, ["rev-parse", "--verify", "--quiet", "HEAD"], what="读 HEAD", check=False)
        sha = res.stdout.decode().strip() if res.exit_code == 0 else ""
        return sha if _SHA.match(sha) else ""

    async def _branch(self, lease: Lease) -> str:
        res = await self._git(lease, ["symbolic-ref", "--quiet", "--short", "HEAD"], what="读分支", check=False)
        return res.stdout.decode("utf-8", errors="replace").strip() if res.exit_code == 0 else ""

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

    async def _credential(self, inp: workspace_pb2.WorkspaceOperationInput, remote: Remote) -> Credential | None:
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
        # 只对远端这台主机生效（重定向到别的主机时不带）；本地的远端用不上凭据。
        config = () if remote.local else ((f"http.{remote.prefix}.extraHeader", header),)
        return Credential(config=config, secrets=tuple(s for s in (password, token, header) if s))

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


def parse_numstat(text: str) -> list[dict[str, Any]]:
    """git diff --numstat -z 的输出：一般是 “增\t删\t路径\0”，改名是 “增\t删\t\0旧路径\0新路径\0”；二进制文件的行数是 -。"""
    tokens = text.split("\x00")
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(tokens):
        fields = tokens[i].split("\t")
        if len(fields) != 3:
            i += 1
            continue
        added, deleted, path = fields
        if path == "" and i + 2 < len(tokens):
            path = tokens[i + 2]  # 改名：取新路径
            i += 3
        else:
            i += 1
        binary = added == "-" or deleted == "-"
        out.append({"path": path, "added": 0 if binary else int(added or 0), "deleted": 0 if binary else int(deleted or 0), "binary": binary})
    return out


def _has_worktree_files(root: Path) -> bool:
    return any(p.is_file() for p in root.rglob("*") if ".git" not in p.relative_to(root).parts[:1])


def _host_head(root: Path) -> tuple[str, str]:
    """挂卷时复制完直接读 HEAD（不为它起环境）。"""
    import asyncio as _asyncio

    from agentruntime_worker.workspace.archive import git_head

    async def read(path: str) -> bytes | None:
        target = root / path
        return target.read_bytes() if target.is_file() and not target.is_symlink() else None

    return _asyncio.run(git_head(read))


async def _file_count(lease: Lease) -> int:
    """环境里工作区的文件数，不算 .git 里的。"""
    with timing.phase("command"):
        entries = await lease.box.manifest(lease.env_id)
    return sum(1 for e in entries if e.type == "file" and e.path != ".git" and not e.path.startswith(".git/"))


def _initial_files(value: Any) -> list[tuple[str, str]]:
    if not value:
        return []
    if not isinstance(value, list) or len(value) > MAX_INITIAL_FILES:
        raise InvalidError("invalid_files", f"files 最多 {MAX_INITIAL_FILES} 个")
    out: list[tuple[str, str]] = []
    total = 0
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or not isinstance(item.get("content", ""), str):
            raise InvalidError("invalid_files", "files 的每一项要是 {path, content}")
        path = check_path(str(item.get("path") or ""))
        content = str(item.get("content") or "")
        total += len(content.encode("utf-8"))
        if path in seen:
            raise InvalidError("invalid_files", f"files 里 {path} 重复了")
        seen.add(path)
        out.append((path, content))
    if total > MAX_INITIAL_BYTES:
        raise InvalidError("invalid_files", "files 合计最多 1 MiB")
    return out


def _author(args: Mapping[str, Any]) -> tuple[str, str]:
    name = str(args.get("author_name") or DEFAULT_AUTHOR[0])
    email = str(args.get("author_email") or DEFAULT_AUTHOR[1])
    if any(c in name + email for c in "<>\n\x00") or len(name) > 200 or len(email) > 200:
        raise InvalidError("invalid_author", "作者的名字或邮箱不合法")
    return name, email


def _author_config(author: tuple[str, str]) -> list[str]:
    return ["-c", f"user.name={author[0]}", "-c", f"user.email={author[1]}"]


def _require(box: Any, method: str) -> None:
    if not hasattr(box, method):
        raise InvalidError("unsupported_archive", "这个沙箱后端不能导出、导入或浏览工作区")


# 冷热分层（DISK-B）的 cooling、cold、restoring 都是“不在本地盘上、有存档”：ARCHIVED。
_STATES = {"active": "WORKSPACE_STATE_ACTIVE", "deleted": "WORKSPACE_STATE_DELETED"}


def workspace_dict(record: WorkspaceRecord, *, branch: str = "") -> dict[str, Any]:
    """与 workspace.v1.Workspace 的 JSON 写法一致（task-service 按 protojson 读）。"""
    out: dict[str, Any] = {
        "workspace_id": record.workspace_id, "state": _STATES.get(record.state, "WORKSPACE_STATE_ARCHIVED"),
        "repo_url": record.repo_url, "head": record.head, "snapshot_id": record.snapshot_id, "size_bytes": str(record.size_bytes), "files": record.files,
        "branch": branch, "environment_id": record.environment_id,
    }
    for name in ("created_at", "updated_at"):
        if getattr(record, name):
            out[name] = getattr(record, name)
    return out


def _op_name(operation: int) -> str:
    """span 与指标里的操作名：WORKSPACE_OPERATION_LIST_FILES → list_files。"""
    try:
        return Op.Name(operation).removeprefix("WORKSPACE_OPERATION_").lower()
    except ValueError:
        return "unknown"


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
    if any(s in lower for s in (
        "not found", "does not exist", "couldn't find remote ref", "unknown revision", "bad revision", "invalid upstream",
        "not our ref", "unadvertised object", "bad object",
    )):
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
                message="这个 Worker 没有配沙箱，也没有配工作区目录（AR_TOOL_WORKSPACE_ROOT），不能执行工作区的操作",
            ))
        # 排队（PERF-00）：这次尝试从排进 program 队列到被一个 Worker 取走，查看文件与 Task 的 activity 抢的就是这一段。
        info = activity.info()
        queued_ms = (info.started_time - info.current_attempt_scheduled_time).total_seconds() * 1000
        return await self.operations.run(inp, queued_ms=queued_ms)

    def activities(self) -> dict[str, object]:
        return {names.WORKSPACE_OPERATION: self.workspace_operation}


__all__ = [
    "CREDENTIAL_SECRET", "WorkspaceActivities", "WorkspaceOperations", "branch_allowed", "check_branch", "check_path", "parse_numstat", "push_key",
]
