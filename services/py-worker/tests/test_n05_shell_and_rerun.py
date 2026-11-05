"""docs/plan/feedback-2026-11-01.md N-05 第 1、2 条：shell_run 的工作目录；原地重做时告诉 Node 上一次做了什么。

线上日志（2026-11-01）：`python -m pytest tests -v` 在仓库根跑失败，之后每条命令都写成 `bash -c cd archive/legacy-trader && …`；
第 2、3 轮的重做又从 `pip install` 开始。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agentruntime.common.v1 import enums_pb2
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.errors import InvalidError
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker import names
from agentruntime_worker.activities.types import StageResult, ToolLoopStep
from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools
from agentruntime_worker.workflows import verdicts as vd
from agentruntime_worker.workflows.events import tool_target

from .service_setup import Setup
from .stage_harness import StageHarness, harness  # noqa: F401 — harness 是 fixture
from .test_stage_runner import deliver, goal, stage
from .test_task_verdicts import out, scripted

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]

T1 = TenantContext(tenant_id="t1", user_id="u1", workspace_id="w1")
PRINT_CWD = [sys.executable, "-c", "import os; print(os.path.basename(os.getcwd()))"]


def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    (root / "t1" / "w1" / "archive" / "legacy-trader").mkdir(parents=True)
    return root


async def run(provider: LocalToolDriverProvider, args: dict) -> dict:
    return (await provider.bind(T1).invoke("shell_run", "v1", args, idempotency_key=f"idk-{os.urandom(4).hex()}")).output


def host(tmp_path: Path) -> LocalToolDriverProvider:
    provider = LocalToolDriverProvider()
    register_platform_tools(provider, PlatformToolsConfig(workspace_root=workspace(tmp_path)))
    return provider


async def test_shell_run_runs_in_the_given_directory(tmp_path: Path) -> None:
    provider = host(tmp_path)
    assert (await run(provider, {"argv": PRINT_CWD, "cwd": "archive/legacy-trader"}))["stdout"].strip() == "legacy-trader"
    assert (await run(provider, {"argv": PRINT_CWD}))["stdout"].strip() == "w1", "不给 cwd 时仍是工作区根"
    assert (await run(provider, {"argv": PRINT_CWD, "cwd": "."}))["stdout"].strip() == "w1"


@pytest.mark.parametrize(("cwd", "code"), [("../..", "path_outside_workspace"), ("/etc", "path_outside_workspace"), ("no/such/dir", "dir_not_found")])
async def test_shell_run_refuses_directories_outside_or_missing(tmp_path: Path, cwd: str, code: str) -> None:
    with pytest.raises(InvalidError) as err:
        await run(host(tmp_path), {"argv": PRINT_CWD, "cwd": cwd})
    assert err.value.code == code


async def test_shell_run_in_a_sandbox_backend_honours_the_directory(tmp_path: Path) -> None:
    s = Setup(tmp_path, sessions=False)  # 经 Sandbox Service（SBX-01）
    (s.host / "t1" / "w1" / "archive" / "legacy-trader").mkdir(parents=True)
    provider = s.tools
    out_ = await run(provider, {"argv": ["python", "-c", "import os; print(os.path.basename(os.getcwd()))"], "cwd": "archive/legacy-trader"})
    assert out_["exit_code"] == 0 and out_["stdout"].strip() == "legacy-trader", out_
    missing = await run(provider, {"argv": ["true"], "cwd": "nope"})
    assert missing["exit_code"] != 0, "沙箱里 cd 不进去时命令不执行，退出码非零"


async def test_the_tool_definition_tells_the_model_about_cwd(tmp_path: Path) -> None:
    spec = await host(tmp_path).bind(T1).describe("shell_run", "v1")
    assert "cwd" in spec.input_schema["properties"] and "cd 不会保留" in spec.description


def test_the_progress_target_shows_the_directory() -> None:
    assert tool_target({"argv": ["python", "-m", "pytest", "tests"], "cwd": "archive/legacy-trader"}) == "python -m pytest tests in archive/legacy-trader"
    assert tool_target({"argv": ["ls"], "cwd": "."}) == "ls"


# --- 重做时记得上一次做了什么 ---


CALLS = [
    {"node": "run_tests", "tool": "shell_run", "target": "pip install -e .[trade] in archive/legacy-trader", "ok": True, "exit_code": 0},
    {"node": "run_tests", "tool": "shell_run", "target": "python -m pytest tests -v in archive/legacy-trader", "ok": True, "exit_code": 1},
    {"node": "run_tests", "tool": "repo_read", "target": "README.md", "ok": False, "error": "file_not_found：没有这个文件"},
]


def test_the_attempt_note_lists_what_was_run_and_how_it_ended() -> None:
    note = vd.attempt_note(CALLS)
    assert "pip install -e .[trade] in archive/legacy-trader（退出码 0）" in note
    assert "python -m pytest tests -v in archive/legacy-trader（退出码 1）" in note
    assert "README.md（失败：file_not_found" in note
    assert vd.attempt_note([]) == ""


def test_only_the_last_calls_are_listed() -> None:
    many = [{"node": "n", "tool": "shell_run", "target": f"step {i}", "ok": True, "exit_code": 0} for i in range(30)]
    note = vd.attempt_note(many)
    assert "共 30 次" in note and "step 29" in note and "step 0（" not in note


def test_a_rerun_goal_carries_the_previous_attempt() -> None:
    s = stage(1, goal("run_tests", "tool_loop", tools=["shell_run"]), deliver(after=["run_tests"]))
    results = {"run_tests": StageResult(role="transform", status="failed", error="测试导入失败")}
    verdict = {"verdict": "repair", "reason": "重试", "repair": {"nodes": ["run_tests"], "strategy": "retry_with_error"}}
    plan = vd.rerun(s, results, verdict, 2, attempts={"run_tests": CALLS})
    goal_text = next(n for n in plan.stage["nodes"] if n["id"] == "run_tests")["goal"]
    assert "上一次的尝试" in goal_text and "pip install" in goal_text and "测试导入失败" in goal_text
    plain = vd.rerun(s, results, verdict, 2)
    assert "上一次的尝试" not in plain.stage["nodes"][0]["goal"], "之前的调用方不给 attempts：goal 与原来一样"


async def test_a_retried_tool_loop_is_told_what_it_ran(harness: StageHarness) -> None:
    s = harness.script
    s.tools.update({"shell_run": "read"})
    s.tool_results["shell_run"] = lambda inp: {"exit_code": 1, "timed_out": False, "stdout": "", "stderr": "ModuleNotFoundError: psycopg"}
    tries = {"n": 0}

    def step(inp: object) -> ToolLoopStep:
        tries["n"] += 1
        if tries["n"] == 1:
            return ToolLoopStep(action="tool", tool="shell_run", args={"argv": ["python", "-m", "pytest", "-q"], "cwd": "archive/legacy-trader"})
        return ToolLoopStep(action="finish", result={"failed": 24})

    s.tool_steps["run_tests"] = step
    s.plans["main"] = [stage(1, goal("run_tests", "tool_loop", tools=["shell_run"], max_iterations=1), deliver(after=["run_tests"]))]
    s.reviewer = scripted(
        out("repair", "重试", repair={"nodes": ["run_tests"], "strategy": "retry_with_error"}),
        out("done", "完成", done={"deliver_node": "answer"}),
    )
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE, [(r.round_seq, {k: (v.status, v.error) for k, v in r.stage_results.items()}) for r in s.reviews]
    steps = [c.inp for c in s.calls if c.activity == names.TOOL_LOOP_STEP and c.workflow_id.startswith(inp.task_id)]
    assert "python -m pytest -q in archive/legacy-trader（退出码 1）" in steps[-1].node["goal"], steps[-1].node["goal"]
