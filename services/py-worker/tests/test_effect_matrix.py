"""S-05 验收（与 S-07 的故障注入共用）：经真实的 Task 工作流、Stage Runner 与 Node Executor 跑 Node。

- 矩阵测试：每种 effect × 每种失败类型，断言重试次数、是否缓存、是否要求幂等键。
- 写操作在“已执行、结果未写入历史”时失联，恢复后外部只生效一次（可逆写由 activity 重试 + 幂等键去重，
  不可逆写不重试，由 Stage Runner 按幂等键对账）。
- UNKNOWN 路径：外部是否生效无法确认时记为 unknown，交给 Reviewer，不重复执行。
- human 节点的问题经 HumanChannel 发出，等待有持久化定时器，超时按 on_timeout 处理；写节点前的审批同样如此。

Planner、Reviewer 等决策用 stage_harness 的脚本，执行器用真实的 NodeExecutor（依赖是 S-02 的开发实现）。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agentruntime.common.v1 import enums_pb2
from agentruntime.workflow.v1 import task_pb2

from agentruntime_core.enums import Effect
from agentruntime_core.errors import NotFoundError, TransientError
from agentruntime_core.providers.human_channel import QuestionStatus
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker import names
from agentruntime_worker.activities.types import StagePolicy
from agentruntime_worker.executor import keys
from agentruntime_worker.executor.node_executor import channel_question_id
from agentruntime_worker.workflows.stage_runner import SubAgentWorkflow, SubgraphWorkflow
from agentruntime_worker.workflows.task import TaskWorkflow

from .executor_harness import RATE_SCHEMA, ExecutorFixture, ExternalLedger, HangAfterEffect, SwitchableTools, make_fixture, register_external
from .stage_harness import Script
from .support import task_input

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]

EFFECTS = ("none", "read", "write_reversible", "write_irreversible")
FAILURES = ("ok", "transient", "invalid_output", "invalid_arguments", "denied")
RETRY_MAX = 2


class ExecHarness:
    def __init__(self, client: Client, script: Script, fx: ExecutorFixture, switch: SwitchableTools) -> None:
        self.client = client
        self.script = script
        self.fx = fx
        self.switch = switch
        self.handles: list[Any] = []

    def one_node(self, node: dict[str, Any], *, scenario: str = "main") -> None:
        nodes = [node]
        deliver = node["id"]
        if node.get("role") != "deliver":
            # 交付只来自 role = deliver 的节点（附录 B.9；S-10 起工作流会校验）：写操作的 Node 后面接一个交付节点。
            deliver = "report"
            nodes.append({"id": deliver, "kind": "atomic", "template": "compute@v1", "executor": "program", "effect": "none", "role": "deliver", "after": [node["id"]]})
        self.script.plans[scenario] = [{"seq": 1, "goal": "执行一个 Node", "exit_when": "完成", "nodes": nodes}]
        self.script.deliver[scenario] = deliver

    async def start(self, **kwargs: Any) -> tuple[task_pb2.TaskWorkflowInput, Any]:
        inp = task_input(scenario="main", **kwargs)
        handle = await self.client.start_workflow(names.TASK_WORKFLOW, inp, id=inp.task_id, task_queue=names.TASK_QUEUE, result_type=task_pb2.TaskWorkflowResult)
        self.handles.append(handle)
        return inp, handle

    async def run(self, *, approve: str | None = None, **kwargs: Any) -> tuple[task_pb2.TaskWorkflowInput, task_pb2.TaskWorkflowResult]:
        inp, handle = await self.start(**kwargs)
        if approve:
            await self.wait_question(inp.task_id, approve)
            await handle.signal(names.HUMAN_REPLY_SIGNAL, task_pb2.HumanReplySignal(question_id=approve, option="approve", replied_by_user_id="u7"))
        return inp, await asyncio.wait_for(handle.result(), timeout=90)

    async def question_status(self, task_id: str, question_id: str) -> QuestionStatus | None:
        """HumanChannel 里的问题 id 带 Task 与 Round（同一租户内唯一）。"""
        channel = self.fx.humans.bind(TenantContext(tenant_id="t1", user_id="u1"))
        try:
            return await channel.status(channel_question_id(task_id, 1, question_id))
        except NotFoundError:
            return None

    async def wait_question(self, task_id: str, question_id: str, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while await self.question_status(task_id, question_id) != QuestionStatus.PENDING:
            if time.monotonic() > deadline:
                raise AssertionError(f"{timeout} 秒内问题 {question_id} 没有经 HumanChannel 发出")
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)

    def result_of(self, task_id: str, node_id: str) -> Any:
        return next(r for r in self.script.reviews if r.task_id == task_id).stage_results[node_id]


@pytest_asyncio.fixture(loop_scope="session")
async def exec_harness(temporal_env: WorkflowEnvironment, tmp_path: Path) -> AsyncIterator[ExecHarness]:
    switch_holder: list[SwitchableTools] = []

    def switchable(inner: Any) -> SwitchableTools:
        switch_holder.append(SwitchableTools(inner))
        return switch_holder[0]

    fx = make_fixture(tmp_path, tools=switchable)
    script = Script()
    client = temporal_env.client
    ex = fx.executor
    workers = [
        Worker(client, task_queue=names.TASK_QUEUE, workflows=[TaskWorkflow, SubAgentWorkflow, SubgraphWorkflow], activities=[script.run_hooks]),
        Worker(
            client,
            task_queue=names.DECISION_QUEUE,
            activities=[script.plan_round, script.review_round, script.finalize_task, script.load_stage_policy, script.resolve_catalog, script.expand_template, ex.ask_human, ex.cancel_human],
        ),
        Worker(client, task_queue=names.MODEL_QUEUE, activities=[ex.execute_model_node, script.tool_loop_step]),
        Worker(client, task_queue=names.PROGRAM_QUEUE, activities=[ex.execute_program_node, ex.reconcile_node, script.call_tool]),
    ]
    h = ExecHarness(client, script, fx, switch_holder[0])
    async with contextlib.AsyncExitStack() as stack:
        for w in workers:
            await stack.enter_async_context(w)
        try:
            yield h
        finally:
            for handle in h.handles:
                with contextlib.suppress(Exception):
                    await handle.terminate("测试结束")


def register_cell(fx: ExecutorFixture, effect: str, failure: str) -> str:
    """每个格子一个专用工具，effect 与 Node 一致。"""
    name = f"{effect}_{failure}"
    kwargs: dict[str, Any] = {"lookup": effect.startswith("write")}
    if failure == "invalid_output":
        kwargs["output_schema"] = RATE_SCHEMA

        async def wrong(_: Any) -> Any:
            return {"rate": "很高"}

        fx.behaviors[name] = wrong
    elif failure == "invalid_arguments":
        kwargs["input_schema"] = {"type": "object", "required": ["region"], "properties": {"region": {"type": "string"}}}
    elif failure == "denied":
        kwargs["permissions"] = frozenset({"never.granted"})
    elif failure == "transient":

        async def timeout(_: Any) -> Any:
            raise TransientError("timeout", "上游超时")

        fx.behaviors[name] = timeout
    fx.register(name, Effect(effect), **kwargs)
    return name


def cell_node(tool: str, effect: str) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": "n",
        "kind": "atomic",
        "template": f"{tool}@v1",
        "executor": "program",
        "effect": effect,
        "role": "act" if effect.startswith("write") else "deliver",
        "inputs": {"q": 1},
        "retry": {"max": RETRY_MAX},
        "budget": {"timeout_s": 20},
    }
    if effect in ("none", "read"):
        node["cache"] = {"ttl_s": 600}
    else:
        # Planner（或模型）写的幂等键不算数，Stage Runner 一律按规则重新生成。
        node["idempotency_key"] = "idk_written_by_the_planner"
    return node


EXPECTED = {
    # 失败类型 → (none/read 与 write_reversible 的调用次数, write_irreversible 的调用次数, 结局状态, 失败分类)
    "ok": (1, 1, "succeeded", ""),
    "transient": (RETRY_MAX + 1, 1, "failed", "transient"),
    "invalid_output": (1, 1, "failed", "invalid_output"),
    "invalid_arguments": (0, 0, "failed", ""),
    "denied": (0, 0, "failed", "denied"),
}


@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("effect", EFFECTS)
async def test_effect_by_failure_matrix(exec_harness: ExecHarness, effect: str, failure: str) -> None:
    h, fx = exec_harness, exec_harness.fx
    tool = register_cell(fx, effect, failure)
    node = cell_node(tool, effect)
    h.one_node(node)
    irreversible = effect == "write_irreversible"
    inp, _ = await h.run(effect_ceiling=enums_pb2.EFFECT_WRITE_IRREVERSIBLE, approve="n.1" if irreversible else None)

    calls, irreversible_calls, status, cls = EXPECTED[failure]
    expected_calls = irreversible_calls if irreversible else calls
    result = h.result_of(inp.task_id, "n")
    assert fx.calls[tool] == expected_calls, f"{effect} × {failure}：应执行 {expected_calls} 次，实际 {fx.calls[tool]}"
    if irreversible and failure == "transient":
        # 不可逆写操作超时：不重试，按幂等键对账，查不到时结果未知。
        assert result.status == "unknown", result
    else:
        assert result.status == status and result.failure_class == cls, result

    # 是否要求幂等键：写操作带代码生成的幂等键，只读不带。
    mine = [i for i in fx.invocations if i.arguments == {"q": 1} and i is not None]
    if expected_calls:
        expected_key = keys.idempotency_key("t1", inp.task_id, "n", {"q": 1}) if effect.startswith("write") else None
        assert {i.idempotency_key for i in mine[-expected_calls:]} == {expected_key}

    # 是否缓存：成功的 none、read 节点在下一个 Task 里同样的输入直接命中缓存；写操作不缓存。
    if failure == "ok":
        before = fx.calls[tool]
        await h.run(effect_ceiling=enums_pb2.EFFECT_WRITE_IRREVERSIBLE, approve="n.1" if irreversible else None)
        assert fx.calls[tool] == before + (0 if effect in ("none", "read") else 1)


async def test_write_executed_but_not_recorded_takes_effect_once(exec_harness: ExecHarness) -> None:
    h, fx = exec_harness, exec_harness.fx
    hang = HangAfterEffect(h.switch.inner, ("update_crm",))
    h.switch.wrap = hang
    node = {**cell_node("update_crm", "write_reversible"), "budget": {"timeout_s": 3}}
    h.one_node(node)
    inp, result = await h.run(effect_ceiling=enums_pb2.EFFECT_WRITE_REVERSIBLE)
    assert hang.hung == ["update_crm"], "第一次执行生效后失联"
    assert fx.calls["update_crm"] == 1, "activity 重试时同一幂等键命中记录，外部只生效一次"
    assert h.result_of(inp.task_id, "n").status == "succeeded"
    assert result.status == enums_pb2.TASK_STATUS_DONE


async def test_irreversible_write_lost_in_flight_is_reconciled_not_retried(exec_harness: ExecHarness) -> None:
    h, fx = exec_harness, exec_harness.fx
    hang = HangAfterEffect(h.switch.inner, ("pay",))
    h.switch.wrap = hang
    node = {**cell_node("pay", "write_irreversible"), "budget": {"timeout_s": 3}}
    h.one_node(node)
    inp, result = await h.run(effect_ceiling=enums_pb2.EFFECT_WRITE_IRREVERSIBLE, approve="n.1")
    assert fx.calls["pay"] == 1, "不可逆写操作不重试"
    r = h.result_of(inp.task_id, "n")
    assert r.status == "succeeded" and r.result == {"echo": {"q": 1}}, "按幂等键对账，外部已生效就取当时的结果"
    assert result.status == enums_pb2.TASK_STATUS_DONE


async def test_unconfirmable_write_is_unknown_and_goes_to_the_reviewer(exec_harness: ExecHarness) -> None:
    h, fx = exec_harness, exec_harness.fx
    hang = HangAfterEffect(h.switch.inner, ("send_mail",))
    h.switch.wrap = hang
    node = {**cell_node("send_mail", "write_irreversible"), "budget": {"timeout_s": 3}}
    h.one_node(node)
    inp, result = await h.run(effect_ceiling=enums_pb2.EFFECT_WRITE_IRREVERSIBLE, approve="n.1")
    assert fx.calls["send_mail"] == 1, "无法确认时也不重复执行"
    r = h.result_of(inp.task_id, "n")
    assert r.status == "unknown" and r.error
    assert result.status == enums_pb2.TASK_STATUS_ABORTED, "结果未知交给 Reviewer（脚本 Reviewer 判 abort）"


async def test_reconcile_retries_while_the_lookup_is_temporarily_unavailable(exec_harness: ExecHarness) -> None:
    """S-07 场景 3：响应丢失后，对账用的查询也暂时不可用。工作流用 reconcile_node 带重试地再查，不当作失败。"""
    h, fx = exec_harness, exec_harness.fx
    ledger = ExternalLedger(lose_response=True)
    ledger.lookup_failures = 2  # Node Executor 里查一次失败，reconcile_node 第一次也失败
    register_external(fx, "ext_transfer", Effect.WRITE_IRREVERSIBLE, ledger)
    node = cell_node("ext_transfer", "write_irreversible")
    del node["retry"]
    h.one_node(node)
    inp, result = await h.run(effect_ceiling=enums_pb2.EFFECT_WRITE_IRREVERSIBLE, approve="n.1")
    assert len(ledger.applied) == 1, "结果未知的不可逆写操作不重试"
    assert ledger.lookup_failures == 0
    r = h.result_of(inp.task_id, "n")
    assert r.status == "succeeded" and r.result == {"txn": "txn-1"}, "查询恢复后按幂等键对账成功"
    assert result.status == enums_pb2.TASK_STATUS_DONE


# --- human 节点与审批 ---


def human(node_id: str = "confirm", **inputs: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": "atomic",
        "template": "confirm_scope@v1",
        "executor": "human",
        "effect": "none",
        "role": "gate",
        "inputs": {"question": "按这个范围继续吗？", "options": ["approve", "reject"], **inputs},
    }


async def test_human_question_goes_out_through_the_channel(exec_harness: ExecHarness) -> None:
    h = exec_harness
    h.one_node(human())
    inp, handle = await h.start()
    await h.wait_question(inp.task_id, "confirm")
    assert "按这个范围继续吗" in h.fx.human_output.getvalue()
    await handle.signal(names.HUMAN_REPLY_SIGNAL, task_pb2.HumanReplySignal(question_id="confirm", option="approve", replied_by_user_id="u7"))
    await asyncio.wait_for(handle.result(), timeout=30)
    assert h.result_of(inp.task_id, "confirm").result == {"option": "approve", "guidance": "", "replied_by": "u7"}


async def test_human_timeout_uses_the_declared_default(exec_harness: ExecHarness) -> None:
    h = exec_harness
    h.one_node(human(timeout_s=2, on_timeout={"default": "reject"}))
    inp, result = await h.run()
    r = h.result_of(inp.task_id, "confirm")
    assert r.status == "succeeded" and r.result == {"option": "reject", "timed_out": True}
    # 截止时间与定时器同时到：通道把问题记为过期，或被工作流撤回；两种都不再接受回复。
    assert await h.question_status(inp.task_id, "confirm") in (QuestionStatus.CANCELLED, QuestionStatus.EXPIRED), "超时后问题不再接受回复"


async def test_human_timeout_rejects_by_default(exec_harness: ExecHarness) -> None:
    h = exec_harness
    h.one_node(human(timeout_s=2))
    inp, _ = await h.run()
    r = h.result_of(inp.task_id, "confirm")
    assert r.status == "failed" and r.failure_class == "denied" and "超时" in r.error


async def test_approval_gate_asks_and_times_out_as_a_rejection(exec_harness: ExecHarness) -> None:
    h, fx = exec_harness, exec_harness.fx
    h.script.policy = StagePolicy(approval_timeout_s=2)
    h.one_node(cell_node("pay", "write_irreversible"))
    inp, _ = await h.run(effect_ceiling=enums_pb2.EFFECT_WRITE_IRREVERSIBLE)
    assert "pay" not in fx.calls, "没有审批通过就不执行"
    r = h.result_of(inp.task_id, "n")
    assert r.status == "failed" and r.failure_class == "denied"
    assert await h.question_status(inp.task_id, "n.1") in (QuestionStatus.CANCELLED, QuestionStatus.EXPIRED)


async def test_cancelling_the_task_withdraws_the_open_question(exec_harness: ExecHarness) -> None:
    h = exec_harness
    h.one_node(human("ask_user"))
    inp, handle = await h.start()
    await h.wait_question(inp.task_id, "ask_user")
    await handle.cancel()
    with pytest.raises(Exception):
        await asyncio.wait_for(handle.result(), timeout=30)
    deadline = time.monotonic() + 10
    while await h.question_status(inp.task_id, "ask_user") != QuestionStatus.CANCELLED:
        assert time.monotonic() < deadline, "取消 Task 时撤回还没回答的问题"
        await asyncio.sleep(0.1)
