"""TMP 计划第 11.15 节，调优第一轮：tool_loop 的一步。

- 模型的回答不合格（调了没给的工具、结构化输出不是合法 JSON、工具参数不是合法 JSON）时，不原样重问：在同一个 activity 里告诉它错在哪，再问；
  问满了才让这一步失败。基线之前的冒烟里，同一个请求原样失败 5 次，节点就没了（`unexpected_tool_call` 35 次、`bad_structured_output` 39 次）。
- 只读不动手的提醒：基线里 3 个任务用满约 150 次工具调用，一行没改，都没解决。节点能改文件，却连续读了很多次时提醒它动手。
- Context Builder 转存的门槛可配：基线里 807/813 次 repo_read 都带 max_lines，一小段一小段地读。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentruntime_core.errors import DeniedError, InvalidError, TransientError
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.context.builder import ContextBuilder
from agentruntime_worker.executor import tool_loop as tl
from agentruntime_worker.executor.model_gateway import billed_cost_usd, with_billed_cost
from agentruntime_worker.executor.tool_loop import ModelToolLoop

from .context_support import GOAL_NODE, ContextGateway, config, objects, read_step, request, text_of, tool_call
from .test_tool_loop_model import step_input


def make_loop(tmp_path: Path, gateway: ContextGateway) -> ModelToolLoop:
    return ModelToolLoop(gateway, ContextBuilder(gateway, objects(tmp_path), config(effective_tokens=100_000)))


def bad(code: str, message: str = "不合格", billed: int = 0) -> InvalidError:
    return with_billed_cost(InvalidError(code, message), billed)


def edit_step(i: int = 0) -> dict[str, Any]:
    return {"tool": "repo_edit", "args": {"path": f"src/module_{i}.py", "new": "x"}, "observation": {"path": f"src/module_{i}.py"}}


def shell_step(*argv: str) -> dict[str, Any]:
    return {"tool": "shell_run", "args": {"argv": list(argv)}, "observation": {"exit_code": 0, "stdout": "", "stderr": ""}}


# --- 不合格的回答：告诉模型错在哪，再问 ---


@pytest.mark.parametrize("code", ["unexpected_tool_call", "bad_structured_output", "bad_tool_arguments"])
async def test_an_unusable_answer_is_corrected_instead_of_failing_the_step(tmp_path: Path, code: str) -> None:
    gateway = ContextGateway(steps=[bad(code), tool_call("repo_read", path="a.py")])
    step = await make_loop(tmp_path, gateway).step(step_input([]))
    assert (step.action, step.tool) == ("tool", "repo_read")
    assert len(gateway.requests) == 2


async def test_the_second_ask_says_what_was_wrong_and_which_tools_exist(tmp_path: Path) -> None:
    gateway = ContextGateway(steps=[bad("unexpected_tool_call", "模型调用了请求里没给的工具：web_browser"), tool_call("repo_read", path="a.py")])
    await make_loop(tmp_path, gateway).step(step_input([]))
    first, second = gateway.requests
    assert tl.CORRECTION_HEADER not in text_of(first.generation.messages), "第一次问的时候还没有错"
    text = text_of(second.generation.messages)
    assert tl.CORRECTION_HEADER in text
    assert "web_browser" in text, "说清楚调错了哪个"
    for name in GOAL_NODE["tools"]:
        assert name in text, "说清楚能用哪些"


async def test_the_answer_format_error_asks_for_json(tmp_path: Path) -> None:
    gateway = ContextGateway(steps=[bad("bad_structured_output"), {"summary": "done"}])
    step = await make_loop(tmp_path, gateway).step(step_input([]))
    assert step.action == "finish"
    assert "JSON" in text_of(gateway.requests[1].generation.messages)


async def test_it_gives_up_after_the_limit_with_every_billed_call_counted(tmp_path: Path) -> None:
    attempts = tl.MAX_CORRECTIONS + 1
    gateway = ContextGateway(steps=[bad("bad_structured_output", billed=2_000) for _ in range(attempts)])
    with pytest.raises(InvalidError) as caught:
        await make_loop(tmp_path, gateway).step(step_input([]))
    assert caught.value.code == "bad_structured_output"
    assert len(gateway.requests) == attempts
    assert billed_cost_usd(caught.value) == pytest.approx(attempts * 0.002), "失败的调用也花了钱，要算进 Round 的成本"


async def test_a_corrected_step_includes_the_cost_of_the_failed_call(tmp_path: Path) -> None:
    gateway = ContextGateway(steps=[bad("unexpected_tool_call", billed=5_000), tool_call("repo_read", path="a.py")])
    step = await make_loop(tmp_path, gateway).step(step_input([]))
    assert step.cost_usd >= 0.005


@pytest.mark.parametrize(
    "error",
    [TransientError("models_exhausted", "暂时不可用"), DeniedError("budget_exceeded", "预算用完"), InvalidError("empty_messages", "请求本身不对")],
    ids=["transient", "denied", "other_invalid"],
)
async def test_other_errors_are_not_corrected(tmp_path: Path, error: Exception) -> None:
    """重试 transient 是工作流的事；denied 与请求本身的错误，再问也没用。"""
    gateway = ContextGateway(steps=[error])
    with pytest.raises(type(error)):
        await make_loop(tmp_path, gateway).step(step_input([]))
    assert len(gateway.requests) == 1


async def test_the_correction_stays_out_of_the_recorded_history(tmp_path: Path) -> None:
    """改正只是这一步里的事：下一步不再带着它。工作流传给下一步的 history 不变。"""
    history = [read_step(1)]
    before = list(history)
    gateway = ContextGateway(steps=[bad("unexpected_tool_call"), tool_call("repo_read", path="a.py")])
    await make_loop(tmp_path, gateway).step(step_input(history))
    assert history == before


# --- 只读不动手的提醒 ---


async def test_many_reads_without_a_change_get_a_reminder(tmp_path: Path) -> None:
    gateway = ContextGateway(steps=[tool_call("repo_read", path="a.py")])
    await make_loop(tmp_path, gateway).step(step_input([read_step(i) for i in range(tl.READ_ONLY_REMINDER_AT)]))
    assert tl.READ_ONLY_REMINDER in text_of(gateway.requests[-1].generation.messages)


async def test_a_few_reads_get_no_reminder(tmp_path: Path) -> None:
    gateway = ContextGateway(steps=[tool_call("repo_read", path="a.py")])
    await make_loop(tmp_path, gateway).step(step_input([read_step(i) for i in range(tl.READ_ONLY_REMINDER_AT - 1)]))
    assert tl.READ_ONLY_REMINDER not in text_of(gateway.requests[-1].generation.messages)


async def test_a_node_that_cannot_change_files_gets_no_reminder(tmp_path: Path) -> None:
    """只读的任务（调研、问答）读再多也对。"""
    gateway = ContextGateway(steps=[tool_call("repo_read", path="a.py")])
    inp = step_input([read_step(i) for i in range(tl.READ_ONLY_REMINDER_AT + 5)])
    inp.node = {**GOAL_NODE, "tools": ["repo_read", "repo_grep"]}
    await make_loop(tmp_path, gateway).step(inp)
    assert tl.READ_ONLY_REMINDER not in text_of(gateway.requests[-1].generation.messages)


async def test_a_change_resets_the_count(tmp_path: Path) -> None:
    history = [*(read_step(i) for i in range(tl.READ_ONLY_REMINDER_AT)), edit_step(), read_step(99)]
    gateway = ContextGateway(steps=[tool_call("repo_read", path="a.py")])
    await make_loop(tmp_path, gateway).step(step_input(history))
    assert tl.READ_ONLY_REMINDER not in text_of(gateway.requests[-1].generation.messages)


async def test_reading_through_the_shell_counts_as_reading(tmp_path: Path) -> None:
    """基线里 pylint 那题用 sed -n、git grep 读了很多遍。"""
    history = [shell_step("sed", "-n", f"{i},{i + 5}p", "a.py") for i in range(tl.READ_ONLY_REMINDER_AT)]
    gateway = ContextGateway(steps=[tool_call("repo_read", path="a.py")])
    await make_loop(tmp_path, gateway).step(step_input(history))
    assert tl.READ_ONLY_REMINDER in text_of(gateway.requests[-1].generation.messages)


async def test_running_something_resets_the_count_too(tmp_path: Path) -> None:
    """跑测试、跑脚本不是“只读”：模型在动手验证。"""
    history = [*(read_step(i) for i in range(tl.READ_ONLY_REMINDER_AT)), shell_step("python", "-m", "pytest", "tests/a.py")]
    gateway = ContextGateway(steps=[tool_call("repo_read", path="a.py")])
    await make_loop(tmp_path, gateway).step(step_input(history))
    assert tl.READ_ONLY_REMINDER not in text_of(gateway.requests[-1].generation.messages)


# --- 转存的门槛可配 ---


def test_the_offload_threshold_comes_from_the_environment() -> None:
    assert WorkerConfig.from_env({}).context_offload_tokens == 2_000, "默认不变"
    assert WorkerConfig.from_env({"AR_CONTEXT_OFFLOAD_TOKENS": "8000"}).context_offload_tokens == 8_000
    for value in ("0", "many"):
        with pytest.raises(ValueError):
            WorkerConfig.from_env({"AR_CONTEXT_OFFLOAD_TOKENS": value})


def test_the_worker_passes_both_context_settings_to_the_builder(tmp_path: Path) -> None:
    built = tl.dev_tool_loop(WorkerConfig(tool_loop="model", data_dir=tmp_path, context_effective_tokens=128_000, context_offload_tokens=8_000))
    assert built.loop.builder.config.effective_tokens == 128_000
    assert built.loop.builder.config.offload_tokens == 8_000


async def test_an_observation_under_the_threshold_stays_in_the_context(tmp_path: Path) -> None:
    gateway = ContextGateway(steps=[])
    step = read_step(1, chars=16_000)  # 约 4000 token
    tail = "<<the very end of the file>>"
    step["observation"]["content"] += tail
    low = await ContextBuilder(gateway, objects(tmp_path / "low"), config(effective_tokens=128_000, offload_tokens=2_000)).build(request([step]))
    high = await ContextBuilder(gateway, objects(tmp_path / "high"), config(effective_tokens=128_000, offload_tokens=8_000)).build(request([step]))
    assert tail not in text_of(low.messages), "默认门槛下转存，只留摘录"
    assert tail in text_of(high.messages), "门槛调高后留在上下文里"
