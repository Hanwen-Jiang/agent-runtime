"""Freeze the original t18 rollout's ordered cohort and summarize its public grading."""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path

def read(path):
    return json.loads(path.read_bytes())

def write(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n")

def main():
    p=argparse.ArgumentParser()
    p.add_argument("base",type=Path)
    p.add_argument("out",type=Path)
    a=p.parse_args()
    run=read(a.base/"run.json")
    report=read(a.base/"report.json")
    grader,=(a.base/"grading").glob("*.json")
    official=read(grader)
    ids=run["instance_ids"]
    assert len(ids)==len(set(ids))==50
    assert [row["instance_id"] for row in report["instances"]]==ids
    assert set(official["resolved_ids"])=={r["instance_id"] for r in report["instances"] if r["grading"]=="resolved"}
    a.out.mkdir(parents=True, exist_ok=True)
    ordered="".join(i+"\n" for i in ids)
    (a.out/"init-50.ids").write_text(ordered)
    dataset_digest=run["selection"]["instances_sha256"]
    write(a.out/"selection.json",{"dataset_name":"SWE-bench/SWE-bench_Verified","dataset_split":"test",
          "population_count":500,"instances_sha256":dataset_digest,"cohort_file_sha256":sha256(ordered.encode()).hexdigest(),
          "seed":0,"limit":50,"stratified":True,"exclude_repos":["psf/requests"],"source_run":"t18-rev19",
          "method_description":"Exact ordered instance_ids frozen in historical t18-rev19; stratified by repository, seed=0 after excluding psf/requests."})
    rows=[{k:r.get(k) for k in ("instance_id","repo","status","grading","rounds","stop_reason","cost_usd")} for r in report["instances"]]
    write(a.out/"baseline.json",{"run_id":run["run_id"],"settings":run["settings"],"limits":run["limits"],
          "source_sha256":{p.name:sha256(p.read_bytes()).hexdigest() for p in [a.base/"run.json",a.base/"report.json",grader]},
          "selected":len(ids),"resolved":len(official["resolved_ids"]),"unresolved":len(official["unresolved_ids"]),
          "cost_usd":report["cost_usd"],"status_counts":dict(Counter(r["grading"] for r in rows)),"instances":rows})
if __name__=="__main__":
    main()
