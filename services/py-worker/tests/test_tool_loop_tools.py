"""T-10 验收：tool_loop 用真的平台内工具跑完一个 Task（细化见 docs/plan/tool-driver.md 第 2 节、第 4 节）。

Temporal 本地开发服务上跑真实的 Task 工作流与 Stage Runner；tool_loop 每一步的“思考”用脚本（不调模型），工具调用走真实的
NodeExecutor.call_tool 与开发用 ToolDriver（平台内工具登记在里面），resolve_catalog 用真实的 DevCatalog（工具 effect 取自登记）。
覆盖：
- 读文件 → 改文件 → 在工作区里跑检查 → 取网页 → 完成：每一步的观察回到下一步的 history，工作区真的改了。
- 关掉缺口：登记为 write_irreversible 的工具，effect 上限为 read 的 Goal Node 展开时就被拒绝，工具一次也没调；
  上限允许时每次调用前问人，批准后才调用。
"""

from __future__ import annotations

import contextlib
import io
import sys
import threading
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agentruntime.common.v1 import enums_pb2

from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.enums import Effect
from agentruntime_core.providers.tool_driver import ToolInvocation, ToolSpec

from agentruntime_worker import names
from agentruntime_worker.activities.types import ToolLoopStep, ToolLoopStepInput
from agentruntime_worker.policy import PolicyEngine
from agentruntime_worker.tools import DevCatalog, ToolCatalog
from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools
from agentruntime_worker.workflows.stage_runner import SubAgentWorkflow, SubgraphWorkflow
from agentruntime_worker.workflows.task import TaskWorkflow

from .executor_harness import make_fixture
from .stage_harness import Script
from .support import task_input
from .test_stage_runner import atomic, goal, scripted_steps, stage
from .test_task_verdicts import reply_with

pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]

CALC = "def round_half(x):\n    return int(x)\n"
CHECK = "from calc import round_half\nassert round_half(2.5) == 3, round_half(2.5)\nprint('ok')\n"


class _Site(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = "<p>存储价格：每 GB 0.02 美元</p>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture
def site() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


class ToolsHarness:
    def __init__(self, client: Client, script: Script, workspace: Path, sent: list[dict[str, Any]]) -> None:
        self.client = client
        self.script = script
        self.workspace = workspace
        self.sent = sent

    async def run(self, **kwargs: Any) -> Any:
        inp = task_input(scenario="main", **kwargs)
        handle = await self.client.start_workflow(names.TASK_WORKFLOW, inp, id=inp.task_id, task_queue=names.TASK_QUEUE)
        return inp, await handle.result()


@pytest_asyncio.fixture(loop_scope="session")
async def tools_harness(temporal_env: WorkflowEnvironment, tmp_path: Path) -> AsyncIterator[ToolsHarness]:
    root = tmp_path / "workspaces"
    workspace = root / "t1" / "default"  # task_input 的租户没有 workspace，用 default
    workspace.mkdir(parents=True)
    (workspace / "calc.py").write_text(CALC, "utf-8", newline="\n")
    (workspace / "check.py").write_text(CHECK, "utf-8", newline="\n")

    provider = LocalToolDriverProvider()
    register_platform_tools(provider, PlatformToolsConfig(workspace_root=root, fetch_hosts=("127.0.0.1",), allow_private_fetch=True))
    sent: list[dict[str, Any]] = []

    async def send_email(invocation: ToolInvocation) -> Any:
        sent.append(dict(invocation.arguments))
        return {"sent": True}

    async def compute(invocation: ToolInvocation) -> Any:
        return {"text": "完成"}

    def spec(name: str, effect: Effect) -> ToolSpec:
        return ToolSpec(name=name, version="v1", description=name, input_schema={"type": "object"}, output_schema={"type": "object"},
                        effect=effect, permissions=frozenset(), timeout=timedelta(seconds=10))

    provider.register(spec("send_email", Effect.WRITE_IRREVERSIBLE), send_email)
    provider.register(spec("compute", Effect.NONE), compute)

    fx = make_fixture(tmp_path / "executor", tools=lambda _inner: provider)
    ex = fx.executor
    catalog = DevCatalog(ToolCatalog(provider))
    script = Script(engine=PolicyEngine(telemetry=ConsoleTelemetryProvider(stream=io.StringIO())))
    client = temporal_env.client
    workers = [
        Worker(client, task_queue=names.TASK_QUEUE, workflows=[TaskWorkflow, SubAgentWorkflow, SubgraphWorkflow], activities=[script.run_hooks]),
        Worker(
            client,
            task_queue=names.DECISION_QUEUE,
            activities=[script.plan_round, script.review_round, script.finalize_task, script.load_stage_policy, catalog.resolve_catalog,
                        script.expand_template, script.ask_human, script.cancel_human],
        ),
        Worker(client, task_queue=names.MODEL_QUEUE, activities=[ex.execute_model_node, script.tool_loop_step]),
        Worker(client, task_queue=names.PROGRAM_QUEUE, activities=[ex.execute_program_node, ex.reconcile_node, ex.call_tool]),
        # T-11b：进度事件交给 Session Worker；这里用脚本的替身记下。
        Worker(client, task_queue=names.SESSION_QUEUE, activities=[script.publish_task_event]),
    ]
    h = ToolsHarness(client, script, workspace, sent)
    async with contextlib.AsyncExitStack() as stack:
        for w in workers:
            await stack.enter_async_context(w)
        yield h


def answer(after: list[str]) -> dict[str, Any]:
    return atomic("answer", executor="program", role="deliver", template="compute@v1", after=after)


def steps_seen(s: Script, goal_id: str) -> list[ToolLoopStepInput]:
    return [c.inp for c in s.calls if c.activity == names.TOOL_LOOP_STEP and c.inp.node["id"] == goal_id]


async def test_tool_loop_finishes_a_task_with_real_tools(tools_harness: ToolsHarness, site: str) -> None:
    h = tools_harness
    s = h.script
    s.tool_steps["fix"] = scripted_steps(
        ToolLoopStep(action="tool", tool="repo_read", args={"path": "calc.py"}),
        ToolLoopStep(action="tool", tool="repo_edit", args={"path": "calc.py", "old": "return int(x)", "new": "return int(x + 0.5)"}),
        ToolLoopStep(action="tool", tool="shell_run", args={"argv": [sys.executable, "check.py"], "timeout_s": 60}),
        ToolLoopStep(action="tool", tool="web_fetch", args={"url": site + "/pricing"}),
        ToolLoopStep(action="finish", result={"patched": True}),
    )
    fix = goal("fix", "tool_loop", tools=["repo_read", "repo_edit", "shell_run", "web_fetch"], effect_ceiling="write_reversible", max_iterations=10)
    s.plans["main"] = [stage(1, fix, answer(["fix"]))]
    s.deliver["main"] = "answer"
    _, result = await h.run(effect_ceiling=enums_pb2.EFFECT_WRITE_REVERSIBLE)

    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert "int(x + 0.5)" in (h.workspace / "calc.py").read_text("utf-8"), "工作区真的改了"
    history = steps_seen(s, "fix")[-1].history
    assert [e["tool"] for e in history] == ["repo_read", "repo_edit", "shell_run", "web_fetch"]
    assert all("error" not in e for e in history), history
    read, edit, run, fetch = (e["observation"] for e in history)
    assert "return int(x)" in read["content"]
    assert edit["path"] == "calc.py"
    assert run["exit_code"] == 0 and "ok" in run["stdout"], "改完之后在工作区里跑检查通过"
    assert fetch["status"] == 200 and "0.02" in fetch["text"]
    assert not s.questions, "没有不可逆工具，不问人"


async def test_an_irreversible_tool_is_refused_below_its_effect(tools_harness: ToolsHarness) -> None:
    """T-09 登记的缺口：桩 Catalog 把它当 read，effect 上限为 read 的 Goal Node 会照样展开、照样发邮件。"""
    h = tools_harness
    s = h.script
    s.tool_steps["mail"] = scripted_steps(ToolLoopStep(action="tool", tool="send_email", args={"to": "ops@example.com"}), ToolLoopStep(action="finish", result={}))
    s.plans["main"] = [stage(1, goal("mail", "tool_loop", tools=["send_email"], effect_ceiling="read"), answer(["mail"]))]
    s.deliver["main"] = "answer"
    inp, _ = await h.run(effect_ceiling=enums_pb2.EFFECT_WRITE_IRREVERSIBLE)
    mail = s.review(inp.task_id, 1).stage_results["mail"]
    assert mail.failure_class == "denied" and "send_email" in mail.error
    assert h.sent == [] and not steps_seen(s, "mail"), "展开时就拒绝，工具一次也没调"


async def test_an_irreversible_tool_call_waits_for_approval(tools_harness: ToolsHarness) -> None:
    h = tools_harness
    s = h.script
    s.tool_steps["mail"] = scripted_steps(ToolLoopStep(action="tool", tool="send_email", args={"to": "ops@example.com"}), ToolLoopStep(action="finish", result={}))
    s.plans["main"] = [stage(1, goal("mail", "tool_loop", tools=["send_email"], effect_ceiling="write_irreversible"), answer(["mail"]))]
    s.deliver["main"] = "answer"
    s.on_question = reply_with(h, "approve")  # type: ignore[arg-type]
    _, result = await h.run(effect_ceiling=enums_pb2.EFFECT_WRITE_IRREVERSIBLE)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert [q.question_id for q in s.questions] == ["mail.2.1"], "每次调用不可逆工具前问人（D16）"
    assert h.sent == [{"to": "ops@example.com"}], "批准后才调用，只调一次"
