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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
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
    """一轮对话：用户的命令、交付的文字与结局（done、aborted、cancelled、failed）。"""

    message: str
    reply: str
    status: str


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
        max_turns: int = 6,
        latest_reply_chars: int = 16000,
        reply_chars: int = 4000,
        message_chars: int = 2000,
        total_chars: int = 32000,
    ) -> None:
        self.store = store
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
        return self._bounded([self._turn(row) for row in reversed(rows)])

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
            Turn(_trim(t.message, self.message_chars), _trim(t.reply, self.latest_reply_chars if i == last else self.reply_chars), t.status)
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
        reply = _trim(turn.reply or _no_delivery(turn), max(share - len(message), MIN_TURN_CHARS // 2))
        lines.insert(0, f"用户：{message}\n助手：{reply}")
        remaining -= len(message) + len(reply)
    if not lines:
        return ""
    return CONVERSATION_HEADER + "\n" + "\n".join(lines)


_shared: dict[tuple[str, str], SessionTranscript] = {}


def session_transcript(dsn: str, data_dir: Path | None) -> SessionTranscript | None:
    """Worker 的组装用：业务库是 Postgres（AR_RELATIONAL_DSN）或数据目录下的 SQLite，与 task-service 同一个。都没配时没有对话可读。
    同一个库共用一个实例（第一次用到才连，只建一个连接池）。"""
    if not dsn and data_dir is None:
        return None
    from agentruntime_worker.sandbox.store import relational_store

    key = (dsn, str(data_dir or ""))
    if key not in _shared:
        _shared[key] = SessionTranscript(relational_store(dsn, data_dir or Path()))
    return _shared[key]
