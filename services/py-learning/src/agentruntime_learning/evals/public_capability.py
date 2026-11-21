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

MAX_BYTES = 2 * 1024 * 1024
MAX_RUNS = 64
NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$")
ROLES = {"act": ("工具执行", "Tool use"), "plan": ("规划", "Planning"), "judge": ("判断", "Judgment"),
         "route": ("分诊", "Routing"), "transform": ("压缩与转换", "Transform"), "deliver": ("交付", "Delivery")}
TIERS = {"low": ("基础", "Low"), "medium": ("中等", "Medium"), "high": ("高", "High"), "default": ("默认", "Default")}
POLICY = "capmatrix-publication-v2"


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


def _selection(column: dict, plan: dict, cell_plan: dict, seed: int | None,
               candidate: dict | None = None) -> dict:
    specs = {}
    entries = cell_plan.get("evals")
    for entry in entries[:32] if isinstance(entries, list) else []:
        spec = {"eval": entry} if isinstance(entry, str) else entry
        if isinstance(spec, dict) and isinstance(spec.get("eval"), str) and spec["eval"] in _EVAL_NAMES:
            specs[_EVAL_NAMES[spec["eval"]]] = spec
    measured = {v["name"]: v["version"] for v in column["evals"] if NAME.fullmatch(v["name"])}
    cohorts = (candidate or {}).get("observed_cohorts", {})
    groups = []
    for dataset in dict.fromkeys([*measured, *specs]):
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
        requested = _count(spec.get("n", cell_plan.get("n", plan.get("n", 40)))) if spec else None
        version = measured.get(dataset)
        groups.append(facts(dataset, method, "scores.json + plan.json" if spec else "scores.json", zh, en,
                            role=column["role"], tier=column["tier"], split=split, seed=seed,
                            requested_count=requested, observed_count=_count(observed.get("count")),
                            version=version if version and re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", version) else None,
                            cohort_sha256=sha(observed.get("sha256"))))
    return facts("capability", "per_evaluator", "scores.json + plan.json" if cell_plan else "scores.json",
                 "不同评测分别取题；计划上限不等于每个候选实际得到的题数，重复运行不另算题。",
                 "Datasets are sampled separately. Planned limits may differ from a candidate’s observed cases; repeats are not new cases.",
                 groups=groups)


def project_scores(name: str, scores: dict[str, Any], plan: dict[str, Any], digest: str) -> dict[str, Any]:
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
            col["selection_info"] = _selection(col, plan, c, seed)
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
        column["selection_info"] = _selection(column, plan, plan_cell, seed)
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
            if isinstance(candidate.get("observed_cohorts"), dict):
                measurement["selection_info"] = _selection(column, plan, plan_cell, seed, candidate)
            row["cells"][column["id"]] = measurement
    return {"id": name, "label": _text(scores.get("plan")) or name, "at": at,
            "evaluation_type": "model", "evaluation_scope": "component",
            "task_family": _text(plan.get("task_family")) or "general.unclassified",
            "selection_info": facts("capability", "per_evaluator", "scores.json + plan.json" if planned else "scores.json",
                                    "按角色、推理档与组件分别取题。", "Items are selected separately per role, tier and evaluator.",
                                    seed=seed, groups=[g for col in columns.values() for g in col["selection_info"]["groups"]]),
            "columns": list(columns.values()), "models": [models[k] for k in sorted(models)], "source_sha256": digest}


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
            for name in names:
                try:
                    raw = self._read(f"mx/{name}/scores.json")
                    plan_raw = b"{}"
                    try:
                        plan_raw = self._read(f"mx/{name}/plan.json")
                    except FileNotFoundError:
                        pass
                    scores, plan = json.loads(raw), json.loads(plan_raw)
                    if not isinstance(scores, dict) or not isinstance(plan, dict):
                        raise ValueError("invalid capability documents")
                    self._good[name] = project_scores(name, scores, plan, hashlib.sha256(raw + b"\0" + plan_raw).hexdigest())
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
            doc = {"source": source, "status": "stale" if errors and runs else "ready" if runs else "unavailable" if errors else "empty",
                   "stale": bool(errors), "checked_at": datetime.now(UTC).isoformat(), "latest_run_id": runs[0]["id"] if runs else None,
                   "runs": runs, "errors": sorted(errors)}
            self._cached = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
            self._at = now
            return self._cached
