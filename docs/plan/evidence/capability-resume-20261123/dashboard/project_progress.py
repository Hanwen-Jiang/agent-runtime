"""Publish aggregate evidence for three sworn-in matrix runs; no model calls or trial contents."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
import time

ROOT = Path("/opt/evals/mx")
MEMBERS = tuple("capability-resume-20261123-" + s for s in ("core", "transform", "judge-v2"))


def quota(row):
    rec = row.get("record") or {}
    counts = rec.get("model_call_failures")
    if isinstance(counts, dict) and counts.get("insufficient_user_quota", 0) > 0:
        return True
    return any(isinstance(v, str) and v.startswith("DeniedError: insufficient_user_quota:")
               for v in (row.get("error"), rec.get("error")))


def clean_receipt(row):
    return (not quota(row) and not row.get("error") and not row.get("contaminated")
            and row.get("pinned_calls",0)>0 and row.get("missed_calls",0)==0
            and row.get("calls",{}).get(row["role"]+":"+row["candidate"],0)>0)


def signature(f):
    try:
        st = f.stat()
        return [st.st_size, st.st_mtime_ns]
    except FileNotFoundError:
        return None


def progress(name):
    folder = ROOT / name
    lockfile = folder/"run-lock.json"
    ledger = folder/"results.jsonl"
    marker = folder/"provider-quota-pause.json"
    user_marker = folder/"user-pause.json"
    reconciled = folder/"resume-reconciliation.json"
    previous = folder/"provider-quota-pause.before-recovery.json"
    saved_score = folder/"scores.before-quota-recovery.json"
    scored = folder/"scores.json"
    output = folder/"public-matrix-progress.json"
    source = {f.name:signature(f) for f in (lockfile, ledger, marker, user_marker, previous, reconciled, saved_score, scored, folder/"plan.json")}
    if not source[lockfile.name] or not source[ledger.name]:
        return False
    if output.exists():
        prior = json.loads(output.read_bytes())
        if prior.get("schema") == 2 and prior.get("files") == source:
            return bool(prior.get("scored_complete"))
    raw = lockfile.read_bytes()
    lock = json.loads(raw)
    plan = json.loads((folder/"plan.json").read_bytes())
    if lock["plan"] != plan or plan["name"] != name:
        raise ValueError("Run lock and plan differ")
    valid, expected = set(), Counter()
    for c in plan["cells"]:
        fam = c.get("task_family", plan.get("task_family", "general.unclassified"))
        role,tier = c["role"],c["tier"]
        cid = f"{fam}/{role}.{tier}"
        for e in c["evals"]:
            spec = e if isinstance(e,dict) else {"eval":e}
            entry, = [v for v in lock["evaluations"] if v["cell"] == f"{fam}/{role}/{tier}" and v["spec"] == spec["eval"]]
            n,r = spec.get("n",c.get("n",plan["n"])),spec.get("r",c.get("r",plan["r"]))
            for model in c["candidates"]:
                slots={(cid, model, entry["name"], entry["version"], i,k) for i in entry["items"][:n] for k in range(r)}
                expected[cid, model]+=len(slots)
                valid.update(slots)
    counts,clean,refused,seen = Counter(),Counter(),Counter(),set()
    data = ledger.read_bytes()
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        row = json.loads(line)
        family,role,tier,model=(row[k] for k in ("task_family","role","tier","candidate"))
        cid=f"{family}/{role}.{tier}"
        key=(cid,model,row["eval"],row["version"],row["item"],row["repeat"])
        if row["plan"] != name or key not in valid or key in seen:
            raise ValueError("Trial differs from frozen source")
        seen.add(key)
        counts[cid,model]+=1
        if quota(row):
            refused[cid,model]+=1
        elif clean_receipt(row):
            clean[cid,model]+=1
    locked_sha=hashlib.sha256(raw).hexdigest()
    data_sha=hashlib.sha256(data).hexdigest()
    rows=[{"column":col,"model":model,"runs":counts[col,model],"clean":clean[col,model],"quota":refused[col,model],"expected":limit}
          for (col,model),limit in sorted(expected.items())]
    checked = datetime.now(timezone.utc).isoformat()
    doc={"schema":2,"run_id":name,"lock_sha256":locked_sha,"ledger_sha256":data_sha,"counts":rows,"checked_at":checked,"files":source,
         "scored_complete":scored.exists() and len(seen)==len(valid) and not refused and not marker.exists() and not user_marker.exists()}
    if source[marker.name]:
        pause=json.loads(marker.read_bytes())
        if (pause.get("schema")=="capability-provider-pause-v1" and pause.get("run_id")==name
                and pause.get("active") is True and pause.get("reason")=="insufficient_user_quota"
                and pause.get("results_sha256")==data_sha and pause.get("run_lock_sha256")==locked_sha
                and pause.get("observed_rows")==len(seen) and pause.get("quota_rows")==sum(refused.values())>0):
            doc["pause"]={"reason":"insufficient_user_quota","detected_at":pause["detected_at"]}
    if source[reconciled.name] and source[previous.name]:
        old = json.loads(previous.read_bytes())
        rec = json.loads(reconciled.read_bytes())
        if (rec.get("schema") != "matrix-native-quota-resume-v1" or rec.get("run_id") != name
                or rec.get("run_lock_sha256") != locked_sha or old.get("run_id") != name
                or old.get("run_lock_sha256") != locked_sha or old.get("reason") != "insufficient_user_quota"
                or old.get("results_sha256") != rec.get("source_results_sha256")
                or old["observed_rows"] != rec["retained_original_receipts"] + rec["reopened_proven_zero"]):
            raise ValueError("Resume evidence differs from previous quota snapshot")
        retained = rec["retained_original_receipts"]
        if retained < 0 or retained > len(seen) or hashlib.sha256(b"".join(data.splitlines(keepends=True)[:retained])).hexdigest() != rec["resumed_results_sha256"]:
            raise ValueError("Retained receipts differ from the resume transaction")
        previous_clean = sum(bool(clean_receipt(json.loads(line))) for line in data.splitlines(keepends=True)[:retained])
        doc["previous_quota"] = {"results_sha256":old["results_sha256"], "runs":old["observed_rows"],
                                 "clean":previous_clean, "quota":old["quota_rows"],
                                 "at":old["detected_at"], "retained":retained}
        if source[saved_score.name]:
            doc["previous_quota"]["scores_sha256"] = hashlib.sha256(saved_score.read_bytes()).hexdigest()
    if source[user_marker.name]:
        stop = json.loads(user_marker.read_bytes())
        if (stop.get("schema") != "matrix-user-stop-v1" or stop.get("reason") != "user_requested_change_of_work"
                or stop.get("state") != "user_paused" or stop.get("run_id") != name
                or stop.get("freeze_sha256") != locked_sha or stop.get("results_sha256") != data_sha
                or stop.get("current_total_rows") != len(seen) or stop.get("current_total_clean") != sum(clean.values())
                or source[marker.name]):
            raise ValueError("User pause is not bound to the current ledger")
        doc["pause"] = {"reason":"user_requested", "detected_at":stop["stopped_at_utc"]}
    if any(signature(folder/key)!=meta for key,meta in source.items()):
        return False
    with NamedTemporaryFile("w",dir=folder,delete=False) as f:
        json.dump(doc,f); tmp=Path(f.name)
    tmp.chmod(0o644);tmp.replace(output)
    return doc["scored_complete"]


if __name__ == "__main__":
    while True:
        if all([progress(name) for name in MEMBERS]):
            break
        time.sleep(30)
