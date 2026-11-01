"""S-10：Reviewer 的 decision activity 与收尾写回（附录 B.9；计划 S-10 交付“收尾写回”、难点“异步写回”）。

收尾写回由 finalize_task 完成，每个 Provider 客户端都按 Task 的租户绑定：
- Task State 归档进 ObjectStore（tasks/<task_id>/task_state.json）；
- 成功或失败的经验、本次 Recipe 的效果作为候选交给 Memory Service（SubmitCandidates，进入它的写入流水线）；
- 成本、升级次数、各 Verdict 的分布写进 trace（Telemetry）。
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.errors import NotFoundError
from agentruntime_core.tenancy import TenantContext
from agentruntime_worker.activities.types import FinalizeTaskInput, ReviewRoundInput, ReviewRoundOutput, StageResult
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.reviewer.activities import ReviewerActivities, StaticCatalogSource, dev_reviewer
from agentruntime_worker.reviewer.reviewer import Reviewer
from agentruntime_worker.reviewer.rules import ReviewCatalog
from agentruntime_worker.reviewer.wrap_up import RecordingMemoryClient, WrapUp

from .reviewer_support import CATALOG, SESSION_ID, TASK_ID, TENANT, FakeReviewGateway, always, answer, atomic, goal, rounds, stage, task_state
from .support import assert_valid

FETCH_STAGE = stage(
    atomic("q_store", role="fetch", template="sql_query@v1", executor="program", effect="read"),
    atomic("q_basket", role="fetch", template="sql_query@v1", executor="program", effect="read"),
    goal_text="取数",
    exit_when="两组数据齐全",
)


def review_input(**kw: Any) -> ReviewRoundInput:
    state = kw.pop("state", task_state(done_when="每个结论有数据支撑"))
    return ReviewRoundInput(
        tenant=kw.pop("tenant", TENANT),
        task_id=TASK_ID,
        round_seq=1,
        task_state=state,
        stage_results=kw.pop("results", {"q_store": StageResult(role="fetch", status="succeeded", result={"rows": 30}), "q_basket": StageResult(role="fetch", status="succeeded", result={"rows": 30})}),
        done_when=state["done_when"],
        stage=kw.pop("stage", FETCH_STAGE),
        history=kw.pop("history", []),
    )


def activities(gateway: FakeReviewGateway, *, source: StaticCatalogSource | None = None, wrap_up: WrapUp | None = None) -> ReviewerActivities:
    return ReviewerActivities(Reviewer(gateway), source or StaticCatalogSource(CATALOG), wrap_up)


# --- review_round ---


async def test_review_round_returns_the_verdict_and_the_state_update() -> None:
    gateway = FakeReviewGateway([answer({"verdict": "continue"}, state_update={"facts": ["两组数据齐全"]})])
    out = await ActivityEnvironment().run(activities(gateway).review_round, review_input())
    assert isinstance(out, ReviewRoundOutput)
    assert_valid("verdict", out.verdict)
    assert out.verdict["verdict"] == "continue"
    assert out.state_update["facts"] == ["两组数据齐全"]


async def test_review_round_checks_reroutes_against_the_tenants_own_recipes() -> None:
    reroute = answer({"verdict": "reroute", "reroute": {"recipe": "ticket_triage@v4"}})
    source = StaticCatalogSource(ReviewCatalog(recipes={}, effect_ceiling="read"), by_tenant={"t1": CATALOG})
    s = stage(goal("explore"))
    results = {"explore": StageResult(role="transform", status="succeeded", result={"finding": "适合工单批处理"})}
    t1 = await ActivityEnvironment().run(activities(FakeReviewGateway(always(reroute)), source=source).review_round, review_input(stage=s, results=results))
    assert t1.verdict["verdict"] == "reroute"
    other = TENANT.__class__(tenant_id="t2", user_id="u2", session_id=SESSION_ID)
    t2 = await ActivityEnvironment().run(activities(FakeReviewGateway(always(reroute)), source=source).review_round, review_input(stage=s, results=results, tenant=other))
    assert t2.verdict["verdict"] == "replan", "t2 的 Catalog 里没有这个 Recipe"


async def test_the_assembled_reviewer_is_told_the_tasks_own_ceiling(tmp_path: Path) -> None:
    """Worker 组装的 Reviewer 曾经写死 read：每个 Task 的 Reviewer 都以为只能读，基线 50 题里 4 题因此说“没有写权限”，有的还放弃了。"""
    gateway = FakeReviewGateway(always(answer({"verdict": "continue"})))
    acts = dev_reviewer(WorkerConfig(reviewer="model", data_dir=tmp_path))
    acts.reviewer = Reviewer(gateway)
    await ActivityEnvironment().run(acts.review_round, review_input(state=task_state(done_when="修好", ceiling="write_reversible")))
    text = "\n".join(m.text for m in gateway.requests[-1].generation.messages)
    assert '"effect_ceiling":"write_reversible"' in text and '"effect_ceiling":"read"' not in text


async def test_a_read_only_task_is_still_told_it_can_only_read(tmp_path: Path) -> None:
    gateway = FakeReviewGateway(always(answer({"verdict": "continue"})))
    acts = dev_reviewer(WorkerConfig(reviewer="model", data_dir=tmp_path))
    acts.reviewer = Reviewer(gateway)
    await ActivityEnvironment().run(acts.review_round, review_input(state=task_state(done_when="答完", ceiling="read")))
    assert '"effect_ceiling":"read"' in "\n".join(m.text for m in gateway.requests[-1].generation.messages)


async def test_review_round_refuses_an_input_without_the_stage() -> None:
    inp = review_input()
    inp.stage = None
    with pytest.raises(ApplicationError) as err:
        await ActivityEnvironment().run(activities(FakeReviewGateway([])).review_round, inp)
    assert err.value.non_retryable


# --- finalize_task：收尾写回 ---


class RecordingTelemetry:
    """记下每个打点和它绑定的租户。"""

    def __init__(self) -> None:
        self.points: list[tuple[str, str, str, float, dict[str, Any]]] = []

    def bind(self, tenant: TenantContext) -> Any:
        return _BoundTelemetry(self, tenant.tenant_id)

    def bind_platform(self, purpose: str) -> Any:
        raise AssertionError("收尾写回只用租户作用域")

    async def aclose(self) -> None:
        return None


class _BoundTelemetry:
    def __init__(self, owner: RecordingTelemetry, tenant_id: str) -> None:
        self.owner = owner
        self.tenant_id = tenant_id

    @asynccontextmanager
    async def start_span(self, name: str, *, attributes: dict[str, Any] | None = None) -> Any:
        self.owner.points.append((self.tenant_id, "span", name, 0.0, dict(attributes or {})))
        yield _Span()

    async def count(self, name: str, value: int = 1, *, attributes: dict[str, Any] | None = None) -> None:
        self.owner.points.append((self.tenant_id, "count", name, float(value), dict(attributes or {})))

    async def observe(self, name: str, value: float, *, attributes: dict[str, Any] | None = None) -> None:
        self.owner.points.append((self.tenant_id, "observe", name, float(value), dict(attributes or {})))

    async def log(self, level: Any, message: str, *, attributes: dict[str, Any] | None = None) -> None:
        return None

    async def flush(self) -> None:
        return None


class _Span:
    trace_id = "0" * 32
    span_id = "0" * 16

    def set_attributes(self, attributes: Any) -> None:
        return None

    def add_event(self, name: str, attributes: Any = None) -> None:
        return None

    def record_error(self, error: BaseException) -> None:
        return None


class FailingMemory(RecordingMemoryClient):
    async def submit_candidates(self, request: Any) -> Any:
        raise ConnectionError("Memory Service 不可用")


def final_state(status: str = "done") -> dict[str, Any]:
    state = task_state(rounds=rounds("continue", "repair", "done", costs=[0.2, 0.1, 0.3]), done_when="简报成稿且引用核对通过")
    state["rounds"] = state["rounds"][:-1]  # 收尾时没有正在评审的 Round
    state["status"] = status
    state["artifacts"] = ["obj://t1/tsk_x/report.md"]
    state["pinned"] = {"recipe": "refund_analysis@v3"}
    return state


def finalize_input(status: str = "done", **kw: Any) -> FinalizeTaskInput:
    verdicts = {
        "done": {"verdict": "done", "reason": "成稿且核对通过", "done": {"deliver_node": "report"}},
        "aborted": {"verdict": "abort", "reason": "预算用尽", "abort": {"completed": ["前两节"], "not_completed": ["第三节"]}},
    }
    return FinalizeTaskInput(
        tenant=TENANT,
        task_id=TASK_ID,
        status=status,
        task_state=final_state(status),
        verdict=verdicts.get(status),
        deliver_node="report" if status == "done" else "",
        artifacts=["obj://t1/tsk_x/report.md"],
        stages=[
            {"seq": 1, "goal": "取数", "nodes": 3, "tiers": []},
            {"seq": 2, "goal": "归因", "nodes": 2, "tiers": ["strong@medium"]},
            {"seq": 3, "goal": "成稿", "nodes": 3, "tiers": ["strong@medium"]},
        ],
        **kw,
    )


@pytest.fixture
def objects(tmp_path: Path) -> LocalDiskObjectStoreProvider:
    return LocalDiskObjectStoreProvider(tmp_path / "objects", signing_key=os.urandom(32))


async def test_finalize_archives_the_task_state_under_the_tenant(objects: LocalDiskObjectStoreProvider) -> None:
    wrap = WrapUp(objects=objects, memory=RecordingMemoryClient(), telemetry=RecordingTelemetry())
    await ActivityEnvironment().run(activities(FakeReviewGateway([]), wrap_up=wrap).finalize_task, finalize_input())
    t1 = objects.bind(TenantContext(tenant_id="t1"))
    archived = await t1.get(f"tasks/{TASK_ID}/task_state.json")
    assert json.loads(archived.data) == final_state()
    with pytest.raises(NotFoundError):
        await objects.bind(TenantContext(tenant_id="t2")).get(f"tasks/{TASK_ID}/task_state.json")


@pytest.mark.parametrize(("status", "kinds"), [("done", {"success", "recipe"}), ("aborted", {"failure", "recipe"}), ("cancelled", set()), ("failed", set())])
async def test_finalize_submits_experience_and_recipe_candidates_for_the_tenant(objects: LocalDiskObjectStoreProvider, status: str, kinds: set[str]) -> None:
    memory = RecordingMemoryClient()
    wrap = WrapUp(objects=objects, memory=memory, telemetry=RecordingTelemetry())
    await ActivityEnvironment().run(activities(FakeReviewGateway([]), wrap_up=wrap).finalize_task, finalize_input(status))
    items = [json.loads(json.dumps(c)) for c in memory.items()]
    assert {i["kind"] for i in items} == kinds
    for request in memory.requests:
        assert request.tenant.tenant_id == "t1"
        assert all(c.source_task_id == TASK_ID for c in request.candidates)
    for item in items:
        assert_valid("memory_item", item)
        assert item["scope"]["tenant"] == "t1" and item["scope"]["task"] == TASK_ID and item["scope"]["session"] == SESSION_ID
        assert item["provenance"]["source"] == "agent_reflection"
    recipe = next((i for i in items if i["kind"] == "recipe"), None)
    if recipe is not None:
        text = json.dumps(recipe, ensure_ascii=False)
        assert "refund_analysis@v3" in text and "strong@medium" in text, "Recipe 的效果：Stage 划分、Tier、成本"


async def test_candidate_ids_are_stable_across_retries(objects: LocalDiskObjectStoreProvider) -> None:
    """finalize_task 失败会被重试：同一个 Task 的候选 id 不变，Memory Service 据此去重。"""
    memory = RecordingMemoryClient()
    wrap = WrapUp(objects=objects, memory=memory, telemetry=RecordingTelemetry())
    acts = activities(FakeReviewGateway([]), wrap_up=wrap)
    await ActivityEnvironment().run(acts.finalize_task, finalize_input())
    first = sorted(i["id"] for i in memory.items())
    memory.requests.clear()
    await ActivityEnvironment().run(acts.finalize_task, finalize_input())
    assert sorted(i["id"] for i in memory.items()) == first


async def test_finalize_writes_cost_escalations_and_verdicts_into_the_trace(objects: LocalDiskObjectStoreProvider) -> None:
    telemetry = RecordingTelemetry()
    wrap = WrapUp(objects=objects, memory=RecordingMemoryClient(), telemetry=telemetry)
    await ActivityEnvironment().run(activities(FakeReviewGateway([]), wrap_up=wrap).finalize_task, finalize_input())
    assert {p[0] for p in telemetry.points} == {"t1"}, "打点都绑定在 Task 的租户上"
    observed = {p[2]: p[3] for p in telemetry.points if p[1] == "observe"}
    assert observed["task.cost_usd"] == pytest.approx(0.6)
    assert observed["task.success_cost_usd"] == pytest.approx(0.6)
    counts = [(p[2], p[3], p[4]) for p in telemetry.points if p[1] == "count"]
    assert ("task.escalations", 1.0, {"status": "done"}) in counts
    verdicts = {p[2]["verdict"]: p[1] for p in counts if p[0] == "task.verdicts"}
    assert verdicts == {"continue": 1.0, "repair": 1.0, "done": 1.0}


async def test_a_failed_task_reports_no_success_cost(objects: LocalDiskObjectStoreProvider) -> None:
    telemetry = RecordingTelemetry()
    wrap = WrapUp(objects=objects, memory=RecordingMemoryClient(), telemetry=telemetry)
    await ActivityEnvironment().run(activities(FakeReviewGateway([]), wrap_up=wrap).finalize_task, finalize_input("aborted"))
    assert "task.success_cost_usd" not in {p[2] for p in telemetry.points}


async def test_a_memory_outage_still_archives_and_records_then_fails_for_retry(objects: LocalDiskObjectStoreProvider) -> None:
    telemetry = RecordingTelemetry()
    wrap = WrapUp(objects=objects, memory=FailingMemory(), telemetry=telemetry)
    with pytest.raises(Exception) as err:
        await ActivityEnvironment().run(activities(FakeReviewGateway([]), wrap_up=wrap).finalize_task, finalize_input())
    assert not (isinstance(err.value, ApplicationError) and err.value.non_retryable), "写回失败要能重试"
    assert await objects.bind(TenantContext(tenant_id="t1")).head(f"tasks/{TASK_ID}/task_state.json")
    assert any(p[2] == "task.cost_usd" for p in telemetry.points)
