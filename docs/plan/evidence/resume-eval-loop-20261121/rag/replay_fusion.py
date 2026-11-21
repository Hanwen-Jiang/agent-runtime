"""A no-inference reference: fuse the two cached top-8 lists, fit source cap on dev only.

The latency column is historical exact + hybrid (sequential estimate); it is not a live SLO.
"""
import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from statistics import mean


def ranking(hybrid, exact, cap):
    candidates, scores = {}, Counter()
    for hits in (hybrid, exact):
        for rank, hit in enumerate(hits, 1):
            candidates.setdefault(hit["id"], hit)
            scores[hit["id"]] += 1 / (60 + rank)
    pool = sorted(candidates.values(), key=lambda hit: (-scores[hit["id"]], hit["id"]))
    taken, views = [], Counter()
    deferred = []
    for hit in pool:
        if views[hit["version_id"]] < cap:
            taken.append(hit)
            views[hit["version_id"]] += 1
        else:
            deferred.append(hit)
    return (taken + deferred)[:8]


def normalize(text):
    return "".join(c.lower() for c in text if c.isalnum())


def relevant(hit, question, files):
    if hit["version_id"] not in question["expected_files"]:
        return False
    clause = next(g for g in files[hit["version_id"]]["gold"] if g["fact"] == question["fact"])
    if normalize(clause["text"]) not in normalize(hit["text"]):
        return False
    anchor = hit["anchor"]
    for key, value in clause["anchor"].items():
        if key == "line_contains":
            if not anchor.get("start_line", 0) <= value <= anchor.get("end_line", 0):
                return False
        elif anchor.get(key) != value:
            return False
    box = anchor.get("bbox")
    return box is None or (len(box) == 4 and all(isinstance(v, (float,int)) and 0 <= v <= 1.001 for v in box)
                           and box[0] <= box[2] and box[1] <= box[3])


def evaluate(ids, questions, files, measured, cap):
    metrics = []
    for qid in ids:
        q, old = questions[qid], measured[qid]
        hits = old["hits"] if cap is None else ranking(old["hits"], old["exact_hits"], cap)
        relevant_hits = [(i,h) for i,h in enumerate(hits, 1) if relevant(h,q,files)]
        metrics.append(dict(hit=int(bool(relevant_hits)),
                            recall=len({h["version_id"] for _,h in relevant_hits}) / len(q["expected_files"]),
                            mrr=1/relevant_hits[0][0] if relevant_hits else 0,
                            estimated_ms=old["hybrid"]["latency_ms"] + (old["exact"]["latency_ms"] if cap else 0)))
    lat=sorted(m["estimated_ms"] for m in metrics)
    return dict(n=len(ids), hits=sum(m["hit"] for m in metrics), recall_at_8=mean(m["recall"] for m in metrics),
                mrr_at_8=mean(m["mrr"] for m in metrics), estimated_p95_ms=lat[math.ceil(.95*len(lat))-1])


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--archive",type=Path, required=True)
    p.add_argument("--run",type=Path, required=True)
    a=p.parse_args()
    proto=json.loads((a.run/"protocol.json").read_text())
    mb=(a.archive/"manifest.json").read_bytes()
    assert hashlib.sha256(mb).hexdigest()==proto["source_manifest_sha256"]
    m=json.loads(mb)
    questions={r["id"]:r for r in m['queries']}; files={f['id']:f for f in m['files']}
    rows={r["id"]:r for r in json.loads((a.archive/'retrieval-summary.json').read_text())['queries']}
    dev,val=proto["split"]['development'],proto["split"]['validation']
    dev_scores={k:evaluate(dev,questions,files,rows,k) for k in [1,2,8]}
    baseline=evaluate(dev,questions,files,rows,None)
    eligible=[k for k,x in dev_scores.items() if x["hits"]>=baseline["hits"] and x["estimated_p95_ms"]<=2000]
    choice=max(eligible,key=lambda k:(dev_scores[k]["recall_at_8"],dev_scores[k]["mrr_at_8"],-k)) if eligible else None
    policy=dict(k=60, candidates=[1,2,8], selected_source_cap=choice, dev=dev_scores, dev_hybrid=baseline,
                tie_break="keep hybrid hit count and sequential proxy P95<=2s, maximize macro recall then MRR then smaller cap; if no eligible cap use hybrid")
    (a.run/"fusion-policy.json").write_text(json.dumps(policy,indent=2)+'\n')
    result={"source":"archived E5-hybrid and exact BM25 top-8; no fresh inference",
            "validation":{"hybrid":evaluate(val,questions,files,rows,None),"candidate":evaluate(val,questions,files,rows,choice)}}
    (a.run/"fusion-validation.json").write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(policy))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
