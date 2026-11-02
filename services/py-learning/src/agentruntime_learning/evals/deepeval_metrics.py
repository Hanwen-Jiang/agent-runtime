"""Small, offline-first adapter for the DeepEval task-loop metrics.

The adapter deliberately keeps the project case format independent from the
optional ``deepeval`` package.  Cases can therefore be validated and scored in
CI/fixtures without network access; installing DeepEval 4.3.1 is only needed
by a caller that wants to pass the normalized case to its own judge runner.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

try:  # optional dependency; fixture mode must import without DeepEval installed
    from deepeval.models import DeepEvalBaseLLM  # type: ignore
except ImportError:  # pragma: no cover
    class DeepEvalBaseLLM:  # type: ignore[no-redef]
        pass

REQUIRED = ("case_id", "input", "actual_output", "tools_called")


@dataclass(frozen=True)
class MetricResult:
    name: str
    score: float | None
    threshold: float | None
    reason: str
    unscored_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "score": self.score,
            "threshold": self.threshold,
            "reason": self.reason,
        }
        if self.unscored_reason:
            out["unscored_reason"] = self.unscored_reason
        return out


@dataclass(frozen=True)
class GatewayJudgeResult:
    """Structured result from a Model Gateway judge call."""

    score: float
    reason: str
    model: str
    usage: dict[str, int]
    cost_usd: float


class GatewayDeepEvalLLM(DeepEvalBaseLLM):
    """Minimal DeepEval custom model backed by Model Gateway."""

    def __init__(self, gateway: Any, *, tenant_id: str, tier: str = "strong@medium", max_cost_usd_micros: int = 0) -> None:
        self.gateway, self.tenant_id, self.tier, self.max_cost_usd_micros = gateway, tenant_id, tier, max_cost_usd_micros

    def get_model_name(self) -> str:
        return f"gateway/{self.tier}"

    async def a_generate(self, prompt: str, schema: Any | None = None, **_: Any) -> Any:
        from google.protobuf import json_format, struct_pb2
        from agentruntime.common.v1 import enums_pb2, payload_pb2, tenant_pb2
        from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
        spec = schema if isinstance(schema, dict) else {"type": "object"}
        value = struct_pb2.Value(); json_format.ParseDict(spec, value)
        response = await self.gateway.generate(mg.GenerateRequest(
            tenant=tenant_pb2.TenantContext(tenant_id=self.tenant_id),
            call=mg.CallContext(node_id="deepeval_judge", role=enums_pb2.ROLE_JUDGE, tier=self.tier, max_cost_usd_micros=max(0, int(self.max_cost_usd_micros))),
            generation=mg.GenerateParams(messages=[mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=prompt)], output_schema=payload_pb2.JsonPayload(value=value)),
        ))
        return json_format.MessageToDict(response.result)

    def generate(self, prompt: str, schema: Any | None = None, **kwargs: Any) -> Any:
        import asyncio
        return asyncio.run(self.a_generate(prompt, schema, **kwargs))


def validate_case(case: Mapping[str, Any]) -> list[str]:
    """Return human-readable validation errors without mutating ``case``."""
    errors = [f"missing field: {key}" for key in REQUIRED if key not in case]
    if "case_id" in case and not isinstance(case["case_id"], str):
        errors.append("case_id must be a string")
    for key in ("input", "actual_output"):
        if key in case and not isinstance(case[key], str):
            errors.append(f"{key} must be a string")
    if "tools_called" in case and not isinstance(case["tools_called"], list):
        errors.append("tools_called must be a list")
    for key in ("expected_tools",):
        if key in case and case[key] is not None and not isinstance(case[key], list):
            errors.append(f"{key} must be a list")
    if "task_completed" in case and not isinstance(case["task_completed"], bool):
        errors.append("task_completed must be boolean")
    return errors


def _tool_names(value: Iterable[Any]) -> list[str]:
    names: list[str] = []
    for item in value:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, Mapping) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return names


def _unscored(name: str, reason: str) -> MetricResult:
    return MetricResult(name, None, None, "unscored", reason)


def score_case(case: Mapping[str, Any]) -> dict[str, MetricResult]:
    """Score the three first-slice metrics using deterministic fixture rules."""
    errors = validate_case(case)
    if errors:
        raise ValueError("invalid DeepEval case: " + "; ".join(errors))

    expected = case.get("expected_tools")
    if not isinstance(expected, list):
        tool = _unscored("ToolCorrectness", "expected_tools annotation is missing")
    else:
        actual_names = _tool_names(case["tools_called"])
        expected_names = _tool_names(expected)
        if not expected_names:
            score = 1.0 if not actual_names else 0.0
        else:
            matches = sum(a == b for a, b in zip(actual_names, expected_names))
            score = matches / max(len(actual_names), len(expected_names))
        tool = MetricResult("ToolCorrectness", score, 0.8, "fixture tool sequence comparison")

    completed = case.get("task_completed")
    if not isinstance(completed, bool):
        task = _unscored("TaskCompletion", "task_completed annotation is missing")
    else:
        task = MetricResult("TaskCompletion", 1.0 if completed else 0.0, 1.0, "manual completion label")

    rounds = case.get("rounds")
    if rounds is None:
        rounds = case.get("round_count")
    tool_count = len(_tool_names(case["tools_called"]))
    if not isinstance(rounds, int) or rounds < 0:
        efficiency = _unscored("StepEfficiency", "rounds/round_count is missing")
    else:
        steps = max(rounds, tool_count)
        efficiency = MetricResult("StepEfficiency", 1.0 if steps <= 1 else 1.0 / steps, 0.5, "fixture rounds/tool-step rule")
    return {r.name: r for r in (tool, task, efficiency)}


def evaluate_cases(cases: Iterable[Mapping[str, Any]], *, source: str = "fixture") -> dict[str, Any]:
    results = []
    for case in cases:
        case_id = case.get("case_id", "<unknown>")
        try:
            metrics = score_case(case)
            results.append({"case_id": case_id, "status": "ok", "metrics": {k: v.as_dict() for k, v in metrics.items()}})
        except ValueError as exc:
            results.append({"case_id": case_id, "status": "invalid", "error": str(exc)})
    return {"adapter": "deepeval", "mode": source, "version": "first-slice", "cases": results}


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        cases = raw
    elif isinstance(raw, Mapping) and isinstance(raw.get("cases"), list):
        cases = raw["cases"]
    else:
        raise ValueError("input JSON must be a case list or an object with cases[]")
    if not all(isinstance(case, Mapping) for case in cases):
        raise ValueError("cases[] entries must be objects")
    return [dict(case) for case in cases]


async def gateway_judge(
    case: Mapping[str, Any],
    gateway: Any,
    *,
    tenant_id: str,
    max_cost_usd_micros: int,
    tier: str = "strong@medium",
) -> GatewayJudgeResult:
    """Ask Model Gateway for a controlled completion judgment.

    Imports for protobufs stay inside the function so fixture-only users do not
    need a running Gateway or its generated dependencies.  The request sends
    only the normalized case fields and enforces ``role=judge`` plus a budget.
    """
    errors = validate_case(case)
    if errors:
        raise ValueError("invalid DeepEval case: " + "; ".join(errors))
    from google.protobuf import json_format, struct_pb2
    from agentruntime.common.v1 import enums_pb2, payload_pb2, tenant_pb2
    from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg

    schema = struct_pb2.Value()
    json_format.ParseDict(
        {"type": "object", "required": ["score", "reason"], "properties": {"score": {"type": "number", "minimum": 0, "maximum": 1}, "reason": {"type": "string"}}, "additionalProperties": False},
        schema,
    )
    material = {key: case.get(key) for key in ("input", "actual_output", "tools_called", "expected_tools", "task_completed", "rounds") if key in case}
    request = mg.GenerateRequest(
        tenant=tenant_pb2.TenantContext(tenant_id=tenant_id),
        call=mg.CallContext(node_id="deepeval_judge", role=enums_pb2.ROLE_JUDGE, tier=tier, max_cost_usd_micros=max(0, int(max_cost_usd_micros))),
        generation=mg.GenerateParams(
            messages=[mg.ChatMessage(role=mg.CHAT_ROLE_SYSTEM, text="You are an evaluation judge. Return only the requested JSON."), mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=json.dumps(material, ensure_ascii=False))],
            output_schema=payload_pb2.JsonPayload(value=schema),
        ),
    )
    response = await gateway.generate(request)
    data = json_format.MessageToDict(response.result)
    score = data.get("score") if isinstance(data, Mapping) else None
    reason = data.get("reason") if isinstance(data, Mapping) else None
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1 or not isinstance(reason, str):
        raise ValueError("judge response does not match score/reason schema")
    usage = response.usage
    usage_dict = {"input_tokens": int(usage.input_tokens), "output_tokens": int(usage.output_tokens), "cost_usd_micros": int(usage.cost_usd_micros)}
    return GatewayJudgeResult(float(score), reason, response.model.vendor, usage_dict, usage.cost_usd_micros / 1_000_000)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline DeepEval first-slice adapter")
    parser.add_argument("input", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    report = evaluate_cases(load_cases(args.input))
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
