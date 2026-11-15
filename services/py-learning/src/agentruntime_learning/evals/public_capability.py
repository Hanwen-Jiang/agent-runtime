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
    for c in planned:
        if not isinstance(c, dict):
            continue
        col = _column({**c, "task_family": c.get("task_family", plan.get("task_family")), "evals": []})
        if col:
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
            row["cells"][column["id"]] = _measurement(candidate, cell, at)
    return {"id": name, "label": _text(scores.get("plan")) or name, "at": at,
            "evaluation_type": "model", "evaluation_scope": "component",
            "task_family": _text(plan.get("task_family")) or "general.unclassified",
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
