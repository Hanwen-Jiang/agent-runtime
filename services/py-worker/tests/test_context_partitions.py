"""Context Builder 主体：五个分区与提示缓存（设计书 3.2.7 第 1 条、附录 G.1；R-03 评审重点“分区顺序与缓存断点”）。"""

from __future__ import annotations

from pathlib import Path

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_worker.context.builder import ContextBuilder
from agentruntime_worker.context.partitions import STATUS_TOKENS, Partitions, profile_with_changes
from agentruntime_worker.context.tokens import estimate

from .context_support import ContextGateway, config, history, objects, request


def test_partitions_are_ordered_by_stability_with_cache_breakpoints() -> None:
    history_msgs = [mg.ChatMessage(role=mg.CHAT_ROLE_ASSISTANT, text="动作"), mg.ChatMessage(role=mg.CHAT_ROLE_TOOL, text="观察")]
    messages = Partitions(system=["平台提示"], profile=["汇报用表格"], brief=["目标：修 CI"], history=history_msgs, status="进度：1/3").to_messages()
    assert [m.text for m in messages[3:5]] == ["动作", "观察"]
    assert messages[0].role == mg.CHAT_ROLE_SYSTEM and "平台提示" in messages[0].text
    assert "汇报用表格" in messages[1].text and "目标：修 CI" in messages[2].text
    assert "进度：1/3" in messages[-1].text, "status 放在最末尾"
    assert [i for i, m in enumerate(messages) if m.cache_breakpoint] == [0, 1, 2], "system、profile、brief 末尾各一个断点"


def test_empty_partitions_are_omitted() -> None:
    messages = Partitions(system=["平台提示"], brief=["目标"], status="进度").to_messages()
    assert len(messages) == 3
    assert [i for i, m in enumerate(messages) if m.cache_breakpoint] == [0, 1]


def test_status_stays_within_its_budget() -> None:
    messages = Partitions(system=["平台提示"], status="待办 " * 2_000).to_messages()
    assert estimate(messages[-1].text) <= STATUS_TOKENS


def test_profile_only_grows_by_changes() -> None:
    previous = ["汇报用表格", "用中文回复"]
    grown = profile_with_changes(previous, ["汇报用表格", "用中文回复", "数字保留两位小数"])
    assert grown[:2] == previous and "数字保留两位小数" in grown[2]
    dropped = profile_with_changes(previous, ["汇报用表格"])
    assert dropped[:2] == previous and "用中文回复" in dropped[2] and "撤销" in dropped[2], "删掉的偏好以撤销追加，不改写前缀"


async def test_the_prefix_is_byte_stable_while_history_grows(tmp_path: Path) -> None:
    """阈值以下时，多一步只在 history 末尾追加：前三个分区逐字节不变，上一次的 history 是这一次的前缀。"""
    builder = ContextBuilder(ContextGateway(), objects(tmp_path), config(effective_tokens=100_000))
    first = await builder.build(request(history(4)))
    second = await builder.build(request(history(5)))
    assert [m.SerializeToString() for m in first.messages[:3]] == [m.SerializeToString() for m in second.messages[:3]]
    # history 之后是精确状态与 status 两条（R-03 评审 D7）。
    old_history, new_history = first.messages[3:-2], second.messages[3:-2]
    assert [m.SerializeToString() for m in new_history[: len(old_history)]] == [m.SerializeToString() for m in old_history]
    assert first.applied == [] and second.applied == []
