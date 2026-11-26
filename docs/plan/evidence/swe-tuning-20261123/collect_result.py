"""Export adjudicated case provenance, initialized environments, ledger cost and latency."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def sql(statement):
    args = ["kubectl","-n","data","exec","-i","pg-1","-c","postgres","--",
            "psql","-XqAt","-d","agentruntime","-v","ON_ERROR_STOP=1"]
    rows = subprocess.check_output(args, input=("begin read only;"+statement+";commit;").encode())
    return json.loads(rows or b"[]")


def collect(out, baseline, cohort):
    manifest = read(out / "run.json")
    protocol = read(out / "paired-protocol.json")
    report = read(out / "report.json")
    grade_file, = (out / "grading").glob("*.json")
    grade = read(grade_file)
    ids = manifest["instance_ids"]
    old = read(baseline)[cohort]["cases"]
    assert len(ids) == 50 and set(ids) == set(old) == set(grade["submitted_ids"])
    assert len(grade["completed_ids"]) == len(set(grade["completed_ids"]))
    assert set(grade["resolved_ids"]).isdisjoint(grade["unresolved_ids"])
    assert set(grade["resolved_ids"])|set(grade["unresolved_ids"]) == set(grade["completed_ids"])
    final_preds = {j["instance_id"]:j for j in map(json.loads,(out/"predictions.jsonl").read_text().splitlines())}
    details = {}
    for iid in ids:
        f = out / "records" / (iid + ".json")
        r = read(f)
        assert r.get("task_finished") is True and re.fullmatch(r"tsk_[A-Za-z0-9]+", r["task_id"])
        env = read(out/"prewarm"/(iid+".json"))
        assert env["ready"] and env["released"] and env["instance_id"]==iid and env["workspace"]==r["workspace_id"]
        assert iid in final_preds and r["patch"]==final_preds[iid]["model_patch"]
        patch = r["patch"].encode()
        old_grade = old[iid]["grade"]
        status = next((name for name in ("resolved","unresolved","empty_patch","infra_failure","error","ambiguous_failure")
                       if iid in grade.get(name+"_ids",[])), None)
        if status not in ("resolved","unresolved","empty_patch"):
            raise ValueError(f"Official verdict still pending for {iid}: {status}")
        entry = {"task_id":r["task_id"],"state":r.get("status"),"agent_verdict":r.get("final_verdict"),
                 "stop_reason":r.get("stop_reason"),"rounds":r.get("rounds"),"before":old_grade,"after":status,
                 "environment_used":r.get("environment_used"),"warmed_image":env["registry_image"],
                 "prewarm_seconds":env["elapsed_seconds"],"patch_sha256":hashlib.sha256(patch).hexdigest(),
                 "patch_bytes":len(patch),"record_sha256":sha(f)}
        test_paths = re.findall(r"^diff --git a/(.*?) b/",r["patch"],re.MULTILINE)
        entry["changed_test_files"] = [name for name in test_paths if re.search(r"(^|/)(?:tests?(?:/|_)|[^/]+_test\.py$)",name)]
        if status in ("resolved","unresolved"):
            item_file, = [file for file in (out/"grading").rglob("report.json") if file.parent.name==iid]
            item = read(item_file)[iid]
            assert bool(item["resolved"]) == (status=="resolved")
            entry["official_test_counts"] = {kind:{k:len(values) for k,values in item["tests_status"][kind].items()}
                                              for kind in ("FAIL_TO_PASS","PASS_TO_PASS")}
            entry["official_report_sha256"]=sha(item_file)
        details[iid] = entry
    tasks = [entry["task_id"] for entry in details.values()]
    assert len(set(tasks))==50
    in_list = ",".join("'"+item+"'" for item in tasks)
    where = f"where tenant_id='swebench' and task_id in ({in_list})"
    ledger = sql(f"""select coalesce(json_agg(r),'[]'::json) from
        (select task_id,model_profile_id,method,count(*) as calls,count(actual_usd_micros) as settled,
          sum(actual_usd_micros) as usd_micros from model_usage {where} group by 1,2,3 order by 1,2,3) r""")
    assert {row["model_profile_id"] for row in ledger if row["method"]!="embed"}=={protocol["model_profile"]}
    timing = sql(f"""select coalesce(json_agg(r),'[]'::json) from
        (select task_id,min(created_at) filter (where event_type='task_accepted') as start,
         max(created_at) filter (where event_type='task_finished') as finish from task_events {where} group by 1) r""")
    moments = {row["task_id"]:row for row in timing}
    for d in details.values():
        l = [row for row in ledger if row["task_id"]==d["task_id"]]
        d["settled_model_usd"]=sum(row["usd_micros"] or 0 for row in l)/1e6
        d["unsettled_calls"]=sum(row["calls"]-row["settled"] for row in l)
        t = moments.get(d["task_id"], {})
        d["accepted_to_finished_s"] = (t["finish"]-t["start"])/1000 if t.get("finish") and t.get("start") else None
    base_solved={k for k,row in old.items() if row["grade"]=="resolved"}
    solved=set(grade["resolved_ids"])
    latencies=sorted(d["accepted_to_finished_s"] for d in details.values() if d["accepted_to_finished_s"] is not None)
    return {"run_id":out.name,"baseline_cohort":cohort,"candidate_tree":protocol["runtime_tree"],"model":protocol["model_profile"],
            "tier":protocol["tier"],"selected":50,"official_verdict_counts":dict(Counter(row["after"] for row in details.values())),
            "baseline_solved":len(base_solved),"candidate_solved":len(solved),"newly_solved":[i for i in ids if i in solved-base_solved],
            "regressions":[i for i in ids if i in base_solved-solved],"official_graded":len(grade["completed_ids"]),
            "cost_reported_usd":report["cost_usd"],"cost_settled_usd":sum(row["usd_micros"] or 0 for row in ledger)/1e6,
            "ledger_unsettled":sum(row["calls"]-row["settled"] for row in ledger),
            "latency":{"scope":"task_accepted to task_finished wall time, excludes workspace preparation and grading",
                       "method":"nearest rank","n":len(latencies),"p50_s":latencies[(len(latencies)*50+99)//100-1] if latencies else None,
                       "p95_s":latencies[(len(latencies)*95+99)//100-1] if latencies else None},
            "source_sha256":{"protocol":sha(out/"paired-protocol.json"),"manifest":sha(out/"run.json"),"baseline":sha(baseline),
                             "predictions":sha(out/"predictions.jsonl"),"official_grader":sha(grade_file),"report":sha(out/"report.json")},
            "instances":details,"ledger":ledger}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out",type=Path)
    parser.add_argument("baseline",type=Path)
    parser.add_argument("cohort",choices=("mini-50","init-50"))
    args=parser.parse_args()
    print(json.dumps(collect(args.out,args.baseline,args.cohort),indent=2))
