"""docs/plan/feedback-2026-11-01.md N-17：上下文占比的总数用实测的输入 token。

之前的总数按字数估算（ASCII 4 个字符一个 token），界面上的占比与实际计费对不上，看着像假的。现在总数取网关这次实际计的
input_tokens（含缓存命中），各类与各工具按估算的比例分摊，各类之和正好等于总数；取不到实测时保留估算。
"""

from __future__ import annotations

from agentruntime_worker.context.builder import measured

PAYLOAD = {
    "window_tokens": 1000000,
    "effective_tokens": 128000,
    "total_tokens": 1339,
    "categories": [
        {"name": "system_prompt", "tokens": 385}, {"name": "system_tools", "tokens": 514}, {"name": "mcp_tools", "tokens": 0},
        {"name": "memory", "tokens": 0}, {"name": "task_brief", "tokens": 312}, {"name": "messages", "tokens": 128},
    ],
    "tools": [{"name": "repo_list", "tokens": 70}, {"name": "repo_read", "tokens": 142}],
}


def test_the_total_is_the_measured_input() -> None:
    out = measured(PAYLOAD, 2711)
    assert out["total_tokens"] == 2711
    assert sum(c["tokens"] for c in out["categories"]) == 2711, "各类之和正好等于实测的总数"
    by = {c["name"]: c["tokens"] for c in out["categories"]}
    assert by["mcp_tools"] == 0 and by["memory"] == 0, "估算为 0 的类仍是 0"
    assert abs(by["system_tools"] / 2711 - 514 / 1339) < 0.001, "按估算的比例分摊"
    assert out["tools"][0]["tokens"] == round(70 * 2711 / 1339)
    assert out["window_tokens"] == 1000000 and out["effective_tokens"] == 128000, "窗口与压缩线不变"


def test_without_a_measurement_the_estimate_stays() -> None:
    assert measured(PAYLOAD, 0) == PAYLOAD
    empty = {**PAYLOAD, "categories": [{"name": "messages", "tokens": 0}]}
    assert measured(empty, 500) == empty


def test_the_original_is_not_changed() -> None:
    before = {**PAYLOAD, "categories": [dict(c) for c in PAYLOAD["categories"]]}
    measured(PAYLOAD, 9999)
    assert PAYLOAD == before
