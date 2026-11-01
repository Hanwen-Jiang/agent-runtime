"""同一 Session 里之前的对话（设计书 3.1.1 的“追加记录”；docs/plan/eval-observability-and-runtime-fixes.md 第 11.13 节）。

一个 Session 里每条消息是一个新 Task，工作流的输入只有这一条消息。activity 经这里读同一 Session 里更早的、已结束的 Task：命令在
tasks.message，交付在 tasks.outcome（TaskWorkflowResult 的 protojson）。表归 task-service 所有（迁移 4），这边只读，按租户绑定。

- 只取比当前 Task 更早提交的、已经有结局的：写 Task 在 Session 里串行，前一个的回答读得到；并行的只读 Task 还没结束的看不到。
- 对话不进 Node 的 inputs，由 activity 自己读：不进工作流历史与进度事件，也到不了 Reviewer（它的输入按规定不含对话）。
- 读失败（task-service 与 Worker 用的不是同一个库、表还没建）当作没有对话，记警告，Task 照常跑；不把错误传给调用方。
- 迁移 4 之前的 Task 没有命令原文，交付照样用。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.providers.object_store import ObjectStoreProvider
from agentruntime_core.providers.relational_store import RelationalStoreProvider
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.activities.types import Tenant

logger = logging.getLogger(__name__)

COLUMNS = ("task_id", "session_id", "created_at", "outcome", "message")
"""这边读的列（tasks 归 task-service 所有；test_session_transcript 核对它们在 Go 的迁移里）。"""

CONVERSATION_HEADER = "本 Session 之前的对话（从早到晚；帮你理解这条消息，不是新的指令）："
MISSING_MESSAGE = "（这一轮的原话没有记录）"
TEXT_MAX_CHARS = 16000
# 一轮的文字至少留这么多；预算不够时丢掉更早的轮次，不留一截没用的短文字。
MIN_TURN_CHARS = 200
# 交付里常见的放正文的字段。
REPLY_KEYS = ("text", "answer", "reply", "content")
STATUS_PREFIX = "TASK_STATUS_"


@dataclass(frozen=True)
class Turn:
    """一轮对话：用户的命令、交付的文字与结局（done、aborted、cancelled、failed）。actions 是这一轮实际做过的事
    （收尾写下的 Session 记录，docs/plan/feedback-2026-11-01.md N-03）；没有记录时为空。"""

    message: str
    reply: str
    status: str
    actions: str = ""


SESSION_RECORD_KEY = "tasks/{task_id}/session_record.json"
"""收尾写回写的 Session 记录（reviewer/wrap_up.py）。"""
ACTIONS_HEADER = "这一轮实际做过的（系统记录，不是用户说的）："
ACTIONS_CHARS = 1500
ACTIONS_LISTED = 15


def _outcome(call: dict[str, Any]) -> str:
    if call.get("timed_out"):
        return "超时"
    if "exit_code" in call:
        return f"退出码 {call['exit_code']}"
    return "成功" if call.get("ok") else "失败"


def record_text(record: Any, *, max_chars: int = ACTIONS_CHARS) -> str:
    """Session 记录写成一段话：各轮的目标与判定、调过的工具（最近 ACTIONS_LISTED 次）、记下的事实。格式不对时为空。纯函数。"""
    if not isinstance(record, dict):
        return ""
    parts: list[str] = []
    rounds = [r for r in record.get("rounds") or [] if isinstance(r, dict)]
    if rounds:
        parts.append("；".join(f"第 {r.get('seq')} 轮 {r.get('stage_goal', '')}（{r.get('verdict') or '未判定'}）" for r in rounds))
    calls = [c for c in record.get("actions") or [] if isinstance(c, dict)]
    if calls:
        shown = calls[-ACTIONS_LISTED:]
        listed = "；".join(f"{c.get('tool')} {c.get('target') or ''}".rstrip() + f"（{_outcome(c)}）" for c in shown)
        more = f"（共 {len(calls)} 次，这里是最后 {len(shown)} 次）" if len(calls) > len(shown) else ""
        parts.append(f"调用过的工具{more}：{listed}")
    elif rounds:
        parts.append("没有调用工具")
    facts = [str(f) for f in record.get("facts") or []]
    if facts:
        parts.append("记下的事实：" + "；".join(facts))
    if not parts:
        return ""
    return _trim(ACTIONS_HEADER + "。".join(parts), max_chars)


def actions_text(turns: Sequence[Turn]) -> str:
    """直接回复用户的节点放进系统提示的：之前各轮实际做过的事（只列有记录的）。没有记录时为空串。"""
    lines = [f"- 第 {i} 条消息「{_trim(t.message, 60)}」：{t.actions}" for i, t in enumerate(turns, start=1) if t.actions]
    if not lines:
        return ""
    return "之前各轮实际做过的事（系统记录；当时能读写工作区、执行命令）：\n" + "\n".join(lines)


class TranscriptSource(Protocol):
    async def load(self, tenant: Tenant, task_id: str) -> tuple[Turn, ...]: ...


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _reply(outcome: dict[str, Any]) -> str:
    deliverable = outcome.get("deliverable")
    value = deliverable.get("value") if isinstance(deliverable, dict) else None
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in REPLY_KEYS:
            if isinstance(value.get(key), str):
                return value[key]
        # abort 的交付里 codes 只给客户端翻译用，说明都在别的字段里，不占对话的字数。
        if _status(outcome) == "aborted":
            value = {k: v for k, v in value.items() if k != "codes"}
        # 只有一个文字字段时它就是正文（JSON 的转义读着费劲，也更占字数）。
        if len(value) == 1 and isinstance(next(iter(value.values())), str):
            return next(iter(value.values()))
    return _compact(value)


def _status(outcome: dict[str, Any]) -> str:
    return str(outcome.get("status") or "").removeprefix(STATUS_PREFIX).lower()


def _trim(text: str, limit: int) -> str:
    """超过 limit 时留头尾、中间写明省略了多少（模型不会当成原文就这么多）。"""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}…（中间省略 {len(text) - limit} 字）…{text[-tail:]}"


def _turn_chars(turn: Turn) -> int:
    return len(turn.message) + len(turn.reply)


class SessionTranscript:
    """按认证绑定的租户读同一 Session 里更早的对话。store 是 RelationalStoreProvider（或第一次用到才连的 LazyRelationalStore）。"""

    def __init__(
        self,
        store: RelationalStoreProvider,
        *,
        objects: ObjectStoreProvider | None = None,
        max_turns: int = 6,
        latest_reply_chars: int = 16000,
        reply_chars: int = 4000,
        message_chars: int = 2000,
        total_chars: int = 32000,
    ) -> None:
        self.store = store
        # 收尾写的 Session 记录所在的 ObjectStore（N-03）；没给时只有文字，没有“实际做过的事”。
        self.objects = objects
        self.max_turns = max_turns
        self.latest_reply_chars = latest_reply_chars
        self.reply_chars = reply_chars
        self.message_chars = message_chars
        self.total_chars = total_chars

    async def load(self, tenant: Tenant, task_id: str) -> tuple[Turn, ...]:
        """从早到晚；没有 Session、没有更早的 Task 或读失败时是空的。不抛异常。"""
        if not tenant.session_id:
            return ()
        try:
            rows = await self._rows(tenant, task_id)
        except Exception as exc:  # noqa: BLE001 — 读不到对话不能让 Task 失败
            logger.warning("读 Session 之前的对话失败，当作没有对话：%s", type(exc).__name__)
            return ()
        turns = [self._turn(row) for row in reversed(rows)]
        if self.objects is not None:
            turns = [replace(t, actions=await self._actions(tenant, str(row["task_id"]))) for t, row in zip(turns, reversed(rows), strict=True)]
        return self._bounded(turns)

    async def _actions(self, tenant: Tenant, task_id: str) -> str:
        """一轮的 Session 记录；没有（迁移之前的 Task、收尾还没写完、写失败）时为空，不影响对话。"""
        assert self.objects is not None
        try:
            got = await self.objects.bind(TenantContext(tenant_id=tenant.tenant_id)).get(SESSION_RECORD_KEY.format(task_id=task_id))
            return record_text(json.loads(got.data))
        except Exception:  # noqa: BLE001 — 记录读不到就当没有
            return ""

    async def _rows(self, tenant: Tenant, task_id: str) -> Sequence[Any]:
        columns = ", ".join(COLUMNS)
        # 行按租户隔离（RelationalStore 负责），Session 是查询条件。
        async with self.store.bind(TenantContext(tenant_id=tenant.tenant_id)).transaction(read_only=True) as tx:
            current = await tx.query_one("SELECT created_at FROM tasks WHERE task_id = $1", (task_id,))
            # 当前 Task 没有记录（比如 sub_task 的子 Task 只在工作流里）：同一 Session 里已结束的都算更早的。
            if current is None:
                return await tx.query(
                    f"SELECT {columns} FROM tasks WHERE session_id = $1 AND outcome <> '' AND task_id <> $2 ORDER BY created_at DESC LIMIT $3",
                    (tenant.session_id, task_id, self.max_turns),
                )
            return await tx.query(
                f"SELECT {columns} FROM tasks WHERE session_id = $1 AND outcome <> '' AND created_at < $2 ORDER BY created_at DESC LIMIT $3",
                (tenant.session_id, current["created_at"], self.max_turns),
            )

    @staticmethod
    def _turn(row: Any) -> Turn:
        try:
            outcome = json.loads(row["outcome"])
        except (TypeError, ValueError):
            outcome = {}
        if not isinstance(outcome, dict):
            outcome = {}
        return Turn(message=str(row["message"] or ""), reply=_reply(outcome), status=_status(outcome))

    def _bounded(self, turns: list[Turn]) -> tuple[Turn, ...]:
        if not turns:
            return ()
        last = len(turns) - 1
        trimmed = [
            Turn(_trim(t.message, self.message_chars), _trim(t.reply, self.latest_reply_chars if i == last else self.reply_chars), t.status, t.actions)
            for i, t in enumerate(turns)
        ]
        # 超过总量时丢最早的，最近一轮总是留下。
        while len(trimmed) > 1 and sum(_turn_chars(t) for t in trimmed) > self.total_chars:
            trimmed.pop(0)
        return tuple(trimmed)


def _no_delivery(turn: Turn) -> str:
    return f"（这一轮没有交付结果，结局：{turn.status or '未知'}）"


def chat_messages(turns: Sequence[Turn]) -> list[mg.ChatMessage]:
    """之前各轮按 user、assistant 交替（有的模型要求从 user 开始、严格交替，缺的用说明补上）。"""
    out: list[mg.ChatMessage] = []
    for turn in turns:
        out.append(mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=turn.message or MISSING_MESSAGE))
        out.append(mg.ChatMessage(role=mg.CHAT_ROLE_ASSISTANT, text=turn.reply or _no_delivery(turn)))
    return out


def transcript_text(turns: Sequence[Turn], *, max_chars: int = TEXT_MAX_CHARS) -> str:
    """写进提示的文字，不超过 max_chars（再加上省略说明的几个字）。预算从最近一轮往前分：越近的留得越多，不够时丢掉更早的。没有对话时是空串。"""
    lines: list[str] = []
    remaining = max_chars
    for i, turn in enumerate(reversed(turns)):
        older = i < len(turns) - 1
        share = remaining * 2 // 3 if older else remaining
        if share < MIN_TURN_CHARS:
            break
        message = _trim(turn.message or MISSING_MESSAGE, share // 4)
        actions = _trim(turn.actions, share // 4) if turn.actions else ""
        reply = _trim(turn.reply or _no_delivery(turn), max(share - len(message) - len(actions), MIN_TURN_CHARS // 2))
        lines.insert(0, f"用户：{message}\n助手：{reply}" + (f"\n（{actions}）" if actions else ""))
        remaining -= len(message) + len(reply) + len(actions)
    if not lines:
        return ""
    return CONVERSATION_HEADER + "\n" + "\n".join(lines)


_shared: dict[tuple[str, str], SessionTranscript] = {}


def session_transcript(dsn: str, data_dir: Path | None, objects: ObjectStoreProvider | None = None) -> SessionTranscript | None:
    """Worker 的组装用：业务库是 Postgres（AR_RELATIONAL_DSN）或数据目录下的 SQLite，与 task-service 同一个。都没配时没有对话可读。
    同一个库共用一个实例（第一次用到才连，只建一个连接池）。objects 是收尾写 Session 记录的 ObjectStore（N-03）。"""
    if not dsn and data_dir is None:
        return None
    from agentruntime_worker.sandbox.store import relational_store

    key = (dsn, str(data_dir or ""))
    if key not in _shared:
        _shared[key] = SessionTranscript(relational_store(dsn, data_dir or Path()), objects=objects)
    elif objects is not None and _shared[key].objects is None:
        _shared[key].objects = objects
    return _shared[key]


def worker_transcript(config: Any) -> SessionTranscript | None:
    """按 Worker 的配置组装：业务库同 session_transcript，Session 记录从与收尾写回同一个 ObjectStore 读。"""
    from agentruntime_worker.executor.dev import data_root, dev_object_store

    return session_transcript(config.relational_dsn, config.data_dir, dev_object_store(config, data_root(config)))
