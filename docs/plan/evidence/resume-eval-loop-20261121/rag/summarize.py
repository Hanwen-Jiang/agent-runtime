"""Aggregate measured paired RPCs and compare with the previous frozen full-rerank run."""
import argparse
import collections
import hashlib
import json
import math
from pathlib import Path
import statistics


def summarize(values):
    latencies = sorted(v["latency_ms"] for v in values)
    metrics = {
        "queries": len(values),
        "hit_count": sum(int(v["hit_at_8"]) for v in values),
        "hit_at_8": statistics.mean(v["hit_at_8"] for v in values),
        "recall_at_8": statistics.mean(v["recall_at_8"] for v in values),
        "mrr_at_8": statistics.mean(v.get("mrr_at_8", v.get("reciprocal_rank_at_8")) for v in values),
        "citation_integrity": statistics.mean(v["citation_integrity"] for v in values),
        "p50_ms": statistics.median(latencies),
        "p95_ms": latencies[math.ceil(.95 * len(values)) - 1],
    }
    if "pairs" in values[0]:
        metrics["scored_pairs"] = sum(v["pairs"] for v in values)
    return metrics


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--archive", type=Path, required=True)
    options=ap.parse_args()
    loc=options.run
    protocol=json.loads((loc/"protocol.json").read_text())
    result={r["id"]:r for r in map(json.loads,(loc/"rows.jsonl").read_text().splitlines())}
    orig=json.loads((options.archive/"comparison.json").read_text())
    assert orig["manifest_sha256"] == protocol["source_manifest_sha256"]
    frozen={r["id"]:r for r in orig["queries"]}
    assert set(result)==set(frozen) and len(result)==len((loc/"rows.jsonl").read_text().splitlines())==100
    groups=protocol["split"]
    assert not set(groups["development"]) & set(groups["validation"])
    assert len(groups["development"])==40 and len(groups["validation"])==60
    reports={}
    for phase,ids in groups.items():
        assert all(result[i]["phase"]==phase for i in ids)
        report={arm:summarize([result[i][arm] for i in ids]) for arm in ["hybrid","capped"]}
        for old in ["hybrid","reranked"]:
            report["historical_"+old]=summarize([frozen[i][old] for i in ids])
        base,cap=report["hybrid"], report["capped"]
        transitions=collections.defaultdict(list)
        for i in ids:
            before,after=(int(result[i][j]["hit_at_8"]) for j in ("hybrid","capped"))
            transitions[f"{before}_to_{after}"].append(i)
        report["paired_hit_transitions"]=transitions
        report["cap_gate_checks"]={
          "hit_gain_5pp":cap["hit_at_8"] >= base["hit_at_8"]+.05-1e-12,
          "recall_nonregression":cap["recall_at_8"] >= base["recall_at_8"]-1e-12,
          "p95_within_2s":cap["p95_ms"]<=2000,
        }
        report["cap_gate_passed"]=all(report["cap_gate_checks"].values())
        report["live_hybrid_diff_from_historical_hit_count"]=sum(result[i]["hybrid"]["hit_at_8"]!=frozen[i]["hybrid"]["hit_at_8"] for i in ids)
        reports[phase]=report
    evidence={"protocol_sha256":hashlib.sha256((loc/"protocol.json").read_bytes()).hexdigest(),
              "rows_sha256":hashlib.sha256((loc/"rows.jsonl").read_bytes()).hexdigest(),
              "comparison_archive_sha256":hashlib.sha256((options.archive/"comparison.json").read_bytes()).hexdigest(),
              "arms":reports}
    (loc/"results.json").write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n")
    for phase,data in reports.items():
        for arm in ["historical_reranked","hybrid","capped"]:
            m=data[arm]
            print(phase,arm,f"{m['hit_count']}/{m['queries']}",f"recall {m['recall_at_8']:.2%}",f"P95 {m['p95_ms']/1000:.3f}s")
        print(phase,"acceptance", data["cap_gate_checks"])


if __name__ == "__main__":
    main()
