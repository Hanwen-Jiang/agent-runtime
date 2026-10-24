"""S-04 验收“重放测试覆盖所有展开路径”（计划 S-04；重放是门槛）。

- 本模块排在最后（conftest.py）：本次测试会话里跑过的 Task、SubAgentWorkflow、SubgraphWorkflow 的事件历史，
  当前代码都必须能回放。设 AR_UPDATE_GOLDEN_HISTORIES=1 时，从中挑出覆盖每条展开路径的历史写进 tests/histories。
- golden 历史覆盖：并行调度、revise、ensemble（vote 与 judge）、search、template（内联与拆子工作流）、tool_loop、
  sub_agent、sub_task、写节点前的人工审批、tool_loop 里不可逆工具调用前的审批（D16）、Hook 的 deny、取消，
  两种子工作流本身，S-10 的 Verdict 去向（原地重试重跑、问人、rollback），T-09 的 effect 核对、before_finish 驳回与 Task 级挂载点，
  T-11b 的进度事件，以及 S-04 缺陷修复之后在第一个工作流任务里一次问完全部版本分支的新历史（workflows/patches.py）。
- Stage Runner 改变了 Task 工作流的命令序列，走 workflow.patched 版本分支：S-03 的 golden 历史仍能回放，
  新历史带版本标记，旧历史没有。
- 把 Stage Runner 里两条标了 replay-mutation 的语句调换顺序，重放必须失败。
"""

from __future__ import annotations

import base64
import importlib.util
import os
import re
import sys
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from temporalio.client import WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner
from temporalio.workflow import NondeterminismError

import agentruntime_worker.workflows.stage_runner as stage_module
import agentruntime_worker.workflows.task as task_module
from agentruntime_worker import names
from agentruntime_worker.replay import load_histories, replay_histories
from agentruntime_worker.workflows.stage_runner import (
    HUMAN_CHANNEL_PATCH,
    HUMAN_REPLY_RACE_PATCH,
    RECONCILE_PATCH,
    TOOL_LOOP_APPROVAL_PATCH,
    SubAgentWorkflow,
    SubgraphWorkflow,
)
from agentruntime_worker.workflows.patches import EVENTS_PATCH, PATCHES_AT_START
from agentruntime_worker.workflows.task import POLICY_PATCH, REVIEWER_PATCH, STAGE_RUNNER_PATCH, TaskWorkflow

from .support import HISTORIES_DIR

S03_GOLDEN = ("completed", "cancelled", "human_reply")
REPLAYABLE = (names.TASK_WORKFLOW, names.SUB_AGENT_WORKFLOW, names.SUBGRAPH_WORKFLOW)

# golden 名 → (工作流类型, 判断历史属于这条路径的正则)。正则匹配原始 JSON 加上解码后的载荷。
STAGE_CASES: dict[str, tuple[str, str]] = {
    "stage_b3_three_rounds": (names.TASK_WORKFLOW, r"write_report.*read_tickets|read_tickets.*write_report"),
    "stage_ensemble_vote": (names.TASK_WORKFLOW, r'"aggregate":\s*"vote"'),
    "stage_ensemble_judge": (names.TASK_WORKFLOW, r'"aggregate":\s*"judge"'),
    "stage_search": (names.TASK_WORKFLOW, r'"type":\s*"search"'),
    "stage_template_batch": (names.TASK_WORKFLOW, r'"name":\s*"SubgraphWorkflow"'),
    "stage_tool_loop": (names.TASK_WORKFLOW, r'"name":\s*"call_tool"'),
    "stage_sub_agent": (names.TASK_WORKFLOW, r'"name":\s*"SubAgentWorkflow"'),
    "stage_sub_task": (names.TASK_WORKFLOW, r'"workflowType":\s*\{\s*"name":\s*"TaskWorkflow"'),
    "stage_approval": (names.TASK_WORKFLOW, r"pay_ticket\.1"),
    "stage_hook_deny": (names.TASK_WORKFLOW, r'"decision":\s*"deny"'),
    "stage_cancel": (names.TASK_WORKFLOW, r"(?i)workflow_?execution_?cancel_?requested.*activity_?task_?cancel_?requested"),
    "subagent_child": (names.SUB_AGENT_WORKFLOW, r'"name":\s*"tool_loop_step"'),
    "subgraph_child": (names.SUBGRAPH_WORKFLOW, r'"name":\s*"execute_program_node"'),
    # S-05：写操作对账、经 HumanChannel 发出的问题、结果未知交给 Reviewer。
    "exec_reconcile": (names.TASK_WORKFLOW, r'"name":\s*"reconcile_node"'),
    "exec_human_channel": (names.TASK_WORKFLOW, r'"name":\s*"ask_human"'),
    "exec_unknown": (names.TASK_WORKFLOW, r'"status":\s*"unknown"'),
    # D16：tool_loop 里调用不可逆工具前的人工审批。
    "stage_tool_loop_approval": (names.TASK_WORKFLOW, r"card_charge.*book\.\d+\.1"),
    # S-10：Verdict 的去向——原地重试重跑同一个 Stage、问人、rollback；收尾先通知再写回。
    # 载荷里的中文是 \u 转义的，正则只用 ASCII：原地重试的判定，加上重跑时给 Node 的 status 区注入。
    "review_repair_rerun": (names.TASK_WORKFLOW, r'"strategy":\s*"retry_with_error".*"zone":\s*"status"'),
    "review_escalate_human": (names.TASK_WORKFLOW, r'"question_id":\s*"_review"'),
    "review_rollback": (names.TASK_WORKFLOW, r'"checkpoint":\s*"round_1"'),
    # T-09：按 Registry 核对 effect 被拒、before_finish 驳回后回到 Planner、Task 级挂载点的 Hook。
    "policy_effect_denied": (names.TASK_WORKFLOW, r"effect_mismatch"),
    "policy_finish_rejected": (names.TASK_WORKFLOW, r'"finish_rejected"'),
    "policy_task_mounts": (names.TASK_WORKFLOW, r'"mount":\s*"on_task_received".*"mount":\s*"on_task_end"'),
    # T-11b：进度事件经 publish_task_event 交给 Session Worker，含发出人工问题之后的 human_input_required。
    "events_progress": (names.TASK_WORKFLOW, r'"name":\s*"publish_task_event".*human_input_required'),
    # S-04 的缺陷修复：新的运行在第一个工作流任务里一次问完全部版本分支，之后才有 Hook 与发问。
    "patches_at_start": (names.TASK_WORKFLOW, r'"name":\s*"ask_human".*s04-patches-at-start'),
}
REVIEW_CASES = ("review_repair_rerun", "review_escalate_human", "review_rollback")
POLICY_CASES = ("policy_effect_denied", "policy_finish_rejected", "policy_task_mounts")
EVENTS_CASES = ("events_progress",)
AT_START_CASES = ("patches_at_start",)

MUTATION_C = "# replay-mutation:C"
MUTATION_D = "# replay-mutation:D"


def history_text(raw: str) -> str:
    """原始 JSON 加上所有 base64 载荷解码后的文本，节点 id、版本标记等都在载荷里。"""
    decoded = []
    for match in re.finditer(r'"data":\s*"([A-Za-z0-9+/=]+)"', raw):
        try:
            decoded.append(base64.b64decode(match.group(1)).decode("utf-8", errors="replace"))
        except ValueError:
            continue
    return raw + "\n" + "\n".join(decoded)


def workflow_type(raw: str) -> str:
    match = re.search(r'"workflowType":\s*\{\s*"name":\s*"([^"]+)"', raw)
    return match.group(1) if match else ""


async def session_histories(env: WorkflowEnvironment) -> list[tuple[str, WorkflowHistory]]:
    out = []
    async for wf in env.client.list_workflows():
        if wf.workflow_type in REPLAYABLE:
            history = await env.client.get_workflow_handle(wf.id, run_id=wf.run_id).fetch_history()
            out.append((wf.workflow_type, history))
    return out


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_every_history_from_this_session_replays(temporal_env: WorkflowEnvironment) -> None:
    histories = await session_histories(temporal_env)
    assert {t for t, _ in histories} >= set(REPLAYABLE), "本次会话应当跑过三种工作流"
    replayer = Replayer(workflows=[TaskWorkflow, SubAgentWorkflow, SubgraphWorkflow])
    failures = []
    for _, history in histories:
        result = await replayer.replay_workflow(history, raise_on_replay_failure=False)
        if result.replay_failure is not None:
            failures.append((history.workflow_id, result.replay_failure))
    assert not failures, failures

    if os.environ.get("AR_UPDATE_GOLDEN_HISTORIES") == "1":
        # 只补缺的 golden，不覆盖已有的：已有的 golden 是旧版本代码留下的历史，用来证明它们仍能回放。
        # 要重新生成某一份，先删掉它。
        used: set[str] = set()
        texts = sorted(((h.workflow_id, t, h.to_json()) for t, h in histories), key=lambda x: x[0])
        for name, (wf_type, pattern) in STAGE_CASES.items():
            if (HISTORIES_DIR / f"{name}.json").exists():
                continue
            match = next(
                ((wid, raw) for wid, t, raw in texts if t == wf_type and wid not in used and re.search(pattern, history_text(raw), re.S)),
                None,
            )
            assert match is not None, f"本次会话里没有“{name}”这条路径的历史"
            used.add(match[0])
            (HISTORIES_DIR / f"{name}.json").write_text(match[1], "utf-8")


def test_golden_histories_cover_every_expansion_path() -> None:
    missing = [name for name in STAGE_CASES if not (HISTORIES_DIR / f"{name}.json").exists()]
    assert not missing, f"tests/histories 缺少这些路径的 golden 历史：{missing}；用 AR_UPDATE_GOLDEN_HISTORIES=1 跑测试生成"
    for name, (wf_type, pattern) in STAGE_CASES.items():
        raw = (HISTORIES_DIR / f"{name}.json").read_text("utf-8")
        assert workflow_type(raw) == wf_type, f"{name} 应是 {wf_type} 的历史"
        assert re.search(pattern, history_text(raw), re.S), f"{name} 不是它声称的那条路径"


def test_version_branch_keeps_old_histories_replayable() -> None:
    for name in S03_GOLDEN:
        assert STAGE_RUNNER_PATCH not in history_text((HISTORIES_DIR / f"{name}.json").read_text("utf-8")), f"S-03 的 {name} 不该有版本标记"
    for name, (wf_type, _) in STAGE_CASES.items():
        if wf_type == names.TASK_WORKFLOW:
            assert STAGE_RUNNER_PATCH in history_text((HISTORIES_DIR / f"{name}.json").read_text("utf-8")), f"{name} 应带版本标记 {STAGE_RUNNER_PATCH}"
    # S-05 改变了 human 节点与写操作超时的命令序列，各有自己的版本分支；S-04 的审批历史没有新标记，按旧路径回放。
    assert HUMAN_CHANNEL_PATCH not in history_text((HISTORIES_DIR / "stage_approval.json").read_text("utf-8"))
    assert HUMAN_CHANNEL_PATCH in history_text((HISTORIES_DIR / "exec_human_channel.json").read_text("utf-8"))
    assert RECONCILE_PATCH in history_text((HISTORIES_DIR / "exec_reconcile.json").read_text("utf-8"))
    # D16 改变了 tool_loop 的命令序列：新历史带版本标记，之前的 tool_loop 历史没有，按旧路径回放。
    approval = history_text((HISTORIES_DIR / "stage_tool_loop_approval.json").read_text("utf-8"))
    assert TOOL_LOOP_APPROVAL_PATCH in approval and HUMAN_REPLY_RACE_PATCH in approval
    assert TOOL_LOOP_APPROVAL_PATCH not in history_text((HISTORIES_DIR / "stage_tool_loop.json").read_text("utf-8"))
    # 发问前先登记回复的改动也有版本分支：S-05 的发问历史没有这个标记，按旧路径回放。
    assert HUMAN_REPLY_RACE_PATCH not in history_text((HISTORIES_DIR / "exec_human_channel.json").read_text("utf-8"))
    # S-10 改变了 Verdict 的去向与收尾的顺序：新历史带版本标记，之前的历史没有，按旧路径回放。
    for name in REVIEW_CASES:
        assert REVIEWER_PATCH in history_text((HISTORIES_DIR / f"{name}.json").read_text("utf-8")), f"{name} 应带版本标记 {REVIEWER_PATCH}"
    for name in (*S03_GOLDEN, *(n for n in STAGE_CASES if n not in REVIEW_CASES + POLICY_CASES + EVENTS_CASES + AT_START_CASES)):
        assert REVIEWER_PATCH not in history_text((HISTORIES_DIR / f"{name}.json").read_text("utf-8")), f"S-10 之前的 {name} 不该有 {REVIEWER_PATCH}"
    # T-09 改变了 Task 工作流的命令序列（提前加载 StagePolicy、Task 级挂载点、Catalog 一并查 Atomic Node 的模板）：
    # 新历史带版本标记，之前的历史都没有，按旧路径回放。
    for name in (*POLICY_CASES, *EVENTS_CASES, *AT_START_CASES):
        text = history_text((HISTORIES_DIR / f"{name}.json").read_text("utf-8"))
        assert POLICY_PATCH in text and REVIEWER_PATCH in text, f"{name} 应带版本标记 {POLICY_PATCH}"
    for name in (*S03_GOLDEN, *(n for n in STAGE_CASES if n not in POLICY_CASES + EVENTS_CASES + AT_START_CASES)):
        assert POLICY_PATCH not in history_text((HISTORIES_DIR / f"{name}.json").read_text("utf-8")), f"T-09 之前的 {name} 不该有 {POLICY_PATCH}"
    # T-11b 加入了推送进度事件的 activity：新历史带版本标记，之前的历史都没有，按旧路径回放（不推事件）。
    for name in (*EVENTS_CASES, *AT_START_CASES):
        assert EVENTS_PATCH in history_text((HISTORIES_DIR / f"{name}.json").read_text("utf-8")), f"{name} 应带版本标记 {EVENTS_PATCH}"
    for name in (*S03_GOLDEN, *(n for n in STAGE_CASES if n not in EVENTS_CASES + AT_START_CASES)):
        assert EVENTS_PATCH not in history_text((HISTORIES_DIR / f"{name}.json").read_text("utf-8")), f"T-11b 之前的 {name} 不该有 {EVENTS_PATCH}"
    # S-04 的缺陷修复：新的运行在第一个工作流任务里一次问完全部版本分支；之前的历史都没有这个标记，
    # 各版本分支仍在原位置第一次问，按原来的命令序列回放。
    for name in AT_START_CASES:
        assert PATCHES_AT_START in history_text((HISTORIES_DIR / f"{name}.json").read_text("utf-8")), f"{name} 应带版本标记 {PATCHES_AT_START}"
    for name in (*S03_GOLDEN, *(n for n in STAGE_CASES if n not in AT_START_CASES)):
        assert PATCHES_AT_START not in history_text((HISTORIES_DIR / f"{name}.json").read_text("utf-8")), f"修复之前的 {name} 不该有 {PATCHES_AT_START}"


async def test_all_golden_histories_replay_with_the_current_code() -> None:
    histories = load_histories(HISTORIES_DIR)
    results = await replay_histories(histories)
    failures = [(h.workflow_id, r.replay_failure) for h, r in zip(histories, results, strict=True) if r.replay_failure]
    assert not failures, failures


def swap(source: str, a: str, b: str) -> str:
    lines = source.split("\n")
    ia = [i for i, line in enumerate(lines) if a in line]
    ib = [i for i, line in enumerate(lines) if b in line]
    assert len(ia) == 1 and len(ib) == 1, f"{a} 与 {b} 应各出现一次"
    lines[ia[0]], lines[ib[0]] = lines[ib[0]], lines[ia[0]]
    return "\n".join(lines)


def load_module(name: str, source: str, tmp_path: Path) -> ModuleType:
    path = tmp_path / f"{name}.py"
    path.write_text(source, "utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_variants(tmp_path: Path, *, mutate: bool) -> list[type]:
    """复制 Stage Runner 与 Task 工作流的源码（可选地调换 C、D 两行），让 Task 工作流用复制出来的 Stage Runner。"""
    stage_source = Path(stage_module.__file__).read_text("utf-8")
    if mutate:
        stage_source = swap(stage_source, MUTATION_C, MUTATION_D)
    stage_name = f"stage_variant_{uuid.uuid4().hex}"
    stage_variant = load_module(stage_name, stage_source, tmp_path)
    task_source = Path(task_module.__file__).read_text("utf-8")
    assert "agentruntime_worker.workflows.stage_runner" in task_source
    task_variant = load_module(f"task_variant_{uuid.uuid4().hex}", task_source.replace("agentruntime_worker.workflows.stage_runner", stage_name), tmp_path)
    return [task_variant.TaskWorkflow, stage_variant.SubAgentWorkflow, stage_variant.SubgraphWorkflow]


async def replay_stage_goldens(workflows: list[type]) -> list[BaseException | None]:
    replayer = Replayer(workflows=workflows, workflow_runner=UnsandboxedWorkflowRunner())
    out: list[BaseException | None] = []
    for name in STAGE_CASES:
        path = HISTORIES_DIR / f"{name}.json"
        history = WorkflowHistory.from_json(name, path.read_text("utf-8"))
        out.append((await replayer.replay_workflow(history, raise_on_replay_failure=False)).replay_failure)
    return out


async def test_unmodified_stage_runner_loaded_the_same_way_replays(tmp_path: Path) -> None:
    test_golden_histories_cover_every_expansion_path()
    failures = await replay_stage_goldens(load_variants(tmp_path, mutate=False))
    assert not any(failures), failures


async def test_replay_detects_reordered_stage_runner_commands(tmp_path: Path) -> None:
    test_golden_histories_cover_every_expansion_path()
    failures = await replay_stage_goldens(load_variants(tmp_path, mutate=True))
    assert any(isinstance(f, NondeterminismError) for f in failures), f"调换 Stage Runner 的命令顺序后重放仍然通过，重放测试无效：{failures}"
