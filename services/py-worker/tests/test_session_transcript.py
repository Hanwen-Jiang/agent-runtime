"""TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 11.13 节：Session 里的后续消息要看得到之前的对话。

现象：一个 Session 里每条消息是一个新 Task，Task 拿不到之前的命令与交付（设计书 3.1.1 的“追加记录”、3.2.1 的“Session 上下文”没有实现）。
用户追问上一条回答里的词，模型说没有这方面的信息；用户说“这不是你上面说的吗”，模型说这是第一轮。

做法（不改 /proto、/schemas 与工作流的命令序列）：task-service 把命令记进业务库的 tasks.message；activity 经 SessionTranscript 读同一 Session 里
更早的、已结束的 Task。分诊与规划的提示带上它，fast 的 answer 节点按真实的对话（user、assistant 交替）问模型，tool_loop 放进 brief。
对话不进 Node 的 inputs：不进工作流历史与进度事件，也到不了 Reviewer（Reviewer 的输入按规定不含它）。

样本都是编的，不用真实租户的内容（要脱敏）。
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from temporalio.testing import ActivityEnvironment

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.providers.relational_store import Migration
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker import names
from agentruntime_worker.activities.types import Tenant
from agentruntime_worker.context.builder import ContextBuilder
from agentruntime_worker.executor.tool_loop import ModelToolLoop
from agentruntime_worker.planner.context import StaticContextSource, TranscriptContextSource
from agentruntime_worker.planner.planner import Planner
from agentruntime_worker.session.transcript import (
    COLUMNS,
    CONVERSATION_HEADER,
    SessionTranscript,
    Turn,
    chat_messages,
    transcript_text,
)

from .context_support import ContextGateway, config, objects, tool_call
from .executor_harness import ExecutorFixture, make_fixture, model, node_input
from .planner_support import FakeGateway, context, plan_answer, prompt_text, request, signals
from .test_planner_activity import plan_input
from .test_planner_full import RISKY_FULL
from .test_tool_loop_model import step_input

REPO = Path(__file__).resolve().parents[3]
TERM = "ZephyrLedger"
TENANT = Tenant(tenant_id="t-a", user_id="u1", workspace_id="w1", session_id="ses_a")
EARLIER = (Turn(message="读一下这个仓库讲讲结构", reply=f"这个仓库的核心模块是 {TERM}，负责对账。", status="done"),)

TASKS_DDL = """CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, session_key TEXT NOT NULL, client_request_id TEXT NOT NULL,
    status BIGINT NOT NULL, signaled BIGINT NOT NULL, last_event_id BIGINT NOT NULL, created_at BIGINT NOT NULL,
    outcome TEXT NOT NULL DEFAULT '', submitted_by TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '')"""


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteRelationalStoreProvider]:
    provider = SQLiteRelationalStoreProvider(tmp_path / "relational")
    await provider.migrate([Migration(1, "tasks_like_task_service", (TASKS_DDL,))])
    yield provider
    await provider.aclose()


def outcome(status: str = "TASK_STATUS_DONE", deliverable: Any = None) -> str:
    result: dict[str, Any] = {"status": status, "deliver_node_id": "answer"}
    if deliverable is not None:
        result["deliverable"] = {"value": deliverable}
    return json.dumps(result, ensure_ascii=False)


async def add_task(
    provider: SQLiteRelationalStoreProvider, task_id: str, created_at: int, *, message: str = "", done: str = "", session_id: str = "ses_a", tenant_id: str = "t-a",
) -> None:
    async with provider.bind(TenantContext(tenant_id=tenant_id)).transaction() as tx:
        await tx.execute(
            "INSERT INTO tasks (task_id, session_id, session_key, client_request_id, status, signaled, last_event_id, created_at, outcome, message) "
            "VALUES ($1, $2, $3, $4, 0, 1, 0, $5, $6, $7)",
            (task_id, session_id, session_id, task_id, created_at, done, message),
        )


class FixedTranscript:
    def __init__(self, turns: tuple[Turn, ...] = EARLIER) -> None:
        self.turns = turns
        self.calls: list[tuple[Tenant, str]] = []

    async def load(self, tenant: Tenant, task_id: str) -> tuple[Turn, ...]:
        self.calls.append((tenant, task_id))
        return self.turns


# --- 读同一 Session 里更早的 Task ---


async def test_earlier_finished_turns_come_back_oldest_first(store: SQLiteRelationalStoreProvider) -> None:
    await add_task(store, "tsk_1", 100, message="第一句", done=outcome(deliverable={"text": "第一个回答"}))
    await add_task(store, "tsk_2", 200, message="第二句", done=outcome(deliverable={"text": "第二个回答"}))
    await add_task(store, "tsk_3", 250, message="还没结束的", done="")
    await add_task(store, "tsk_now", 300, message="现在这句", done="")
    await add_task(store, "tsk_later", 400, message="更晚的", done=outcome(deliverable={"text": "更晚的回答"}))
    turns = await SessionTranscript(store).load(TENANT, "tsk_now")
    assert turns == (Turn("第一句", "第一个回答", "done"), Turn("第二句", "第二个回答", "done"))


async def test_other_sessions_and_tenants_stay_out(store: SQLiteRelationalStoreProvider) -> None:
    await add_task(store, "tsk_other_session", 100, message="别的 Session", done=outcome(deliverable={"text": "x"}), session_id="ses_b")
    await add_task(store, "tsk_other_tenant", 100, message="别的租户", done=outcome(deliverable={"text": "x"}), tenant_id="t-b")
    await add_task(store, "tsk_now", 300, message="现在这句")
    assert await SessionTranscript(store).load(TENANT, "tsk_now") == ()


@pytest.mark.parametrize(
    ("deliverable", "reply"),
    [
        ("直接是文字", "直接是文字"),
        ({"text": "在 text 里"}, "在 text 里"),
        ({"answer": "在 answer 里"}, "在 answer 里"),
        ({"patch": "diff", "files": 2}, '{"files":2,"patch":"diff"}'),
    ],
    ids=["str", "text", "answer", "other-json"],
)
async def test_the_reply_is_read_from_the_deliverable(store: SQLiteRelationalStoreProvider, deliverable: Any, reply: str) -> None:
    await add_task(store, "tsk_1", 100, message="问", done=outcome(deliverable=deliverable))
    await add_task(store, "tsk_now", 200)
    [turn] = await SessionTranscript(store).load(TENANT, "tsk_now")
    assert turn.reply == reply


async def test_a_turn_without_a_delivery_keeps_its_ending(store: SQLiteRelationalStoreProvider) -> None:
    await add_task(store, "tsk_1", 100, message="做不完的事", done=outcome("TASK_STATUS_ABORTED"))
    await add_task(store, "tsk_now", 200)
    [turn] = await SessionTranscript(store).load(TENANT, "tsk_now")
    assert (turn.message, turn.reply, turn.status) == ("做不完的事", "", "aborted")


async def test_tasks_from_before_the_message_column_still_count(store: SQLiteRelationalStoreProvider) -> None:
    await add_task(store, "tsk_old", 100, message="", done=outcome(deliverable={"text": f"提到了 {TERM}"}))
    await add_task(store, "tsk_now", 200)
    [turn] = await SessionTranscript(store).load(TENANT, "tsk_now")
    assert TERM in turn.reply and turn.message == ""


async def test_only_the_latest_turns_are_kept(store: SQLiteRelationalStoreProvider) -> None:
    for i in range(5):
        await add_task(store, f"tsk_{i}", 100 + i, message=f"第 {i} 句", done=outcome(deliverable={"text": f"回答 {i}"}))
    await add_task(store, "tsk_now", 300)
    turns = await SessionTranscript(store, max_turns=2).load(TENANT, "tsk_now")
    assert [t.message for t in turns] == ["第 3 句", "第 4 句"]


async def test_the_latest_reply_is_kept_nearly_whole(store: SQLiteRelationalStoreProvider) -> None:
    """用户最常追问的就是上一条回答中间提到的东西（一条约一万字的仓库说明）。"""
    long_reply = "前文" * 3000 + TERM + "后文" * 3000
    await add_task(store, "tsk_1", 100, message="讲讲结构", done=outcome(deliverable={"text": long_reply}))
    await add_task(store, "tsk_now", 200)
    [turn] = await SessionTranscript(store).load(TENANT, "tsk_now")
    assert TERM in turn.reply


async def test_long_replies_are_trimmed_and_older_ones_harder(store: SQLiteRelationalStoreProvider) -> None:
    await add_task(store, "tsk_1", 100, message="早的", done=outcome(deliverable={"text": "早" * 5000}))
    await add_task(store, "tsk_2", 200, message="近的", done=outcome(deliverable={"text": "近" * 5000}))
    await add_task(store, "tsk_now", 300)
    older, latest = await SessionTranscript(store, latest_reply_chars=1000, reply_chars=100).load(TENANT, "tsk_now")
    assert len(older.reply) < 200 and len(latest.reply) < 1100
    assert "省略" in older.reply and "省略" in latest.reply, "截断处写明了，模型不会当成原文就这么多"
    assert latest.reply.startswith("近") and latest.reply.endswith("近"), "头尾都留下"


async def test_the_total_is_bounded_by_dropping_the_oldest(store: SQLiteRelationalStoreProvider) -> None:
    for i in range(4):
        await add_task(store, f"tsk_{i}", 100 + i, message=f"第 {i} 句", done=outcome(deliverable={"text": "字" * 1000}))
    await add_task(store, "tsk_now", 300)
    turns = await SessionTranscript(store, reply_chars=1000, latest_reply_chars=1000, total_chars=2500).load(TENANT, "tsk_now")
    assert [t.message for t in turns] == ["第 2 句", "第 3 句"]


@pytest.mark.parametrize("tenant", [dataclasses.replace(TENANT, session_id="")], ids=["no-session"])
async def test_no_session_means_no_conversation(store: SQLiteRelationalStoreProvider, tenant: Tenant) -> None:
    assert await SessionTranscript(store).load(tenant, "tsk_now") == ()


async def test_a_store_problem_never_fails_the_task(tmp_path: Path) -> None:
    """业务库没有这张表（task-service 与 Worker 用的不是同一个库）或读不了：当作没有对话，Task 照常跑。"""
    provider = SQLiteRelationalStoreProvider(tmp_path / "empty")
    try:
        assert await SessionTranscript(provider).load(TENANT, "tsk_now") == ()
    finally:
        await provider.aclose()


def test_the_columns_read_here_exist_in_the_task_service_table() -> None:
    """表归 task-service（Go）所有，这边只读：名字对不上时这里先发现。"""
    source = (REPO / "services" / "go-task-service" / "internal" / "taskstore" / "taskstore.go").read_text("utf-8")
    migrations = source[source.index("func Migrations()") : source.index("// Task 是一行 Task 记录")]
    for column in COLUMNS:
        assert re.search(rf"\b{column}\b", migrations), f"tasks 里没有 {column}"


# --- 怎么放进提示 ---


def test_chat_messages_alternate_user_and_assistant() -> None:
    messages = chat_messages((Turn("问一", "答一", "done"), Turn("问二", "答二", "done")))
    assert [(m.role, m.text) for m in messages] == [
        (mg.CHAT_ROLE_USER, "问一"), (mg.CHAT_ROLE_ASSISTANT, "答一"), (mg.CHAT_ROLE_USER, "问二"), (mg.CHAT_ROLE_ASSISTANT, "答二"),
    ]  # fmt: skip


def test_chat_messages_never_leave_a_gap_in_the_alternation() -> None:
    """有的模型要求从 user 开始、user 与 assistant 交替：没记下原话或没有交付的那一轮用说明补上。"""
    messages = chat_messages((Turn("", "答一", "done"), Turn("问二", "", "aborted")))
    assert [m.role for m in messages] == [mg.CHAT_ROLE_USER, mg.CHAT_ROLE_ASSISTANT, mg.CHAT_ROLE_USER, mg.CHAT_ROLE_ASSISTANT]
    assert all(m.text for m in messages)
    assert "aborted" in messages[3].text


def test_transcript_text_is_bounded_and_labelled() -> None:
    assert transcript_text(()) == ""
    text = transcript_text((Turn("问一", "答" * 50_000, "done"),), max_chars=2000)
    assert text.startswith(CONVERSATION_HEADER) and "问一" in text and len(text) < 2200


# --- 分诊与规划 ---


def with_conversation(**kw: Any) -> Any:
    return dataclasses.replace(context(), conversation=EARLIER, has_workspace=True, **kw)


async def test_triage_sees_the_conversation_and_the_workspace() -> None:
    gateway = FakeGateway(triage=signals("question", family=None, effect="none"), plan=[plan_answer()])
    await Planner(gateway).plan(request(f"解释一下 {TERM}", ctx=with_conversation()))
    [call] = gateway.calls("triage")
    text = prompt_text(call)
    assert TERM in text.split("用户消息：")[1].replace(f"解释一下 {TERM}", "", 1), "分诊看得到上一条回答"
    assert "这个 Session 有工作区" in text


async def test_triage_is_told_when_a_message_needs_the_workspace() -> None:
    gateway = FakeGateway(triage=signals("question", family=None, effect="none"), plan=[plan_answer()])
    await Planner(gateway).plan(request("深入讲讲这个模块", ctx=with_conversation()))
    system = gateway.calls("triage")[0].generation.messages[0].text
    assert "工作区" in system and "effect 填 read" in system, "要读工作区才答得好的（含追问）不能走单次调用"


async def test_triage_without_a_conversation_is_as_before() -> None:
    gateway = FakeGateway(triage=signals("question", family=None, effect="none"), plan=[plan_answer()])
    await Planner(gateway).plan(request("今天星期几"))
    text = prompt_text(gateway.calls("triage")[0])
    assert CONVERSATION_HEADER not in text and "这个 Session 有工作区" not in text


async def test_planning_sees_the_conversation() -> None:
    gateway = FakeGateway(triage=RISKY_FULL, plan=[plan_answer()])
    await Planner(gateway).plan(request(f"把 {TERM} 那部分展开", ctx=with_conversation()))
    text = prompt_text(gateway.calls("plan")[0])
    assert CONVERSATION_HEADER in text and "这个仓库的核心模块" in text


async def test_the_fast_stage_does_not_carry_the_conversation() -> None:
    """对话不进 Node 的 inputs：不进工作流历史与进度事件，也到不了 Reviewer。"""
    gateway = FakeGateway(triage=signals("question", family=None, effect="none"))
    result = await Planner(gateway).plan(request("解释一下", ctx=with_conversation()))
    assert result.triage_level == "fast"
    [node] = result.stage["nodes"]
    assert node["inputs"] == {"request": "解释一下"}
    assert TERM not in json.dumps(result.stage, ensure_ascii=False)


async def test_the_context_source_adds_the_conversation_and_the_workspace() -> None:
    transcript = FixedTranscript()
    source = TranscriptContextSource(StaticContextSource(context()), transcript)
    inp = dataclasses.replace(plan_input(), tenant=TENANT)
    ctx = await source.load(inp)
    assert ctx.conversation == EARLIER and ctx.has_workspace is True
    assert transcript.calls == [(TENANT, inp.task_id)]
    no_workspace = await source.load(dataclasses.replace(inp, tenant=dataclasses.replace(TENANT, workspace_id="")))
    assert no_workspace.has_workspace is False


# --- fast 的 answer 节点 ---


@pytest.fixture
def fx(tmp_path: Path) -> ExecutorFixture:
    return make_fixture(tmp_path)


def with_transcript(fx: ExecutorFixture, transcript: FixedTranscript) -> None:
    fx.executor.deps = dataclasses.replace(fx.executor.deps, transcript=transcript)


async def run_node(fx: ExecutorFixture, node: dict[str, Any]) -> dict[str, Any]:
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, activity_type=names.EXECUTE_MODEL_NODE, task_queue=names.MODEL_QUEUE, heartbeat_timeout=None)
    return await env.run(fx.executor.execute_model_node, node_input(node))


def answer_node(text: str = "这不是你上面说的吗", template: str = "answer@v1") -> dict[str, Any]:
    return model("answer", inputs={"request": text}, template=template, role="deliver")


@pytest.mark.parametrize("template", ["answer@v1", "rewrite@v1"])
async def test_the_answer_node_asks_the_model_as_a_real_conversation(fx: ExecutorFixture, template: str) -> None:
    transcript = FixedTranscript()
    with_transcript(fx, transcript)
    fx.models.reply({"text": "是的，上面提到过"})
    await run_node(fx, answer_node(template=template))
    messages = fx.models.requests[0].generation.messages
    assert [m.role for m in messages] == [mg.CHAT_ROLE_SYSTEM, mg.CHAT_ROLE_USER, mg.CHAT_ROLE_ASSISTANT, mg.CHAT_ROLE_USER]
    assert messages[1].text == EARLIER[0].message and messages[2].text == EARLIER[0].reply
    assert messages[3].text == "这不是你上面说的吗", "用户的消息原样，不是模板加输入的 JSON"
    assert transcript.calls and transcript.calls[0][1] == node_input(answer_node()).task_id


async def test_the_answer_prompt_does_not_expose_internal_names(fx: ExecutorFixture) -> None:
    """用户看到过模型说“我只收到了 request 和 answer@v1 模板”。"""
    with_transcript(fx, FixedTranscript())
    fx.models.reply({"text": "好"})
    await run_node(fx, answer_node())
    messages = fx.models.requests[0].generation.messages
    text = "\n".join(m.text for m in messages)
    for internal in ("answer@v1", '"template"', '"inputs"', '"request"', "Atomic Node", "Agent Runtime"):
        assert internal not in text, f"提示里不该有 {internal}"
    system = messages[0].text
    assert "内部" in system, "明确要求不提内部的名字"
    assert "前面的对话" in system, "告诉模型前面的对话是真实发生过的"


async def test_the_answer_node_without_earlier_turns_is_still_a_plain_message(fx: ExecutorFixture) -> None:
    with_transcript(fx, FixedTranscript(()))
    fx.models.reply({"text": "好"})
    await run_node(fx, answer_node("今天星期几"))
    messages = fx.models.requests[0].generation.messages
    assert [m.role for m in messages] == [mg.CHAT_ROLE_SYSTEM, mg.CHAT_ROLE_USER]
    assert messages[1].text == "今天星期几"


async def test_without_a_transcript_the_answer_node_still_works(fx: ExecutorFixture) -> None:
    fx.models.reply({"text": "好"})
    await run_node(fx, answer_node("今天星期几"))
    assert fx.models.requests[0].generation.messages[-1].text == "今天星期几"


async def test_other_model_nodes_keep_the_template_and_inputs(fx: ExecutorFixture) -> None:
    transcript = FixedTranscript()
    with_transcript(fx, transcript)
    fx.models.reply({"summary": "物流延迟"})
    await run_node(fx, model("analyze"))
    text = "\n".join(m.text for m in fx.models.requests[0].generation.messages)
    assert "summarize@v2" in text and "华东退款率上升" in text
    assert transcript.calls == [], "只有回复用户的节点才读对话"
    assert EARLIER[0].reply not in text


async def test_a_deliver_node_after_a_recipe_keeps_its_inputs(fx: ExecutorFixture) -> None:
    """light 的 deliver 节点也用 answer@v1，但输入是上游的结果，不是用户的消息。"""
    with_transcript(fx, FixedTranscript())
    fx.models.reply({"text": "结果"})
    await run_node(fx, model("deliver", inputs={"result": "上游的结果"}, template="answer@v1", role="deliver"))
    assert "上游的结果" in "\n".join(m.text for m in fx.models.requests[0].generation.messages)


# --- tool_loop ---


def loop_with(tmp_path: Path, gateway: ContextGateway, transcript: FixedTranscript) -> ModelToolLoop:
    return ModelToolLoop(gateway, ContextBuilder(gateway, objects(tmp_path), config(effective_tokens=100_000)), transcript=transcript)


async def test_the_tool_loop_brief_carries_the_conversation(tmp_path: Path) -> None:
    gateway = ContextGateway(steps=[tool_call("repo_read", path="a.py"), tool_call("repo_read", path="b.py")])
    tool_loop = loop_with(tmp_path, gateway, FixedTranscript())
    await tool_loop.step(step_input([]))
    await tool_loop.step(step_input([]))
    first, second = gateway.requests
    [brief] = [m for m in first.generation.messages if CONVERSATION_HEADER in m.text]
    assert TERM in brief.text and brief.role == mg.CHAT_ROLE_USER
    assert [m.text for m in second.generation.messages if CONVERSATION_HEADER in m.text] == [brief.text], "每一步都一样，不破坏提示缓存"


async def test_the_tool_loop_keeps_the_conversation_small(tmp_path: Path) -> None:
    """上下文的预算要留给工具的结果：一条很长的回答不能占掉一大半。"""
    gateway = ContextGateway(steps=[tool_call("repo_read", path="a.py")])
    await loop_with(tmp_path, gateway, FixedTranscript((Turn("问", "答" * 50_000, "done"),))).step(step_input([]))
    [brief] = [m for m in gateway.requests[0].generation.messages if CONVERSATION_HEADER in m.text]
    assert len(brief.text) < 12_000


async def test_the_tool_loop_without_a_transcript_is_as_before(tmp_path: Path) -> None:
    gateway = ContextGateway(steps=[tool_call("repo_read", path="a.py")])
    await ModelToolLoop(gateway, ContextBuilder(gateway, objects(tmp_path), config(effective_tokens=100_000))).step(step_input([]))
    assert not [m for m in gateway.requests[0].generation.messages if CONVERSATION_HEADER in m.text]
