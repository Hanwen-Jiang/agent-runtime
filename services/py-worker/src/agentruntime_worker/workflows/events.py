"""进度事件（设计书 4.14、附录 K.1；T-11b，决定见 docs/plan/TMP-completion-plan.md 的 T-11b）。

Task 工作流把进度事件交给 Go 的 Session Worker：跨语言 activity publish_task_event（session 队列，名字见 names.proto 的
ActivityName），参数是租户上下文、去重键与 taskapi.v1.TaskEvent；由它分配 event_id、推进 Cache，客户端经 SSE 收到。

- 事件：round_started、stage_started、node_finished（Stage 的顶层 Node）、human_input_required、verdict。载荷带 event
  （与 SSE 的事件名相同），schema 是 /schemas/task_event_payload.schema.json；不带 Node 的结果与 rationale。
- 每个事件都等推送完成再往下走：event_id 按发生的先后排，Task 结束前的事件都排在 task_finished 前面。
- 去重键 r<Round>/<事件>[/<Node 或问题>]，重放与 activity 重试都不变。
- 推送失败不让 Task 失败：记一条警告，这个 Round 余下的事件不再推，下一个 Round 再试。
- 只有顶层的 Task 推送（子 Task、sub_agent 等子工作流不推）；新的运行才推（版本分支 EVENTS_PATCH）。

是工作流代码：时间用 workflow.now()，不做 I/O。
"""

from __future__ import annotations

import asyncio
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

# T-11b 加入了推送进度事件的 activity，改变了命令序列：新的运行走这个版本分支，之前的历史照旧回放。
EVENTS_PATCH = "t11b-progress-events"
PUBLISH_START_TO_CLOSE = timedelta(seconds=5)
# 连排队带重试的上限：Session Worker 不在时，一个 Round 最多多等这么久（之后这个 Round 不再推）。
PUBLISH_SCHEDULE_TO_CLOSE = timedelta(seconds=10)
PUBLISH_RETRY = RetryPolicy(initial_interval=timedelta(milliseconds=500), backoff_coefficient=2.0, maximum_attempts=3)
ERROR_CHARS = 300

_TYPES = {
    "round_started": task_api_pb2.TASK_EVENT_TYPE_ROUND_STARTED,
    "stage_started": task_api_pb2.TASK_EVENT_TYPE_STAGE_STARTED,
    "node_finished": task_api_pb2.TASK_EVENT_TYPE_NODE_FINISHED,
    "human_input_required": task_api_pb2.TASK_EVENT_TYPE_HUMAN_INPUT_REQUIRED,
    "verdict": task_api_pb2.TASK_EVENT_TYPE_VERDICT,
}


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
    return payload


class EventPublisher:
    """一个 Task 的进度事件。只在工作流里用。"""

    def __init__(self, tenant: Tenant, task_id: str) -> None:
        self._tenant = tenant_pb2.TenantContext(
            tenant_id=tenant.tenant_id, user_id=tenant.user_id, workspace_id=tenant.workspace_id, session_id=tenant.session_id
        )
        self._task_id = task_id
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

    async def node_finished(self, round_seq: int, node_id: str, result: StageResult) -> None:
        await self.publish(round_seq, "node_finished", node_payload(node_id, result), about=node_id)

    async def human_input_required(self, round_seq: int, question_id: str, kind: str, text: str, options: tuple[str, ...], deadline: str) -> None:
        payload = {"question_id": question_id, "kind": kind, "text": text, "options": list(options), "deadline": deadline}
        await self.publish(round_seq, "human_input_required", payload, about=question_id)

    async def verdict(self, round_seq: int, verdict: dict[str, Any]) -> None:
        await self.publish(round_seq, "verdict", {"verdict": str(verdict["verdict"]), "reason": str(verdict.get("reason", ""))})
