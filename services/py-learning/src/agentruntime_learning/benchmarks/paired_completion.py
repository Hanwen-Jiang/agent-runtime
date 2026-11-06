"""Prerecorded N16-R/EV-REAL rubric, safe plan evidence, and shared spending.

No model is called here. Semantic review consumes the same paid Task answers;
Temporal payloads are decoded in memory and only graph structure is retained.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agentruntime_learning.benchmarks import provenance as pv

VERSION = "paired-semantic-planning-v1"
MANDATORY_ID = "http-libraries-jev-regression"
OLD_TASK = "tsk_01M6Q70KK2A2NRVG2HYXDSN5DD"
ORIGINAL_MESSAGE = "帮我做个技术选型调研：分别独立调查 Python 的三个 HTTP 客户端库 requests、httpx、aiohttp（各自的同步/异步支持、超时与重试的写法、连接池、HTTP/2 支持、维护活跃度），三份调查互不依赖，可以并行。最后综合成一份对比结论，给出在“高并发爬虫”和“普通后端调用第三方 API”两个场景下各推荐哪个，并说明理由。直接在回答里给出结论，不用写文件。"
DATA = Path(__file__).resolve().parents[3] / "evalsets" / "paired"
IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:@#/-]{0,127}\Z")


def rubric() -> dict[str, Any]:
    return json.loads((DATA / "completion-rubric.json").read_text())


def completion_cases() -> list[dict[str, Any]]:
    cases = json.loads((DATA / "cases.json").read_text())
    cases.append({"id": MANDATORY_ID, "message": ORIGINAL_MESSAGE,
                  "should_delegate": True, "required_patterns": ["requests", "httpx", "aiohttp", "高并发", "API"], "files": []})
    return cases


def finite(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    n = float(value)
    if not math.isfinite(n) or n < 0 or (positive and n == 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return n


def spending(plan: Mapping[str, Any], history: Mapping[str, Any]) -> dict[str, Any]:
    """Soft Task targets cannot be described as an upper bound."""
    reserved = plan.get("cost_reserves", {})
    judge = finite(reserved.get("semantic_judge_usd", 0), "semantic judge reserve")
    jev = finite(reserved.get("external_jev_usd", 0), "Jev reserve")
    cap = finite(plan["budget_usd"], "shared approved cap", positive=True)
    if cap > 40:
        raise ValueError("EV-REAL/N16-R shared approved cap is $40")
    prior_external = finite(plan.get("prior_external_cost_usd", 0), "prior external cost")
    task_budget = cap - history["known_usd"] - prior_external - judge - jev
    target = 2 * len(plan["cases"]) * finite(plan["task_usd_limit"], "Task soft target", positive=True)
    return {"soft_task_target_total_usd": target, "semantic_judge_reserve_usd": judge,
            "external_jev_reserve_usd": jev, "prior_external_cost_usd": prior_external,
            "task_gateway_budget_usd": max(0, task_budget), "budget_usd": cap,
            "fits": not history["unknown_task_ids"] and target <= task_budget,
            "is_hard_upper_bound": False,
            "note": "Task cancellation is a soft target; the observed isolated Gateway cap and separately reserved judge/Jev charges enforce the shared allowance"}


def validate_completion(plan: Mapping[str, Any]) -> None:
    frozen = plan.get("completion")
    if not isinstance(frozen, dict) or frozen.get("version") != VERSION or frozen.get("rubric") != rubric():
        raise ValueError("freeze the preregistered completion rubric before running")
    if frozen.get("cases_sha256") != pv.digest(plan["cases"]) or plan["cases"] != completion_cases():
        raise ValueError("keep all ten baseline cases and the exact original HTTP/Jev regression")
    if frozen.get("original_message_sha256") != hashlib.sha256(ORIGINAL_MESSAGE.encode()).hexdigest():
        raise ValueError("original HTTP/Jev prompt changed")
    reserve = plan.get("cost_reserves", {})
    finite(reserve.get("semantic_judge_usd", -1), "semantic judge reserve", positive=True)
    finite(reserve.get("external_jev_usd", -1), "Jev reserve", positive=True)
    if not frozen.get("price_estimate") or not frozen.get("observation_receipts"):
        raise ValueError("need observed prices and both live deployment receipts")


def sanitize_stage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("nodes"), list) or not value["nodes"]:
        raise ValueError("planner result has no stage graph")
    if len(value["nodes"]) > 128:
        raise ValueError("planner graph exceeds evidence limit")
    nodes = []
    for raw in value["nodes"]:
        if not isinstance(raw, dict) or not IDENTIFIER.fullmatch(str(raw.get("id", ""))):
            raise ValueError("invalid planner node identity")
        after = raw.get("after", [])
        if not isinstance(after, list) or not all(isinstance(x, str) and IDENTIFIER.fullmatch(x) for x in after):
            raise ValueError("invalid planner dependency")
        node = {"id": raw["id"], "after": sorted(after)}
        for key in ("kind", "mode", "role", "tier"):
            if key in raw:
                if not isinstance(raw[key], str) or not IDENTIFIER.fullmatch(raw[key]):
                    raise ValueError("invalid planner structural field")
                node[key] = raw[key]
        # Do not serialize prompts, goals, tool arguments, credentials or outputs.
        nodes.append(node)
    return {"nodes": nodes}


async def plan_evidence(history: Any, tenant_id: str, *, claim_check: Any = None) -> dict[str, Any]:
    from agentruntime_core.adapters.claim_check import is_reference, tenant_from_history
    from temporalio.converter import DataConverter

    owner = tenant_from_history(history)
    if owner is None or owner.tenant_id != tenant_id:
        raise ValueError("Temporal plan evidence tenant does not match the experiment")
    reviews = {int(e.event_id) for e in history.events
               if e.HasField("activity_task_scheduled_event_attributes")
               and e.activity_task_scheduled_event_attributes.activity_type.name == "review_round"}
    judge_rows, judge_completed, judge_known = [], set(), True
    scheduled = {int(e.event_id) for e in history.events
                 if e.HasField("activity_task_scheduled_event_attributes")
                 and e.activity_task_scheduled_event_attributes.activity_type.name == "plan_round"}
    plans, errors = [], []
    for event in history.events:
        if not event.HasField("activity_task_completed_event_attributes"):
            continue
        completed = event.activity_task_completed_event_attributes
        if int(completed.scheduled_event_id) not in scheduled | reviews:
            continue
        payloads = list(completed.result.payloads)
        try:
            if any(is_reference(p) for p in payloads):
                if claim_check is None:
                    raise ValueError("claim_check_reader_required")
                payloads = await claim_check.bind(owner).decode(payloads)
            decoded = await DataConverter.default.decode(payloads)
            if len(decoded) != 1 or not isinstance(decoded[0], dict):
                raise ValueError("invalid_plan_result")
            result = decoded[0]
            if int(completed.scheduled_event_id) in reviews:
                judge_completed.add(int(completed.scheduled_event_id))
                usage = result.get("judge_usage")
                if not isinstance(usage, dict) or not usage.get("complete") or not usage.get("usage_known"):
                    judge_known = False
                if isinstance(usage, dict):
                    judge_rows.extend(usage.get("attempts", []))
                continue
            graph = sanitize_stage(result.get("stage"))
            level = result.get("triage_level")
            if level not in ("fast", "light", "full", "replan"):
                raise ValueError("invalid_triage_level")
            plans.append({"workflow_id": history.workflow_id, "event_id": int(event.event_id),
                          "scheduled_event_id": int(completed.scheduled_event_id), "triage_level": level,
                          "graph": graph, "graph_sha256": pv.digest(graph),
                          "payload_sha256": [hashlib.sha256(p.SerializeToString(deterministic=True)).hexdigest() for p in payloads]})
        except Exception as exc:  # Payload errors can contain secrets; retain only an error class.
            errors.append({"event_id": int(event.event_id), "error": type(exc).__name__,
                           "claim_check": any(is_reference(p) for p in completed.result.payloads)})
    attempted = [r for r in judge_rows if r.get("provider_attempted")]
    jev = jev_cost(attempted, len(attempted))
    jev["known"] = jev["known"] and judge_known and judge_completed == reviews
    if not jev["known"]:
        jev["cost_usd"] = None
    return {"plans": plans, "complete": bool(plans) and not errors, "errors": errors,
            "judge_usage": {"attempts": judge_rows, "cost": jev, "review_activities": len(reviews), "completed_review_activities": len(judge_completed)},
            "source": "Temporal plan_round completed payloads; structural fields only"}


def planning_score(evidence: Mapping[str, Any], *, should_delegate: bool, arm: str) -> dict[str, Any]:
    plans = evidence.get("plans", [])
    if not evidence.get("complete") or not plans:
        return {"complete": False, "passed": False, "reason": "missing_or_undecodable_planner_evidence"}
    # First real plan is fixed before looking at quality. Replans remain in evidence.
    nodes = plans[0]["graph"]["nodes"]
    ids = [n["id"] for n in nodes]
    by_id = {n["id"]: n for n in nodes}
    children = [n["id"] for n in nodes if n.get("mode") == "sub_agent"]
    dangling = sorted({d for n in nodes for d in n["after"] if d not in by_id})
    # Visit each reachable node once per source. Recursive path enumeration
    # grows exponentially on valid dense DAGs and can hang report generation.
    upstream = {}
    for source in ids:
        found: set[str] = set()
        pending = set(by_id[source].get("after", []))
        while pending:
            parent = pending.pop()
            if parent in found:
                continue
            found.add(parent)
            pending.update(set(by_id.get(parent, {}).get("after", [])) - found)
        upstream[source] = found
    cyclic = any(source in ancestors for source, ancestors in upstream.items())
    valid = len(ids) == len(set(ids)) and not dangling and not cyclic
    independent = all(not (upstream[c] & set(children)) for c in children)
    joins = [i for i in ids if i not in children and set(children) <= upstream[i]] if children else []
    expected = should_delegate and arm == "rules"
    decomposition = (len(children) == 3 and independent and bool(joins)) if expected else not children
    return {"complete": True, "valid_dag": valid, "dangling_dependencies": dangling, "cyclic": cyclic,
            "sub_agent_count": len(children), "independent_branches": independent, "synthesis_nodes": joins,
            "passed": valid and decomposition, "rubric": VERSION,
            "scope": "graph validity and decomposition; semantic planning coverage requires separate review"}


def review_export(out: Path, destination: Path) -> dict[str, Any]:
    """Produce blinded review items from existing answers, never run more Tasks."""
    from agentruntime_learning.benchmarks import swebench as sb
    plan = json.loads((out / "paired-manifest.json").read_text())
    validate_completion(plan)
    items = []
    for case in plan["cases"]:
        for arm in ("off", "rules"):
            run = out / arm / case["id"]
            metric = json.loads((run / "metrics.json").read_text())
            if not metric.get("valid") or not metric.get("planning", {}).get("complete"):
                raise ValueError("review requires collected valid Task and plan evidence for every arm")
            rec = sb.read_records(run)[case["id"]]
            body = {"input": case["message"], "actual_output": rec.get("deliverable", ""),
                    "context": [f["content"] for f in case.get("files", [])],
                    "planning": json.loads((run / "temporal-evidence.json").read_text())["planning_evidence"],
                    "tool_evidence": rec.get("tool_evidence")}
            items.append({"id": pv.digest([plan["run_id"], arm, case["id"]])[:24],
                          "evidence_sha256": pv.digest(body), **body})
    result = {"rubric": rubric(), "rubric_sha256": pv.digest(rubric()), "items": sorted(items, key=lambda x: x["id"]),
              "judge_reserve_usd": plan["cost_reserves"]["semantic_judge_usd"],
              "instructions": "Evaluate every preregistered dimension on the same existing answers. Do not infer correctness from keywords or penalize explicit uncertainty. Record model/version, raw judgments and actual cost; no additional Task submission."}
    with destination.open("x", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return {"items": len(items), "path": str(destination), "new_task_cost_usd": 0}


def jev_cost(checks: Sequence[Mapping[str, Any]], expected_calls: int) -> dict[str, Any]:
    """Every actual check counts, even if its veto was ignored. No token guess."""
    unique = {}
    for row in checks:
        key = (row.get("task_id"), row.get("attempt_id"))
        if not all(key) or key in unique:
            raise ValueError("Jev check needs a unique Task/attempt identity")
        unique[key] = row
    known = len(unique) == expected_calls and all(r.get("usage_known") is True and isinstance(r.get("input_tokens"), int)
                    and not isinstance(r["input_tokens"], bool) and r["input_tokens"] >= 0 for r in unique.values())
    tokens = sum(r["input_tokens"] for r in unique.values()) if known else None
    return {"known": known, "calls": len(unique), "expected_calls": expected_calls, "input_tokens": tokens,
            "cost_usd": tokens * .042 / 1e6 if tokens is not None else None,
            "price_usd_per_million_input_tokens": .042, "output_cost_usd": 0,
            "price_source": "https://help.typesafe.ai/articles/5187202169-how-much-does-jev-cost"}


def scope_cost(before: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]], task_ids: set[str]) -> dict[str, Any]:
    """Include all isolated-tenant attempts, including Memory embedding calls."""
    original = {r["reservation_id"]: r for r in before}
    current = {r["reservation_id"]: r for r in after}
    if len(original) != len(before) or len(current) != len(after):
        raise ValueError("duplicate scope-ledger reservation")
    if not original.keys() <= current.keys() or any(current[k] != v for k, v in original.items()):
        raise ValueError("baseline ledger was unsettled or changed; reconcile before evaluation")
    delta = [r for key, r in current.items() if key not in original]
    known = all(isinstance(r.get("actual_usd_micros"), int) and r["actual_usd_micros"] >= 0 for r in delta)
    return {"known": known, "attempts": len(delta),
            "total_gateway_usd": sum(r["actual_usd_micros"] for r in delta) / 1e6 if known else None,
            "task_gateway_usd": sum(r["actual_usd_micros"] for r in delta if r.get("task_id") in task_ids) / 1e6 if known else None,
            "additional_gateway_usd": sum(r["actual_usd_micros"] for r in delta if r.get("task_id") not in task_ids) / 1e6 if known else None,
            "source": "isolated tenant model_usage before/after, includes Memory embeddings and non-Task attempts"}


def semantic_review(export: Mapping[str, Any], review: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a complete frozen rubric review; never accept missing hard cases."""
    if review.get("rubric_sha256") != export["rubric_sha256"] or not review.get("reviewer_version"):
        raise ValueError("review must identify the frozen rubric and reviewer version")
    dims = {d["name"] for d in export["rubric"]["semantic_dimensions"]}
    scores = {r["id"]: r for r in review["items"]}
    if len(scores) != len(review["items"]) or set(scores) != {r["id"] for r in export["items"]}:
        raise ValueError("semantic review must include every arm/case exactly once")
    result = {}
    for item in export["items"]:
        row = scores[item["id"]]
        if row.get("evidence_sha256") != item["evidence_sha256"] or set(row.get("scores", {})) != dims:
            raise ValueError("semantic review evidence or dimensions changed")
        for dim in dims:
            score = row["scores"][dim]
            if score.get("score") not in (0, .5, 1) or not isinstance(score.get("reason"), str) or not score["reason"].strip():
                raise ValueError("each semantic dimension requires a preregistered score anchor and reason")
        result[item["id"]] = sum(float(row["scores"][d]["score"]) for d in dims) / len(dims)
    return result


def completed_report(out: Path, export: Mapping[str, Any], review: Mapping[str, Any], scopes: Mapping[str, Any]) -> dict[str, Any]:
    """Combine semantic judgments and authoritative full-scope costs without reruns."""
    plan = json.loads((out / "paired-manifest.json").read_text())
    validate_completion(plan)
    semantic = semantic_review(export, review)
    cost, quality, planning, regression = {}, {}, {}, {}
    for arm in ("off", "rules"):
        metrics, checks, expected = [], [], 0
        known = True
        for case in plan["cases"]:
            directory = out / arm / case["id"]
            metric = json.loads((directory / "metrics.json").read_text())
            if not metric.get("valid"):
                raise ValueError("an invalid or missing pair cannot enter the completion report")
            metrics.append(metric)
            evidence = json.loads((directory / "temporal-evidence.json").read_text()).get("planning_evidence") or {}
            usage = evidence.get("judge_usage") or {}
            if not usage.get("cost", {}).get("known"):
                known = False
            attempted = [r for r in usage.get("attempts", []) if r.get("provider_attempted")]
            checks.extend(attempted)
            expected += usage.get("cost", {}).get("expected_calls", 0)
            if case["id"] == MANDATORY_ID:
                regression[arm] = {k: metric[k] for k in ("task_id", "jev_vetoes", "jev_veto_rounds", "extra_rounds", "elapsed_s", "ledger")}
        gateway = scope_cost(scopes[arm]["before"], scopes[arm]["after"], {m["task_id"] for m in metrics})
        if gateway["known"] and abs(gateway["task_gateway_usd"] - sum(m["ledger"]["total_cost_usd"] for m in metrics)) > 1e-6:
            raise ValueError("scope ledger does not include every collected Task charge")
        jev = jev_cost(checks, expected)
        jev["known"] &= known
        if not jev["known"]:
            jev["cost_usd"] = None
        total = gateway["total_gateway_usd"] + jev["cost_usd"] if gateway["known"] and jev["known"] else None
        cost[arm] = {"gateway": gateway, "jev": jev, "total_usd": total}
        quality[arm] = sum(semantic[pv.digest([plan["run_id"], arm, c["id"]])[:24]] for c in plan["cases"]) / len(plan["cases"])
        planning[arm] = sum(bool(m.get("planning", {}).get("passed")) for m in metrics) / len(metrics)
    judge = finite(review.get("actual_cost_usd", -1), "semantic judge actual cost")
    if not review.get("cost_evidence"):
        raise ValueError("semantic judging requires cost evidence, including explicit manual zero-cost review")
    settled = all(c["total_usd"] is not None for c in cost.values())
    ratio = cost["rules"]["total_usd"] / cost["off"]["total_usd"] if settled and cost["off"]["total_usd"] else None
    quality_gain = quality["rules"] - quality["off"]
    prior = pv.historical_cost([Path(p) for p in plan.get("historical_runs", [])])
    total = sum(c["total_usd"] for c in cost.values()) + judge + prior["known_usd"] + plan.get("prior_external_cost_usd", 0) if settled and not prior["unknown_task_ids"] else None
    gate = rubric()["acceptance"]
    valid = (total is not None and total <= plan["budget_usd"] and judge <= plan["cost_reserves"]["semantic_judge_usd"]
             and sum(c["jev"]["cost_usd"] for c in cost.values() if c["jev"]["known"]) <= plan["cost_reserves"]["external_jev_usd"])
    return {"evidence_complete": valid, "actual_shared_total_usd": total, "semantic_judge_usd": judge, "arm_costs": cost,
            "quality": quality, "quality_gain": quality_gain, "total_cost_ratio": ratio, "planning_structure_pass_rate": planning,
            "n16_r": {"passed": valid and ratio is not None and ratio <= gate["maximum_total_cost_ratio"] and quality_gain >= gate["minimum_semantic_quality_gain"]},
            "ev_real": {"passed": valid and all(v >= gate["planning_structure_pass_rate"] for v in planning.values()),
                        "semantic_planning_review_included": True},
            "original_jev_regression": regression,
            "limitations": ["No inference that a one-veto cap fixes the first false veto; inspect the mandatory pair", "Repeating public tool targets remains a proxy, not full tool-argument equality"]}
