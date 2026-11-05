"""联网授权（设计书改动稿 W8；docs/plan/cloud-deploy.md D-16；docs/plan/sandbox-platform.md 第 2 节的 sandbox_grants）。

沙箱默认断网，只连得到包镜像。要联网的 Task 申请授权：提交时声明（Task API 的 network_request，task-service 核对后直接
写进 sandbox_grants），或运行中由 agent 调 request_network 工具（write_irreversible，tool_loop 里逐次人工审批，D16）。
批准的授权就是 gate 记录（附录 D.4）：在它的主机、端口、时限与用量上限之内，出网网关（services/go-egress-gateway）
放行，不再逐次审批。

- 授权属于 Task，不属于环境：Task 结束（task-service 的 FinishTask）、到期或被撤销即失效；会话级的环境还活着也一样。
- 范围由租户策略的 sandbox.network 定：allowed（不写为不允许）、hosts（能放行的主机范围，可以用 *. 开头）、
  max_grant_minutes、approver（submitter 或 admin）、单次授权的 max_up_bytes、max_down_bytes、max_conns、max_rps，
  preview（能不能开预览地址）。申请超出范围一律 denied 并说明原因，不悄悄截断。
- 主机只能是域名：IP 地址（任何写法）、localhost、集群内的名字（.local、.internal、.svc 等）不收。网关自己解析并锁定地址，
  解析到内网的照样拒绝（那一侧的检查在网关）。
- 时长只有 15、30、60 分钟三档（与 HumanChannel 问人时的选项一致），不超过策略的上限。
"""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from typing import Any

from agentruntime_core.errors import DeniedError, InvalidError
from agentruntime_core.providers.relational_store import RelationalStore, RelationalStoreProvider
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.sandbox.times import iso, utc_now

GRANT_MINUTES = (15, 30, 60)
DEFAULT_PORTS = (80, 443)
APPROVERS = ("submitter", "admin")
MAX_HOSTS = 20
# 与 tenant_policy.schema.json 的 host_pattern 相同：最后一段以字母开头，所以 IP 的点分写法不合法。
_HOST = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*\.[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")
# 集群内与本机的名字：解析到哪里都不放行（网关同样拦）。
_INTERNAL_SUFFIXES = (".local", ".localhost", ".internal", ".svc", ".cluster.local", ".home.arpa", ".lan", ".intranet", ".corp")


@dataclass(frozen=True)
class NetworkPolicy:
    """租户策略的 sandbox.network；0 表示不限。"""

    allowed: bool = False
    hosts: tuple[str, ...] = ()
    max_grant_minutes: int = 60
    approver: str = "submitter"
    max_up_bytes: int = 0
    max_down_bytes: int = 0
    max_conns: int = 0
    max_rps: int = 0
    preview: bool = False

    @classmethod
    def from_policy(cls, doc: Mapping[str, Any] | None) -> NetworkPolicy:
        doc = dict(doc or {})
        approver = str(doc.get("approver") or "submitter")
        if approver not in APPROVERS:
            raise InvalidError("invalid_network_policy", f"租户策略的 sandbox.network.approver 只能是 {'、'.join(APPROVERS)}")
        return cls(
            allowed=bool(doc.get("allowed", False)),
            hosts=tuple(str(h).lower() for h in doc.get("hosts") or ()),
            max_grant_minutes=int(doc.get("max_grant_minutes") or 60),
            approver=approver,
            max_up_bytes=int(doc.get("max_up_bytes") or 0),
            max_down_bytes=int(doc.get("max_down_bytes") or 0),
            max_conns=int(doc.get("max_conns") or 0),
            max_rps=int(doc.get("max_rps") or 0),
            preview=bool(doc.get("preview", False)),
        )

    @property
    def audited(self) -> bool:
        """有用量上限：只有经出网网关的后端做得到（托管后端只能按域名放行）。"""
        return bool(self.max_up_bytes or self.max_down_bytes or self.max_conns or self.max_rps)


def check_host(host: str) -> str:
    """规范化并校验一个申请里的主机（可以 *. 开头）；不合法 invalid。"""
    h = str(host).strip().lower().rstrip(".")
    bare = h[2:] if h.startswith("*.") else h
    try:
        ipaddress.ip_address(bare.strip("[]"))
        is_ip = True
    except ValueError:
        is_ip = False
    if is_ip or not _HOST.match(h) or len(h) > 253:
        raise InvalidError("invalid_host", f"{host!r} 不是合法的主机名（不能是 IP 地址，不带端口与路径）")
    if bare == "localhost" or any(bare.endswith(s) or bare == s[1:] for s in _INTERNAL_SUFFIXES):
        raise DeniedError("host_not_allowed", f"{host} 是本机或内网的名字，不放行")
    return h


def covers(pattern: str, host: str) -> bool:
    """pattern（策略或授权里的一项）是否覆盖 host（具体主机，或申请里的 *. 通配）。"""
    if pattern == host:
        return True
    if not pattern.startswith("*."):
        return False
    suffix = pattern[1:]  # ".example.com"
    target = host[1:] if host.startswith("*.") else host
    return target.endswith(suffix) and target != suffix[1:]


@dataclass(frozen=True)
class NetworkRequest:
    hosts: tuple[str, ...]
    ports: tuple[int, ...]
    minutes: int
    reason: str
    preview_port: int = 0


def check_request(
    policy: NetworkPolicy,
    *,
    hosts: Sequence[str],
    ports: Sequence[int] = (),
    minutes: int,
    reason: str = "",
    preview_port: int = 0,
    effect_ceiling: str = "",
) -> NetworkRequest:
    """按租户策略核对一次申请（Task API 的 network_request 与 request_network 工具共用这套规则，Go 一侧在
    task-service 的 taskapi/network.go，两边对着同一组用例测）。超出范围 denied，写法不对 invalid，都说明原因。"""
    if effect_ceiling == "read":
        raise DeniedError("network_read_only", "只读的 Task 不能申请联网（联网的副作用按不可逆写处理）")
    if not policy.allowed:
        raise DeniedError("network_not_allowed", "租户策略不允许沙箱联网（sandbox.network.allowed）")
    if not hosts:
        raise InvalidError("invalid_network_request", "要写明访问哪些主机")
    if len(hosts) > MAX_HOSTS:
        raise InvalidError("invalid_network_request", f"一次最多申请 {MAX_HOSTS} 个主机")
    normalized: list[str] = []
    for host in hosts:
        h = check_host(host)
        if not any(covers(p, h) for p in policy.hosts):
            raise DeniedError("host_not_allowed", f"{h} 不在租户策略能放行的主机范围里")
        if h not in normalized:
            normalized.append(h)
    out_ports: list[int] = []
    for port in ports or DEFAULT_PORTS:
        p = int(port)
        if not 1 <= p <= 65535:
            raise InvalidError("invalid_network_request", f"端口 {port} 不在 1 到 65535 之间")
        if p not in out_ports:
            out_ports.append(p)
    if int(minutes) not in GRANT_MINUTES:
        raise InvalidError("invalid_network_request", f"授权时长只能是 {'、'.join(map(str, GRANT_MINUTES))} 分钟")
    if int(minutes) > policy.max_grant_minutes:
        raise DeniedError("grant_too_long", f"租户策略的授权最长 {policy.max_grant_minutes} 分钟")
    if preview_port:
        if not policy.preview:
            raise DeniedError("preview_not_allowed", "租户策略不允许开预览地址（sandbox.network.preview）")
        if not 1 <= int(preview_port) <= 65535:
            raise InvalidError("invalid_network_request", "预览的端口不在 1 到 65535 之间")
    reason = str(reason or "").strip()
    if len(reason) > 500:
        raise InvalidError("invalid_network_request", "理由最多 500 个字符")
    return NetworkRequest(hosts=tuple(sorted(normalized)), ports=tuple(sorted(out_ports)), minutes=int(minutes), reason=reason, preview_port=int(preview_port or 0))


@dataclass(frozen=True)
class Grant:
    grant_id: str
    owner: str
    task_id: str
    hosts: tuple[str, ...]
    ports: tuple[int, ...]
    expires_at: str
    state: str
    created_at: str
    workspace_id: str = ""
    session_id: str = ""
    max_up_bytes: int = 0
    max_down_bytes: int = 0
    max_conns: int = 0
    max_rps: int = 0
    up_bytes: int = 0
    down_bytes: int = 0
    preview_port: int = 0
    preview_env: str = ""
    preview_target: str = ""
    approved_by: str = ""
    reason: str = ""
    revoked_at: str = ""

    @classmethod
    def from_row(cls, owner: str, row: Mapping[str, Any]) -> Grant:
        names = {f.name for f in fields(cls)} - {"owner", "hosts", "ports"}
        values = {k: row[k] for k in names if k in row}
        return cls(owner=owner, hosts=tuple(json.loads(row["hosts"] or "[]")), ports=tuple(int(p) for p in json.loads(row["ports"] or "[]")), **values)

    def active(self, now: datetime) -> bool:
        return self.state == "active" and self.expires_at > iso(now)

    @property
    def audited(self) -> bool:
        return bool(self.max_up_bytes or self.max_down_bytes or self.max_conns or self.max_rps)


_COLUMNS = (
    "grant_id", "task_id", "workspace_id", "session_id", "hosts", "ports", "expires_at", "max_up_bytes", "max_down_bytes", "max_conns", "max_rps",
    "up_bytes", "down_bytes", "preview_port", "state", "approved_by", "reason", "created_at", "revoked_at", "preview_env", "preview_target",
)


def new_grant_id() -> str:
    return "sbg-" + secrets.token_hex(8)


class GrantStore:
    """sandbox_grants 的读写。表的迁移归 Sandbox Service（SBX-01）；出网网关（Go）读同一张表，task-service 也写它。
    Worker 只在审批联网（request_network、Workspace 操作连远端）时写，读写都按租户绑定业务库。"""

    def __init__(self, store: RelationalStoreProvider, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._store = store
        self.clock = clock

    def bind(self, owner: str) -> RelationalStore:
        return self._store.bind(TenantContext(tenant_id=owner))

    async def create(
        self,
        owner: str,
        request: NetworkRequest,
        policy: NetworkPolicy,
        *,
        task_id: str,
        approved_by: str,
        workspace_id: str = "",
        session_id: str = "",
        preview_env: str = "",
        preview_target: str = "",
    ) -> Grant:
        now = self.clock()
        grant = Grant(
            grant_id=new_grant_id(), owner=owner, task_id=task_id, hosts=request.hosts, ports=request.ports,
            expires_at=iso(now + timedelta(minutes=request.minutes)), state="active", created_at=iso(now), workspace_id=workspace_id,
            session_id=session_id, max_up_bytes=policy.max_up_bytes, max_down_bytes=policy.max_down_bytes, max_conns=policy.max_conns,
            max_rps=policy.max_rps, preview_port=request.preview_port, preview_env=preview_env, preview_target=preview_target,
            approved_by=approved_by, reason=request.reason,
        )
        row = {c: getattr(grant, c) for c in _COLUMNS if c not in ("hosts", "ports")}
        row["hosts"] = json.dumps(list(grant.hosts))
        row["ports"] = json.dumps(list(grant.ports))
        async with self.bind(owner).transaction() as tx:
            await tx.execute(
                f"INSERT INTO sandbox_grants ({', '.join(_COLUMNS)}) VALUES ({', '.join(f'${i}' for i in range(1, len(_COLUMNS) + 1))})",
                [row[c] for c in _COLUMNS],
            )
        return grant

    async def for_task(self, owner: str, task_id: str, *, active_only: bool = True) -> list[Grant]:
        async with self.bind(owner).transaction(read_only=True) as tx:
            rows = await tx.query(f"SELECT {', '.join(_COLUMNS)} FROM sandbox_grants WHERE task_id = $1 ORDER BY created_at", (task_id,))
        grants = [Grant.from_row(owner, r) for r in rows]
        if active_only:
            now = self.clock()
            grants = [g for g in grants if g.active(now)]
        return grants

    async def revoke_task(self, owner: str, task_id: str) -> int:
        """收回这个 Task 的全部授权；返回收回了几个。Task 结束时 task-service 的 FinishTask 做同样的事。"""
        async with self.bind(owner).transaction() as tx:
            return await tx.execute(
                "UPDATE sandbox_grants SET state = 'revoked', revoked_at = $1 WHERE task_id = $2 AND state = 'active'", (iso(self.clock()), task_id)
            )

    async def revoke_all(self, owner: str, reason: str = "tenant deleted") -> int:
        async with self.bind(owner).transaction() as tx:
            return await tx.execute(
                "UPDATE sandbox_grants SET state = 'revoked', revoked_at = $1 WHERE state = 'active'", (iso(self.clock()),)
            )

    async def audit(self, owner: str, task_id: str) -> list[dict[str, Any]]:
        """出网网关记下的这个 Task 的每个连接（主机、字节数、时长、结果），按时间排。"""
        async with self.bind(owner).transaction(read_only=True) as tx:
            rows = await tx.query(
                "SELECT audit_id, grant_id, env_ref, direction, host, port, address, started_at, duration_ms, up_bytes, down_bytes, result, reason "
                "FROM sandbox_egress_audit WHERE task_id = $1 ORDER BY started_at, audit_id",
                (task_id,),
            )
        return [dict(r) for r in rows]


def union_hosts(grants: Iterable[Grant]) -> tuple[str, ...]:
    out: set[str] = set()
    for g in grants:
        out.update(g.hosts)
    return tuple(sorted(out))


__all__ = [
    "DEFAULT_PORTS", "GRANT_MINUTES", "Grant", "GrantStore", "NetworkPolicy", "NetworkRequest", "check_host", "check_request", "covers", "union_hosts",
]
