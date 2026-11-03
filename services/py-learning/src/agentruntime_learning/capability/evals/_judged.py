"""要裁判的问答题（SimpleQA、FRAMES；docs/plan/capability-matrix-evals.md 第 3、5 节，role deliver）的共用流程。

1. 被测模型作答：role deliver（矩阵的 PinningGateway 把它钉在候选模型上）。
2. 裁判按数据集官方的判分提示和标准答案比：role judge、tier strong@medium，`pinned_model_profile` 明确填 Opus 5.5，经
   `evals.judging` 的 `RecordingJudgeGateway` 调用——每次判分写一行 JSONL，标签里有评测、题号、指标、被测模型，留给 gpt-6-astra
   复核（第 5 节）。这两个评测声明 `uses_judge = True`，矩阵调度（capability.matrix）就在 gateway 底下包好 RecordingJudgeGateway、
   记到运行目录的 judgments.jsonl；单独用（gateway 链上没有 RecordingJudgeGateway）时这里自己包一层，写到 `judge_log`
   （默认 `<AR_EVAL_DATA_DIR>/judgments/<评测>.jsonl`），不会记两遍。
3. `ItemResult.record` 里另有裁判看到的完整输入、结论、理由、实际服务的裁判模型与花费；被测模型也是 Anthropic 的时
   `same_vendor = True`（同家冲突：照判，单独标出，复核时优先看）。

温度：人定裁判温度 0，但 Model Gateway 的 GenerateParams 没有温度字段（改要走 S-01），所以请求里给不了，按 Profile 的默认值；
record 里如实写 `temperature: "provider default"`。裁判没落在 Opus 上（网关切到别的模型）时这道题记为出错，不计分。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_learning.capability.evals import _datasets as ds
from agentruntime_learning.evals import judging

JUDGE_PROFILE = judging.DEFAULT_JUDGE
JUDGE_TIER = "strong@medium"  # 网关只在请求的档里找钉的模型；Opus 5.5 是 strong
TEMPERATURE_NOTE = "provider default"


class JudgeNotPinned(RuntimeError):
    """裁判的调用没落在钉的模型上。"""


def judge_log_path(name: str, judge_log: Path | str | None) -> Path:
    return Path(judge_log) if judge_log else ds.data_dir() / "judgments" / f"{name}.jsonl"


def has_recorder(gateway: Any, depth: int = 8) -> bool:
    """gateway 链上（顺着 `.inner`）已经有 RecordingJudgeGateway 吗。"""
    for _ in range(depth):
        if isinstance(gateway, judging.RecordingJudgeGateway):
            return True
        gateway = getattr(gateway, "inner", None)
        if gateway is None:
            return False
    return False


def same_vendor(candidate: str, judge: str = JUDGE_PROFILE) -> bool:
    return judging.vendor_of(candidate) == judging.vendor_of(judge)


async def judge(
    meter: ds.Meter, *, eval_name: str, item_id: str, metric: str, candidate: str, messages: Sequence[mg.ChatMessage], log_path: Path,
    record: dict[str, Any], max_output_tokens: int = 0,
) -> dict[str, Any]:
    """经 RecordingJudgeGateway 调一次裁判（role judge，钉 Opus 5.5）；裁判记录（text 是裁判的回答）写进 record["judge"] 并返回。"""
    recorded = has_recorder(meter.gateway)
    gateway = meter.gateway if recorded else judging.RecordingJudgeGateway(meter.gateway, log_path, judge_profile=JUDGE_PROFILE)
    with judging.judging(eval=eval_name, case_id=item_id, metric=metric, subject_models=[candidate]):
        response = await meter.generate(role="judge", node=f"{eval_name}.grade", messages=messages, pinned=JUDGE_PROFILE, tier=JUDGE_TIER,
                                        gateway=gateway, max_output_tokens=max_output_tokens)
    served = response.model.model_profile_id if response.HasField("model") else ""
    out = {
        "requested": JUDGE_PROFILE, "served": served, "temperature": TEMPERATURE_NOTE, "messages": ds.messages_record(messages),
        "text": response.text, "cost_usd": meter.calls[-1]["cost_usd"], "log": "matrix judgments.jsonl" if recorded else str(log_path),
    }
    record["judge"] = out
    if served != JUDGE_PROFILE:
        raise JudgeNotPinned(f"裁判由 {served or '未知模型'} 服务，不是 {JUDGE_PROFILE}")
    return out


__all__ = ["JUDGE_PROFILE", "JUDGE_TIER", "JudgeNotPinned", "has_recorder", "judge", "judge_log_path", "same_vendor"]
