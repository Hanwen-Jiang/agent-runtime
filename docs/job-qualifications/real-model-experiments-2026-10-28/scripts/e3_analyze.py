"""E1–E3 的汇总：各变体、各次运行的指标、门槛、成本，失败原因的分布，开发集上每条样本在各次运行里的对错（稳定错 vs 波动）。
用法：e3_analyze.py <实验目录> <输出 json>"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

sys.stdout.reconfigure(encoding="utf-8")
root, out = Path(sys.argv[1]), Path(sys.argv[2])

S09 = [("planner", "triage_accuracy", ">=", 0.9), ("planner", "schema_pass_rate", ">=", 1.0), ("planner", "rule_violations", "<=", 0)]
S10 = [("reviewer", "verdict_accuracy", ">=", 0.85), (None, "stuck_retry_errors", "<=", 0), ("reviewer", "schema_pass_rate", ">=", 1.0), ("rounds", "schema_pass_rate", ">=", 1.0)]


def metric(m: dict, group: str | None, key: str):
    return (m.get(group) or {}).get(key) if group else m.get(key)


def check(m: dict, rules) -> tuple[bool, list[str]]:
    gaps = []
    for group, key, op, threshold in rules:
        v = metric(m, group, key)
        if v is None:
            gaps.append(f"{key}=缺")
            continue
        if (op == ">=" and v < threshold) or (op == "<=" and v > threshold):
            gaps.append(f"{group + '.' if group else ''}{key}={v:.3g}（门槛 {op} {threshold}）")
    return not gaps, gaps


runs = []
for path in sorted(root.glob("*/task-loop--*.json")):
    d = json.loads(path.read_text("utf-8"))
    m = d["metrics"]
    tasks = [c for c in d.get("cases", []) if c["kind"] == "task"]
    s09, g09 = check(m, S09)
    s10, g10 = check(m, S10)
    keep_nodes = sum(1 for r in d.get("failure_reasons", []) if r.get("code") == "keep_nodes")
    runs.append({
        "variant": path.parent.name, "split": d["split"], "report": str(path), "created_at": d.get("created_at"),
        "planner": m.get("planner"), "reviewer": m.get("reviewer"), "rounds": m.get("rounds"), "stuck_retry_errors": m.get("stuck_retry_errors"),
        "cost": m.get("cost"), "task_success": (sum(c["passed"] for c in tasks), len(tasks)) if tasks else None,
        "s09": {"passed": s09 and keep_nodes == 0, "gaps": g09 + ([f"keep_nodes 违例 {keep_nodes}"] if keep_nodes else [])},
        "s10": {"passed": s10, "gaps": g10},
        "model_call_failures": d.get("model_call_failure_codes"),
        "failure_reasons": d.get("failure_reasons", []),
        "cases": {c["id"]: {"passed": c["passed"], "kind": c["kind"], "codes": sorted({f["code"] for f in c.get("failures", [])}), "cost": c.get("cost_usd")}
                  for c in d.get("cases", [])},
    })

summary = {"runs": [{k: v for k, v in r.items() if k not in ("cases", "failure_reasons")} for r in runs]}

# 失败原因：每个变体在开发集上的计数（E1 是三次之和，另记次数）。
reasons: dict[str, Counter] = defaultdict(Counter)
n_runs: Counter = Counter()
for r in runs:
    if r["split"] != "dev":
        continue
    n_runs[r["variant"]] += 1
    for fr in r["failure_reasons"]:
        reasons[r["variant"]][f"{fr['dimension']}/{fr['code']}: {fr['message']}"] += fr["count"]
summary["failure_reasons_dev"] = {v: {"runs": n_runs[v], "top": c.most_common(12)} for v, c in reasons.items()}

# 开发集上每条样本：在哪些运行里错了。基线干净的三次开发集运行在 e1b、e1c、e1d；e1 受供应商 unreachable 影响（22 次回退成规则），不算。
BASELINE = ("e1b", "e1c", "e1d")
matrix: dict[str, dict[str, bool]] = defaultdict(dict)
for r in runs:
    if r["split"] != "dev":
        continue
    for cid, c in r["cases"].items():
        matrix[cid][r["variant"]] = c["passed"]
e1_labels = [l for l in BASELINE if any(l in row for row in matrix.values())]
stable_fail = sorted(cid for cid, row in matrix.items() if e1_labels and all(not row.get(l, True) for l in e1_labels))
flaky = sorted(cid for cid, row in matrix.items() if e1_labels and len({row.get(l) for l in e1_labels if l in row}) > 1)
fixed_by = {cid: sorted(l for l, ok in row.items() if ok and l not in BASELINE) for cid, row in matrix.items() if cid in stable_fail}

# 基线三次的中位数与范围（同 --repeat 的汇总，门槛按中位数判）。
def spread(values):
    values = [v for v in values if v is not None]
    return {"values": values, "median": median(values) if values else None, "min": min(values, default=None), "max": max(values, default=None)}


base = [r for r in runs if r["variant"] in BASELINE and r["split"] == "dev"]
summary["baseline_dev_spread"] = {
    "runs": len(base),
    "triage_accuracy": spread([(r["planner"] or {}).get("triage_accuracy") for r in base]),
    "shape_match_rate": spread([(r["planner"] or {}).get("shape_match_rate") for r in base]),
    "reviewer_verdict_accuracy": spread([(r["reviewer"] or {}).get("verdict_accuracy") for r in base]),
    "rounds_verdict_accuracy": spread([(r["rounds"] or {}).get("verdict_accuracy") for r in base]),
    "stuck_retry_errors": spread([r["stuck_retry_errors"] for r in base]),
    "total_usd": spread([(r["cost"] or {}).get("total_usd") for r in base]),
    "task_success": [r["task_success"] for r in base],
}
summary["cases_dev"] = {"labels": sorted({l for row in matrix.values() for l in row}), "e1_stable_fail": stable_fail, "e1_flaky": flaky,
                        "stable_fail_fixed_by_other_models": fixed_by, "matrix": matrix}
summary["case_codes"] = {cid: sorted({code for r in runs if r["split"] == "dev" for code in r["cases"].get(cid, {}).get("codes", [])}) for cid in stable_fail + flaky}

ctx = []
for path in sorted(root.glob("*/context-*.json")):
    d = json.loads(path.read_text("utf-8"))
    ctx.append({"variant": path.parent.name, "split": d.get("split"), "repeat": d.get("repeat"), "gate_passed": d.get("gate_passed"), "metrics": d.get("metrics"),
                "ranges": d.get("ranges"), "report": str(path)})
summary["context"] = ctx
repeats = []
for path in sorted(root.glob("*/repeat--*.json")):
    d = json.loads(path.read_text("utf-8"))
    repeats.append({"variant": path.parent.name, "split": d.get("split"), "gate": d.get("gate"), "report": str(path)})
summary["repeats"] = repeats

out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
for r in summary["runs"]:
    p, rv, c = r["planner"] or {}, r["reviewer"] or {}, r["cost"] or {}
    print(f"{r['variant']:<16} {r['split']:<8} triage={p.get('triage_accuracy', 0):.3f} shape={p.get('shape_match_rate', 0):.2f} "
          f"verdict={rv.get('verdict_accuracy', 0):.3f} stuck={r['stuck_retry_errors']} tasks={r['task_success']} "
          f"cost=${c.get('total_usd', 0):.2f} per_case=${c.get('per_case_usd') or 0:.3f} S09={'过' if r['s09']['passed'] else '没过'} S10={'过' if r['s10']['passed'] else '没过'}")
print("E1 三次都错：", stable_fail)
print("E1 时对时错：", flaky)
