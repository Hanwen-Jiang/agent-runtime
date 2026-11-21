"""Compare two native SWE runs on the public Mini; a missing label is never a negative grade."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
VALID = {"resolved", "unresolved", "empty_patch"}
INFRA = {"run_error", "infra_failure", "error", "ambiguous_failure"}


def unique_object(pairs):
    obj = {}
    for k, value in pairs:
        if k in obj:
            raise ValueError(f"Duplicate JSON key: {k!r}")
        obj[k] = value
    return obj


def read(path):
    return json.loads(path.read_text(), object_pairs_hook=unique_object)


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def arm(path, ids, dataset, model, tier):
    manifest = read(path / "run.json")
    selected = manifest["instance_ids"]
    require(len(selected) == len(set(selected)) == len(ids) and set(selected) == set(ids),
            f"{path}: selected cohort differs or contains duplicates")
    require(manifest.get("selection", {}).get("instances_sha256") == dataset,
            f"{path}: unbound or different Verified dataset")
    settings = manifest["settings"]
    require(settings.get("hints") is False and settings.get("environment") == "official",
            f"{path}: expected no-hints and official environment")
    run_id = manifest["run_id"]
    report_path = path / "report.json"
    report = read(report_path) if report_path.is_file() else None
    records = {}
    if report is not None:
        require(report.get("run_id") == run_id and report.get("selected") == len(ids), "report identity differs")
        for entry in report["instances"]:
            i = entry["instance_id"]
            require(i in ids and i not in records, f"unexpected/duplicate case: {i}")
            records[i] = entry
        if report.get("graded"):
            require(report.get("resolved") == sum(r.get("grading") == "resolved" for r in records.values()),
                    "official resolved total and item outcomes disagree")
    audit_path = path / "model-audit.json"
    audit = read(audit_path) if audit_path.is_file() else {}
    if audit:
        require(audit.get("run_id") == run_id, "model audit belongs to another run")
        require(isinstance(audit.get("items"), dict) and set(audit["items"]) <= set(ids), "invalid model audit items")
    conditions = {"dataset_sha256": dataset, "environment": settings["environment"], "hints": settings["hints"],
                  "limits": manifest.get("limits"), "model_profile": audit.get("model_profile"),
                  "tier": audit.get("tier"), "settings_digest": audit.get("settings_digest")}
    protocol_ok = (isinstance(conditions["limits"], dict)
                   and all(type(conditions["limits"].get(k)) in (int,float) and conditions["limits"][k] > 0
                           for k in ("budget_usd","task_usd_limit","concurrency","task_timeout_s"))
                   and audit.get("model_profile") == model and audit.get("tier") == tier
                   and isinstance(audit.get("runtime_revision"), str) and bool(audit["runtime_revision"])
                   and bool(re.fullmatch(r"[0-9a-f]{64}", str(audit.get("settings_digest","")))))
    rows = {}
    for i in ids:
        r = records.get(i, {})
        a = audit.get("items", {}).get(i, {})
        grade = r.get("grading") or "not_graded"
        state = r.get("state") or "not_reported"
        issues = []
        attempts = a.get("generation_attempts")
        calls = a.get("model_calls")
        profiles = a.get("actual_profiles")
        tiers = a.get("actual_tiers")
        if isinstance(attempts, int) and attempts > 1:
            issues.append("multiple_generation_attempts")
        if (isinstance(profiles, dict) and any(k != model and n for k,n in profiles.items())
                or isinstance(tiers, dict) and any(k != tier and n for k,n in tiers.items())):
            issues.append("routing_contaminated")
        if (a.get("audit_complete") is not True or type(attempts) is not int or attempts < 1
                or type(calls) is not int or calls < 1 or not isinstance(profiles,dict) or not isinstance(tiers,dict)
                or any(type(n) is not int or n<0 for n in [*(profiles or {}).values(),*(tiers or {}).values()])
                or sum(profiles.values()) != calls or sum(tiers.values()) != calls):
            issues.append("unverified_model_or_attempts")
        if (state == "finished" and r.get("environment_used") is not True
                and a.get("official_environment_verified") is not True
                and not (a.get("audit_complete") is True and type(a.get("audited_shell_calls")) is int
                         and a["audited_shell_calls"] == 0)):
            issues.append("official_runtime_environment_unverified")
        if state == "error" or grade in INFRA:
            issues.append("environment_or_grader_error")
        elif state != "finished" or not report or report.get("graded") is not True or grade not in VALID:
            issues.append("unrun_or_ungraded")
        rows[i] = {"state":state,"grade":grade,"issues":issues,"generation_attempts":attempts,"model_calls":calls}
    return {"run_id":run_id,"runtime_revision":audit.get("runtime_revision"),"conditions":conditions,
            "protocol_evidence_present":protocol_ok, "rows":rows,
            "report_sha256":hashlib.sha256(report_path.read_bytes()).hexdigest() if report else None,
            "audit_sha256":hashlib.sha256(audit_path.read_bytes()).hexdigest() if audit else None,
            "graded": bool(report and report.get("graded") is True),
            "reported_cost_usd":report.get("cost_usd") if report else None}


def summarize(ids, arms, comparable):
    bins=Counter()
    for i in ids:
        l,r=(a["rows"][i] for a in arms)
        if l["issues"] or r["issues"]:
            bins["not_a_valid_pair"]+=1
        else:
            bins[{(True,True):"both_resolved",(False,True):"gained",(True,False):"lost",(False,False):"neither_resolved"}[
                l["grade"]=="resolved",r["grade"]=="resolved"]]+=1
    good=comparable and bins["not_a_valid_pair"]==0
    per_arm=[]
    for a in arms:
        won=sum(a["rows"][i]["grade"]=="resolved" for i in ids) if a["graded"] else None
        per_arm.append({"run_id":a["run_id"],"selected":len(ids),"observed_official_resolved":won,
                        "observed_resolved_per_selected":won/len(ids) if won is not None else None,
                        "outcomes":dict(Counter(a["rows"][i]["grade"] for i in ids)),
                        "issue_flags_nonexclusive":dict(Counter(issue for i in ids for issue in a["rows"][i]["issues"])),
                        "audited_first_attempt_rate":won/len(ids) if good else None})
    return {"selected":len(ids),"paired_outcomes":dict(bins),"formal":good,
            "delta_resolved_count":bins["gained"]-bins["lost"] if good else None,
            "delta_percentage_points":round(100*(bins["gained"]-bins["lost"])/len(ids),4) if good else None,
            "valid_first_attempt_pairs":len(ids)-bins["not_a_valid_pair"],"arms":per_arm}


def compare(base,new,expected_model,expected_tier):
    c=read(HERE/'cohort.json')
    ids=read(HERE/'upstream-mini50-ids.json')
    require(len(set(ids))==len(ids)==50 and set(ids)=={i['instance_id'] for i in c['instance_image_cache']},'invalid frozen cohort')
    b,n=(arm(p,ids,c["source_file_sha256"],expected_model,expected_tier) for p in (base,new))
    require(b['run_id']!=n['run_id'],'expected different runs')
    common=(b["conditions"]==n["conditions"] and all(a["protocol_evidence_present"] for a in (b,n))
            and b["runtime_revision"]!=n["runtime_revision"])
    groups={'public_mini_50':ids,'not_in_old50':c['relative_new44'],'overlap_old50':c['old50_overlap'],
            'overlap_sphinx_smoke':c['smoke_overlap']}
    require(len(groups['not_in_old50'])==44 and len(groups['overlap_old50'])==6 and
            set(groups['not_in_old50']).isdisjoint(groups['overlap_old50']) and
            set(groups['not_in_old50']+groups['overlap_old50'])==set(ids),'invalid partition')
    return {'protocol':'public-mini-50-v1','protocols_match':common,
            'groups':{k:summarize(v,(b,n),common) for k,v in groups.items()},
            'baseline':b,'candidate':n}


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('baseline',type=Path);p.add_argument('candidate',type=Path)
    p.add_argument('--expected-model',required=True);p.add_argument('--expected-tier',required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    try:
        result=compare(args.baseline,args.candidate,args.expected_model,args.expected_tier)
    except (ValueError,KeyError,TypeError,OSError) as e:
        p.exit(2,f"Input not comparable: {e}\n")
    args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(result['groups']['public_mini_50'],ensure_ascii=False))
    raise SystemExit(0 if result['groups']['public_mini_50']['formal'] else 3)
