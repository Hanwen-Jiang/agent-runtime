"""docs/plan/running-task-interaction.md 第 2 节验收：打断真的撤销了这次调用（activity 这一层，不经工作流）。

- 取消要真的送到：tool_loop_step、plan_round、review_round 等模型时发心跳；取消时模型调用跟着停，gRPC 真的断开。
- 打断工具调用：进程组都被杀掉（不只是直接的子进程）；write_reversible 的文件还原到调用前；不是 git 仓库、撤不回的照实报告。
- 报告的结果带回工作流（workflows/interrupts.py，SDK 上报取消时丢掉 details，所以是 type 为 tool_interrupted 的 ApplicationError），
  工作流据此告诉模型。

K8s 与 Cloudflare 后端的 exec 经 AgentClient，取消时断开连接的测试在 libs/py-core 的 tests/adapters/test_sandbox_exec_cancel.py；
agent 一侧断开后杀进程组的测试在 tests/sandbox-agent 的 test_cancel.py（Go 版与 Rust 版都跑，D-28）。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import grpc
import pytest
from temporalio.exceptions import ApplicationError, CancelledError

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime.modelgateway.v1 import model_gateway_pb2_grpc as mg_grpc
from agentruntime_core.dev.sandbox import LocalSubprocessSandbox
from agentruntime_core.enums import Effect
from agentruntime_core.providers.sandbox import ExecNotStopped
from agentruntime_core.providers.tool_driver import ToolInvocation, ToolSpec

from agentruntime_worker import names
from agentruntime_worker.activities.types import ToolCallInput
from agentruntime_worker.context.builder import ContextBuilder
from agentruntime_worker.executor import keys
from agentruntime_worker.executor.grpc_gateway import GrpcModelGateway
from agentruntime_worker.executor.tool_loop import ModelToolLoop, ToolLoopActivities
from agentruntime_worker.planner.activities import PlannerActivities
from agentruntime_worker.planner.context import StaticContextSource
from agentruntime_worker.planner.planner import Planner
from agentruntime_worker.sandbox.broker import SandboxBroker
from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools
from agentruntime_worker.tools.sandboxes import Sandboxes

from .context_support import config, objects
from .executor_harness import TASK_ID, TENANT, ExecutorFixture, make_fixture
from .planner_support import context
from .test_node_executor import env_for, node_input
from .test_planner_activity import plan_input
from .test_reviewer_activity import activities as reviewer_activities
from .test_reviewer_activity import review_input
from .test_tool_loop_model import step_input

GIT = shutil.which("git")


class BlockingGateway:
    """generate 一直等；记下有没有被取消。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("不会走到这里")

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        raise AssertionError("不做嵌入")


def waiting_on_a_model(kind: str, gateway: BlockingGateway, tmp_path: Path) -> tuple[str, Any, Any]:
    if kind == names.TOOL_LOOP_STEP:
        loop = ModelToolLoop(gateway, ContextBuilder(gateway, objects(tmp_path), config(effective_tokens=100_000)))
        return kind, ToolLoopActivities(loop).tool_loop_step, step_input([])
    if kind == names.PLAN_ROUND:
        return kind, PlannerActivities(Planner(gateway), StaticContextSource(context())).plan_round, plan_input()
    assert kind == names.REVIEW_ROUND
    return kind, reviewer_activities(gateway).review_round, review_input()


@pytest.mark.parametrize("kind", [names.TOOL_LOOP_STEP, names.PLAN_ROUND, names.REVIEW_ROUND])
async def test_waiting_on_a_model_sends_heartbeats_and_cancelling_stops_the_call(tmp_path: Path, kind: str) -> None:
    """Temporal 的取消靠心跳送达。Planner、Reviewer 把模型出错当成一次不合格，这里不能把取消也吞掉。"""
    gateway = BlockingGateway()
    name, fn, inp = waiting_on_a_model(kind, gateway, tmp_path)
    env = env_for(name, queue=names.DECISION_QUEUE, heartbeat=1)
    beats: list[Any] = []
    env.on_heartbeat = lambda *details: beats.append(details)
    task = asyncio.create_task(env.run(fn, inp))
    await asyncio.wait_for(gateway.started.wait(), 10)
    await asyncio.sleep(1.5)
    assert beats, f"{name} 等模型的时候没有发心跳，取消送不到"
    env.cancel()
    with pytest.raises((asyncio.CancelledError, CancelledError)):
        await asyncio.wait_for(task, 10)
    assert gateway.cancelled is True


async def test_cancelling_a_grpc_call_reaches_the_gateway() -> None:
    """网关随之断开上游：上游不再继续生成、继续花钱。"""
    started = asyncio.Event()
    seen = asyncio.Event()

    class Service(mg_grpc.ModelGatewayServiceServicer):
        async def Generate(self, request: mg.GenerateRequest, ctx: grpc.aio.ServicerContext) -> mg.GenerateResponse:  # noqa: N802
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                seen.set()
                raise
            return mg.GenerateResponse()

    server = grpc.aio.server()
    mg_grpc.add_ModelGatewayServiceServicer_to_server(Service(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        gateway = GrpcModelGateway(f"127.0.0.1:{port}")
        call = asyncio.create_task(gateway.generate(mg.GenerateRequest()))
        await asyncio.wait_for(started.wait(), 10)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        await asyncio.wait_for(seen.wait(), 10)
    finally:
        await server.stop(0)


# --- 工具调用 ---

TREE = r"""
import pathlib, subprocess, sys, time

beats = pathlib.Path(sys.argv[1])
who = sys.argv[2]
if who == "parent":
    pathlib.Path("tracked.txt").write_text("被这次调用改了", encoding="utf-8")
    pathlib.Path("made_by_the_call.txt").write_text("新建的", encoding="utf-8")
    subprocess.Popen([sys.executable, __file__, sys.argv[1], "child"])
# 到时间自己退出：取消没有杀掉它时（测试失败时）不留在机器上。
end = time.monotonic() + 30
while time.monotonic() < end:
    with (beats / who).open("a") as f:
        f.write(".")
    time.sleep(0.05)
"""


def workspace_of(root: Path, *, git: bool = True) -> Path:
    work = root / "workspaces" / TENANT.tenant_id / "default"
    work.mkdir(parents=True)
    (work / "tracked.txt").write_text("原来的", "utf-8")
    if git:
        assert GIT is not None
        for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "first"]):
            subprocess.run([GIT, *args], cwd=work, check=True, capture_output=True)
    return work


def with_platform_tools(tmp_path: Path, backend: str) -> ExecutorFixture:
    fx = make_fixture(tmp_path)
    root = tmp_path / "workspaces"
    if backend == "host":
        register_platform_tools(fx.tools, PlatformToolsConfig(workspace_root=root))
    else:
        async def no_policy(_: str) -> None:
            return None

        boxes = Sandboxes({"plain": {"kind": "local", "isolation": "none"}}, "plain", workspace_root=root, policy=no_policy)
        register_platform_tools(fx.tools, PlatformToolsConfig(workspace_root=root, shell_sandbox="sandbox"), sandboxes=boxes)
    return fx


def call(tool: str, effect: str, args: dict[str, Any]) -> ToolCallInput:
    return ToolCallInput(tenant=TENANT, task_id=TASK_ID, round_seq=1, node_id="fix.4", tool=tool, args=args, effect=effect)


async def wait_for_file(path: Path, running: asyncio.Task[Any], timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while not (path.exists() and path.stat().st_size > 0):
        if running.done():
            raise AssertionError(f"调用已经结束了，没有等到 {path}：{running.exception() or running.result()}")
        if time.monotonic() > deadline:
            raise AssertionError(f"{timeout} 秒内没有等到 {path}")
        await asyncio.sleep(0.05)


async def still_growing(path: Path) -> bool:
    before = path.stat().st_size
    await asyncio.sleep(1.0)
    return path.stat().st_size > before


def undo_of(err: BaseException) -> str:
    from agentruntime_worker.workflows.interrupts import undo_of as read

    return read(err)


def tree_call(tmp_path: Path) -> tuple[ToolCallInput, Path]:
    script = tmp_path / "tree.py"
    script.write_text(TREE, "utf-8")
    beats = tmp_path / "beats"
    beats.mkdir()
    return call("shell_run", "write_reversible", {"argv": [sys.executable, str(script), str(beats), "parent"], "timeout_s": 120}), beats


async def interrupted(fx: ExecutorFixture, inp: ToolCallInput, ready: Path) -> BaseException:
    env = env_for(names.CALL_TOOL, heartbeat=1)
    task = asyncio.create_task(env.run(fx.executor.call_tool, inp))
    await wait_for_file(ready, task)
    env.cancel()
    with pytest.raises(ApplicationError) as caught:
        await asyncio.wait_for(task, 20)
    return caught.value


@pytest.mark.skipif(GIT is None, reason="要 git")
@pytest.mark.parametrize("backend", ["host", "local_sandbox"])
async def test_interrupting_shell_run_kills_the_process_tree_and_restores_the_files(tmp_path: Path, backend: str) -> None:
    work = workspace_of(tmp_path)
    fx = with_platform_tools(tmp_path, backend)
    inp, beats = tree_call(tmp_path)
    err = await interrupted(fx, inp, beats / "child")
    assert undo_of(err) == "restored"
    assert not await still_growing(beats / "parent"), "命令还在跑"
    assert not await still_growing(beats / "child"), "命令的子进程还在跑"
    assert (work / "tracked.txt").read_text("utf-8") == "原来的"
    assert not (work / "made_by_the_call.txt").exists()


@pytest.mark.parametrize("backend", ["host", "local_sandbox"])
async def test_outside_a_git_repository_it_kills_the_command_and_says_it_could_not_undo(tmp_path: Path, backend: str) -> None:
    work = workspace_of(tmp_path, git=False)
    fx = with_platform_tools(tmp_path, backend)
    inp, beats = tree_call(tmp_path)
    err = await interrupted(fx, inp, beats / "child")
    assert undo_of(err) == "no_checkpoint"
    assert not await still_growing(beats / "child")
    assert (work / "tracked.txt").read_text("utf-8") == "被这次调用改了", "撤不回的不假装撤回了"


@pytest.mark.skipif(GIT is None, reason="要 git")
async def test_a_stop_the_agent_did_not_confirm_is_not_reported_as_undone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """沙箱里的 agent 没确认命令停了（旧的 agent、桥不转发）：命令可能还在改文件，还原了也可能被改回去，所以不还原，照实说。"""
    original = LocalSubprocessSandbox.exec

    async def unconfirmed(self: LocalSubprocessSandbox, env_id: str, argv: Any, **kwargs: Any) -> Any:
        try:
            return await original(self, env_id, argv, **kwargs)
        except asyncio.CancelledError:
            raise ExecNotStopped() from None

    monkeypatch.setattr(LocalSubprocessSandbox, "exec", unconfirmed)
    work = workspace_of(tmp_path)
    fx = with_platform_tools(tmp_path, "local_sandbox")
    inp, beats = tree_call(tmp_path)
    err = await interrupted(fx, inp, beats / "child")
    assert undo_of(err) == "failed"
    assert (work / "tracked.txt").read_text("utf-8") == "被这次调用改了"


@pytest.mark.skipif(GIT is None, reason="要 git")
async def test_interrupted_while_making_the_checkpoint_the_environment_goes_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """命令还没开始：没有什么要撤，借到的环境照样归还（不归还就一直占着租户的并发额度）。"""
    original = LocalSubprocessSandbox.exec
    in_checkpoint = asyncio.Event()

    async def stuck_in_git(self: LocalSubprocessSandbox, env_id: str, argv: Any, **kwargs: Any) -> Any:
        if argv[0] == "git":
            in_checkpoint.set()
            await asyncio.Event().wait()
        return await original(self, env_id, argv, **kwargs)

    monkeypatch.setattr(LocalSubprocessSandbox, "exec", stuck_in_git)
    released = []
    release = SandboxBroker.release

    async def counted(self: SandboxBroker, lease: Any, **kwargs: Any) -> Any:
        released.append(lease)
        return await release(self, lease, **kwargs)

    monkeypatch.setattr(SandboxBroker, "release", counted)
    workspace_of(tmp_path)
    fx = with_platform_tools(tmp_path, "local_sandbox")
    inp, beats = tree_call(tmp_path)
    env = env_for(names.CALL_TOOL, heartbeat=1)
    task = asyncio.create_task(env.run(fx.executor.call_tool, inp))
    await asyncio.wait_for(in_checkpoint.wait(), 20)
    env.cancel()
    with pytest.raises(ApplicationError) as caught:
        await asyncio.wait_for(task, 20)
    assert undo_of(caught.value) == "not_needed"
    assert len(released) == 1
    assert not (beats / "parent").exists(), "命令不该开始"


@pytest.mark.skipif(GIT is None, reason="要 git")
async def test_a_call_that_finishes_keeps_its_changes(tmp_path: Path) -> None:
    work = workspace_of(tmp_path)
    fx = with_platform_tools(tmp_path, "host")
    code = "import pathlib; pathlib.Path('tracked.txt').write_text('改好了', encoding='utf-8')"
    out = await env_for(names.CALL_TOOL, heartbeat=1).run(fx.executor.call_tool, call("shell_run", "write_reversible", {"argv": [sys.executable, "-c", code]}))
    assert out["exit_code"] == 0
    assert (work / "tracked.txt").read_text("utf-8") == "改好了"


@pytest.mark.skipif(GIT is None, reason="要 git")
async def test_a_program_node_is_undone_the_same_way(tmp_path: Path) -> None:
    """Atomic Node 也可以是 shell_run。"""
    work = workspace_of(tmp_path)
    fx = with_platform_tools(tmp_path, "host")
    inp, beats = tree_call(tmp_path)
    key = keys.idempotency_key(TENANT.tenant_id, TASK_ID, "run", inp.args)
    node = {"id": "run", "kind": "atomic", "template": "shell_run@v1", "executor": "program", "effect": "write_reversible", "role": "act", "idempotency_key": key, "inputs": inp.args}
    env = env_for(names.EXECUTE_PROGRAM_NODE, heartbeat=1)
    task = asyncio.create_task(env.run(fx.executor.execute_program_node, node_input(node)))
    await wait_for_file(beats / "child", task)
    env.cancel()
    with pytest.raises(ApplicationError) as caught:
        await asyncio.wait_for(task, 20)
    assert undo_of(caught.value) == "restored"
    assert not await still_growing(beats / "child")
    assert (work / "tracked.txt").read_text("utf-8") == "原来的"


def blocking_tool(fx: ExecutorFixture, name: str, effect: Effect, started: asyncio.Event) -> None:
    async def handler(_: ToolInvocation) -> Any:
        started.set()
        await asyncio.Event().wait()

    fx.tools.register(
        ToolSpec(name=name, version="v1", description=name, input_schema={"type": "object"}, output_schema={"type": "object"}, effect=effect, permissions=frozenset(), timeout=timedelta(seconds=60)),
        handler,
    )


@pytest.mark.parametrize(
    ("effect", "effect_name", "undo"),
    [(Effect.READ, "read", "not_needed"), (Effect.WRITE_REVERSIBLE, "write_reversible", "no_checkpoint")],
    ids=["read", "reversible_without_a_working_copy"],
)
async def test_other_tools_report_what_can_be_said(tmp_path: Path, effect: Effect, effect_name: str, undo: str) -> None:
    """只读的没什么要撤；登记为 write_reversible 但不在工作副本里的（MCP 等）撤不回，照实说。"""
    fx = make_fixture(tmp_path)
    started = asyncio.Event()
    blocking_tool(fx, "slow", effect, started)
    env = env_for(names.CALL_TOOL, heartbeat=1)
    task = asyncio.create_task(env.run(fx.executor.call_tool, call("slow", effect_name, {})))
    await asyncio.wait_for(started.wait(), 10)
    env.cancel()
    with pytest.raises(ApplicationError) as caught:
        await asyncio.wait_for(task, 10)
    assert undo_of(caught.value) == undo
    assert fx.calls.get("slow", 0) <= 1


def test_undo_survives_the_trip_through_temporal() -> None:
    """工作流拿到的是 ActivityError，原因是 tool_interrupted；说明按撤销的结果给，每种都不假装撤回了。"""
    from agentruntime_worker.workflows.interrupts import interruption_note, tool_interrupted

    assert "已撤销这次调用的改动" in interruption_note(tool_interrupted("restored"))
    assert "撤不回" in interruption_note(tool_interrupted("restored")), "网络请求与已经跑完的后台效果要照实说"
    assert "没能撤销" in interruption_note(tool_interrupted("failed"))
    assert "没能撤销" in interruption_note(tool_interrupted("no_checkpoint"))
    assert "被用户打断，没有结果" in interruption_note(tool_interrupted("not_needed"))
    assert "被用户打断，没有结果" in interruption_note(RuntimeError("别的取消原因")), "取不到结果时不说撤销了"
