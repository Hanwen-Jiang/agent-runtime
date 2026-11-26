#!/usr/bin/env python3
"""Paired live ablation for pre-execution JSON Schema validation + field feedback (WS2).

Compares:
  Arm A (baseline): execute first model turn directly without pre-exec schema guard
  Arm B (candidate): intercept invalid tool calls via validate_tool_arguments() +
                     up to 2 live repair turns with format_schema_correction()

Hard budget guard defaults to $0.75 (never exceeds $0.80) using gateway-reported
usage.cost_usd_micros.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.protobuf import json_format

ROOT = Path(__file__).resolve().parents[1]
for pkg in ("py-core/src", "py-proto/src", "py-worker/src", "py-learning/src"):
    p = str(ROOT / "services" / pkg)
    if p not in sys.path:
        sys.path.insert(0, p)

from agentruntime.common.v1 import enums_pb2, tenant_pb2  # noqa: E402
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg  # noqa: E402
from agentruntime_learning.capability.evals import _datasets as ds  # noqa: E402
from agentruntime_learning.capability.evals import bfcl  # noqa: E402
from agentruntime_worker.executor.grpc_gateway import GrpcModelGateway  # noqa: E402
from agentruntime_worker.executor.tool_schema import (  # noqa: E402
    format_schema_correction,
    validate_tool_arguments,
)

GATEWAY_ADDR = os.environ.get("AR_MODEL_GATEWAY_ADDRESS", "127.0.0.1:18091")
TENANT_ID = os.environ.get("AR_ABLATION_TENANT", "ablation-20261127")
MAX_BUDGET_USD = float(os.environ.get("AR_TOOL_ABLATION_BUDGET_USD", "0.75"))
MAX_REPAIR_TURNS = 2

MODELS: tuple[tuple[str, str], ...] = (
    ("a6api/qwen3.8-flash@2026-11", "small@low"),
    ("a6api/gemini-3.8-flash@2026-11", "mid@low"),
)


def _load_balanced_bfcl(limit_per_cat: int = 5) -> list[dict[str, Any]]:
    path = ROOT / "services/py-learning/evalsets/public/bfcl.jsonl"
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        cat = str(row["extra"]["category"])
        buckets[cat].append(row)
    selected: list[dict[str, Any]] = []
    for cat in ("simple", "multiple", "parallel", "parallel_multiple"):
        selected.extend(buckets[cat][:limit_per_cat])
    return selected


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return round(ordered[idx], 2)


def _run_schema_fuzz_stress(items: list[dict[str, Any]]) -> dict[str, Any]:
    total = 0
    caught = 0
    by_kind: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for item in items:
        for fn in item["extra"]["function"]:
            t = bfcl.to_tool(fn)
            schema = t["parameters"]
            name = t["name"]
            props = schema.get("properties") or {}
            req = list(schema.get("required") or [])

            mutations: list[tuple[str, dict[str, Any]]] = []
            valid_base: dict[str, Any] = {}
            for k, pschema in props.items():
                ptype = pschema.get("type", "string")
                if "enum" in pschema and pschema["enum"]:
                    valid_base[k] = pschema["enum"][0]
                elif ptype == "integer":
                    valid_base[k] = 1
                elif ptype == "number":
                    valid_base[k] = 1.5
                elif ptype == "boolean":
                    valid_base[k] = True
                elif ptype == "array":
                    valid_base[k] = []
                elif ptype == "object":
                    valid_base[k] = {}
                else:
                    valid_base[k] = "sample"

            if req:
                missing = dict(valid_base)
                missing.pop(req[0], None)
                mutations.append(("missing_required", missing))
            for k, pschema in props.items():
                if pschema.get("type") == "integer":
                    bad = dict(valid_base)
                    bad[k] = "not-an-integer"
                    mutations.append(("type_mismatch", bad))
                    break
            extra = dict(valid_base)
            extra["__unexpected_probe_field__"] = 123
            schema_no_extra = {**schema, "additionalProperties": False}
            errs = validate_tool_arguments(name, extra, schema_no_extra)
            total += 1
            by_kind["additional_property"][0] += 1
            if errs:
                caught += 1
                by_kind["additional_property"][1] += 1

            for kind, payload in mutations:
                total += 1
                by_kind[kind][0] += 1
                if validate_tool_arguments(name, payload, schema):
                    caught += 1
                    by_kind[kind][1] += 1

    return {
        "total_cases": total,
        "caught_cases": caught,
        "catch_rate": round(caught / total, 4) if total else 0.0,
        "by_kind": {k: {"total": v[0], "caught": v[1]} for k, v in sorted(by_kind.items())},
    }


def _inspect_calls(
    tool_calls: list[mg.ToolCall],
    schemas: dict[str, dict[str, Any]],
    functions: list[dict[str, Any]],
    category: str,
    ground_truth: Any,
) -> tuple[bool, list[str], bool, list[dict[str, Any]]]:
    if not tool_calls:
        return False, ["no tool call returned; invoke one of the declared tools"], False, []

    errors: list[str] = []
    specs_props = {
        bfcl.tool_name(f["name"]): (f.get("parameters") or {}).get("properties") or {}
        for f in functions
    }
    output: list[dict[str, Any]] = []
    for c in tool_calls:
        raw_args = json_format.MessageToDict(c.arguments) if c.HasField("arguments") else {}
        schema = schemas.get(c.name)
        if schema is None:
            errors.append(f"unknown tool {c.name}; allowed tools: {', '.join(sorted(schemas))}")
        else:
            schema_no_extra = {**schema, "additionalProperties": False}
            call_errs = validate_tool_arguments(c.name, raw_args, schema_no_extra)
            errors.extend(call_errs)
        restored = {
            k: bfcl.restore_ints(v, specs_props.get(c.name, {}).get(k))
            for k, v in raw_args.items()
        }
        output.append({c.name: restored})

    schema_valid = len(errors) == 0
    ast_res = bfcl.ast_check(category, functions, output, ground_truth)
    return schema_valid, errors, bool(ast_res["valid"]), output


async def _call_gateway(
    gw: GrpcModelGateway,
    *,
    profile: str,
    tier: str,
    task_id: str,
    messages: list[mg.ChatMessage],
    tool_specs: list[mg.ToolSpec],
) -> tuple[mg.GenerateResponse | None, float, float, str | None]:
    t0 = time.perf_counter()
    try:
        resp = await gw.generate(
            mg.GenerateRequest(
                tenant=tenant_pb2.TenantContext(tenant_id=TENANT_ID, workspace_id="tool-ablation"),
                call=mg.CallContext(
                    task_id=task_id,
                    node_id="bfcl_tool",
                    role=enums_pb2.ROLE_ACT,
                    task_family="general.coding",
                    tier=tier,
                    pinned_model_profile=profile,
                ),
                generation=mg.GenerateParams(
                    messages=messages,
                    tools=tool_specs,
                    max_output_tokens=1024,
                ),
            )
        )
        latency_ms = (time.perf_counter() - t0) * 1000.0
        cost = float(resp.usage.cost_usd_micros) / 1_000_000.0
        return resp, cost, latency_ms, None
    except Exception as exc:  # noqa: BLE001
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return None, 0.0, latency_ms, f"{type(exc).__name__}: {exc}"


async def main() -> None:
    items = _load_balanced_bfcl(limit_per_cat=5)
    fuzz = _run_schema_fuzz_stress(items)

    gw = GrpcModelGateway(GATEWAY_ADDR, timeout_s=60.0)
    total_cost_a = 0.0
    total_cost_b = 0.0
    cumulative_cost = 0.0
    stopped_for_budget = False
    cost_lock = asyncio.Lock()
    sem = asyncio.Semaphore(4)

    async def _run_pair(profile: str, tier: str, item: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal total_cost_a, total_cost_b, cumulative_cost, stopped_for_budget
        async with sem:
            async with cost_lock:
                if cumulative_cost >= MAX_BUDGET_USD:
                    stopped_for_budget = True
                    return None

            raw_q = json.loads(item["question"])
            functions = item["extra"]["function"]
            category = item["extra"]["category"]
            tools = [bfcl.to_tool(f) for f in functions]
            schemas = {t["name"]: t["parameters"] for t in tools}
            tool_specs = [ds.tool_spec(t["name"], t["description"], t["parameters"]) for t in tools]
            messages = [ds.chat(m["role"], m["content"]) for m in raw_q[0]]

            resp_a, cost_a, lat_a, err_a = await _call_gateway(
                gw,
                profile=profile,
                tier=tier,
                task_id=f"tool-ablation:{profile}:{item['id']}:a",
                messages=list(messages),
                tool_specs=tool_specs,
            )
            async with cost_lock:
                cumulative_cost += cost_a
                total_cost_a += cost_a
                total_cost_b += cost_a

            if resp_a is None:
                return {
                    "model": profile,
                    "item_id": item["id"],
                    "category": category,
                    "error_a": err_a,
                    "schema_valid_a": False,
                    "ast_valid_a": False,
                    "schema_valid_b": False,
                    "ast_valid_b": False,
                    "repaired": False,
                    "avoided_bad_execution": False,
                    "extra_turns": 0,
                    "cost_a_usd": round(cost_a, 6),
                    "cost_b_usd": round(cost_a, 6),
                    "latency_a_ms": round(lat_a, 2),
                    "latency_b_ms": round(lat_a, 2),
                }

            schema_ok_a, errs_a, ast_ok_a, _ = _inspect_calls(
                list(resp_a.tool_calls), schemas, functions, category, item["answer"]
            )

            schema_ok_b = schema_ok_a
            ast_ok_b = ast_ok_a
            extra_turns = 0
            cost_b_extra = 0.0
            lat_b_extra = 0.0
            errs_b = errs_a
            repaired = False
            avoided_bad_exec = not schema_ok_a

            if not schema_ok_a:
                conv = list(messages)
                if resp_a.tool_calls or resp_a.text:
                    conv.append(mg.ChatMessage(role=mg.CHAT_ROLE_ASSISTANT, text=resp_a.text, tool_calls=list(resp_a.tool_calls)))
                first_bad_tool = resp_a.tool_calls[0].name if resp_a.tool_calls else sorted(schemas)[0]
                conv.append(mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=format_schema_correction(first_bad_tool, errs_a)))

                for turn in range(1, MAX_REPAIR_TURNS + 1):
                    async with cost_lock:
                        if cumulative_cost >= MAX_BUDGET_USD:
                            stopped_for_budget = True
                            break
                    extra_turns += 1
                    resp_r, cost_r, lat_r, err_r = await _call_gateway(
                        gw,
                        profile=profile,
                        tier=tier,
                        task_id=f"tool-ablation:{profile}:{item['id']}:r{turn}",
                        messages=list(conv),
                        tool_specs=tool_specs,
                    )
                    async with cost_lock:
                        cumulative_cost += cost_r
                        cost_b_extra += cost_r
                        total_cost_b += cost_r
                    lat_b_extra += lat_r
                    if resp_r is None:
                        break
                    schema_ok_r, errs_r, ast_ok_r, _ = _inspect_calls(
                        list(resp_r.tool_calls), schemas, functions, category, item["answer"]
                    )
                    schema_ok_b, ast_ok_b, errs_b = schema_ok_r, ast_ok_r, errs_r
                    if schema_ok_r:
                        repaired = True
                        break
                    if resp_r.tool_calls or resp_r.text:
                        conv.append(mg.ChatMessage(role=mg.CHAT_ROLE_ASSISTANT, text=resp_r.text, tool_calls=list(resp_r.tool_calls)))
                    bad_tool = resp_r.tool_calls[0].name if resp_r.tool_calls else sorted(schemas)[0]
                    conv.append(mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=format_schema_correction(bad_tool, errs_r)))

            return {
                "model": profile,
                "item_id": item["id"],
                "category": category,
                "schema_valid_a": schema_ok_a,
                "ast_valid_a": ast_ok_a,
                "errors_a": errs_a,
                "schema_valid_b": schema_ok_b,
                "ast_valid_b": ast_ok_b,
                "errors_b": errs_b,
                "repaired": repaired,
                "avoided_bad_execution": avoided_bad_exec,
                "extra_turns": extra_turns,
                "cost_a_usd": round(cost_a, 6),
                "cost_b_usd": round(cost_a + cost_b_extra, 6),
                "latency_a_ms": round(lat_a, 2),
                "latency_b_ms": round(lat_a + lat_b_extra, 2),
            }

    try:
        coros = [_run_pair(profile, tier, item) for profile, tier in MODELS for item in items]
        records = [r for r in await asyncio.gather(*coros) if r is not None]
    finally:
        await gw.aclose()

    n = len(records)
    per_model: dict[str, Any] = {}
    for profile, _ in MODELS:
        sub = [r for r in records if r["model"] == profile]
        m = len(sub)
        if m == 0:
            continue
        per_model[profile] = {
            "trials": m,
            "schema_valid_rate_a": round(sum(1 for r in sub if r["schema_valid_a"]) / m, 4),
            "schema_valid_rate_b": round(sum(1 for r in sub if r["schema_valid_b"]) / m, 4),
            "ast_pass_rate_a": round(sum(1 for r in sub if r["ast_valid_a"]) / m, 4),
            "ast_pass_rate_b": round(sum(1 for r in sub if r["ast_valid_b"]) / m, 4),
            "repaired_calls": sum(1 for r in sub if r["repaired"]),
            "avoided_bad_executions": sum(1 for r in sub if r["avoided_bad_execution"]),
            "extra_turns": sum(r["extra_turns"] for r in sub),
            "cost_a_usd": round(sum(r["cost_a_usd"] for r in sub), 6),
            "cost_b_usd": round(sum(r["cost_b_usd"] for r in sub), 6),
            "latency_p50_a_ms": _percentile([r["latency_a_ms"] for r in sub], 50),
            "latency_p95_a_ms": _percentile([r["latency_a_ms"] for r in sub], 95),
            "latency_p50_b_ms": _percentile([r["latency_b_ms"] for r in sub], 50),
            "latency_p95_b_ms": _percentile([r["latency_b_ms"] for r in sub], 95),
        }

    lat_a_all = [r["latency_a_ms"] for r in records]
    lat_b_all = [r["latency_b_ms"] for r in records]
    summary = {
        "mode": "live_gateway_paired",
        "generated_at": datetime.now(UTC).isoformat(),
        "gateway_address": GATEWAY_ADDR,
        "budget_cap_usd": MAX_BUDGET_USD,
        "stopped_for_budget": stopped_for_budget,
        "trials": n,
        "models": [m[0] for m in MODELS],
        "schema_valid_rate_arm_a": round(sum(1 for r in records if r["schema_valid_a"]) / n, 4) if n else 0.0,
        "schema_valid_rate_arm_b": round(sum(1 for r in records if r["schema_valid_b"]) / n, 4) if n else 0.0,
        "ast_pass_rate_arm_a": round(sum(1 for r in records if r["ast_valid_a"]) / n, 4) if n else 0.0,
        "ast_pass_rate_arm_b": round(sum(1 for r in records if r["ast_valid_b"]) / n, 4) if n else 0.0,
        "repaired_calls": sum(1 for r in records if r["repaired"]),
        "avoided_bad_executions": sum(1 for r in records if r["avoided_bad_execution"]),
        "extra_turns": sum(r["extra_turns"] for r in records),
        "cost_usd_arm_a": round(total_cost_a, 6),
        "cost_usd_arm_b": round(total_cost_b, 6),
        "total_spend_usd": round(cumulative_cost, 6),
        "latency_p50_arm_a_ms": _percentile(lat_a_all, 50),
        "latency_p95_arm_a_ms": _percentile(lat_a_all, 95),
        "latency_p50_arm_b_ms": _percentile(lat_b_all, 50),
        "latency_p95_arm_b_ms": _percentile(lat_b_all, 95),
        "per_model": per_model,
        "schema_fuzz_stress": fuzz,
        "items": records,
    }

    out_dir = ROOT / "docs/plan/evidence/tool-schema-ablation-20261127"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ablation-results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    md_lines = [
        "# Tool-Schema Pre-Execution Validation Paired Ablation (2026-11-27)",
        "",
        f"- **Mode**: `{summary['mode']}` (live `ar-agent-runtime-model-gateway` via SSH tunnel)",
        f"- **Paired trials**: {n} (`20` balanced BFCL items × `2` models: `qwen3.8-flash` & `gemini-3.8-flash`)",
        f"- **Total live spend**: `${summary['total_spend_usd']:.4f}` (cap `${MAX_BUDGET_USD:.2f}`)",
        "",
        "| Metric | Arm A (Baseline) | Arm B (+Schema Guard & Repair) | Delta |",
        "| --- | ---: | ---: | ---: |",
        f"| Schema Valid Rate | {summary['schema_valid_rate_arm_a']:.1%} | {summary['schema_valid_rate_arm_b']:.1%} | {summary['schema_valid_rate_arm_b'] - summary['schema_valid_rate_arm_a']:+.1%} |",
        f"| BFCL AST Pass Rate | {summary['ast_pass_rate_arm_a']:.1%} | {summary['ast_pass_rate_arm_b']:.1%} | {summary['ast_pass_rate_arm_b'] - summary['ast_pass_rate_arm_a']:+.1%} |",
        f"| Avoided Bad Executions | — | {summary['avoided_bad_executions']} | +{summary['avoided_bad_executions']} |",
        f"| Repaired Calls (Live) | — | {summary['repaired_calls']} (extra turns: {summary['extra_turns']}) | +{summary['repaired_calls']} |",
        f"| Live Cost (USD) | ${summary['cost_usd_arm_a']:.4f} | ${summary['cost_usd_arm_b']:.4f} | +${summary['cost_usd_arm_b'] - summary['cost_usd_arm_a']:.4f} |",
        f"| P50 / P95 Latency (ms) | {summary['latency_p50_arm_a_ms']} / {summary['latency_p95_arm_a_ms']} | {summary['latency_p50_arm_b_ms']} / {summary['latency_p95_arm_b_ms']} | — |",
        "",
        "## Per-Model Breakdown",
        "",
        "| Model | Trials | Schema Valid A -> B | AST Pass A -> B | Avoided Bad Exec | Repaired | Cost A -> B |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for profile, stats in per_model.items():
        md_lines.append(
            f"| `{profile}` | {stats['trials']} | {stats['schema_valid_rate_a']:.1%} -> {stats['schema_valid_rate_b']:.1%} | "
            f"{stats['ast_pass_rate_a']:.1%} -> {stats['ast_pass_rate_b']:.1%} | {stats['avoided_bad_executions']} | "
            f"{stats['repaired_calls']} | ${stats['cost_a_usd']:.4f} -> ${stats['cost_b_usd']:.4f} |"
        )
    md_lines.extend([
        "",
        "## Deterministic Schema-Guard Stress Verification",
        "",
        f"- Injected malformed payloads across all 20 selected BFCL tasks: **{fuzz['caught_cases']}/{fuzz['total_cases']}** intercepted pre-execution (**{fuzz['catch_rate']:.1%}**).",
        "",
    ])
    (out_dir / "README.md").write_text("\n".join(md_lines), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "items"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
