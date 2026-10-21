"""Context Builder 主体：Compaction Ladder（设计书 3.2.7 第 2–4 条、附录 G.2、G.3、G.5；R-03 评审重点
“外置内容只以路径出现；阈值按有效长度设”）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentruntime_core.errors import NotFoundError
from agentruntime_worker.context.builder import ContextBuilder, tenant_context
from agentruntime_worker.context.tokens import estimate

from .context_support import (
    OTHER_TENANT,
    TASK_ID,
    TASK_PLAN,
    TASK_STATE,
    TENANT,
    ContextGateway,
    config,
    delta,
    error_step,
    filler,
    history,
    objects,
    read_step,
    request,
    run_tests_step,
    search_step,
    text_of,
)


def builder(tmp_path: Path, gateway: ContextGateway | None = None, **cfg) -> ContextBuilder:
    return ContextBuilder(gateway or ContextGateway(), objects(tmp_path), config(**cfg))


async def test_below_the_thresholds_nothing_is_compacted(tmp_path: Path) -> None:
    gateway = ContextGateway()
    built = await builder(tmp_path, gateway, effective_tokens=100_000).build(request(history(6)))
    assert built.applied == [] and gateway.requests == []
    assert all(filler(i, 800)[:40] in text_of(built.messages) for i in range(6))


async def test_large_outputs_are_offloaded_and_appear_only_as_a_path(tmp_path: Path) -> None:
    big = read_step(0, chars=20_000)
    provider = objects(tmp_path)
    b = ContextBuilder(ContextGateway(), provider, config(effective_tokens=100_000, offload_tokens=2_000))
    built = await b.build(request([big, read_step(1)]))
    text = text_of(built.messages)
    assert built.applied == ["offload"]
    assert big["observation"]["content"] not in text and big["observation"]["content"][:100] in text, "只留开头的摘录"
    store = provider.bind(tenant_context(TENANT))
    key = f"context/{TASK_ID}/r1/fix/step-0.json"
    assert store.uri(key) in text, "上下文里给出完整输出的路径"
    stored = json.loads((await store.get(key)).data)
    assert stored["observation"]["content"] == big["observation"]["content"]
    again = await b.build(request([big, read_step(1)]))
    assert text_of(again.messages) == text, "同一步重复构建结果相同（键固定、内容不变）"
    with pytest.raises(NotFoundError):
        await provider.bind(tenant_context(OTHER_TENANT)).get(key)


async def test_mask_starts_at_the_mask_threshold_and_keeps_recent_steps(tmp_path: Path) -> None:
    steps = history(12, chars=600)  # 约 12 × 170 token，超过 4000 的 40%，不到 70%
    built = await builder(tmp_path, effective_tokens=4_000).build(request(steps))
    text = text_of(built.messages)
    assert built.applied == ["mask"]
    assert estimate(text) < 0.7 * 4_000
    assert filler(11, 600)[:40] in text and filler(10, 600)[:40] in text, "最近几步原样保留"
    assert filler(0, 600)[:40] not in text, "较早的观察被遮蔽"
    assert f"context/{TASK_ID}/r1/fix/step-0.json" in text, "占位符带完整输出的路径"


async def test_masked_placeholders_keep_what_the_work_mode_must_keep(tmp_path: Path) -> None:
    steps = [run_tests_step(0, 600, failed=("test_checkout", "test_refund")), error_step(1), *history(12, chars=600)[2:]]
    text = text_of((await builder(tmp_path, effective_tokens=4_000).build(request(steps))).messages)
    assert filler(0, 600)[:40] not in text
    placeholder = next(line for line in text.splitlines() if "step-0.json" in line)
    assert "test_refund" in placeholder and '"exit_code": 1' in placeholder, "编码模式：失败的测试与退出码留在占位符里"
    assert "patch 第 1 处不适用" in text, "工具报的错误不遮蔽"


async def test_research_mode_keeps_citations_in_placeholders(tmp_path: Path) -> None:
    plan = {**TASK_PLAN, "context": {"work_mode": "research"}}
    steps = [search_step(i, 600) for i in range(12)]
    text = text_of((await builder(tmp_path, effective_tokens=4_000).build(request(steps, task_plan=plan))).messages)
    assert filler(0, 600)[:40] not in text
    assert "https://docs.example/page-0" in text and "Page 0" in text, "研究模式：URL 与出处留在占位符里"


async def test_thresholds_follow_the_effective_length_and_the_task_plan(tmp_path: Path) -> None:
    steps = history(12, chars=600)
    assert (await builder(tmp_path / "a", effective_tokens=40_000).build(request(steps))).applied == []
    assert (await builder(tmp_path / "b", effective_tokens=4_000).build(request(steps))).applied == ["mask"]
    early = {**TASK_PLAN, "context": {"work_mode": "coding", "mask_at": 0.05, "summarize_at": 0.9}}
    assert (await builder(tmp_path / "c", effective_tokens=40_000).build(request(steps, task_plan=early))).applied == ["mask"], "Task Plan 的 mask_at 覆盖默认值"


async def test_summarize_starts_at_the_summarize_threshold(tmp_path: Path) -> None:
    gateway = ContextGateway(summaries=[delta(intent=["修复 test_checkout"], decisions=["先读 checkout 模块"])])
    steps = history(40, chars=600)
    built = await builder(tmp_path, gateway, effective_tokens=4_000).build(request(steps))
    assert "summarize" in built.applied and "mask" in built.applied
    assert len(gateway.of("summary")) == 1
    text = text_of(built.messages)
    assert "先读 checkout 模块" in text, "history 开头放持久摘要"
    assert filler(0, 600)[:40] not in text and "step-0.json" not in text, "摘要过的步骤从 history 里拿掉"
    assert "已执行过的动作" in text and 'repo_read {"path": "src/module_0.py"}' in text, "摘要后仍看得到做过的动作，避免重做"
    assert built.tokens <= 0.7 * 4_000


async def test_repeated_actions_trigger_a_reset(tmp_path: Path) -> None:
    gateway = ContextGateway(summaries=[delta(intent=["修复 test_checkout"])])
    steps = [*history(6, chars=300), run_tests_step(6, 300), run_tests_step(7, 300), run_tests_step(8, 300)]
    built = await builder(tmp_path, gateway, effective_tokens=100_000).build(request(steps))
    assert built.applied[-1] == "reset"
    text = text_of(built.messages)
    assert "修复 test_checkout" in text, "reset 后用持久摘要接着做"
    assert filler(8, 300)[:40] not in text and filler(0, 300)[:40] not in text, "reset 后不带原始 history"
    assert 'run_tests {"target": "make test"}' in text, "做过的动作仍在持久摘要后面"


async def test_exact_state_comes_from_the_task_state_not_the_summary(tmp_path: Path) -> None:
    gateway = ContextGateway(summaries=[delta(intent=["修复 test_checkout"])])
    built = await builder(tmp_path, gateway, effective_tokens=4_000).build(request(history(40, chars=600)))
    status = built.messages[-1].text
    assert "summarize" in built.applied
    for exact in ("6379", "obj://t1/artifacts/patch-checkout.diff", "fixtures/orders.json", "2.5"):
        assert exact in status, f"status 区注入 Task State 的精确状态：{exact}"
    assert estimate(status) <= 500


async def test_every_model_call_carries_the_tenant(tmp_path: Path) -> None:
    gateway = ContextGateway(summaries=[delta(intent=["修复 test_checkout"])])
    await builder(tmp_path, gateway, effective_tokens=4_000).build(request(history(40, chars=600)))
    assert gateway.requests
    assert all(r.tenant.tenant_id == TENANT.tenant_id and r.tenant.session_id == TENANT.session_id for r in gateway.requests)


def test_the_fixture_task_state_is_valid() -> None:
    from .support import assert_valid

    assert_valid("task_state", TASK_STATE)
    assert_valid("task_plan", TASK_PLAN)
