"""Publication gates, separate from descriptive scores (MX-PUB).

Keep the observations and scores even when they cannot be published. In
particular a zero-width interval from one all-correct item is not evidence
that the documented minimum sample size was met.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

POLICY_VERSION = "capmatrix-publication-v2"
S09_CONTRACT = "s09-strict-v2"
MIN_ITEMS = 40


def evidence_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    canonical = sorted(json.dumps(dict(r), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) for r in rows)
    return hashlib.sha256("\n".join(canonical).encode()).hexdigest()


def enforce(
    cell: dict[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    expected_repeats: Mapping[str, int],
    allow_extended_samples: bool = False,
) -> None:
    """Apply fixed gates to a newly computed cell; never alter raw scores."""
    by_candidate: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_candidate.setdefault(str(row["candidate"]), []).append(row)
    blocked: dict[str, list[str]] = {}
    item_sets = {
        cid: {(str(r["eval"]), str(r["item"])) for r in mine}
        for cid, mine in by_candidate.items()
    }
    comparable = [c["candidate"] for c in cell["candidates"] if c["publishable"] and c["items"] >= MIN_ITEMS]
    mismatched = set()
    for i, cid in enumerate(comparable):
        for other in comparable[i + 1:]:
            left, right = item_sets[cid], item_sets[other]
            compatible = left <= right or right <= left if allow_extended_samples else left == right
            if not compatible:
                mismatched.update((cid, other))
    for candidate in cell["candidates"]:
        cid = candidate["candidate"]
        mine = by_candidate.get(cid, [])
        reasons = []
        if cid in mismatched:
            reasons.append("different_item_sets")
        if any(r.get("cost_known") is False for r in mine):
            reasons.append("unknown_cost")
        if any(r.get("deployment_frozen") is False for r in mine):
            reasons.append("deployment_not_frozen")
        identities = [(str(r["eval"]), str(r["item"]), int(r.get("repeat", 0))) for r in mine]
        if any(n > 1 for n in Counter(identities).values()):
            reasons.append("duplicate_observations")
        if candidate["items"] < MIN_ITEMS:
            reasons.append("minimum_40_items")
        samples: dict[tuple[str, str], set[int]] = {}
        for name, item, repeat in identities:
            samples.setdefault((name, item), set()).add(repeat)
        if any(not set(range(expected_repeats.get(name, 1))) <= repeats for (name, _), repeats in samples.items()):
            reasons.append("incomplete_repeats")
        if expected_repeats and set(expected_repeats) - {name for name, _ in samples}:
            reasons.append("missing_evaluation")
        for row in mine:
            # The old plan scores are known to measure an undocumented prompt
            # contract. Merely running the new scorer over them cannot fix that.
            if row.get("eval") == "s09" and cell["role"] == "plan":
                evidence = (row.get("record") or {}).get("scoring_contract") or {}
                if (
                    evidence.get("id") != S09_CONTRACT
                    or not re.fullmatch(r"[a-f0-9]{64}", str(evidence.get("implementation_sha256", "")))
                    or not re.fullmatch(r"[a-f0-9]{64}", str(row.get("experiment_sha256", "")))
                    or row.get("deployment_frozen") is not True
                ):
                    reasons.append("legacy_planning_evidence")
                    break
        if reasons:
            blocked[cid] = sorted(set(reasons))
            if candidate["publishable"]:
                candidate["status"] = "evidence_invalid"
                candidate["status_text"] = "发布证据不足"
            candidate["publishable"] = False
            candidate["reason"] = "; ".join(filter(None, [candidate.get("reason", ""), *blocked[cid]]))
    eligible = sum(bool(c["publishable"]) for c in cell["candidates"])
    required = (len(cell["candidates"]) + 1) // 2
    coverage_ok = eligible >= required
    if not coverage_ok:
        cell["problems"].append(f"可发布候选 {eligible}/{len(cell['candidates'])}，不足一半：整格不发布")
        for c in cell["candidates"]:
            if c["publishable"]:
                c.update(publishable=False, status="coverage_insufficient", status_text="可发布候选不足一半", reason="cell_coverage_below_half")
    cell["publication"] = {
        "policy": POLICY_VERSION,
        "evidence_sha256": evidence_digest(rows),
        "minimum_items": MIN_ITEMS,
        "expected_repeats": dict(expected_repeats),
        "allow_extended_samples": allow_extended_samples,
        "eligible_candidates": eligible,
        "required_candidates": required,
        "coverage_ok": coverage_ok,
        "blocked": blocked,
    }
