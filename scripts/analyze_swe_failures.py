#!/usr/bin/env python3
"""Deterministic 4-bucket SWE-bench failure analyzer over mini-50 A/B and tuning C (WS4)."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

BUCKETS = (
    "discovery_or_readonly_exhaustion",
    "regression_break",
    "test_entry_misidentification",
    "patch_or_format_failure",
)


def _bucket_tuning_instance(iid: str, row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    state = str(row.get("state") or "")
    verdict = str(row.get("agent_verdict") or "")
    after = str(row.get("after") or "")
    before = str(row.get("before") or "")
    rounds = int(row.get("rounds") or 0)
    patch_bytes = int(row.get("patch_bytes") or 0)
    stop_reason = row.get("stop_reason")
    changed_tests = list(row.get("changed_test_files") or [])
    counts = row.get("official_test_counts") or {}
    f2p = counts.get("FAIL_TO_PASS") or {}
    p2p = counts.get("PASS_TO_PASS") or {}
    f2p_fail = int(f2p.get("failure") or 0)
    f2p_ok = int(f2p.get("success") or 0)
    p2p_fail = int(p2p.get("failure") or 0)

    if after == "empty_patch" or patch_bytes == 0:
        bucket = "patch_or_format_failure"
        why = "produced empty patch or no valid diff"
    elif p2p_fail > 0:
        bucket = "regression_break"
        why = f"PASS_TO_PASS regression failures={p2p_fail} (F2P ok={f2p_ok}, fail={f2p_fail})"
    elif state in ("aborted", "error") or verdict in ("abort", "escalate") or stop_reason is not None or rounds >= 5:
        bucket = "discovery_or_readonly_exhaustion"
        why = f"exhausted rounds/budget (state={state}, verdict={verdict}, rounds={rounds})"
    elif state == "done" and f2p_fail > 0 and not changed_tests:
        bucket = "test_entry_misidentification"
        why = f"agent declared done with F2P failure={f2p_fail} and zero test edits"
    else:
        bucket = "discovery_or_readonly_exhaustion"
        why = f"target fix incomplete (state={state}, verdict={verdict}, f2p_fail={f2p_fail})"

    return bucket, {
        "instance_id": iid,
        "before": before,
        "after": after,
        "flipped_regression": before == "resolved" and after != "resolved",
        "bucket": bucket,
        "reason": why,
        "state": state,
        "verdict": verdict,
        "rounds": rounds,
        "patch_bytes": patch_bytes,
        "f2p": f2p,
        "p2p": p2p,
    }


def _bucket_mini50_b_instance(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    grading = str(row.get("grading") or "")
    status = str(row.get("status") or "")
    verdict = str(row.get("final_verdict") or "")
    stop_reason = row.get("stop_reason")
    patch_files = list(row.get("patch_files") or [])
    patch_error = row.get("patch_error")
    err = row.get("error")
    rounds = int(row.get("rounds") or 0)
    test_edited = any("test" in f.lower() for f in patch_files)

    if grading in ("empty_patch", "run_error") or patch_error or err or not patch_files:
        bucket = "patch_or_format_failure"
        why = f"grading={grading}, patch_error={patch_error}, err={str(err)[:80] if err else None}"
    elif status in ("aborted", "failed") or verdict in ("abort", "escalate") or stop_reason is not None or rounds >= 5:
        bucket = "discovery_or_readonly_exhaustion"
        why = f"status={status}, verdict={verdict}, rounds={rounds}, stop={stop_reason}"
    elif status == "done" and not test_edited:
        bucket = "test_entry_misidentification"
        why = f"agent finished without test-file repro/edit; patch_files={patch_files}"
    else:
        bucket = "discovery_or_readonly_exhaustion"
        why = f"completed without passing official F2P/P2P suite (status={status}, verdict={verdict}, patch_files={patch_files})"

    return bucket, {
        "instance_id": row["instance_id"],
        "repo": row.get("repo"),
        "grading": grading,
        "bucket": bucket,
        "reason": why,
        "rounds": rounds,
        "patch_files": patch_files,
    }


def main() -> None:
    tuning_init = json.loads((ROOT / "docs/plan/evidence/swe-tuning-20261123/init-50/result.json").read_text("utf-8"))
    tuning_mini = json.loads((ROOT / "docs/plan/evidence/swe-tuning-20261123/mini-50/result.json").read_text("utf-8"))
    mini_b = json.loads((ROOT / "docs/plan/evidence/swe-mini50-20261121/final/mini50-b-report.json").read_text("utf-8"))

    tuning_rows: dict[str, list[dict[str, Any]]] = {}
    tuning_counts: dict[str, Counter[str]] = {}
    for cohort_name, data in (("init-50", tuning_init), ("mini-50", tuning_mini)):
        cnt: Counter[str] = Counter()
        items: list[dict[str, Any]] = []
        for iid, row in sorted(data["instances"].items()):
            after = str(row.get("after") or "")
            if after == "resolved":
                continue
            b, info = _bucket_tuning_instance(iid, row)
            cnt[b] += 1
            items.append(info)
        tuning_counts[cohort_name] = cnt
        tuning_rows[cohort_name] = items

    b_cnt: Counter[str] = Counter()
    b_items: list[dict[str, Any]] = []
    for row in sorted(mini_b["instances"], key=lambda x: x["instance_id"]):
        if row.get("grading") == "resolved":
            continue
        b, info = _bucket_mini50_b_instance(row)
        b_cnt[b] += 1
        b_items.append(info)

    # Combined failures on Candidate C (init-50 + mini-50) excluding pure infra/empty edge if examining code reasoning
    combined_c: Counter[str] = tuning_counts["init-50"] + tuning_counts["mini-50"]
    flipped_c = [x for cohort in tuning_rows.values() for x in cohort if x["flipped_regression"]]
    flipped_buckets = Counter(x["bucket"] for x in flipped_c)
    dominant_c_bucket = max(BUCKETS, key=lambda b: (combined_c[b], b))

    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "sources": {
            "mini50_b": "docs/plan/evidence/swe-mini50-20261121/final/mini50-b-report.json",
            "tuning_init50_c": "docs/plan/evidence/swe-tuning-20261123/init-50/result.json",
            "tuning_mini50_c": "docs/plan/evidence/swe-tuning-20261123/mini-50/result.json",
        },
        "mini50_b_unsolved_total": len(b_items),
        "mini50_b_buckets": {b: b_cnt[b] for b in BUCKETS},
        "tuning_c_unsolved_init50_total": len(tuning_rows["init-50"]),
        "tuning_c_unsolved_init50_buckets": {b: tuning_counts["init-50"][b] for b in BUCKETS},
        "tuning_c_unsolved_mini50_total": len(tuning_rows["mini-50"]),
        "tuning_c_unsolved_mini50_buckets": {b: tuning_counts["mini-50"][b] for b in BUCKETS},
        "tuning_c_combined_buckets": {b: combined_c[b] for b in BUCKETS},
        "tuning_c_flipped_regressions_count": len(flipped_c),
        "tuning_c_flipped_regressions_buckets": {b: flipped_buckets[b] for b in BUCKETS},
        "dominant_c_bucket": dominant_c_bucket,
        "flipped_instances": flipped_c,
        "mini50_b_unsolved": b_items,
        "tuning_c_unsolved": tuning_rows,
    }

    out_dir = ROOT / "docs/plan/evidence/swe-round4-diagnosis-20261127"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "failure-buckets.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    diag_md = [
        "# SWE-bench Round 4 Failure Diagnosis (2026-11-27)",
        "",
        "## 1. 4-Bucket Breakdown Across Baseline B & Candidate C",
        "",
        "| Cohort | Total Unsolved | ① Discovery / Exhaustion | ② Regression Break | ③ Test Entry Misid | ④ Patch / Format / Env |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        f"| Baseline B (`mini-50`) | {summary['mini50_b_unsolved_total']} | {b_cnt['discovery_or_readonly_exhaustion']} | {b_cnt['regression_break']} | {b_cnt['test_entry_misidentification']} | {b_cnt['patch_or_format_failure']} |",
        f"| Candidate C (`init-50`) | {summary['tuning_c_unsolved_init50_total']} | {tuning_counts['init-50']['discovery_or_readonly_exhaustion']} | {tuning_counts['init-50']['regression_break']} | {tuning_counts['init-50']['test_entry_misidentification']} | {tuning_counts['init-50']['patch_or_format_failure']} |",
        f"| Candidate C (`mini-50`) | {summary['tuning_c_unsolved_mini50_total']} | {tuning_counts['mini-50']['discovery_or_readonly_exhaustion']} | {tuning_counts['mini-50']['regression_break']} | {tuning_counts['mini-50']['test_entry_misidentification']} | {tuning_counts['mini-50']['patch_or_format_failure']} |",
        f"| **Candidate C Flips (Baseline Resolved -> C Unresolved)** | **{len(flipped_c)}** | **{flipped_buckets['discovery_or_readonly_exhaustion']}** | **{flipped_buckets['regression_break']}** | **{flipped_buckets['test_entry_misidentification']}** | **{flipped_buckets['patch_or_format_failure']}** |",
        "",
        f"- **Dominant unsolved bucket in Candidate C**: `{dominant_c_bucket}` (`{combined_c[dominant_c_bucket]}/{len(tuning_rows['init-50']) + len(tuning_rows['mini-50'])}` unsolved).",
        f"- **Exact Regression Breaks (`PASS_TO_PASS > 0`)**: {combined_c['regression_break']} combined cases across Candidate C.",
        "",
        "## 2. Flipped Instances (Resolved in Prior Baseline, Unresolved in Candidate C)",
        "",
    ]
    for fx in flipped_c:
        diag_md.append(f"- `{fx['instance_id']}` -> `{fx['bucket']}`: {fx['reason']}")
    diag_md.extend([
        "",
        "## 3. Single-Variable Hypothesis for Candidate D",
        "",
        f"Because `{dominant_c_bucket}` accounts for **{combined_c[dominant_c_bucket]}/{len(tuning_rows['init-50']) + len(tuning_rows['mini-50'])}** unsolved cases and **{flipped_buckets[dominant_c_bucket]}/{len(flipped_c)}** regressions flipped from Baseline B -> Candidate C (`agent declared done` with `FAIL_TO_PASS > 0` and zero test-entry edits/verification), Candidate D introduces strictly ONE isolated mechanism variable: **`AR_TEST_ENTRY_GUARD` (`require_repo_test_runner`)**.",
        "",
        "- Default `False` preserves frozen Baseline B identically.",
        "- When enabled (`1`), `VerificationRecord.called()` distinguishes ad-hoc `python -c` / script runs from actual repository test-runner entries (`pytest`, `python -m unittest`, `runtests.py`, `tox`, `nox`, `go test`, etc.) after code edits (`wrote_code`), and Reviewer `unproven_done()` blocks premature `done` once until a repository test-runner verification executes.",
        "",
    ])
    (out_dir / "DIAGNOSIS.md").write_text("\n".join(diag_md), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("mini50_b_unsolved", "tuning_c_unsolved")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
