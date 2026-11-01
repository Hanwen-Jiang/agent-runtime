"""docs/plan/feedback-2026-11-01.md N-03：Session 追问带上之前做过的事，fast 不再说自己没有工具。

线上日志（2026-11-01）：读仓库的回答之后追问“为何不做递归枚举”“你有什么工具”“你是怎么读取的目录结构”，fast 的回答一路说
“我没有工具”“那张表是我编的”。它只拿到之前的文字，不知道上一轮实际调过 repo_list。现在：收尾写一份 Session 记录（各轮的目标与判定、
事实、实际调过的工具），之后的追问读它；fast 的系统提示说清这一轮不能用工具、之前的回答是用工具得来的。
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from temporalio.testing import ActivityEnvironment

from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.errors import NotFoundError
from agentruntime_core.providers.relational_store import Migration
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.activities.types import Tenant, ToolLoopStep
from agentruntime_worker.reviewer.wrap_up import RecordingMemoryClient, WrapUp, session_record
from agentruntime_worker.session.transcript import SessionTranscript, Turn, actions_text, record_text, transcript_text

from .executor_harness import ExecutorFixture, make_fixture
from .test_reviewer_activity import TASK_ID, RecordingTelemetry, activities, finalize_input
from .test_session_transcript import TASKS_DDL, FixedTranscript, add_task, answer_node, outcome, run_node, with_transcript
from .reviewer_support import FakeReviewGateway
from .stage_harness import StageHarness, harness  # noqa: F401 — harness 是 fixture
from .test_stage_runner import deliver, goal, one_round, scripted_steps

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]

ACTIONS = [
    {"round": 1, "node": "list_root", "tool": "repo_list", "target": ".", "ok": True},
    {"round": 1, "node": "read_package_json", "tool": "repo_read", "target": "package.json", "ok": False, "error": "file_not_found：不在工作区"},
]
RECORD = {
    "status": "done",
    "rounds": [{"seq": 1, "stage_goal": "列出仓库结构并读关键配置", "triage_level": "full", "verdict": "done", "tool_calls": 2}],
    "facts": ["根目录没有 package.json"],
    "artifacts": [],
    "actions": ACTIONS,
}


# --- 收尾写 Session 记录 ---


@pytest.fixture
def objects(tmp_path: Path) -> LocalDiskObjectStoreProvider:
    return LocalDiskObjectStoreProvider(tmp_path / "objects", signing_key=os.urandom(32))


async def test_finalize_writes_the_session_record_under_the_tenant(objects: LocalDiskObjectStoreProvider) -> None:
    wrap = WrapUp(objects=objects, memory=RecordingMemoryClient(), telemetry=RecordingTelemetry())
    await ActivityEnvironment().run(activities(FakeReviewGateway([]), wrap_up=wrap).finalize_task, finalize_input(actions=ACTIONS))
    got = json.loads((await objects.bind(TenantContext(tenant_id="t1")).get(f"tasks/{TASK_ID}/session_record.json")).data)
    assert got["actions"] == ACTIONS and got["status"] == "done"
    assert [r["seq"] for r in got["rounds"]] == [1, 2, 3] and all("stage_goal" in r for r in got["rounds"])
    with pytest.raises(NotFoundError):
        await objects.bind(TenantContext(tenant_id="t2")).get(f"tasks/{TASK_ID}/session_record.json")


def test_the_record_does_not_carry_the_delivery_text() -> None:
    record = session_record(finalize_input(actions=ACTIONS))
    assert set(record) == {"status", "rounds", "facts", "artifacts", "actions"}


# --- 读：每轮附上实际做过的事 ---


def test_the_record_reads_as_one_short_paragraph() -> None:
    text = record_text(RECORD)
    assert "列出仓库结构并读关键配置（done）" in text
    assert "repo_list .（成功）" in text and "repo_read package.json（失败）" in text
    assert "根目录没有 package.json" in text
    assert record_text({"rounds": [{"seq": 1, "stage_goal": "回复用户这条消息", "verdict": "done"}], "actions": []}).endswith("没有调用工具")
    assert record_text("乱的") == "" and record_text({}) == ""


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteRelationalStoreProvider]:
    provider = SQLiteRelationalStoreProvider(tmp_path / "relational")
    await provider.migrate([Migration(1, "tasks_like_task_service", (TASKS_DDL,))])
    yield provider
    await provider.aclose()


TENANT = Tenant(tenant_id="t-a", user_id="u1", workspace_id="w1", session_id="ses_a")


async def test_each_turn_carries_what_it_actually_did(store: SQLiteRelationalStoreProvider, objects: LocalDiskObjectStoreProvider) -> None:
    await add_task(store, "tsk_1", 1, message="讲讲仓库结构", done=outcome(deliverable={"text": "这是一个文档仓库……"}))
    await add_task(store, "tsk_2", 2, message="为何不做递归枚举")
    await objects.bind(TenantContext(tenant_id="t-a")).put("tasks/tsk_1/session_record.json", json.dumps(RECORD).encode(), content_type="application/json")
    turns = await SessionTranscript(store, objects=objects).load(TENANT, "tsk_2")
    assert len(turns) == 1 and "repo_list ." in turns[0].actions
    assert "repo_list ." in transcript_text(turns), "分诊与规划的提示里也有"


async def test_a_turn_without_a_record_is_as_before(store: SQLiteRelationalStoreProvider, objects: LocalDiskObjectStoreProvider) -> None:
    await add_task(store, "tsk_1", 1, message="你好", done=outcome(deliverable={"text": "你好！"}))
    await add_task(store, "tsk_2", 2, message="继续")
    turns = await SessionTranscript(store, objects=objects).load(TENANT, "tsk_2")
    assert turns[0].actions == "" and turns[0].reply == "你好！"
    assert (await SessionTranscript(store).load(TENANT, "tsk_2"))[0].actions == "", "没给 ObjectStore 时只有文字"


def test_actions_text_lists_only_turns_with_records() -> None:
    turns = (Turn("讲讲仓库结构", "……", "done", record_text(RECORD)), Turn("你好", "你好！", "done"))
    text = actions_text(turns)
    assert "第 1 条消息「讲讲仓库结构」" in text and "repo_list" in text and "第 2 条" not in text
    assert actions_text((Turn("你好", "你好！", "done"),)) == ""


# --- fast 的回答 ---


@pytest.fixture
def fx(tmp_path: Path) -> ExecutorFixture:
    return make_fixture(tmp_path)


async def test_the_fast_answer_sees_what_was_done_and_does_not_disown_it(fx: ExecutorFixture) -> None:
    earlier = (Turn("读一下这个仓库讲讲结构", "根目录有 docs/、archive/……", "done", record_text(RECORD)),)
    with_transcript(fx, FixedTranscript(earlier))
    fx.models.reply({"text": "上一轮用 repo_list 列了根目录"})
    await run_node(fx, answer_node("你是怎么读取的目录结构呢"))
    system = fx.models.requests[0].generation.messages[0].text
    assert "repo_list .（成功）" in system, "系统提示里有上一轮实际调过的工具"
    assert "不要说自己从来没有工具" in system and "不再读文件" in system
    others = [m.text for m in fx.models.requests[0].generation.messages[1:]]
    assert not any("系统记录" in t for t in others), "记录只在系统提示里，对话的轮次照原样"


async def test_without_records_the_fast_prompt_still_explains_the_situation(fx: ExecutorFixture) -> None:
    with_transcript(fx, FixedTranscript((dataclasses.replace(Turn("你好", "你好！", "done")),)))
    fx.models.reply({"text": "好"})
    await run_node(fx, answer_node("你有什么工具"))
    system = fx.models.requests[0].generation.messages[0].text
    assert "之前各轮实际做过的事" not in system and "不要说自己从来没有工具" in system


# --- 工作流把实际调过的工具交给收尾 ---


async def test_the_task_hands_its_tool_calls_to_the_wrap_up(harness: StageHarness) -> None:
    s = harness.script
    s.tools.update({"repo_list": "read"})
    s.tool_steps["explore"] = scripted_steps(
        ToolLoopStep(action="tool", tool="repo_list", args={"path": "."}),
        ToolLoopStep(action="finish", result={"entries": ["docs", "README.md"]}),
    )
    one_round(harness, goal("explore", "tool_loop", tools=["repo_list"]), deliver(after=["explore"]))
    inp, _ = await harness.run()
    finalized = next(f for f in s.finalized if f.task_id == inp.task_id)
    assert finalized.actions == [{"round": 1, "node": "explore", "tool": "repo_list", "target": ".", "ok": True}]
