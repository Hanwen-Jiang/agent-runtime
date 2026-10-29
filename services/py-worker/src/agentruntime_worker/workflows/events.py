"""进度事件（设计书 4.14、附录 K.1；TMP 计划 T-11b，决定见该计划删除前最后一版的 T-11b，取法见 docs/reference/risks-and-open-questions.md 第 3 节）。

Task 工作流把进度事件交给 Go 的 Session Worker：跨语言 activity publish_task_event（session 队列，名字见 names.proto 的
ActivityName），参数是租户上下文、去重键与 taskapi.v1.TaskEvent；由它分配 event_id、推进 Cache，客户端经 SSE 收到。

- 事件：round_started、stage_started、node_finished（Stage 的顶层 Node）、human_input_required、verdict。载荷带 event
  （与 SSE 的事件名相同），schema 是 /schemas/task_event_payload.schema.json；不带 Node 的结果与 rationale。
- 每个事件都等推送完成再往下走：event_id 按发生的先后排，Task 结束前的事件都排在 task_finished 前面。
- 去重键 r<Round>/<事件>[/<Node 或问题>]，重放与 activity 重试都不变。
- 推送失败不让 Task 失败：记一条警告，这个 Round 余下的事件不再推，下一个 Round 再试。
- 只有顶层的 Task 推送（子 Task、sub_agent 等子工作流不推）；新的运行才推（版本分支 patches.EVENTS_PATCH）。
- 2026-10-28（S-01 人审通过，版本分支 patches.PROGRESS_DETAIL_PATCH）：顶层 Node 开始执行时 node_started；顶层 Goal Node
  的 tool_loop 里每次工具调用前 tool_status running、后 succeeded 或 failed（用时、新建沙箱环境时的冷启动时间）。target 是
  tool_target 给出的简短说明，经凭据脱敏；不带工具的输出。
- TMP 计划（eval-observability-and-runtime-fixes）第 3～5 节：决策调用重试之前推 decision_retry（DECISION_RETRY_PATCH）；
  node_finished、human_input_required、verdict 带上给客户端翻译用的代码与参数（有的时候）。

是工作流代码：时间用 workflow.now()，不做 I/O。
"""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import FailureError

with workflow.unsafe.imports_passed_through():
    from google.protobuf import json_format, struct_pb2

    from agentruntime.common.v1 import payload_pb2, tenant_pb2
    from agentruntime.taskapi.v1 import task_api_pb2

    from agentruntime_worker import names
    from agentruntime_worker.activities.types import StageResult, Tenant
    from agentruntime_worker.workflows.failures import caused_by_cancel, failure_message

PUBLISH_START_TO_CLOSE = timedelta(seconds=5)
# 连排队带重试的上限：Session Worker 不在时，一个 Round 最多多等这么久（之后这个 Round 不再推）。
PUBLISH_SCHEDULE_TO_CLOSE = timedelta(seconds=10)
PUBLISH_RETRY = RetryPolicy(initial_interval=timedelta(milliseconds=500), backoff_coefficient=2.0, maximum_attempts=3)
ERROR_CHARS = 300
TARGET_CHARS = 160
TOOL_CHARS = 128

_TYPES = {
    "round_started": task_api_pb2.TASK_EVENT_TYPE_ROUND_STARTED,
    "stage_started": task_api_pb2.TASK_EVENT_TYPE_STAGE_STARTED,
    "node_started": task_api_pb2.TASK_EVENT_TYPE_NODE_STARTED,
    "node_finished": task_api_pb2.TASK_EVENT_TYPE_NODE_FINISHED,
    "tool_status": task_api_pb2.TASK_EVENT_TYPE_TOOL_STATUS,
    "human_input_required": task_api_pb2.TASK_EVENT_TYPE_HUMAN_INPUT_REQUIRED,
    "verdict": task_api_pb2.TASK_EVENT_TYPE_VERDICT,
    "decision_retry": task_api_pb2.TASK_EVENT_TYPE_DECISION_RETRY,
}
# defs.schema.json 的 message_code。
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# target 只取这些参数（按顺序第一个有的）：作用在什么上，不取环境变量、请求体之类。
_TARGET_ARGS = ("argv", "command", "path", "file", "url", "query", "q", "pattern", "name")
# 凭据的写法：先换掉带键名的（保留键名），再换掉网址里的账号口令与常见令牌前缀，最后是长的不透明串（字母与数字混在一起、32 个字符以上）。
_SECRETS = (
    (re.compile(r"(?i)\b(bearer|basic|token)(\s+)[A-Za-z0-9._~+/=-]{6,}"), r"\1\2***"),
    (re.compile(r"(?i)\b([A-Za-z0-9_]*(?:token|secret|passwd|password|pwd|api[_-]?key|access[_-]?key|credential)[A-Za-z0-9_]*)(\s*[=:]\s*)[^\s&'\"]+"), r"\1\2***"),
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://***@"),
    (re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghs|ghu|github_pat|glpat|xox[abps])[-_][A-Za-z0-9_-]{6,}"), "***"),
    (re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}\b"), "***"),
)


def redact(text: str) -> str:
    """把看起来像凭据的部分换成 ***（进度事件给浏览器看，不能带凭据）。"""
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


def tool_target(args: Any) -> str:
    """一次工具调用作用在什么上（路径、命令、网址……）：脱敏后截到 TARGET_CHARS；取不出时为空。纯函数，工作流里可用。"""
    if not isinstance(args, dict):
        return ""
    for key in _TARGET_ARGS:
        value = args.get(key)
        if isinstance(value, list) and value and all(isinstance(v, (str, int, float)) for v in value):
            value = " ".join(str(v) for v in value)
        if isinstance(value, str) and value.strip():
            text = redact(" ".join(value.split()))
            if key == "pattern" and isinstance(args.get("path"), str):
                text = f"{text} in {redact(args['path'])}"
            return text if len(text) <= TARGET_CHARS else text[: TARGET_CHARS - 1] + "…"
    return ""


def message_code(text: str) -> str:
    """把错误类型之类的名字整理成 message_code（snake_case）；整理不出来时为空。纯函数。"""
    snake = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\g<1>", str(text)).lower()
    snake = re.sub(r"[^a-z0-9_]+", "_", snake).strip("_")[:64]
    return snake if _CODE.match(snake) else ""


def sandbox_startup_ms(observation: Any) -> int | None:
    """工具的输出里报的沙箱冷启动时间（shell_run 新建了环境时才有）。"""
    value = observation.get("sandbox_startup_ms") if isinstance(observation, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _value(data: Any) -> struct_pb2.Value:
    value = struct_pb2.Value()
    json_format.ParseDict(data, value)
    return value


def stage_payload(stage: dict[str, Any], triage_level: str | None) -> dict[str, Any]:
    """stage_started：目标、退出条件与节点清单（只有 id、kind、role）。重跑时没有 triage_level。"""
    nodes = []
    for node in stage.get("nodes", []):
        entry = {"id": str(node["id"]), "kind": str(node.get("kind", "atomic"))}
        if node.get("role"):
            entry["role"] = str(node["role"])
        nodes.append(entry)
    payload: dict[str, Any] = {"goal": str(stage.get("goal", "")), "exit_when": str(stage.get("exit_when", "")), "nodes": nodes}
    if triage_level:
        payload["triage_level"] = triage_level
    return payload


def node_payload(node_id: str, result: StageResult) -> dict[str, Any]:
    """node_finished：状态、失败分类与截短的失败说明；不带结果。"""
    payload: dict[str, Any] = {"node_id": node_id, "role": result.role, "status": result.status}
    if result.failure_class:
        payload["failure_class"] = result.failure_class
    if result.error:
        payload["error"] = result.error[:ERROR_CHARS]
    if result.error_code and _CODE.match(result.error_code):
        payload["error_code"] = result.error_code
        if result.params:
            payload["params"] = dict(result.params)
    return payload


def node_started_payload(node: dict[str, Any]) -> dict[str, Any]:
    """node_started：id、kind，有的话加 role 与 executor（Goal Node 没有 executor）。"""
    payload: dict[str, Any] = {"node_id": str(node["id"]), "kind": str(node.get("kind", "atomic"))}
    if node.get("role"):
        payload["role"] = str(node["role"])
    if payload["kind"] == "atomic" and node.get("executor"):
        payload["executor"] = str(node["executor"])
    return payload


class EventPublisher:
    """一个 Task 的进度事件。只在工作流里用。detail 为 False（PROGRESS_DETAIL_PATCH 之前的运行）时不推 node_started、tool_status。"""

    def __init__(self, tenant: Tenant, task_id: str, *, detail: bool = False) -> None:
        self._tenant = tenant_pb2.TenantContext(
            tenant_id=tenant.tenant_id, user_id=tenant.user_id, workspace_id=tenant.workspace_id, session_id=tenant.session_id
        )
        self._task_id = task_id
        self._detail = detail
        # 推送失败的那个 Round：它余下的事件不再推。
        self._broken_round = 0

    async def publish(self, round_seq: int, event: str, payload: dict[str, Any], *, about: str = "") -> None:
        if round_seq == self._broken_round:
            return
        key = f"r{round_seq}/{event}" + (f"/{about}" if about else "")
        message = task_api_pb2.TaskEvent(
            task_id=self._task_id,
            session_id=self._tenant.session_id,
            type=_TYPES[event],
            payload=payload_pb2.JsonPayload(value=_value({"event": event, "round_seq": round_seq, **payload})),
        )
        message.emitted_at.FromDatetime(workflow.now())
        try:
            await workflow.execute_activity(
                names.PUBLISH_TASK_EVENT,
                args=[self._tenant, key, message],
                task_queue=names.SESSION_QUEUE,
                start_to_close_timeout=PUBLISH_START_TO_CLOSE,
                schedule_to_close_timeout=PUBLISH_SCHEDULE_TO_CLOSE,
                retry_policy=PUBLISH_RETRY,
                result_type=int,
            )
        except FailureError as err:
            if caused_by_cancel(err):
                raise asyncio.CancelledError() from err
            self._broken_round = round_seq
            workflow.logger.warning("推送进度事件 %s 失败，第 %d 轮余下的事件不再推：%s", key, round_seq, failure_message(err))

    async def round_started(self, round_seq: int, *, rerun: bool) -> None:
        await self.publish(round_seq, "round_started", {"rerun": rerun})

    async def stage_started(self, round_seq: int, stage: dict[str, Any], triage_level: str | None) -> None:
        await self.publish(round_seq, "stage_started", stage_payload(stage, triage_level))

    async def node_started(self, round_seq: int, node: dict[str, Any]) -> None:
        if self._detail:
            await self.publish(round_seq, "node_started", node_started_payload(node), about=str(node["id"]))

    async def tool_status(
        self, round_seq: int, node_id: str, step: int, tool: str, state: str, *,
        target: str = "", duration_ms: int | None = None, startup_ms: int | None = None, error: str = "",
    ) -> None:
        """tool_loop 里一次工具调用的状态；去重键 r<Round>/tool_status/<Goal Node>/<步>/<状态>。"""
        if not self._detail:
            return
        payload: dict[str, Any] = {"node_id": node_id, "step": step, "tool": tool[:TOOL_CHARS], "state": state}
        if target:
            payload["target"] = target
        if duration_ms is not None:
            payload["duration_ms"] = max(0, duration_ms)
        if startup_ms is not None:
            payload["sandbox_startup_ms"] = startup_ms
        if error:
            payload["error"] = redact(error)[:ERROR_CHARS]
        await self.publish(round_seq, "tool_status", payload, about=f"{node_id}/{step}/{state}")

    async def node_finished(self, round_seq: int, node_id: str, result: StageResult) -> None:
        await self.publish(round_seq, "node_finished", node_payload(node_id, result), about=node_id)

    async def human_input_required(
        self, round_seq: int, question_id: str, kind: str, text: str, options: tuple[str, ...], deadline: str, *,
        code: str = "", params: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"question_id": question_id, "kind": kind, "text": text, "options": list(options), "deadline": deadline}
        if code and _CODE.match(code):
            payload["question_code"] = code
            if params:
                payload["params"] = dict(params)
        await self.publish(round_seq, "human_input_required", payload, about=question_id)

    async def verdict(self, round_seq: int, verdict: dict[str, Any]) -> None:
        payload: dict[str, Any] = {"verdict": str(verdict["verdict"]), "reason": str(verdict.get("reason", ""))}
        code = str(verdict.get("reason_code") or "")
        if code and _CODE.match(code):
            payload["reason_code"] = code
            if verdict.get("reason_params"):
                payload["params"] = dict(verdict["reason_params"])
        await self.publish(round_seq, "verdict", payload)

    async def decision_retry(
        self, round_seq: int, step: str, attempt: int, max_attempts: int, *, node_id: str = "", error_code: str = "", error: str = "", about: str = "",
    ) -> None:
        """决策调用的第 attempt 次尝试之前（attempt ≥ 2）；去重键 r<Round>/decision_retry/<步>[/<about>]/<次>。"""
        payload: dict[str, Any] = {"step": step, "attempt": attempt, "max_attempts": max_attempts}
        if node_id:
            payload["node_id"] = node_id
        if error_code and _CODE.match(error_code):
            payload["error_code"] = error_code
        if error:
            payload["error"] = redact(error)[:ERROR_CHARS]
        await self.publish(round_seq, "decision_retry", payload, about="/".join(p for p in (step, about, str(attempt)) if p))
