"""Read generation costs and served profile/tier for the two isolated evaluator runs."""
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess


RUNS = Path("/opt/swebench/runs")
CACHE = Path("/opt/swebench/cache/swe-tuning-20261123")
PROFILE = "a6api/gpt-6.2-sol@2026-11"


def read(path):
    return json.loads(path.read_bytes())


def usage(rid, meter):
    folder = RUNS / rid
    manifest = read(folder / "run.json")
    task_ids = []
    for iid in manifest["instance_ids"]:
        rec = read(folder / "records" / (iid + ".json"))
        task = rec.get("task_id")
        if task:
            if not re.fullmatch(r"tsk_[A-Za-z0-9]+", task) or task in task_ids:
                raise ValueError("Invalid or repeated task identity")
            task_ids.append(task)
    if not task_ids:
        raise ValueError("Missing task metadata")
    ids_sql = ",".join("'" + tid + "'" for tid in task_ids)
    sql = f"""begin read only;
        select json_agg(row_to_json(u)) from (
          select task_id,model_profile_id,method,count(*) as reserved,count(actual_usd_micros) as settled,
                 sum(actual_usd_micros) as micros from model_usage
          where tenant_id='swebench' and task_id in ({ids_sql})
          group by 1,2,3 order by 1,2,3) u;
        commit;"""
    rows = json.loads(subprocess.check_output(["kubectl","-n","data","exec","-i","pg-1","-c","postgres",
                      "--","psql","-XqAt","-v","ON_ERROR_STOP=1","-d","agentruntime"], input=sql.encode()))
    paid = [r for r in rows if r["method"] != "embed"]
    events = [e for e in meter if e.get("task_id") in task_ids and e.get("status") == "ok" and e.get("method") != "embed"]
    profiles = sorted({r["model_profile_id"] for r in paid})
    tiers = sorted({e.get("tier","unknown") for e in events})
    by_task = {}
    spans = []
    for task in task_ids:
        calls = [e for e in events if e["task_id"] == task]
        timestamps = sorted(datetime.fromisoformat(e["at"].replace("Z","+00:00")) for e in calls)
        span = (timestamps[-1]-timestamps[0]).total_seconds() if len(timestamps) >= 2 else None
        if span is not None:
            spans.append(span)
        by_task[task] = {"meter_calls":len(calls), "meter_span_s":span,
                         "settled_usd":sum(r["micros"] or 0 for r in rows if r["task_id"] == task)/1e6}
    cost = sum(r["micros"] or 0 for r in rows)/1e6
    return {"run_id":rid, "tasks":len(task_ids), "tasks_with_generation_ledger":len({r["task_id"] for r in paid}),
            "tasks_with_gateway_meter":len({e["task_id"] for e in events}), "settled_usd":cost,
            "pending_reservations":sum(r["reserved"]-r["settled"] for r in rows),
            "served_profiles":profiles, "metered_tiers":tiers,
            "profile_matches":profiles==[PROFILE], "tier_matches":tiers==["strong@medium"],
            "metered_roles":dict(Counter(e.get("role","unknown") for e in events)),
            "tasks_with_two_meter_timestamps":len(spans),
            "per_task":by_task}


if __name__ == "__main__":
    events = [json.loads(line) for line in (CACHE / "meter.jsonl").read_text().splitlines()]
    print(json.dumps({batch:usage(rid,events) for batch,rid in (
        ("init-50","init50-gpt62-tuned-20261123"),("mini-50","mini50-tuned-c-gpt62-20261123"))},indent=2))
