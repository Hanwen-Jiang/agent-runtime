"""Dynamic, public projection of capability scores; never read prompts or ledgers."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from agentruntime_learning.evals.public_cohorts import facts, sha
from agentruntime_learning.evals.public_matrix_panel import MEMBERS, coverage, fingerprint, panel

MAX_BYTES = 2 * 1024 * 1024
MAX_RUNS = 64
NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$")
ROLES = {"act": ("工具执行", "Tool use"), "plan": ("规划", "Planning"), "judge": ("判断", "Judgment"),
         "route": ("分诊", "Routing"), "transform": ("压缩与转换", "Transform"), "deliver": ("交付", "Delivery")}
TIERS = {"low": ("基础", "Low"), "medium": ("中等", "Medium"), "high": ("高", "High"), "default": ("默认", "Default")}
POLICY = "capmatrix-publication-v2"
SUPPLEMENT_MANIFESTS = {
    "capability-resume-20261123-core": "79659c292af3e42c20f4fdd61b96ed68518baa30112fec42fc7ec09d075f615b",
    "capability-resume-20261123-transform": "a4189297b6da8aba28e5831205f230d21e9f0d9e782ba6249bcc9185fba5b7ad",
    "capability-resume-20261123-judge-v2": "2740978d5ae2481a981b099215def1fc9b88a90b3bbfea4632965a6ce2a5155c",
}


def _text(value: Any, limit: int = 256) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _number(value: Any, *, ratio: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        return None
    return float(value) if not ratio or 0 <= value <= 1 else None


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 1_000_000_000 else None


def _date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value
    except ValueError:
        return None


def _column(cell: dict[str, Any]) -> dict[str, Any] | None:
    role, tier = cell.get("role"), cell.get("tier")
    family = _text(cell.get("task_family") or "general.unclassified", 128)
    if role not in ROLES or not isinstance(tier, str) or not NAME.fullmatch(tier) or not family:
        return None
    zh, en = ROLES[role]
    tz, te = TIERS.get(tier, (tier, tier))
    evals = [{"name": _text(e.get("name"), 96), "version": _text(e.get("version"), 128)}
             for e in cell.get("evals", []) if isinstance(e, dict) and _text(e.get("name"), 96)]
    return {"id": f"{family}/{role}.{tier}", "role": role, "tier": tier, "task_family": family,
            "label_zh": f"{zh} · {tz}", "label_en": f"{en} · {te}", "evals": evals[:32]}


def _measurement(candidate: dict[str, Any], cell: dict[str, Any], at: str | None) -> dict[str, Any]:
    samples = _count(candidate.get("items"))
    score = _number(candidate.get("score"), ratio=True) if samples != 0 else None
    raw = _number(candidate.get("p"), ratio=True) if samples != 0 else None
    ci = candidate.get("ci")
    interval = [_number(v, ratio=True) for v in ci] if isinstance(ci, list) and len(ci) == 2 else None
    if samples == 0 or interval and (None in interval or interval[0] > interval[1]):
        interval = None
    pub = cell.get("publication") if isinstance(cell.get("publication"), dict) else {}
    source_publishable = candidate.get("publishable") if type(candidate.get("publishable")) is bool else None
    publication_known = pub.get("policy") == POLICY
    status = _text(candidate.get("status"), 40)
    if score is None:
        status, zh, en = "not_measured", "尚无测量结果", "No measurement"
    elif status == "contaminated":
        zh, en = "部分调用未落在指定模型，保留原观测值", "Some calls used another model; original observations retained"
    elif cell.get("role") == "plan" and not publication_known:
        status, zh, en = "historical_contract", "历史规划评分，未验证当前评分契约", "Historical planning score; current scoring contract is unverified"
    elif status == "not_leading":
        zh, en = "先导测试结果", "Pilot measurement"
    elif status == "insufficient" or samples is not None and samples < 40:
        status, zh, en = "insufficient", "样本或置信区间不足，保留观测值", "Limited sample or confidence interval; observations retained"
    elif status in ("evidence_invalid", "coverage_insufficient", "inconsistent"):
        zh, en = "发布证据未通过校验，保留观测值", "Publication evidence was not validated; observations retained"
    else:
        status, zh, en = "observed", "实测分数", "Measured score"
    publishable = source_publishable if publication_known else None
    return {"score": score, "raw_score": raw, "ci95": interval, "samples": samples,
            "repeats": _count(candidate.get("repeats")), "runs": _count(candidate.get("runs")),
            "contaminated": _count(candidate.get("contaminated")), "errors": _count(candidate.get("errors")),
            "contamination_rate": _number(candidate.get("contamination_rate"), ratio=True),
            "status": status, "reason_zh": zh, "reason_en": en, "publishable": publishable,
            "publication_state": "eligible" if publishable is True else "not_eligible" if publishable is False else "unverified",
            "source_publishable": source_publishable, "method": "shrunken_score", "ci_method": "raw_score_bootstrap",
            "at": _date(cell.get("at")) or at}


# Public evaluator aliases as registered in capability/evals. Unknown plugins
# keep their version and counts but must not inherit a built-in sampling rule.
_EVAL_NAMES = {"bfcl": "bfcl-v4", "tau2": "tau2-bench", "longbench": "longbench-v2",
               "s09:route": "s09-route", **{n: n for n in ("s09", "s10", "s11", "judgebench", "rewardbench2", "simpleqa", "frames")}}
_BUCKETS = {"bfcl-v4": ("工具调用类别", "tool-call category"), "tau2-bench": ("业务领域", "domain"),
            "rewardbench2": ("子任务", "subtask")}


# Source item IDs are shareable only for known public suites with non-private selection.
_PUBLIC_SUITES = {"bfcl-v4", "tau2-bench", "longbench-v2", "judgebench", "rewardbench2", "frames", "simpleqa"}
_PUBLIC_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.: -]{0,127}")


def _private(part):
    if not isinstance(part, dict):
        return False
    return (any(part.get(k) is True for k in ("holdout_hidden", "ids_hidden", "private"))
            or any(re.search(r"hold[-_ ]?out|held[-_ ]?out|private|hidden", str(part.get(k, "")), re.I)
                   for k in ("visibility", "split", "dataset_split")))


def _items(ids):
    return (isinstance(ids, list) and 0 < len(ids) <= 4096 and
            all(isinstance(i, str) and _PUBLIC_ID.fullmatch(i) for i in ids) and len(set(ids)) == len(ids))


def _lock_groups(lock, column):
    cell = "/".join([column["task_family"], column["role"], column["tier"]])
    return {v.get("name"): v for v in lock.get("evaluations", []) if isinstance(v, dict) and v.get("cell") == cell}


def _selection(column: dict, plan: dict, cell_plan: dict, seed: int | None,
               candidate: dict | None = None, locked: dict | None = None, historical: dict | None = None) -> dict:
    specs = {}
    entries = cell_plan.get("evals")
    for entry in entries[:32] if isinstance(entries, list) else []:
        spec = {"eval": entry} if isinstance(entry, str) else entry
        if isinstance(spec, dict) and isinstance(spec.get("eval"), str) and spec["eval"] in _EVAL_NAMES:
            specs[_EVAL_NAMES[spec["eval"]]] = spec
    for key, frozen in (locked or {}).items():
        raw_spec = next((v if isinstance(v, dict) else {"eval":v} for v in (entries or [])
                         if (v.get("eval") if isinstance(v, dict) else v) == frozen.get("spec")), None)
        if raw_spec:
            specs[key] = raw_spec
    measured = {v["name"]: v["version"] for v in column["evals"] if NAME.fullmatch(v["name"])}
    cohorts = (candidate or {}).get("observed_cohorts", {})
    groups = []
    for dataset in dict.fromkeys([*measured, *specs, *(locked or {})]):
        spec = specs.get(dataset, {})
        options = spec.get("options") if isinstance(spec.get("options"), dict) else {}
        split = options.get("split", "all") if dataset in {"s09", "s09-route", "s10", "s11"} and spec else None
        if split not in ("all", "dev", "holdout"):
            split = None
        method, zh, en = "historical_unrecorded", "未记录如何从原始题库选题。", "The original selection method is not recorded."
        if dataset in _BUCKETS:
            zh_group, en_group = _BUCKETS[dataset]
            method = "seeded_round_robin"
            zh, en = (f"按{zh_group}分层，层内按种子与题号的 SHA-256 排序，各层轮流取题。",
                      f"Group by {en_group}, rank IDs within each group by a seeded SHA-256 hash, then draw round-robin.")
        elif dataset in ("longbench-v2", "simpleqa", "frames", "judgebench"):
            method = "seeded_hash_prefix"
            zh, en = "按种子与题号的 SHA-256 排序，从固定顺序开头取题。", "Rank IDs by a seeded SHA-256 hash and take a prefix."
        elif dataset in ("s09", "s09-route", "s10", "s11"):
            method = "seeded_shuffle_prefix"
            zh, en = "对该组件的合格题号排序，用种子打乱，从开头取题。", "Sort eligible component IDs, shuffle with the seed, then take a prefix."
        observed = cohorts.get(dataset) if isinstance(cohorts, dict) else None
        observed = observed if isinstance(observed, dict) else {}
        if spec.get("eval") in {"core_cohorts:plan", "core_cohorts:route"}:
            method = "seeded_shuffle_prefix"
            zh, en = "在出行规划和改写任务中优先选已确认标注，再以同一种子打乱各组补齐。", "Prioritize confirmed annotations, shuffle within each group using the same seed, then fill from drafts."
        elif spec.get("eval") == "core_cohorts:act":
            zh, en = "先依业务函数筛选 BFCL 题库，再按调用类别分组，以种子排序后轮流取题。", "Filter BFCL by business function, rank within call categories using the seed, then sample round-robin."
        elif (options.get("cache") or options.get("file")) and dataset in _PUBLIC_SUITES:
            zh, en = "在本批冻结的候选池中取题。" + zh, "Select from this run’s frozen eligible pool. " + en
        requested = _count(spec.get("n", cell_plan.get("n", plan.get("n", 40)))) if spec else None
        entry = (locked or {}).get(dataset, {})
        version = measured.get(dataset) or entry.get("version")
        ids = entry.get("items")
        # An observed candidate may include an adaptive prefix rather than the whole lock.
        # The score stores a hash of its sorted unique IDs, not the lock order.
        extra = {}
        hidden = dataset not in _PUBLIC_SUITES or any(_private(x) for x in (plan, cell_plan, spec, options))
        if hidden:
            extra["ids_hidden"] = True
        if isinstance(entry.get("version"), str) and version == entry["version"] and isinstance(ids, list):
            extra["source"] = "run-lock.json + scores.json" if measured else "run-lock.json"
            if candidate is None:
                extra["selected_count"] = len(ids)
                if not hidden and _items(ids):
                    observed = {"sha256": fingerprint(sorted(ids))}
                if not hidden and _items(ids):
                    extra["selection_ids"] = ids
            elif not hidden and _items(ids) and type(observed.get("count")) is int:
                n = observed["count"]
                if 0 < n <= len(ids) and fingerprint(sorted(ids[:n])) == observed.get("sha256"):
                    extra.update(selection_ids=ids[:n], selected_count=n)
        old = (historical or {}).get(dataset, {})
        if not hidden and old.get("version") == version and _items(old.get("ids")):
            extra.update(selection_ids=old["ids"], selected_count=len(old["ids"]),
                         source="public-cohorts.json (observed results)")
            observed = {"count": len(old["ids"]), "sha256": fingerprint(sorted(old["ids"]))}
        source = extra.pop("source", "scores.json + plan.json" if spec else "scores.json")
        groups.append(facts(dataset, method, source, zh, en, **extra,
                            role=column["role"], tier=column["tier"], split=split, seed=seed,
                            requested_count=requested, observed_count=_count(observed.get("count")),
                            version=version if version and re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", version) else None,
                            cohort_sha256=sha(observed.get("sha256"))))
    return facts("capability", "per_evaluator", "scores.json + plan.json" if cell_plan else "scores.json",
                 "不同评测分别取题；计划上限不等于每个候选实际得到的题数，重复运行不另算题。",
                 "Datasets are sampled separately. Planned limits may differ from a candidate’s observed cases; repeats are not new cases.",
                 groups=groups)


def project_scores(name: str, scores: dict[str, Any], plan: dict[str, Any], digest: str, *, lock=None, historical=None) -> dict[str, Any]:
    lock = lock or {}
    historical = historical or {}
    if not isinstance(scores.get("cells"), list) or len(scores["cells"]) > 64:
        raise ValueError("invalid capability cells")
    at = _date(scores.get("scored_at"))
    columns: dict[str, dict[str, Any]] = {}
    models: dict[str, dict[str, Any]] = {}
    # Planned but unmeasured roles/models remain present with empty cells, never zero.
    planned = plan.get("cells", [])
    if not isinstance(planned, list) or len(planned) > 64:
        raise ValueError("invalid capability plan")
    plans: dict[str, dict] = {}
    seed = scores.get("seed", plan.get("seed", 0 if planned else None))
    seed = seed if type(seed) is int and abs(seed) < 2**53 else None
    for c in planned:
        if not isinstance(c, dict):
            continue
        col = _column({**c, "task_family": c.get("task_family", plan.get("task_family")), "evals": []})
        if col:
            plans[col["id"]] = c
            col["planned_models"] = list(dict.fromkeys(_text(m) for m in c.get("candidates", [])[:256] if _text(m)))
            col["selection_info"] = _selection(col, plan, c, seed, locked=_lock_groups(lock, col))
            columns[col["id"]] = col
            for model in c.get("candidates", [])[:256]:
                model = _text(model)
                if model:
                    models.setdefault(model, {"id": model, "label": model.split("/", 1)[-1], "cells": {}})
    for cell in scores["cells"]:
        if not isinstance(cell, dict):
            raise ValueError("invalid capability cell")
        column = _column(cell)
        if column is None:
            continue
        plan_cell = plans.get(column["id"], {})
        column["planned_models"] = columns.get(column["id"], {}).get("planned_models", [])
        lock_evals = _lock_groups(lock, column)
        observed_models = historical.get(column["id"], {})
        common = {}
        for ev in column["evals"]:
            observations = [v.get(ev["name"]) for v in observed_models.values() if v.get(ev["name"])]
            if observations and all(v == observations[0] for v in observations):
                common[ev["name"]] = observations[0]
        column["selection_info"] = _selection(column, plan, plan_cell, seed, locked=lock_evals, historical=common)
        columns[column["id"]] = column
        candidates = cell.get("candidates", [])
        if not isinstance(candidates, list) or len(candidates) > 256:
            raise ValueError("invalid capability candidates")
        seen = set()
        for candidate in candidates:
            if not isinstance(candidate, dict) or not (model := _text(candidate.get("candidate"))):
                continue
            if model in seen:
                raise ValueError("duplicate model measurement")
            seen.add(model)
            row = models.setdefault(model, {"id": model, "label": model.split("/", 1)[-1], "cells": {}})
            measurement = _measurement(candidate, cell, at)
            # A model can have an early tournament stop or adaptive extension.
            # Publish per-evaluator cohorts only when present in this model's own score.
            old = historical.get(column["id"], {}).get(model, {})
            if isinstance(candidate.get("observed_cohorts"), dict) or old:
                measurement["selection_info"] = _selection(column, plan, plan_cell, seed, candidate, lock_evals, old)
            row["cells"][column["id"]] = measurement
    return {"id": name, "label": _text(scores.get("plan")) or name, "at": at,
            "plan_known": bool(planned), "coverage": coverage(columns.values(), models.values()) if planned else None,
            "evaluation_type": "model", "evaluation_scope": "component",
            "task_family": _text(plan.get("task_family")) or "general.unclassified",
            "selection_info": facts("capability", "per_evaluator", "scores.json + plan.json" if planned else "scores.json",
                                    "按角色、推理档与组件分别取题。", "Items are selected separately per role, tier and evaluator.",
                                    seed=seed, groups=[g for col in columns.values() for g in col["selection_info"]["groups"]]),
            "columns": list(columns.values()), "models": [models[k] for k in sorted(models)], "source_sha256": digest}


def apply_progress(projected, progress):
    cols = {col["id"]: col for col in projected["columns"]}
    models = {row["id"]: row for row in projected["models"]}
    planned = {(cid, mid) for cid,col in cols.items() for mid in col["planned_models"]}
    rows = progress.get("counts", [])
    seen = set()
    totals = {k:0 for k in ("expected","runs","clean","quota")}
    clean_cells = 0
    for row in rows:
        cid, mid = row["column"], row["model"]
        slot = (cid, mid)
        values = {k:_count(row.get(k)) for k in totals}
        if (slot in seen or slot not in planned or None in values.values()
                or values["clean"] + values["quota"] > values["runs"] or values["runs"] > values["expected"]):
            raise ValueError("invalid public matrix execution counts")
        seen.add(slot)
        for k in totals:
            totals[k] += values[k]
        clean_cells += int(values["clean"] > 0)
        if values["runs"] == 0:
            continue
        cell = models[mid]["cells"].setdefault(cid, {"score":None,"raw_score":None,
            "status":"awaiting_score","reason_zh":"已归档执行记录，尚无该格的评分汇总。","reason_en":"Trials recorded; this cell has no score summary."})
        cell.update(runs=values["runs"], trial_counts=values)
        if values["clean"] == 0:
            cell.update(score=None, raw_score=None, ci95=None, samples=0, publishable=False,
                        publication_state="not_eligible", status="no_valid_trials",
                        reason_zh="没有成功命中指定模型且通过有效性检查的执行记录。",reason_en="No valid trials returned from the pinned model.")
    if seen != planned:
        raise ValueError("incomplete progress projection")
    projected.update(trial_coverage={**totals,"clean_cells":clean_cells},progress_at=_date(progress.get("checked_at")))
    projected["coverage"] = coverage(cols.values(), models.values())


def metrics_runs(raw: bytes) -> list[dict[str, Any]]:
    doc = json.loads(raw)
    grouped: dict[str, dict[str, Any]] = {}
    for r in doc.get("runs", [])[:200]:
        if not isinstance(r, dict) or r.get("suite") != "capability" or not r.get("finished"):
            continue
        model = _text(r.get("model"))
        group = _text(r.get("subject"), 96) or "live"
        if not model or not isinstance(r.get("scores"), dict):
            continue
        out = grouped.setdefault(group, {"id": "metrics-" + group, "label": group, "at": None,
                                        "task_family": "general.unclassified", "columns": {}, "models": {}, "source_sha256": None})
        at = _date(r.get("updated_at"))
        if at and (out["at"] is None or at > out["at"]):
            out["at"] = at
        row = out["models"].setdefault(model, {"id": model, "label": model.split("/", 1)[-1], "cells": {}})
        for key, value in r["scores"].items():
            role, sep, tier = key.partition(".")
            col = _column({"role": role, "tier": tier}) if sep else None
            if col is None or (score := _number(value, ratio=True)) is None:
                continue
            out["columns"][col["id"]] = col
            existing = row["cells"].get(col["id"])
            if existing and (existing.get("at") or "") >= (at or ""):
                continue
            row["cells"][col["id"]] = {"score": score, "raw_score": None, "samples": None, "ci95": None,
                "contamination_rate": None, "publishable": None, "publication_state": "unverified", "source_publishable": None,
                "method": "reported_score", "status": "observed", "at": at,
                "reason_zh": "实时指标观测值，样本和区间尚未提供", "reason_en": "Live observation; sample and interval metadata are unavailable"}
    return [{**r, "columns": list(r["columns"].values()), "models": [r["models"][k] for k in sorted(r["models"])]} for r in grouped.values()]


class PublicCapabilitySource:
    def __init__(self, root: Path | None, *, live: Callable[[], bytes] | None = None, ttl_s: float = 5.0) -> None:
        self.root, self.live, self.ttl_s = Path(root) if root else None, live, ttl_s
        self._lock = threading.Lock()
        self._at = float("-inf")
        self._good: dict[str, dict[str, Any]] = {}
        self._cached: bytes | None = None

    def _read(self, relative: str) -> bytes:
        assert self.root is not None
        parts = relative.split("/")
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = child
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            with os.fdopen(file_fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                    raise ValueError("invalid capability document")
                data = stream.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ValueError("oversized capability document")
                return data
        finally:
            os.close(fd)

    def _signature(self, name, filename):
        assert self.root is not None
        base = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in ("mx", name):
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base)
                os.close(base)
                base = child
            try:
                info = os.stat(filename, dir_fd=base, follow_symlinks=False)
            except FileNotFoundError:
                return None
            return [info.st_size, info.st_mtime_ns] if stat.S_ISREG(info.st_mode) else None
        finally:
            os.close(base)

    def _optional(self, name, filename):
        try:
            return self._read(f"mx/{name}/{filename}")
        except FileNotFoundError:
            return b"{}"

    def _supplement(self, name, lock_sha, progress):
        """Use one disposition per frozen trial, including its designated recovery attempt."""
        if name not in SUPPLEMENT_MANIFESTS:
            return None
        raw = self._optional(name, "public-supplemental-coverage.json")
        record = json.loads(raw)
        if not record:
            return None
        sig = record.get("native_signature")
        current = self._signature(name, "results.jsonl")
        if (record.get("schema") != "matrix-supplement-coverage-v1" or record.get("run_id") != name
                or record.get("manifest_sha256") != SUPPLEMENT_MANIFESTS[name] or record.get("lock_sha256") != lock_sha
                or not sha(record.get("native_ledger_sha256")) or not sha(record.get("supplemental_ledger_sha256"))
                or not isinstance(sig, list) or len(sig) != 2 or any(type(n) is not int or n < 0 for n in sig)):
            raise ValueError("Invalid supplemental matrix provenance")
        if not current or any(now < old for now, old in zip(current, sig)) or (current[0] == sig[0] and current != sig):
            return None
        if (sig == progress.get("files", {}).get("results.jsonl")
                and record["native_ledger_sha256"] != progress.get("ledger_sha256")):
            raise ValueError("Supplement differs from the native ledger")
        expected = {(r["column"], r["model"]): r["expected"] for r in progress["counts"]}
        actual = {(r["column"], r["model"]): r["expected"] for r in record["counts"]}
        if expected != actual or len(actual) != len(record["counts"]):
            raise ValueError("Supplement differs from the native plan")
        chosen = record["selected_sources"]
        if not isinstance(chosen, dict) or set(chosen) - {"native", "supplemental"}:
            raise ValueError("Unknown recovery source")
        for source in ("native", "supplemental"):
            n, used = _count(record.get(source + "_receipts")), _count(chosen.get(source, 0))
            if n is None or used is None or used > n:
                raise ValueError("Invalid supplemental source counts")
        if any(_count(r.get("terminal")) is None or not 0 <= r["terminal"] <= r["runs"] for r in record["counts"]):
            raise ValueError("Invalid recovery disposition count")
        total = sum(r["runs"] for r in record["counts"])
        if (total != record.get("observed") or total != sum(chosen.values())
                or sum(actual.values()) != record.get("expected")
                or sum(r["quota"] for r in record["counts"]) != record.get("quota")
                or sum(r["terminal"] for r in record["counts"]) != record.get("terminal_dispositions")
                or sum(r["clean"] for r in record["counts"]) != record.get("clean")):
            raise ValueError("Invalid supplemental coverage")
        score_raw = b'{"cells":[]}'
        if record.get("complete") is True:
            if current != sig or total != record["expected"] or record["quota"] or record["terminal_dispositions"] != total:
                raise ValueError("Incomplete supplemental scoring input")
            score_raw = self._read(f"mx/{name}/supplemental-scores.json")
            if hashlib.sha256(score_raw).hexdigest() != record.get("scores_sha256"):
                raise ValueError("Supplemental scores differ from the scoring receipt")
        return record, score_raw, raw

    def _names(self) -> list[str]:
        assert self.root is not None
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            child = os.open("mx", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
            with os.scandir(fd) as entries:
                names = []
                for entry in entries:
                    if NAME.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
                        names.append(entry.name)
                        if len(names) > MAX_RUNS:
                            raise ValueError("too many capability runs")
            return sorted(names)
        finally:
            os.close(fd)

    def get(self) -> bytes:
        with self._lock:
            now = time.monotonic()
            if self._cached is not None and now - self._at < self.ttl_s:
                return self._cached
            errors = set()
            names: list[str] = []
            if self.root:
                try:
                    names = self._names()
                except FileNotFoundError:
                    pass
                except (OSError, ValueError):
                    errors.add("capability_artifacts_unavailable")
            verified_locks = {}
            for name in names:
                try:
                    plan_raw = self._optional(name, "plan.json")
                    plan = json.loads(plan_raw)
                    if not isinstance(plan, dict):
                        raise ValueError("invalid capability plan")
                    lock_raw = self._optional(name, "run-lock.json")
                    lock = json.loads(lock_raw)
                    if lock and (not isinstance(lock, dict) or lock.get("plan") != plan
                                 or not isinstance(lock.get("evaluations"), list)):
                        lock = {}
                    progress_raw = self._optional(name, "public-matrix-progress.json") if name in MEMBERS and lock else b'{}'
                    progress = json.loads(progress_raw)
                    files = progress.get("files", {})
                    valid_progress = (progress.get("schema") == 2 and progress.get("run_id") == name
                                      and progress.get("lock_sha256") == hashlib.sha256(lock_raw).hexdigest()
                                      and sha(progress.get("ledger_sha256")) and files.get("results.jsonl"))
                    previous = progress.get("previous_quota", {})
                    def matches_file(key):
                        return files.get(key) and files[key] == self._signature(name, key)
                    verified_resume = (valid_progress and sha(previous.get("results_sha256"))
                                       and matches_file("resume-reconciliation.json")
                                       and matches_file("provider-quota-pause.before-recovery.json")
                                       and matches_file("results.jsonl"))
                    try:
                        raw = self._read(f"mx/{name}/scores.json")
                    except FileNotFoundError:
                        archived_score = (verified_resume and sha(previous.get("scores_sha256"))
                                          and matches_file("scores.before-quota-recovery.json"))
                        if (self._good.get(name, {}).get("at") and not archived_score) or name not in MEMBERS or not lock:
                            raise
                        # Registered member has a verified frozen plan; scoring is pending.
                        raw = b'{"cells":[]}'
                    scores = json.loads(raw)
                    supplement = self._supplement(name, progress.get("lock_sha256"), progress) if valid_progress else None
                    if supplement:
                        effective, raw, effective_raw = supplement
                        scores = json.loads(raw)
                        if effective.get("complete") and (scores.get("plan") != name or scores.get("seed") != plan.get("seed", 0)):
                            raise ValueError("Supplemental scores differ from the original run identity")
                    if not isinstance(scores, dict):
                        raise ValueError("invalid capability scores")
                    legacy_raw = self._optional(name, "public-cohorts.json")
                    legacy = json.loads(legacy_raw)
                    bound = isinstance(legacy, dict) and legacy.get("source_sha256") == hashlib.sha256(raw + b"\0" + plan_raw).hexdigest()
                    observed = legacy.get("observations", {}) if bound else {}
                    digest = hashlib.sha256(b"\0".join((raw, plan_raw, lock_raw, legacy_raw))).hexdigest()
                    projected = project_scores(name, scores, plan, digest, lock=lock, historical=observed)
                    if valid_progress:
                        apply_progress(projected, effective if supplement else progress)
                        projected["source_sha256"] = hashlib.sha256(digest.encode() + progress_raw + (effective_raw if supplement else b"")).hexdigest()
                        pause = progress.get("pause", {})
                        if (pause.get("reason") == "insufficient_user_quota" and files.get("provider-quota-pause.json")
                                and files["results.jsonl"] == self._signature(name,"results.jsonl")
                                and self._signature(name,"provider-quota-pause.json") == files["provider-quota-pause.json"]
                                and projected["trial_coverage"]["quota"]):
                            projected["pause"] = {"reason":"insufficient_user_quota","detected_at":_date(pause.get("detected_at"))}
                        if (pause.get("reason") == "user_requested" and matches_file("results.jsonl")
                                and matches_file("user-pause.json")):
                            projected["pause"] = {"reason":"user_requested","detected_at":_date(pause.get("detected_at"))}
                        if verified_resume:
                            prior = {k:_count(previous.get(k)) for k in ("runs","clean","quota","retained")}
                            if (None not in prior.values() and prior["clean"] + prior["quota"] <= prior["runs"]
                                    and prior["retained"] <= prior["runs"]):
                                projected["previous_quota"] = {**prior,"at":_date(previous.get("at")),
                                                               "results_sha256":previous["results_sha256"]}
                        if supplement:
                            projected["recovery"] = {k:effective[k] for k in ("complete","native_receipts","supplemental_receipts","terminal_dispositions","manifest_sha256")}
                            paused = effective.get("provider_pause")
                            if isinstance(paused, dict) and paused.get("reason") == "insufficient_user_quota":
                                projected["pause"] = {"reason":"insufficient_user_quota","detected_at":_date(paused.get("detected_at"))}
                    self._good[name] = projected
                    if lock and (not scores["cells"] or scores.get("plan") == plan.get("name") and scores.get("seed") == plan.get("seed", 0)):
                        verified_locks[name] = lock
                except FileNotFoundError:
                    if name in self._good:
                        errors.add("capability_summary_missing")
                except (ValueError, OSError, TypeError, KeyError, OverflowError, AttributeError):
                    errors.add("capability_summary_invalid")
            if set(self._good) - set(names):
                errors.add("capability_summary_missing")
            runs = list(self._good.values())
            source = "artifacts" if runs else "none"
            if not runs and self.live:
                try:
                    runs = metrics_runs(self.live())
                    if runs:
                        source = "metrics"
                except (ValueError, TypeError, KeyError, AttributeError):
                    errors.add("capability_metrics_unavailable")
            runs.sort(key=lambda r: (r.get("at") or "", r["id"]), reverse=True)
            combined = panel(self._good, verified_locks)
            primary = combined or next((r for r in runs if r["id"] not in MEMBERS and len({c["role"] for c in r["columns"]}) > 1), None)
            doc = {"primary_run_id": primary["id"] if primary else (runs[0]["id"] if runs else None), "source": source, "status": "stale" if errors and runs else "ready" if runs else "unavailable" if errors else "empty",
                   "stale": bool(errors), "checked_at": datetime.now(UTC).isoformat(), "latest_run_id": runs[0]["id"] if runs else None,
                   "runs": ([combined] if combined else []) + runs, "errors": sorted(errors)}
            self._cached = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
            self._at = now
            return self._cached
