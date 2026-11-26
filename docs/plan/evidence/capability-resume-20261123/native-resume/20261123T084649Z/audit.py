"""Separate the unchanged checkpoint from new native observations and immutable future slots."""
import collections
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/evals/deployments/capability-resume-20261123")
STAMP = "20261123T084649Z"
START = BASE / ("native-resume-" + STAMP) / "receipt.json"
OUT = Path("/opt/evals/mx")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def quota(row):
    return (bool(row.get("provider_quota", {}).get("provider_refused"))
            or (row.get("record") or {}).get("model_call_failures", {}).get("insufficient_user_quota", 0) > 0
            or "insufficient_user_quota" in (row.get("error") or ""))


def clean(row):
    return (not quota(row) and not row.get("error") and not row.get("contaminated")
            and row.get("pinned_calls", 0) > 0 and row.get("missed_calls", 0) == 0
            and row.get("calls", {}).get(row["role"] + ":" + row["candidate"], 0) > 0)


def short_code(row):
    if quota(row):
        return "insufficient_user_quota"
    if not row.get("contaminated") and not row.get("error") and clean(row):
        return "clean_pinned"
    codes = (row.get("record") or {}).get("model_call_failures") or {}
    if codes:
        known = sorted(code for code in codes if code.replace("_", "").isalnum() and len(code) <= 60)
        if known:
            return " + ".join(known)
    err = str(row.get("error") or "unverified_or_missed_pin")
    return err.split(": ", 2)[1 if err.startswith(("ConflictError: ", "DeniedError: ", "InvalidError: ", "TransientError: ")) else 0][:65]


def summary(rows):
    return {"rows":len(rows), "clean_pinned":sum(map(clean, rows)), "quota_denied":sum(map(quota, rows)),
            "recorded_cost_usd":round(sum(row.get("cost_usd") or 0 for row in rows), 6),
            "rows_with_unknown_cost":sum(row.get("cost_known") is not True for row in rows),
            "outcomes":dict(collections.Counter(short_code(row) for row in rows))}


def report():
    groups = {}
    for start in json.loads(START.read_bytes())["runs"]:
        folder = OUT / start["run_id"]
        raw = (folder / "results.jsonl").read_bytes()
        lock = (folder / "run-lock.json").read_bytes()
        complete = raw.rsplit(b"\n", 1)[0]
        lines = complete.split(b"\n") if complete else []
        n = start["starting_rows"]
        assert len(lines) >= n and digest(b"\n".join(lines[:n]) + b"\n") == start["starting_results_sha256"]
        assert digest(lock) == start["base_freeze_sha256"]
        rows = [json.loads(line) for line in lines]
        assert len(rows) == len({(r["role"],r["tier"],r["eval"],r["candidate"],r["item"],r["repeat"]) for r in rows})
        groups[folder.name] = {"retained_at_start": summary(rows[:n]), "new_native":summary(rows[n:]),
                               "native_all":summary(rows), "results_sha256":digest(raw), "run_lock_sha256":digest(lock),
                               "native_score_exists":(folder / "scores.json").exists(),
                               "provider_pause":(folder / "provider-quota-pause.json").exists()}
    return {"observed_utc":datetime.now(timezone.utc).isoformat(),"resume":STAMP,"runs":groups}


if __name__ == "__main__":
    print(json.dumps(report(),ensure_ascii=False,indent=2))
