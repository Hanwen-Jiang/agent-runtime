"""Public evaluation application: same-origin UI, live metrics, dataset catalog and report archive.

The report archive is read on every request. Legacy public ConfigMap HTML is accepted as
an archive input during migration, but is never served as the application UI. Only explicitly
allowlisted assets and evaluation-only JSON are exposed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from agentruntime_learning.evals.site_catalog import Catalog, build_catalog
from agentruntime_learning.evals.site_sync import SiteSync
from agentruntime_learning.evals.public_experiments import PublicExperimentSource
from agentruntime_learning.evals.public_capability import PublicCapabilitySource
from agentruntime_learning.evals.public_benchmarks import PublicBenchmarkSource
from agentruntime_learning.evals.public_terminalbench import PublicTerminalBenchSource
from agentruntime_learning.evals.public_task_loop import PublicTaskLoopSource, merge_task_loop_runs, public_models

PAGE_NAME = "index.html"
# 评测网站（docs/plan/eval-site.md）：/ 是主页，原来的看板搬到 /results，旧地址 /index.html 永久跳过去。
RESULTS_PATH = "/results"
OLD_RESULTS_PATH = "/index.html"
STATIC_DIR = Path(__file__).parent / "site_pages"
STATIC_PAGES: Final[dict[str, str]] = {"/": "app.html", "/datasets": "app.html", "/results": "app.html"}
ASSETS: Final[dict[str, str]] = {"/assets/app.js": "text/javascript; charset=utf-8", "/assets/app.css": "text/css; charset=utf-8"}
ASSETS.update({f"/assets/ibm-plex-{family}-latin-{weight}-normal.woff2": "font/woff2"
               for family, weights in (("sans", (400, 500, 600)), ("mono", (400,))) for weight in weights})
REPORTS_PATH = "/api/reports.json"
CAPABILITY_PATH = "/api/capability.json"
BENCHMARKS_PATH = "/api/benchmarks.json"
TERMINALBENCH_PATH = "/api/terminalbench.json"
HEALTH_PATH = "/healthz"
LIVE_PATH = "/api/live.json"
SITE_INDEX_PATH = "/api/site.json"
DATASET_PATH_RE = re.compile(r"^/api/datasets/([a-z0-9][a-z0-9-]{0,63})\.json$")
SITE_CACHE_CONTROL = "public, max-age=60"
SITE_TTL_S = 30.0
SITE_REPO_ENV = "AR_EVAL_SITE_REPO"
SITE_REF_ENV = "AR_EVAL_SITE_REF"
SITE_TOKEN_FILE_ENV = "AR_EVAL_SITE_TOKEN_FILE"
SITE_CACHE_DIR_ENV = "AR_EVAL_SITE_CACHE_DIR"
EXPERIMENTS_DIR_ENV = "AR_EVAL_EXPERIMENTS_DIR"
BENCHMARKS_DIR_ENV = "AR_EVAL_BENCHMARKS_DIR"
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
# Cloudflare 可以短暂缓存；发布后几分钟内更新。
PAGE_CACHE_CONTROL = "no-cache"
LIVE_CACHE_CONTROL = "public, max-age=15"
REQUEST_TIMEOUT_S = 30

METRICS_URL_ENV = "AR_EVAL_METRICS_URL"
LIVE_TTL_S = 20.0
METRICS_TIMEOUT_S = 5.0
# VictoriaMetrics 的默认保留期是一个月；更早的运行靠静态页里写死的历史。
LOOKBACK = "31d"
# 多久没有新的用例结果、也没结束的运行算“停了”（SWE-bench 一题可能要半小时以上）。
STALLED_AFTER_S = 2 * 3600
MAX_RUNS = 80

_K = "eval_run_id, eval_process"
# 写死的查询：只有 runtime_eval_* 指标。键是合成时用的名字。
QUERIES: Final[dict[str, str]] = {
    "planned": f"max by ({_K}, eval_suite, eval_subject, eval_split) (last_over_time(runtime_eval_cases_planned_total[{LOOKBACK}]))",
    "carried": f"max by ({_K}) (last_over_time(runtime_eval_cases_carried_total[{LOOKBACK}]))",
    "carried_cost": f"max by ({_K}) (last_over_time(runtime_eval_cost_carried_usd_sum[{LOOKBACK}]))",
    "cases": f"sum by ({_K}, eval_outcome) (last_over_time(runtime_eval_cases_total[{LOOKBACK}]))",
    "cost": f"sum by ({_K}) (last_over_time(runtime_eval_case_cost_usd_sum[{LOOKBACK}]))",
    "finished": f"max by ({_K}, eval_model) (last_over_time(runtime_eval_finished_total[{LOOKBACK}]))",
    "valid": f"min by (eval_run_id, eval_valid) (last_over_time(runtime_eval_runs_total[{LOOKBACK}]))",
    "scores": (
        f"max by (eval_run_id, eval_metric) (last_over_time(runtime_eval_score_sum[{LOOKBACK}]) / last_over_time(runtime_eval_score_count[{LOOKBACK}]))"
    ),
    "started": f"min by (eval_run_id) (tfirst_over_time(runtime_eval_cases_planned_total[{LOOKBACK}]))",
    "updated": f'max by (eval_run_id) (tlast_over_time({{__name__=~"runtime_eval_(cases|cases_planned|finished)_total"}}[{LOOKBACK}]))',
}

Fetch = Callable[[str, float], bytes]


def _http_get(url: str, timeout_s: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310 — 地址来自部署配置，查询是写死的
        return bytes(resp.read())


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _value(sample: Mapping[str, Any]) -> float | None:
    try:
        value = float(sample["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def combine(results: Mapping[str, list[dict[str, Any]]], now: float) -> list[dict[str, Any]]:
    """把各条查询的结果合成每次运行一行。一次运行可以分几个进程跑（续跑）：已跑完的数与花费取各进程“带过来的 + 自己跑完的”的最大值，
    各种结果的条数取各进程之和。"""
    runs: dict[str, dict[str, Any]] = {}
    procs: dict[tuple[str, str], dict[str, float]] = {}

    def run_of(labels: Mapping[str, str]) -> dict[str, Any] | None:
        rid = labels.get("eval_run_id")
        if not rid:
            return None
        return runs.setdefault(rid, {
            "run_id": rid, "suite": "", "subject": "", "split": "", "model": "", "planned": 0, "done": 0,
            "outcomes": {}, "cost_usd": 0.0, "finished": False, "valid": None, "scores": {}, "started_at": None, "updated_at": None,
        })

    def proc_of(labels: Mapping[str, str]) -> dict[str, float]:
        return procs.setdefault((labels.get("eval_run_id", ""), labels.get("eval_process", "")), {"carried": 0.0, "carried_cost": 0.0, "done": 0.0, "cost": 0.0})

    for sample in results.get("planned", []):
        labels, value = sample.get("metric", {}), _value(sample)
        run = run_of(labels)
        if run is None or value is None:
            continue
        run["planned"] = max(run["planned"], int(value))
        for key in ("suite", "subject", "split"):
            run[key] = run[key] or str(labels.get(f"eval_{key}", ""))
        proc_of(labels)
    for name, field in (("carried", "carried"), ("carried_cost", "carried_cost"), ("cost", "cost")):
        for sample in results.get(name, []):
            labels, value = sample.get("metric", {}), _value(sample)
            if run_of(labels) is not None and value is not None:
                proc_of(labels)[field] += value
    for sample in results.get("cases", []):
        labels, value = sample.get("metric", {}), _value(sample)
        run = run_of(labels)
        if run is None or value is None:
            continue
        outcome = str(labels.get("eval_outcome", ""))
        run["outcomes"][outcome] = run["outcomes"].get(outcome, 0) + int(value)
        if outcome != "error":
            proc_of(labels)["done"] += value
    for sample in results.get("finished", []):
        labels = sample.get("metric", {})
        run = run_of(labels)
        if run is not None and (_value(sample) or 0) > 0:
            run["finished"] = True
            run["model"] = run["model"] or str(labels.get("eval_model", ""))
    for sample in results.get("valid", []):
        labels = sample.get("metric", {})
        run = run_of(labels)
        if run is not None and labels.get("eval_valid") in ("true", "false"):
            run["valid"] = labels["eval_valid"] == "true" and run["valid"] is not False
    for sample in results.get("scores", []):
        labels, value = sample.get("metric", {}), _value(sample)
        run = run_of(labels)
        if run is not None and value is not None and labels.get("eval_metric"):
            run["scores"][str(labels["eval_metric"])] = round(value, 6)
    for name, field in (("started", "started_at"), ("updated", "updated_at")):
        for sample in results.get(name, []):
            run, value = run_of(sample.get("metric", {})), _value(sample)
            if run is not None and value is not None:
                run[field] = value
    for (rid, _), p in procs.items():
        run = runs.get(rid)
        if run is None:
            continue
        run["done"] = max(run["done"], int(round(p["carried"] + p["done"])))
        run["cost_usd"] = max(run["cost_usd"], round(p["carried_cost"] + p["cost"], 6))
    rows = []
    for run in runs.values():
        if not run["suite"]:
            continue  # 只有零星的分数、没有开始记录的（例如别的版本发的）不列
        updated = run["updated_at"]
        run["status"] = "finished" if run["finished"] else "stalled" if updated is None or now - updated > STALLED_AFTER_S else "running"
        run["done"] = min(run["done"], run["planned"]) if run["planned"] else run["done"]
        run["started_at"], run["updated_at"] = _iso(run["started_at"]), _iso(updated)
        rows.append(run)
    rows.sort(key=lambda r: (r["status"] != "running", -(datetime.fromisoformat(r["updated_at"]).timestamp() if r["updated_at"] else 0)))
    return rows[:MAX_RUNS]


class LiveSource:
    """/api/live.json 的数据：按写死的查询读 VictoriaMetrics，缓存 LIVE_TTL_S 秒；连不上时给最后一份并标明。"""

    def __init__(self, metrics_url: str | None, *, fetch: Fetch | None = None, clock: Callable[[], float] = time.time, ttl_s: float = LIVE_TTL_S) -> None:
        url = (metrics_url or "").strip().rstrip("/")
        if url and urlsplit(url).scheme not in ("http", "https"):
            raise ValueError(f"{METRICS_URL_ENV} 要是 http 或 https 的地址")
        self.url = url
        self.fetch = fetch or _http_get
        self.clock = clock
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._body: bytes | None = None
        self._at = float("-inf")
        self._good: dict[str, Any] | None = None

    def _query(self, query: str) -> list[dict[str, Any]]:
        raw = self.fetch(f"{self.url}/api/v1/query?{urllib.parse.urlencode({'query': query})}", METRICS_TIMEOUT_S)
        data = json.loads(raw)
        if data.get("status") != "success" or not isinstance(data.get("data", {}).get("result"), list):
            raise ValueError("unexpected answer")
        return list(data["data"]["result"])

    def get(self) -> bytes:
        with self._lock:
            now = self.clock()
            if self._body is not None and now - self._at < self.ttl_s:
                return self._body
            doc: dict[str, Any]
            if not self.url:
                doc = {"source": "none", "stale": False, "generated_at": _iso(now), "runs": []}
            else:
                try:
                    results = {name: self._query(q) for name, q in QUERIES.items()}
                    doc = {"source": "metrics", "stale": False, "generated_at": _iso(now), "runs": combine(results, now)}
                    self._good = doc
                except Exception:  # noqa: BLE001 — 只说连不上，不回显地址与异常（可能带内部地址）
                    doc = {**(self._good or {"source": "metrics", "generated_at": None, "runs": []}), "stale": True, "error": "metrics_unreachable"}
            self._body = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()
            self._at = now
            return self._body


class SiteSource:
    """/api/site.json 与 /api/datasets/<名字>.json 的数据（docs/plan/eval-site.md）：读 SiteSync 当前的一份 evalsets，
    建一次目录缓存 SITE_TTL_S 秒；同步换了新的一份时下一次请求就用新的。"""

    def __init__(self, sync: SiteSync | None, *, clock: Callable[[], float] = time.time, ttl_s: float = SITE_TTL_S) -> None:
        self.sync = sync
        self.clock = clock
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._catalog: Catalog | None = None
        self._key: tuple[Path | None, float] = (None, float("-inf"))

    def _get(self) -> Catalog | None:
        if self.sync is None or self.sync.current() is None:
            return None
        with self._lock:
            root, now = self.sync.current(), self.clock()
            if self._catalog is None or self._key[0] != root or now - self._key[1] >= self.ttl_s:
                try:
                    self._catalog = build_catalog(root)  # type: ignore[arg-type]
                except (OSError, ValueError):
                    self._catalog = self._catalog or Catalog()
                self._key = (root, now)
            return self._catalog

    def index(self) -> bytes:
        catalog = self._get()
        sync = self.sync.status() if self.sync is not None else {"source": "none"}
        doc = {**(catalog.index() if catalog else {"datasets": [], "frameworks": [], "groupings": [], "default_grouping": "eval_category", "excluded": []}),
               "sync": sync}
        doc.pop("excluded", None)  # 只给运维看（日志），页面不用
        return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()

    def dataset(self, name: str) -> bytes | None:
        catalog = self._get()
        doc = catalog.dataset(name) if catalog else None
        return None if doc is None else json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()


def _static_page(name: str) -> bytes:
    return (STATIC_DIR / name).read_bytes()



_REPORT_FIELDS: Final[dict[str, tuple[set[str], set[str], set[str]]]] = {
    "runs": (
        {"group", "subject", "split", "created_at", "label_zh", "label_en", "run_id", "model", "eval_set_name", "eval_set_version", "dataset_hash", "source_sha256", "source_kind", "protocol", "variant", "comparison_id", "code_revision", "code_sha256", "scoring_version", "cohort_id", "deliverable_judge", "selection_sha256", "reviewer_profile"},
        {"triage", "shape", "reviewer", "rounds", "stuck", "total_usd", "per_case_usd", "task_pass", "task_total", "fallbacks", "structured_failures", "decisions", "reviewer_samples", "round_samples", "reviewer_schema_pass_rate", "reviewer_fallbacks", "fallback_rate", "max_fallback_rate", "labels_confirmed", "labels_draft", "repeat"},
        {"valid"},
    ),
    "contexts": (
        {"group", "split", "date"},
        {"full_probe", "mask_probe", "full_retention", "repeat_rate"},
        {"gate"},
    ),
    "benchmarks": (
        {"run_id", "model", "environment"},
        {"selected", "resolved", "rate", "cost_usd", "per_problem_usd", "rounds_median"},
        {"complete"},
    ),
    "tuning": (
        {"run_id", "short_zh", "short_en"},
        {"selected", "resolved", "rate", "low", "high", "per_problem_usd", "gained", "lost"},
        {"same_problems"},
    ),
    "experiments": (
        {"id", "kind", "title_zh", "title_en", "completed_at", "updated_at", "status", "blocked_reason", "evaluated_on", "reported_on", "baseline_label", "candidate_label", "source_sha256", "evaluation_type", "evaluation_scope", "runtime_revision", "runtime_source_sha256", "comparison_id", "protocol", "config_sha256", "dataset_name", "dataset_version", "cohort_sha256", "model_profile", "baseline_run_id", "candidate_run_id", "baseline_variant", "candidate_variant", "source_mode", "source_revision", "data_updated_at", "checked_at"},
        {"sample_count", "completed_count", "known_cost_usd", "actual_cost_usd", "pending_reserved_usd", "repeats", "answer_context_budget_tokens", "paired_gained", "paired_lost", "paired_accuracy_delta", "paired_ci95_low", "paired_ci95_high", "paired_mcnemar_p"},
        {"collection_complete", "quality_valid", "acceptance_passed", "cost_known"},
    ),
}
_MAX_ARCHIVE_BYTES = 8 * 1024 * 1024


def _archive_number(value: Any) -> bool:
    # Booleans are not scores. Huge integers and non-finite floats are not safe JSON numbers in browsers.
    return (type(value) is int and abs(value) <= 2**53 - 1) or (type(value) is float and math.isfinite(value))


def _archive_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {key: count for key, count in value.items() if isinstance(key, str) and type(count) is int and 0 <= count <= 2**53 - 1}


def _clean_report_document(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(data.get("reports"), dict):
        raise ValueError("invalid archive")
    reports = data["reports"]
    clean: dict[str, Any] = {}
    for kind, (texts, numbers, flags) in _REPORT_FIELDS.items():
        rows = reports.get(kind, [])
        if not isinstance(rows, list):
            raise ValueError("invalid archive section")
        clean[kind] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid archive row")
            item = {key: value for key, value in row.items() if
                    (key in texts and (value is None or isinstance(value, str))) or
                    (key in numbers and (value is None or _archive_number(value))) or
                    (key in flags and (value is None or type(value) is bool))}
            if kind == "runs" and "models" in row:
                item["models"] = public_models(row["models"])
            if kind == "benchmarks":
                for field in ("by_repo", "by_difficulty"):
                    groups = row.get(field, {})
                    item[field] = {
                        key: {k: v for k, v in _archive_counts(counts).items() if k in {"selected", "ran", "resolved"}}
                        for key, counts in groups.items() if isinstance(key, str) and isinstance(counts, dict)
                    } if isinstance(groups, dict) else {}
                item["gradings"] = _archive_counts(row.get("gradings"))
            if kind == "experiments":
                item["metrics"] = []
                for metric in row.get("metrics", []) if isinstance(row.get("metrics"), list) else []:
                    if not isinstance(metric, dict):
                        continue
                    item["metrics"].append({key: value for key, value in metric.items() if
                        (key in {"key", "label_zh", "label_en", "unit"} and isinstance(value, str)) or
                        (key in {"baseline", "candidate"} and (value is None or _archive_number(value)))})
                variants = row.get("variants")
                if item.get("kind") == "context-cohort-comparison" and isinstance(variants, list) and 2 <= len(variants) <= 8:
                    allowed = {"id", "label_zh", "label_en", "protocol", "source_run_id", "status"}
                    item["variants"] = [{k: v for k, v in variant.items() if k in allowed and isinstance(v, str) and len(v) <= 256}
                                        for variant in variants if isinstance(variant, dict)]
                    if len(item["variants"]) != len(variants):
                        raise ValueError("invalid public comparison variants")
                    for original, cleaned in zip(row.get("metrics", []), item["metrics"], strict=True):
                        values = original.get("values")
                        if not isinstance(values, list) or len(values) != len(variants) or any(v is not None and not _archive_number(v) for v in values):
                            raise ValueError("invalid public comparison values")
                        cleaned["values"] = values
                for field in ("limitations_zh", "limitations_en"):
                    item[field] = [value for value in row.get(field, []) if isinstance(value, str)] if isinstance(row.get(field), list) else []
            clean[kind].append(item)
    failures = reports.get("failures", {})
    if not isinstance(failures, dict):
        raise ValueError("invalid archive failures")
    clean["failures"] = {
        group: [[pair[0], pair[1]] for pair in pairs if isinstance(pair, list) and len(pair) == 2
                and isinstance(pair[0], str) and type(pair[1]) is int and 0 <= pair[1] <= 2**53 - 1]
        for group, pairs in failures.items() if isinstance(group, str) and isinstance(pairs, list)
    }
    return {"generated_at": data.get("generated_at") if isinstance(data.get("generated_at"), str) else None, "reports": clean}


def _archive_timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        # The existing public generator uses "2026-11-02 21:49 UTC"; also accept ISO 8601.
        stamp = datetime.fromisoformat(value.removesuffix(" UTC").replace("Z", "+00:00"))
        return stamp.replace(tzinfo=UTC).timestamp() if stamp.tzinfo is None else stamp.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


class ReportArchiveSource:
    """Read both publication formats on demand and retain only the last valid public projection.

    During migration a newer legacy HTML publication must not be hidden by an older reports.json.
    Compare embedded generation times when both are available, otherwise file modification times;
    JSON wins ties. These timestamps describe publication, never a live evaluation event.
    """

    def __init__(self, root: Path, *, experiments: PublicExperimentSource | None = None, task_loop: PublicTaskLoopSource | None = None) -> None:
        self.root = Path(root)
        self.experiments = experiments
        self.task_loop = task_loop
        self._lock = threading.Lock()
        self._good: dict[str, Any] | None = None

    def get(self) -> bytes:
        with self._lock:
            candidates: list[tuple[dict[str, Any], float | None, int, bool]] = []
            errors: list[str] = []
            for name in (PAGE_NAME, "reports.json"):
                try:
                    with (self.root / name).open("rb") as stream:
                        raw = stream.read(_MAX_ARCHIVE_BYTES + 1)
                        modified = os.fstat(stream.fileno()).st_mtime_ns
                    if len(raw) > _MAX_ARCHIVE_BYTES:
                        raise ValueError("archive too large")
                    text = raw.decode("utf-8")
                    if name == PAGE_NAME:
                        marker = re.search(r"\bconst\s+D\s*=\s*(?=\{)", text)
                        if not marker:
                            raise ValueError("missing public archive")
                        data, _ = json.JSONDecoder().raw_decode(text[marker.end():])
                    else:
                        data = json.loads(text)
                    doc = _clean_report_document(data)
                    candidates.append((doc, _archive_timestamp(doc["generated_at"]), modified, name == "reports.json"))
                except FileNotFoundError:
                    continue
                except OSError:
                    errors.append("archive_unreadable")
                except (ValueError, RecursionError):
                    errors.append("archive_invalid")
            if candidates:
                use_generation = all(candidate[1] is not None for candidate in candidates)
                chosen = max(candidates, key=lambda candidate: (candidate[1] if use_generation else candidate[2], candidate[3]))
                previous_time = _archive_timestamp(self._good.get("generated_at")) if self._good else None
                # A corrupt new JSON must not replace the last good response with an older legacy copy.
                if not (errors and self._good is not None and
                        (previous_time is None or chosen[1] is None or previous_time >= chosen[1])):
                    self._good = chosen[0]
            error = errors[0] if errors else "archive_missing" if not candidates else None
            if self._good is not None:
                doc = {**self._good, "source": "archive", "status": "stale" if error else "ready", "stale": error is not None}
            else:
                doc = {"source": "none", "status": "unavailable", "stale": bool(errors), "generated_at": None,
                       "reports": {**{kind: [] for kind in _REPORT_FIELDS}, "failures": {}}}
            if error:
                doc["error"] = error
            if self.experiments is not None:
                live = self.experiments.get()
                projected = _clean_report_document({"reports": {"experiments": live["items"]}})["reports"]["experiments"]
                # Start with legacy history, then prefer actual artifact summaries by stable run ID.
                merged = {row["id"]: row for row in doc["reports"]["experiments"] if isinstance(row.get("id"), str)}
                merged.update({row["id"]: row for row in projected if isinstance(row.get("id"), str)})
                doc["reports"] = {**doc["reports"], "experiments": list(merged.values())}
                doc["experiment_source"] = {key: value for key, value in live.items() if key != "items"}
                if projected:
                    doc["source"] = "archive+artifacts" if self._good is not None else "artifacts"
                    if doc.get("error") == "archive_missing" and self._good is None:
                        doc.pop("error", None)
                        doc["status"], doc["stale"] = "ready", False
                if live["stale"]:
                    doc["stale"] = True
                    doc["status"] = "stale" if self._good is not None or projected else "unavailable"
                    doc.setdefault("error", "experiments_unavailable")
            if self.task_loop is not None:
                live = self.task_loop.get()
                projected = _clean_report_document({"reports": {"runs": live["items"]}})["reports"]["runs"]
                doc["reports"] = {**doc["reports"], "runs": merge_task_loop_runs(doc["reports"]["runs"], projected)}
                doc["task_loop_source"] = {key: value for key, value in live.items() if key != "items"}
                if projected:
                    doc["source"] = "archive+artifacts" if self._good is not None else "artifacts"
                    if doc.get("error") == "archive_missing" and self._good is None:
                        doc.pop("error", None)
                        doc["status"], doc["stale"] = "ready", False
                if live["stale"]:
                    doc["stale"] = True
                    doc["status"] = "stale" if self._good is not None or projected else "unavailable"
                    doc.setdefault("error", "task_loop_unavailable")
            # A newly available source must not mask another source's read failure.
            if any(doc.get(key, {}).get("stale") for key in ("experiment_source", "task_loop_source")):
                doc["stale"] = True
                doc["status"] = "stale" if any(doc["reports"].get(key) for key in ("runs", "experiments")) or self._good is not None else "unavailable"
            return json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _report_archive(root: Path) -> bytes:
    """One-shot archive projection; servers use a persistent ReportArchiveSource for failure recovery."""
    return ReportArchiveSource(root).get()

def _merge_terminalbench_feed(body: bytes, terminalbench: dict[str, Any], exclusions: list[dict[str, Any]] | None = None) -> bytes:
    """A final artifact replaces its telemetry row, including null costs/scores.

    Incomplete grading stays partial: never merge stale max-score telemetry into
    a final summary or turn unstarted cases into zero-reward observations.
    """
    doc = json.loads(body)
    artifacts = terminalbench.get("runs") or []
    rows = {(row.get("suite"), row.get("run_id")): row for row in doc.get("runs", [])}
    rows.update({(row["suite"], row["run_id"]): row for row in artifacts})
    audits = {(row.get("suite"), row.get("run_id")): row for row in [*(doc.get("exclusions") or []), *(exclusions or [])]}
    for key, audit in audits.items():
        if key in rows:
            fields = {name: audit[name] for name in ("execution_valid", "invalid_reason", "reason_zh", "reason_en") if name in audit}
            rows[key] = {**rows[key], **fields}
    if audits:
        doc["exclusions"] = list(audits.values())
    doc["runs"] = sorted(rows.values(), key=lambda row: str(row.get("updated_at") or row.get("completed_at") or row.get("created_at") or ""), reverse=True)
    doc["terminalbench_source"] = {key: value for key, value in terminalbench.items() if key != "runs"}
    if artifacts:
        doc["source"] = "artifacts" if doc.get("source") in (None, "none") else str(doc["source"]) + "+terminalbench_artifacts"
        if doc.get("status") in (None, "empty", "unavailable") and not doc.get("stale"):
            doc["status"] = "ready"
        doc.pop("unavailable", None)
    if terminalbench.get("stale"):
        doc["stale"] = True
        doc["status"] = "stale" if doc["runs"] else "unavailable"
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def make_server(root: Path, host: str, port: int, *, live: LiveSource | None = None, site: SiteSync | None = None, experiments_dir: Path | None = None, experiments_ttl_s: float = 5.0, benchmarks_dir: Path | None = None) -> ThreadingHTTPServer:
    """绑定地址、还没开始服务的 HTTP 服务；port 为 0 时随机分配。live 不给时 /api/live.json 回 source 为 none；
    site 不给时 /api/site.json 回 source 为 none、没有数据集。"""
    source = live or LiveSource(None)
    site_source = SiteSource(site)
    experiments = PublicExperimentSource(experiments_dir, ttl_s=experiments_ttl_s) if experiments_dir is not None else None
    task_loop = PublicTaskLoopSource(experiments_dir, ttl_s=experiments_ttl_s) if experiments_dir is not None else None
    report_source = ReportArchiveSource(Path(root), experiments=experiments, task_loop=task_loop)
    capability_source = PublicCapabilitySource(experiments_dir, live=source.get, ttl_s=experiments_ttl_s)
    benchmark_source = PublicBenchmarkSource(benchmarks_dir, ttl_s=experiments_ttl_s, audit_root=experiments_dir)
    terminalbench_source = PublicTerminalBenchSource(experiments_dir, ttl_s=experiments_ttl_s)

    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT_S

        def version_string(self) -> str:
            return "eval-dashboard"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — 基类的参数名
            return

        def _reply(self, status: int, body: bytes, content_type: str, **headers: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            for name, value in headers.items():
                self.send_header(name.replace("_", "-"), value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _text(self, status: int, **headers: str) -> None:
            self._reply(status, (HTTPStatus(status).phrase.lower() + "\n").encode(), "text/plain; charset=utf-8", **headers)

        def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
            # 基类的错误页会回显请求里的内容（方法、版本），这里只说状态。
            self._text(code)

        def _get(self) -> None:
            path = urlsplit(self.path).path
            if path == HEALTH_PATH:
                self._reply(HTTPStatus.OK, b"ok", "text/plain; charset=utf-8", Cache_Control="no-store")
                return
            if path == LIVE_PATH:
                # 查询参数一概不用：查什么在服务端写死。
                self._reply(HTTPStatus.OK, _merge_terminalbench_feed(source.get(), terminalbench_source.get(), json.loads(benchmark_source.get()).get("exclusions")), "application/json; charset=utf-8", Cache_Control=LIVE_CACHE_CONTROL)
                return
            if path == BENCHMARKS_PATH:
                self._reply(HTTPStatus.OK, _merge_terminalbench_feed(benchmark_source.get(), terminalbench_source.get()), "application/json; charset=utf-8", Cache_Control="no-cache")
                return
            if path == TERMINALBENCH_PATH:
                self._reply(HTTPStatus.OK, json.dumps(terminalbench_source.get(), ensure_ascii=False, allow_nan=False).encode(), "application/json; charset=utf-8", Cache_Control="no-cache")
                return
            if path == CAPABILITY_PATH:
                self._reply(HTTPStatus.OK, capability_source.get(), "application/json; charset=utf-8", Cache_Control="no-cache")
                return
            if path == REPORTS_PATH:
                self._reply(HTTPStatus.OK, report_source.get(), "application/json; charset=utf-8", Cache_Control="no-cache")
                return
            if path in ASSETS:
                self._reply(HTTPStatus.OK, _static_page(path.lstrip("/")), ASSETS[path], Cache_Control="no-cache")
                return
            if path == SITE_INDEX_PATH:
                self._reply(HTTPStatus.OK, site_source.index(), "application/json; charset=utf-8", Cache_Control=SITE_CACHE_CONTROL)
                return
            match = DATASET_PATH_RE.match(path)
            if match:
                body = site_source.dataset(match.group(1))
                if body is None:
                    self._text(HTTPStatus.NOT_FOUND)
                else:
                    self._reply(HTTPStatus.OK, body, "application/json; charset=utf-8", Cache_Control=SITE_CACHE_CONTROL)
                return
            if path in STATIC_PAGES:
                self._reply(HTTPStatus.OK, _static_page(STATIC_PAGES[path]), "text/html; charset=utf-8", Cache_Control=PAGE_CACHE_CONTROL)
                return
            if path == OLD_RESULTS_PATH:
                self._text(HTTPStatus.MOVED_PERMANENTLY, Location=RESULTS_PATH)
                return
            self._text(HTTPStatus.NOT_FOUND)

        def _not_allowed(self) -> None:
            self._text(HTTPStatus.METHOD_NOT_ALLOWED, Allow="GET, HEAD")

        do_GET = do_HEAD = _get  # noqa: N815 — 基类按方法名分派
        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed  # noqa: N815

    class EvaluationServer(ThreadingHTTPServer):
        request_queue_size = 64
        daemon_threads = True

    return EvaluationServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agentruntime_learning.evals.dashboard_server", description="动态评测网站")
    parser.add_argument("--dir", type=Path, required=True, help="公开报告归档目录（reports.json，兼容旧版 index.html）")
    parser.add_argument("--experiments-dir", type=Path, default=os.environ.get(EXPERIMENTS_DIR_ENV) or None, help="实时实验产物只读目录；自动发现并刷新公开汇总")
    parser.add_argument("--benchmarks-dir", type=Path, default=os.environ.get(BENCHMARKS_DIR_ENV) or None, help="SWE-bench 真实运行目录；自动读取清单和判分汇总")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 — 在 Pod 里监听，入站由 NetworkPolicy 限定
    parser.add_argument("--metrics-url", default=os.environ.get(METRICS_URL_ENV, ""), help=f"VictoriaMetrics 的地址（默认取 {METRICS_URL_ENV}）")
    parser.add_argument("--site-repo", default=os.environ.get(SITE_REPO_ENV, ""), help=f"取 evalsets 的仓库 owner/name（默认取 {SITE_REPO_ENV}）")
    parser.add_argument("--site-ref", default=os.environ.get(SITE_REF_ENV, "main"))
    parser.add_argument("--site-token-file", default=os.environ.get(SITE_TOKEN_FILE_ENV, ""), help="只读令牌的文件（可以没有）")
    parser.add_argument("--site-cache-dir", type=Path, default=Path(os.environ.get(SITE_CACHE_DIR_ENV, "/tmp/eval-site")))  # noqa: S108
    parser.add_argument("--site-dir", type=Path, default=None, help="本机：直接读这个 evalsets 目录，不联网")
    args = parser.parse_args(argv)
    site = _site_from_args(args)
    with make_server(args.dir, args.host, args.port, live=LiveSource(args.metrics_url), site=site, experiments_dir=args.experiments_dir, benchmarks_dir=args.benchmarks_dir) as server:
        server.serve_forever()
    return 0


def _site_from_args(args: argparse.Namespace) -> SiteSync | None:
    if args.site_dir is not None:
        return SiteSync.local(args.site_dir)
    if not args.site_repo:
        return None
    token = None
    if args.site_token_file:
        try:
            token = Path(args.site_token_file).read_text("utf-8").strip() or None
        except OSError:
            token = None  # 公开仓库不要令牌；Secret 没挂上时照样能取公开仓库
    sync = SiteSync(repo=args.site_repo, ref=args.site_ref, token=token, cache_dir=args.site_cache_dir)

    def loop() -> None:
        while True:
            sync.refresh()
            time.sleep(15)

    threading.Thread(target=loop, name="eval-site-sync", daemon=True).start()
    return sync


if __name__ == "__main__":
    sys.exit(main())
