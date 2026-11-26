#!/usr/bin/env python3
"""Flagship capability matrix Round 2 evaluation across BFCL, JudgeBench, RewardBench2, and FRAMES (WS2).

Measures empirical accuracy (with Wilson 95% CI), P50/P95 latency, and gateway cost across:
  - a6api/gemini-4-argon@2026-11
  - a6api/deepseek-v4-pro@2026-11
  - a6api/qwen3.8-max@2026-11
  - a6api/kimi-k3.1-pro@2026-11

Hard budget guard defaults to $0.95 (keeps total WS2 live spend well below $2.00).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
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
from agentruntime_learning.capability.evals import bfcl, judgebench, rewardbench2  # noqa: E402
from agentruntime_worker.executor.grpc_gateway import GrpcModelGateway  # noqa: E402

GATEWAY_ADDR = os.environ.get("AR_MODEL_GATEWAY_ADDRESS", "127.0.0.1:18091")
TENANT_ID = os.environ.get("AR_ABLATION_TENANT", "ablation-20261127")
MAX_BUDGET_USD = float(os.environ.get("AR_CAP_MATRIX_BUDGET_USD", "0.95"))
CONCURRENCY = 4

CANDIDATES: tuple[tuple[str, str], ...] = (
    ("a6api/gemini-4-argon@2026-11", "strong@medium"),
    ("a6api/deepseek-v4-pro@2026-11", "strong@medium"),
    ("a6api/qwen3.8-max@2026-11", "strong@medium"),
    ("a6api/kimi-k3.1-pro@2026-11", "strong@medium"),
)


def _read_jsonl(rel: str) -> list[dict[str, Any]]:
    path = ROOT / "services/py-learning/evalsets/public" / rel
    return [json.loads(l) for l in path.read_text("utf-8").splitlines() if l.strip()]


def _load_slice() -> dict[str, list[dict[str, Any]]]:
    bfcl_rows = _read_jsonl("bfcl.jsonl")
    by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in bfcl_rows:
        by_cat[r["extra"]["category"]].append(r)
    bfcl_slice: list[dict[str, Any]] = []
    for cat in ("simple", "multiple", "parallel", "parallel_multiple"):
        bfcl_slice.extend(by_cat[cat][:2])  # 8 items

    jb_rows = _read_jsonl("judgebench.jsonl")[:6]  # 6 items (12 comparisons each)
    rb_rows = _read_jsonl("rewardbench2.jsonl")
    rb_by_sub: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rb_rows:
        rb_by_sub[r["extra"]["subset"]].append(r)
    rb_slice: list[dict[str, Any]] = []
    for sub in sorted(rb_by_sub):
        rb_slice.extend(rb_by_sub[sub][:1])
    if len(rb_slice) < 6:
        rb_slice.extend(rb_rows[len(rb_slice) : 6])

    frames_rows = _read_jsonl("frames.jsonl")[:6]  # 6 items
    return {
        "bfcl": bfcl_slice,
        "judgebench": jb_rows,
        "rewardbench2": rb_slice[:6],
        "frames": frames_rows,
    }


def _wilson_ci(wins: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 0.0)
    p = max(0.0, min(1.0, wins / n))
    denom = 1.0 + (z * z) / n
    center = (p + (z * z) / (2.0 * n)) / denom
    half = (z * math.sqrt((p * (1.0 - p) + (z * z) / (4.0 * n)) / n)) / denom
    return (round(max(0.0, center - half), 4), round(min(1.0, center + half), 4))


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return round(ordered[idx], 2)


def _norm_jb_verdict(v: str | None) -> str | None:
    if not v:
        return None
    if v.startswith("A>"):
        return "A>B"
    if v.startswith("B>"):
        return "B>A"
    return v


def _flip_jb_verdict(v: str | None) -> str | None:
    if v == "A>B":
        return "B>A"
    if v == "B>A":
        return "A>B"
    return v


def _check_frames_answer(pred: str, gold: str) -> bool:
    p = re.sub(r"\s+", " ", (pred or "").strip().lower())
    g = re.sub(r"\s+", " ", (gold or "").strip().lower())
    if not p:
        return False
    nums_g = re.findall(r"\b\d+(?:\.\d+)?\b", g)
    if nums_g and all(num in p for num in nums_g):
        return True
    tokens_g = [t for t in re.findall(r"[a-z0-9]+", g) if len(t) >= 3]
    if not tokens_g:
        return g in p
    matched = sum(1 for t in tokens_g if t in p)
    return matched >= max(1, math.ceil(len(tokens_g) * 0.7))


class _BudgetTracker:
    def __init__(self, cap_usd: float) -> None:
        self.cap_usd = cap_usd
        self.spent_usd = 0.0
        self.lock = asyncio.Lock()

    async def can_spend(self) -> bool:
        async with self.lock:
            return self.spent_usd < self.cap_usd

    async def add(self, cost: float) -> None:
        async with self.lock:
            self.spent_usd += cost


async def _gen(
    gw: GrpcModelGateway,
    *,
    profile: str,
    tier: str,
    role: int,
    task_id: str,
    messages: list[mg.ChatMessage],
    tools: list[mg.ToolSpec] = (),
    max_output_tokens: int = 1536,
) -> tuple[mg.GenerateResponse | None, float, float, str | None]:
    t0 = time.perf_counter()
    try:
        resp = await gw.generate(
            mg.GenerateRequest(
                tenant=tenant_pb2.TenantContext(tenant_id=TENANT_ID, workspace_id="cap-round2"),
                call=mg.CallContext(
                    task_id=task_id,
                    node_id="cap_eval",
                    role=role,
                    task_family="general.coding",
                    tier=tier,
                    pinned_model_profile=profile,
                ),
                generation=mg.GenerateParams(
                    messages=list(messages),
                    tools=list(tools),
                    max_output_tokens=max_output_tokens,
                ),
            )
        )
        return resp, float(resp.usage.cost_usd_micros) / 1_000_000.0, (time.perf_counter() - t0) * 1000.0, None
    except Exception as exc:  # noqa: BLE001
        return None, 0.0, (time.perf_counter() - t0) * 1000.0, f"{type(exc).__name__}: {exc}"


async def _eval_bfcl(gw: GrpcModelGateway, budget: _BudgetTracker, profile: str, tier: str, item: dict[str, Any]) -> dict[str, Any]:
    if not await budget.can_spend():
        return {"suite": "bfcl", "model": profile, "id": item["id"], "skipped": True, "score": 0.0, "cost_usd": 0.0, "latency_ms": 0.0}
    raw_q = json.loads(item["question"])
    functions = item["extra"]["function"]
    tools = [bfcl.to_tool(f) for f in functions]
    tool_specs = [ds.tool_spec(t["name"], t["description"], t["parameters"]) for t in tools]
    msgs = [ds.chat(m["role"], m["content"]) for m in raw_q[0]]
    resp, cost, lat, err = await _gen(
        gw, profile=profile, tier=tier, role=enums_pb2.ROLE_ACT, task_id=f"cap:bfcl:{profile}:{item['id']}", messages=msgs, tools=tool_specs
    )
    await budget.add(cost)
    if resp is None:
        return {"suite": "bfcl", "model": profile, "id": item["id"], "error": err, "score": 0.0, "cost_usd": cost, "latency_ms": lat}
    specs = {bfcl.tool_name(f["name"]): (f.get("parameters") or {}).get("properties") or {} for f in functions}
    out = [{c.name: {k: bfcl.restore_ints(v, specs.get(c.name, {}).get(k)) for k, v in json_format.MessageToDict(c.arguments).items()}} for c in resp.tool_calls]
    chk = bfcl.ast_check(item["extra"]["category"], functions, out, item["answer"])
    return {"suite": "bfcl", "model": profile, "id": item["id"], "score": 1.0 if chk["valid"] else 0.0, "cost_usd": cost, "latency_ms": lat, "reason": chk.get("error_type", "")}


async def _eval_judgebench(gw: GrpcModelGateway, budget: _BudgetTracker, profile: str, tier: str, item: dict[str, Any]) -> dict[str, Any]:
    if not await budget.can_spend():
        return {"suite": "judgebench", "model": profile, "id": item["id"], "skipped": True, "score": 0.0, "cost_usd": 0.0, "latency_ms": 0.0}
    prompt = item["question"]
    ans_a = item["extra"]["response_A"]
    ans_b = item["extra"]["response_B"]
    gold = item["answer"]

    verdicts: list[str | None] = []
    cost_sum = 0.0
    lat_sum = 0.0
    errors: list[str] = []
    for order, (first, second) in (("ab", (ans_a, ans_b)), ("ba", (ans_b, ans_a))):
        if not await budget.can_spend():
            return {"suite": "judgebench", "model": profile, "id": item["id"], "skipped": True, "score": 0.0, "cost_usd": cost_sum, "latency_ms": lat_sum}
        user_txt = judgebench.USER.format(prompt=prompt, answer_a=first, answer_b=second)
        msgs = [ds.chat("system", judgebench.SYSTEM_NOTIE), ds.chat("user", user_txt)]
        resp, cost, lat, err = await _gen(
            gw, profile=profile, tier=tier, role=enums_pb2.ROLE_JUDGE, task_id=f"cap:jb:{profile}:{item['id']}:{order}", messages=msgs
        )
        await budget.add(cost)
        cost_sum += cost
        lat_sum += lat
        if err:
            errors.append(err)
            verdicts.append(None)
            continue
        raw_v, _ = judgebench.parse_verdict(resp.text or "") if hasattr(judgebench, "parse_verdict") else (None, False)
        norm = _norm_jb_verdict(raw_v)
        if order == "ba":
            norm = _flip_jb_verdict(norm)
        verdicts.append(norm)

    consistent_pass = 1.0 if (verdicts == [gold, gold]) else 0.0
    return {
        "suite": "judgebench",
        "model": profile,
        "id": item["id"],
        "score": consistent_pass,
        "verdicts": verdicts,
        "gold": gold,
        "errors": errors,
        "cost_usd": cost_sum,
        "latency_ms": lat_sum,
    }


async def _eval_rewardbench2(gw: GrpcModelGateway, budget: _BudgetTracker, profile: str, tier: str, item: dict[str, Any]) -> dict[str, Any]:
    if not await budget.can_spend():
        return {"suite": "rewardbench2", "model": profile, "id": item["id"], "skipped": True, "score": 0.0, "cost_usd": 0.0, "latency_ms": 0.0}
    chosen = item["answer"][0]
    rejected = list(item["extra"]["rejected"][:3])
    candidates = [("chosen", chosen), *[(f"rej{i}", r) for i, r in enumerate(rejected)]]
    # Deterministic permutation via item id hash
    h = int(hashlib.sha256(item["id"].encode("utf-8")).hexdigest(), 16)
    pos = h % len(candidates)
    ordered = list(candidates[1:])
    ordered.insert(pos, candidates[0])
    gold_letter = "ABCD"[pos]

    user_txt = rewardbench2.FOUR_WAY_TEMPLATE.format(
        question=item["question"],
        answer_a=ordered[0][1],
        answer_b=ordered[1][1],
        answer_c=ordered[2][1],
        answer_d=ordered[3][1],
    )
    msgs = [ds.chat("system", rewardbench2.FOUR_WAY_SYSTEM), ds.chat("user", user_txt)]
    resp, cost, lat, err = await _gen(
        gw, profile=profile, tier=tier, role=enums_pb2.ROLE_JUDGE, task_id=f"cap:rb2:{profile}:{item['id']}", messages=msgs
    )
    await budget.add(cost)
    if resp is None:
        return {"suite": "rewardbench2", "model": profile, "id": item["id"], "error": err, "score": 0.0, "cost_usd": cost, "latency_ms": lat}
    m = re.search(r"\[\[([ABCD])\]\]", resp.text or "")
    pred = m.group(1) if m else rewardbench2.parse_choice(resp.text or "") if hasattr(rewardbench2, "parse_choice") else ""
    return {
        "suite": "rewardbench2",
        "model": profile,
        "id": item["id"],
        "score": 1.0 if pred == gold_letter else 0.0,
        "pred": pred,
        "gold": gold_letter,
        "cost_usd": cost,
        "latency_ms": lat,
    }


async def _eval_frames(gw: GrpcModelGateway, budget: _BudgetTracker, profile: str, tier: str, item: dict[str, Any]) -> dict[str, Any]:
    if not await budget.can_spend():
        return {"suite": "frames", "model": profile, "id": item["id"], "skipped": True, "score": 0.0, "cost_usd": 0.0, "latency_ms": 0.0}
    msgs = [
        ds.chat("system", "Answer the multi-constraint factual reasoning question accurately and concisely in one short sentence."),
        ds.chat("user", item["question"]),
    ]
    resp, cost, lat, err = await _gen(
        gw, profile=profile, tier=tier, role=enums_pb2.ROLE_PLAN, task_id=f"cap:frames:{profile}:{item['id']}", messages=msgs, max_output_tokens=1024
    )
    await budget.add(cost)
    if resp is None:
        return {"suite": "frames", "model": profile, "id": item["id"], "error": err, "score": 0.0, "cost_usd": cost, "latency_ms": lat}
    ok = _check_frames_answer(resp.text or "", str(item["answer"]))
    return {"suite": "frames", "model": profile, "id": item["id"], "score": 1.0 if ok else 0.0, "pred": (resp.text or "")[:160], "cost_usd": cost, "latency_ms": lat}


async def main() -> None:
    slices = _load_slice()
    budget = _BudgetTracker(MAX_BUDGET_USD)
    sem = asyncio.Semaphore(CONCURRENCY)
    gw = GrpcModelGateway(GATEWAY_ADDR, timeout_s=60.0)

    async def bounded(coro: Any) -> dict[str, Any]:
        async with sem:
            return await coro

    tasks: list[Any] = []
    for profile, tier in CANDIDATES:
        for item in slices["bfcl"]:
            tasks.append(bounded(_eval_bfcl(gw, budget, profile, tier, item)))
        for item in slices["judgebench"]:
            tasks.append(bounded(_eval_judgebench(gw, budget, profile, tier, item)))
        for item in slices["rewardbench2"]:
            tasks.append(bounded(_eval_rewardbench2(gw, budget, profile, tier, item)))
        for item in slices["frames"]:
            tasks.append(bounded(_eval_frames(gw, budget, profile, tier, item)))

    try:
        results = await asyncio.gather(*tasks)
    finally:
        await gw.aclose()

    summary_by_model: dict[str, Any] = {}
    for profile, _ in CANDIDATES:
        rows = [r for r in results if r["model"] == profile and not r.get("skipped")]
        if not rows:
            continue
        by_suite: dict[str, Any] = {}
        for suite_name in ("bfcl", "judgebench", "rewardbench2", "frames"):
            sub = [r for r in rows if r["suite"] == suite_name]
            wins = sum(r["score"] for r in sub)
            lo, hi = _wilson_ci(wins, len(sub))
            by_suite[suite_name] = {
                "n": len(sub),
                "wins": wins,
                "score": round(wins / len(sub), 4) if sub else 0.0,
                "wilson_95_ci": [lo, hi],
                "cost_usd": round(sum(r["cost_usd"] for r in sub), 6),
                "latency_p50_ms": _percentile([r["latency_ms"] for r in sub], 50),
                "latency_p95_ms": _percentile([r["latency_ms"] for r in sub], 95),
            }
        # Derive per-role empirical scores replacing 0.5 priors
        act_score = by_suite["bfcl"]["score"]
        judge_rows = [r for r in rows if r["suite"] in ("judgebench", "rewardbench2")]
        judge_wins = sum(r["score"] for r in judge_rows)
        plan_score = by_suite["frames"]["score"]
        overall_wins = sum(r["score"] for r in rows)
        summary_by_model[profile] = {
            "completed_trials": len(rows),
            "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
            "cost_per_trial_usd": round(sum(r["cost_usd"] for r in rows) / len(rows), 6),
            "latency_p50_ms": _percentile([r["latency_ms"] for r in rows], 50),
            "latency_p95_ms": _percentile([r["latency_ms"] for r in rows], 95),
            "overall_score": round(overall_wins / len(rows), 4),
            "overall_wilson_95_ci": list(_wilson_ci(overall_wins, len(rows))),
            "role_scores": {
                "act": act_score,
                "judge": round(judge_wins / len(judge_rows), 4) if judge_rows else 0.0,
                "plan_reason": plan_score,
            },
            "suites": by_suite,
        }

    # Compute Pareto-undominated set on (overall_score high, cost_per_trial_usd low)
    pareto: list[str] = []
    for p, stats in summary_by_model.items():
        dominated = any(
            other != p
            and s2["overall_score"] >= stats["overall_score"]
            and s2["cost_per_trial_usd"] <= stats["cost_per_trial_usd"]
            and (s2["overall_score"] > stats["overall_score"] or s2["cost_per_trial_usd"] < stats["cost_per_trial_usd"])
            for other, s2 in summary_by_model.items()
        )
        if not dominated:
            pareto.append(p)

    payload = {
        "mode": "live_gateway_matrix_round2",
        "generated_at": datetime.now(UTC).isoformat(),
        "gateway_address": GATEWAY_ADDR,
        "budget_cap_usd": MAX_BUDGET_USD,
        "total_spend_usd": round(budget.spent_usd, 6),
        "prior_replaced": 0.5,
        "slice_counts": {k: len(v) for k, v in slices.items()},
        "pareto_undominated": pareto,
        "models": summary_by_model,
        "items": results,
    }

    out_dir = ROOT / "docs/plan/evidence/capability-matrix-round2-20261127"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "matrix-results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    md = [
        "# Flagship Capability Matrix Round 2 (2026-11-27)",
        "",
        f"- **Mode**: `{payload['mode']}` (`tenant={TENANT_ID}`, live `ar-agent-runtime-model-gateway`)",
        f"- **Paired Slice**: BFCL (`8`, `act`), JudgeBench (`6`×2 consistent, `judge`), RewardBench2 (`6`, `judge`), FRAMES (`6`, `plan_reason`)",
        f"- **Total Spend**: `${payload['total_spend_usd']:.4f}` (cap `${MAX_BUDGET_USD:.2f}`)",
        f"- **Pareto Undominated (Score ↑ vs Cost/Trial ↓)**: {', '.join(f'`{m}`' for m in pareto)}",
        "",
        "| Model | Overall (95% CI) | BFCL (`act`) | JudgeBench | RewardBench2 | FRAMES (`plan`) | Role `judge` | Cost / Trial | P50 / P95 (ms) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for profile, st in summary_by_model.items():
        ci = st["overall_wilson_95_ci"]
        su = st["suites"]
        md.append(
            f"| `{profile}` | **{st['overall_score']:.1%}** [{ci[0]:.1%}, {ci[1]:.1%}] | "
            f"{su['bfcl']['score']:.1%} | {su['judgebench']['score']:.1%} | {su['rewardbench2']['score']:.1%} | "
            f"{su['frames']['score']:.1%} | {st['role_scores']['judge']:.1%} | "
            f"${st['cost_per_trial_usd']:.5f} | {st['latency_p50_ms']} / {st['latency_p95_ms']} |"
        )
    md.append("")
    (out_dir / "README.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps({k: v for k, v in payload.items() if k != "items"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
