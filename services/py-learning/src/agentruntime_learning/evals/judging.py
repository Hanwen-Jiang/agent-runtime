"""评测的裁判经 Model Gateway 调用，并把每次判分落盘（2026-11-03 人定：裁判先用 Opus 5.5，全程留记录，之后由 gpt-6-astra 抽查复核）。

`RecordingJudgeGateway` 包在 Model Gateway 外面：

- role 为 judge 的调用填 `pinned_model_profile = 裁判模型`（默认 Opus 5.5），其他调用原样转发；
- 每次裁判调用写一行 JSONL：时间、评测与用例、指标名、裁判看到的全部消息、输出 schema、回答（文字或结构化结果）、实际服务的模型、
  token 与花费、出错时的错误类型；以及被测一侧的模型（调用方给的 `subject_models`），裁判与被测同家时标 `same_vendor`；
- 只记模型的输入输出，不记请求头或密钥（Gateway 的客户端不把密钥放进请求体）；
- 没落在钉的模型上（裁判超时、Gateway 按选档回退到别的模型，2026-11-03 t18 的 Ragas 40 次里有 4 次换成了 Sonnet 5.5）时
  重问，至多 `pin_attempts` 次；每次尝试都记一行（`attempt`），被重问取代的那行标 `superseded`，汇总只按最后一次算。

DeepEval、Ragas 的 Gateway 模型（`deepeval_metrics.GatewayDeepEvalLLM`、`ragas.ModelGatewayRagasLLM`）把这个包装当 gateway 用，
所以框架内部的多次调用（G-Eval 的步骤与打分、Ragas 的逐句判断）都会被逐条记下。
"""

from __future__ import annotations

import contextvars
import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.protobuf import json_format

from agentruntime.common.v1 import enums_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg

DEFAULT_JUDGE = "a6api/claude-opus-5-5@2026-10"
JUDGE_LOG = "judgments.jsonl"

_case: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("judge_case", default={})


def vendor_of(profile: str) -> str:
    """Profile id 的模型名猜供应商：claude → anthropic、gpt/o → openai、gemini → google，其余取模型名的前缀。"""
    model = profile.split("/", 1)[-1].split("@", 1)[0].lower()
    for prefix, vendor in (("claude", "anthropic"), ("gpt", "openai"), ("o1", "openai"), ("o3", "openai"), ("o4", "openai"),
                           ("gemini", "google"), ("deepseek", "deepseek"), ("text-embedding", "openai")):
        if model.startswith(prefix):
            return vendor
    return model.split("-", 1)[0]


@contextmanager
def judging(**labels: Any) -> Iterator[None]:
    """给这段代码里的裁判调用打标签（评测、用例 id、指标名、被测模型等），写进每一行记录。"""
    token = _case.set({**_case.get(), **labels})
    try:
        yield
    finally:
        _case.reset(token)


class RecordingJudgeGateway:
    def __init__(self, inner: Any, log_path: Path, *, judge_profile: str = DEFAULT_JUDGE, pin_attempts: int = 3) -> None:
        self.inner = inner
        self.judge_profile = judge_profile
        self.pin_attempts = max(1, int(pin_attempts))
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.cost_usd_micros = 0
        self.calls = 0

    def _write(self, row: Mapping[str, Any]) -> None:
        line = json.dumps(row, ensure_ascii=False)
        with self._lock, self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        if request.call.role != enums_pb2.ROLE_JUDGE:
            return await self.inner.generate(request)
        pinned = mg.GenerateRequest.FromString(request.SerializeToString())
        pinned.call.pinned_model_profile = self.judge_profile
        for attempt in range(1, self.pin_attempts + 1):
            response, row = await self._judge(pinned, attempt)
            if row["judge_pinned_ok"] or attempt == self.pin_attempts:
                self._write(row)
                return response
            self._write({**row, "superseded": True})
        raise AssertionError("unreachable")

    async def _judge(self, pinned: mg.GenerateRequest, attempt: int) -> tuple[mg.GenerateResponse, dict[str, Any]]:
        labels = dict(_case.get())
        subject = [str(m) for m in labels.pop("subject_models", []) or []]
        row: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"), **labels, "attempt": attempt,
            "judge_requested": self.judge_profile,
            "messages": [{"role": mg.ChatRole.Name(m.role), "text": m.text} for m in pinned.generation.messages],
            "output_schema": json_format.MessageToDict(pinned.generation.output_schema.value) if pinned.generation.HasField("output_schema") else None,
            "subject_models": subject,
        }
        try:
            response = await self.inner.generate(pinned)
        except Exception as exc:
            row["error"] = type(exc).__name__
            row["billed_cost_usd_micros"] = int(getattr(exc, "billed_cost_usd_micros", 0) or 0)
            self.cost_usd_micros += row["billed_cost_usd_micros"]
            self._write(row)
            raise
        served = response.model.model_profile_id if response.HasField("model") else ""
        self.calls += 1
        self.cost_usd_micros += int(response.usage.cost_usd_micros)
        row.update({
            "judge_served": served,
            "judge_pinned_ok": served == self.judge_profile,
            "same_vendor": any(vendor_of(m) == vendor_of(served or self.judge_profile) for m in subject),
            "text": response.text,
            "result": json_format.MessageToDict(response.result) if response.HasField("result") else None,
            "usage": {"input_tokens": int(response.usage.input_tokens), "output_tokens": int(response.usage.output_tokens),
                      "cost_usd_micros": int(response.usage.cost_usd_micros)},
        })
        return response, row

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        return await self.inner.embed(request)


def summarize_log(path: Path) -> dict[str, Any]:
    """裁判记录的汇总：调用数（含被重问取代的）、重问数、出错数、最后仍没落在钉的模型上的次数、同家冲突数、花费（每次调用都算）。"""
    rows = [json.loads(line) for line in Path(path).read_text("utf-8").splitlines() if line.strip()] if Path(path).exists() else []
    final = [r for r in rows if not r.get("superseded")]
    return {
        "calls": len(rows),
        "retried": len(rows) - len(final),
        "errors": sum(1 for r in final if r.get("error")),
        "not_pinned": sum(1 for r in final if not r.get("error") and not r.get("judge_pinned_ok")),
        "same_vendor": sum(1 for r in final if r.get("same_vendor")),
        "cost_usd": round(sum(int((r.get("usage") or {}).get("cost_usd_micros") or r.get("billed_cost_usd_micros") or 0) for r in rows) / 1e6, 6),
    }


def gateway_from_env() -> Any:
    """AR_EVAL_GATEWAY_FACTORY 给的 Model Gateway（同评测的被测对象，worker_subject._gateway）。"""
    from agentruntime_learning.evals.worker_subject import _gateway

    return _gateway()


__all__ = ["DEFAULT_JUDGE", "JUDGE_LOG", "RecordingJudgeGateway", "gateway_from_env", "judging", "summarize_log", "vendor_of"]

