"""Compose only the explicitly frozen member runs of the 2026-11-23 job-role panel."""
import hashlib
import json
from pathlib import Path

PANEL_ID = "capability-panel-20261123"
MEMBERS = {
    "capability-resume-20261123-core": {"plan", "route", "act"},
    "capability-resume-20261123-transform": {"transform"},
    "capability-resume-20261123-judge-v2": {"judge"},
}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def coverage(columns, models):
    planned = [(model, col) for col in columns for model in col.get("planned_models", [])]
    cells = {(row["id"], key): value for row in models for key, value in row["cells"].items()}
    selected = [cells.get((model, col["id"]), {}) for model, col in planned]
    return {"planned": len(selected), "observed": sum(bool(v.get("runs")) for v in selected),
            "numeric": sum(v.get("score") is not None for v in selected),
            "eligible": sum(v.get("publishable") is True for v in selected)}


def panel(runs, locked):
    if not all(name in runs and name in locked for name in MEMBERS):
        return None
    pins = json.loads(Path(__file__).with_name("capability_panel_20261123.json").read_text())
    columns, rows = [], {}
    for name, roles in MEMBERS.items():
        run, lock = runs[name], locked[name]
        pin = pins.get("members", {}).get(name)
        if (not pin or fingerprint(lock.get("deployment")) != pin["deployment_sha256"]
                or fingerprint({k:v for k,v in lock["deployment"].items() if k != "deployment_manifest_sha256"}) != pins["runtime_freeze_sha256"]
                or lock.get("profiles_sha256") != pins["profiles_sha256"]
                or fingerprint(lock.get("registry_revisions")) != pins["registry_sha256"]
                or fingerprint(lock["plan"]) != pin["plan_sha256"]
                or fingerprint(lock["evaluations"]) != pin["evaluations_sha256"]
                or {col["role"] for col in run["columns"]} != roles):
            return None
        for col in run["columns"]:
            columns.append({**col, "source_run_id": name, "source_at": run["at"] or run.get("progress_at")})
            for row in run["models"]:
                key, cid = row["id"], col["id"]
                cell = row["cells"].get(cid)
                if cell is not None and key not in col["planned_models"]:
                    return None
                if key not in col["planned_models"]:
                    continue
                out = rows.setdefault(key, {**row, "cells": {}})
                if cell is not None:
                    out["cells"][cid] = cell
    if sum(len(c["planned_models"]) for c in columns) != 27 or len(columns) != 5:
        return None
    ordered = {"plan": 0, "act": 1, "route": 2, "judge": 3, "transform": 4}
    columns.sort(key=lambda c: ordered[c["role"]])
    models = sorted(rows.values(), key=lambda r: r["id"])
    trials = [runs[n].get("trial_coverage") for n in MEMBERS]
    trial_coverage = {k:sum(t[k] for t in trials) for k in ("expected","runs","clean","quota","clean_cells")} if all(trials) else None
    reasons = {n:runs[n]["pause"]["reason"] for n in MEMBERS if runs[n].get("pause")}
    paused_members = list(reasons)
    prior = {n:runs[n]["previous_quota"] for n in MEMBERS if "previous_quota" in runs[n]}
    previous_quota = ({"sources":prior, **{k:sum(v[k] for v in prior.values()) for k in ("runs","clean","quota","retained")}}
                      if len(prior) == len(MEMBERS) else None)
    return {"trial_coverage": trial_coverage, "previous_quota":previous_quota, "member_pause_reasons":reasons, "paused_members": paused_members, "id": PANEL_ID, "label": "2026-11-23 · 5 roles", "at": max((runs[n]["at"] or runs[n].get("progress_at") or "" for n in MEMBERS), default="") or None,
            "source_runs": list(MEMBERS), "panel_kind": "composite", "evaluation_type": "model",
            "evaluation_scope": "component", "task_family": "general.unclassified", "columns": columns, "models": models,
            "coverage": coverage(columns, models), "source_sha256": fingerprint([runs[n]["source_sha256"] for n in MEMBERS])}
