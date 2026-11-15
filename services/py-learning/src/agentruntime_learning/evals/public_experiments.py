"""Read-only, allowlisted projection of evaluation artifacts for the public site.

Only allowlisted aggregate formats and fixed execution-record sources are read.
Raw source passages, question/answer content and model ledgers are never exposed. File updates are observed at a short TTL.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import statistics
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentruntime_learning.evals.public_longbench import project_longbench_summary
from agentruntime_learning.evals.public_context_optimization import project_context_optimization
from agentruntime_learning.evals.public_context_comparison import project_context_comparison
from agentruntime_learning.evals.live_context_comparison import build_live_cohort
from agentruntime_learning.evals.live_context_focus import build_live_focus, PROTOCOL as FOCUS_PROTOCOL

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_RUN_DIRECTORIES = 128
RUN_DIRECTORY = re.compile(r"^(longmemeval-recovered-|longmemeval-final-|n16-evreal-pinned-|paired-final-|memory-bm25-full-|longbench-context-)([0-9]{8}[a-zA-Z0-9_-]*)$")
MEMORY_FILES = ("lexical-summary.json", "live-summary.json", "comparison.json", "final-acceptance.json")
PAIRED_FILES = ("paired-completion-final.json", "analysis-notes.json", "source-provenance.json")


def _finite_public_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _bool(value: Any) -> bool | None:
    return value if type(value) is bool else None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _gate_text(value: bool | None, zh: bool) -> str:
    return ("已通过" if value else "未通过") if zh and value is not None else ("passed" if value else "did not pass") if value is not None else "尚未确认" if zh else "is unconfirmed"


def _cost_projection(flag: Any, actual: Any, known: Any) -> tuple[bool | None, float | None, float | None, str, str]:
    cost_known = _bool(flag)
    total, lower = _finite_public_number(actual), _finite_public_number(known)
    if total is not None and total < 0:
        total = None
    if lower is not None and lower < 0:
        lower = None
    if cost_known is True and total is None:
        cost_known = None
    if cost_known is not True:
        total = None
    if cost_known is True:
        zh, en = "费用已完整结算。", "Actual cost is fully settled."
    elif cost_known is False:
        zh, en = "费用尚未完整结算；已知费用仅为下界，不是最终实际总额。", "Cost is not fully settled; known cost is a lower bound, not the final actual total."
    else:
        zh, en = "费用完整性尚未确认，实际总额未知。", "Cost completeness is unconfirmed and the actual total is unknown."
    return cost_known, total, lower, zh, en


def _project_documents(raw_documents: dict[str, bytes], narrative: str | None = None, *,
                       memory_identity: str = "longmemeval-20261107",
                       paired_identity: str = "paired-policy-20261107") -> list[dict[str, Any]]:
    """Project each run's own facts; directory aliases below are internal adapter keys only.

    Unknown validity, acceptance and accounting are represented by None, not a failed or
    passing result. The audited historical judge problem belongs only to its actual run ID
    or to explicit analysis/provenance evidence identifying that problem.
    """
    experiments: list[dict[str, Any]] = []

    def documents(directory: str, names: tuple[str, ...], identity: str) -> tuple[list[dict[str, Any]], str] | None:
        digest = hashlib.sha256(identity.encode() + b"\0")
        values = []
        try:
            for name in names:
                raw = raw_documents[directory + "/" + name]
                if len(raw) > MAX_DOCUMENT_BYTES:
                    raise ValueError("experiment summary too large")
                item = json.loads(raw)
                if not isinstance(item, dict):
                    raise ValueError("invalid experiment summary")
                values.append(item)
                digest.update(name.encode() + b"\0" + raw + b"\0")
        except (OSError, ValueError, KeyError):
            return None
        return values, digest.hexdigest()

    def metric(key: str, zh: str, en: str, baseline: Any, candidate: Any, unit: str = "rate") -> dict[str, Any]:
        return {"key": key, "label_zh": zh, "label_en": en,
                "baseline": _finite_public_number(baseline), "candidate": _finite_public_number(candidate), "unit": unit}

    memory = documents("longmemeval-final-20261107", MEMORY_FILES, memory_identity)
    if memory:
        (baseline, candidate, comparison, acceptance), digest = memory
        if not isinstance(baseline.get("session", {}), dict) or not isinstance(candidate.get("session", {}), dict):
            raise ValueError("invalid retrieval metric structure")
        count, completed = candidate.get("cohort_questions", candidate.get("selected")), candidate.get("completed")
        if type(count) is int and count > 0 and type(completed) is int and completed == count and comparison.get("questions") == count:
            quality_valid = _bool(candidate.get("valid_for_quality_gate"))
            if quality_valid is None:
                quality_valid = _bool(_mapping(acceptance.get("validation")).get("quality_valid"))
            gate = _bool(comparison.get("passed"))
            final_gate = _bool(acceptance.get("passed"))
            if gate is False:
                final_gate = False
            flag = acceptance.get("cost_known", candidate.get("cost_known"))
            cost_known, actual_cost, known_cost, cost_zh, cost_en = _cost_projection(flag, candidate.get("actual_cost_usd"), candidate.get("known_experiment_cost_usd"))
            limitations_zh = [f"固定 {count} 题完整采集；质量门槛{_gate_text(gate, True)}。"]
            limitations_en = [f"All {count} fixed questions were collected; the quality gate {_gate_text(gate, False)}."]
            gain, probability = _finite_public_number(comparison.get("recall_any_at_10_gain")), _finite_public_number(comparison.get("exact_one_sided_p"))
            if gain is not None or probability is not None:
                limitations_zh.append(f"召回率 @10 增益 {gain if gain is not None else '未知'}；单侧精确 McNemar p={probability if probability is not None else '未知'}。")
                limitations_en.append(f"Recall @10 gain: {gain if gain is not None else 'unknown'}; one-sided exact McNemar p={probability if probability is not None else 'unknown'}.")
            limitations_zh.append(cost_zh); limitations_en.append(cost_en)
            metrics = [metric(key, zh, en, _mapping(baseline.get("session")).get(key), _mapping(candidate.get("session")).get(key), "score" if key.startswith("ndcg") else "rate")
                       for key, zh, en in (("recall_any@5", "会话召回率 @5", "Session recall @5"),
                                           ("recall_any@10", "会话召回率 @10", "Session recall @10"),
                                           ("recall_any@30", "会话召回率 @30", "Session recall @30"),
                                           ("ndcg_any@10", "会话 NDCG @10", "Session NDCG @10"))]
            experiments.append({
                "id": memory_identity, "kind": "retrieval-comparison", "title_zh": "LongMemEval 记忆检索", "title_en": "LongMemEval memory retrieval",
                "completed_at": candidate.get("created_at") if isinstance(candidate.get("created_at"), str) else None,
                "sample_count": count, "completed_count": completed,
                "collection_complete": _bool(comparison.get("integrity_passed")), "quality_valid": quality_valid,
                "acceptance_passed": final_gate, "cost_known": cost_known,
                "known_cost_usd": known_cost, "actual_cost_usd": actual_cost,
                "baseline_label": "Lexical", "candidate_label": "Memory Service", "metrics": metrics,
                "limitations_zh": limitations_zh, "limitations_en": limitations_en, "source_sha256": digest,
            })

    paired = documents("paired-final-20261107", PAIRED_FILES, paired_identity)
    if paired:
        (report, analysis, provenance), digest = paired
        pairs = report.get("pairs")
        if isinstance(pairs, list) and pairs and report.get("collection_complete") is True and all(isinstance(row, dict) and isinstance(row.get("off"), dict) and isinstance(row.get("rules"), dict) for row in pairs):
            def aggregate(arm: str, key: str, median: bool = False) -> float | None:
                values = [_finite_public_number(row[arm].get(key)) for row in pairs]
                if not all(value is not None for value in values):
                    return None
                return statistics.median(values) if median else sum(values)

            def aborted(arm: str) -> int:
                return sum(str(row[arm].get("task_status", "")).lower() in {"aborted", "task_status_aborted"} for row in pairs)

            legacy_problem = paired_identity == "paired-policy-20261107" or report.get("run_id") == "n16-evreal-pinned-20261107" or _mapping(provenance.get("semantic_judge")).get("run_id") == "n16-evreal-pinned-20261107-semantic-judge"
            documented_problem = analysis.get("judge_condition_misapplied") is True or (analysis.get("kind") == "paired-final-analysis-limitations-v1" and isinstance(analysis.get("finding"), str) and bool(analysis["finding"].strip()))
            quality_valid = _bool(report.get("quality_valid"))
            for validation in (report.get("validation"), analysis.get("validation"), provenance.get("validation")):
                if quality_valid is None:
                    quality_valid = _bool(_mapping(validation).get("quality_valid"))
            if legacy_problem or documented_problem:
                quality_valid = False
            n16, ev = _bool(_mapping(report.get("n16_r")).get("passed")), _bool(_mapping(report.get("ev_real")).get("passed"))
            final_gate = _bool(report.get("acceptance_passed"))
            if n16 is False or ev is False:
                final_gate = False
            elif final_gate is None and n16 is True and ev is True:
                final_gate = True
            cost_known, actual_cost, known_cost, cost_zh, cost_en = _cost_projection(report.get("cost_known"), report.get("actual_complete_total_usd"), report.get("known_fixed_cohort_usd"))
            limitations_zh = [f"{len(pairs)} 题 × 2 策略，共 {len(pairs) * 2} 次执行记录已完整采集；N16-R {_gate_text(n16, True)}，EV-REAL {_gate_text(ev, True)}。"]
            limitations_en = [f"All {len(pairs) * 2} execution records from {len(pairs)} paired cases were collected; N16-R {_gate_text(n16, False)}, EV-REAL {_gate_text(ev, False)}."]
            if legacy_problem or documented_problem:
                limitations_zh.append("审计记录了裁判条件适用问题；原始裁判均值不作为已验证能力分展示。")
                limitations_en.append("The audit records a judge applicability problem; raw judge means are not presented as validated capability scores.")
            elif quality_valid is False:
                limitations_zh.append("本次评分有效性未通过验证。")
                limitations_en.append("The validity of these scores failed validation.")
            elif quality_valid is None:
                limitations_zh.append("本次评分有效性尚未确认。")
                limitations_en.append("The validity of these scores has not been confirmed.")
            failures = report.get("judge_raw_schema_failures")
            if type(failures) is int and failures > 0:
                limitations_zh.append(f"{failures} 份原始评分未通过 schema 校验，原始失败记录保留。")
                limitations_en.append(f"{failures} raw judgments failed schema validation; those failures remain recorded.")
            limitations_zh.append("结构条件按各自策略判断，不应将通过率直接解释为同一任务质量指标。")
            limitations_en.append("Structure requirements are evaluated per policy; their pass rates are not a shared task-quality measure.")
            if aborted("off") or aborted("rules"):
                limitations_zh.append(f"以 aborted 结束的执行：Off {aborted('off')} 次，Rules {aborted('rules')} 次。")
                limitations_en.append(f"Executions ending aborted: Off {aborted('off')}, Rules {aborted('rules')}.")
            limitations_zh.append(cost_zh); limitations_en.append(cost_en)
            metrics = [
                metric("planning_structure_pass_rate", "各策略自身结构条件通过率", "Policy-specific structure pass rate", _mapping(report.get("planning_structure_pass_rate")).get("off"), _mapping(report.get("planning_structure_pass_rate")).get("rules")),
                metric("exact_repeated_tools", "重复工具请求数", "Repeated tool requests", aggregate("off", "exact_repeated_tools"), aggregate("rules", "exact_repeated_tools"), "number"),
                metric("extra_rounds", "额外轮次", "Extra rounds", aggregate("off", "extra_rounds"), aggregate("rules", "extra_rounds"), "number"),
                metric("elapsed_median", "任务耗时中位数", "Median task duration", aggregate("off", "elapsed_s", True), aggregate("rules", "elapsed_s", True), "seconds"),
            ]
            evaluated_on = report.get("evaluated_on") if isinstance(report.get("evaluated_on"), str) else None
            reported_on = report.get("reported_on") if isinstance(report.get("reported_on"), str) else None
            if paired_identity == "paired-policy-20261107":
                dates = re.search(r"N16-R / EV-REAL 最终配对交付（(\d{4}-\d{2}-\d{2}) 实验，(\d{4}-\d{2}-\d{2}) 汇总）", narrative or "")
                if dates:
                    evaluated_on, reported_on = dates.groups()
            experiments.append({
                "id": paired_identity, "kind": "paired-policy", "title_zh": "多智能体策略配对实验", "title_en": "Paired multi-agent policy evaluation",
                "completed_at": report.get("completed_at") if isinstance(report.get("completed_at"), str) else None,
                "evaluated_on": evaluated_on, "reported_on": reported_on,
                "sample_count": len(pairs) * 2, "completed_count": len(pairs) * 2, "collection_complete": True,
                "quality_valid": quality_valid, "acceptance_passed": final_gate,
                "cost_known": cost_known, "known_cost_usd": known_cost, "actual_cost_usd": actual_cost,
                "baseline_label": "Off", "candidate_label": "Rules", "metrics": metrics,
                "limitations_zh": limitations_zh, "limitations_en": limitations_en, "source_sha256": digest,
            })
    return experiments


def collect_public_experiments(root: Path) -> list[dict[str, Any]]:
    """Offline export of the checked-in, reviewed summaries (the serving path uses artifacts)."""
    raw: dict[str, bytes] = {}
    for directory, names in (("longmemeval-final-20261107", MEMORY_FILES), ("paired-final-20261107", PAIRED_FILES)):
        for name in names:
            try:
                data = (root / directory / name).read_bytes()
                if len(data) <= MAX_DOCUMENT_BYTES:
                    raw[directory + "/" + name] = data
            except OSError:
                pass
    try:
        narrative = (root.parent / "goal-evaluation-2026-11-06.md").read_text("utf-8")
    except OSError:
        narrative = None
    return _project_documents(raw, narrative)


class PublicExperimentSource:
    """Discover completed artifact bundles without copying/publishing HTML or restarting.

    The caller mounts the experiment root read-only. Every directory and file open rejects
    symlinks. Individual malformed or temporarily missing bundles retain their last good
    public projection; newly incomplete bundles are never presented as successful runs.
    """
    def __init__(self, root: Path, *, ttl_s: float = 5.0) -> None:
        self.root = Path(root)
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._last_read = float("-inf")
        self._good: dict[str, dict[str, Any]] = {}
        self._cached: dict[str, Any] | None = None

    def _read(self, relative: str) -> bytes:
        return self._read_meta(relative)[0]

    def _read_meta(self, relative: str) -> tuple[bytes, float]:
        parts = Path(relative).parts
        if not parts or any(part in ("", ".", "..") for part in parts) or Path(relative).is_absolute():
            raise ValueError("invalid experiment path")
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            with os.fdopen(file_fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DOCUMENT_BYTES:
                    raise ValueError("invalid experiment file")
                data = stream.read(MAX_DOCUMENT_BYTES + 1)
                if len(data) > MAX_DOCUMENT_BYTES:
                    raise ValueError("experiment file too large")
                return data, info.st_mtime
        finally:
            os.close(fd)

    def _names(self, relative: str) -> list[str]:
        # Directory inventory is used only for the fixed v3 accounting source.
        parts = Path(relative).parts
        if not parts or any(part in ("", ".", "..") for part in parts) or Path(relative).is_absolute():
            raise ValueError("invalid experiment directory")
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = child
            entries = os.listdir(fd)
            if len(entries) > 2048:
                raise ValueError("too many experiment usage directories")
            return entries
        finally:
            os.close(fd)

    def _first(self, alternatives: list[str]) -> bytes:
        for name in alternatives:
            try:
                return self._read(name)
            except FileNotFoundError:
                continue
        raise FileNotFoundError("experiment summary missing")

    def _bundle(self, name: str, prefix: str, suffix: str) -> dict[str, Any] | None:
        raw: dict[str, bytes] = {}
        if prefix == "longbench-context-":
            summary = self._read(name + "/public-summary.json")
            document = json.loads(summary)
            if not isinstance(document, dict):
                raise ValueError("invalid LongBench summary document")
            schema = document.get("schema")
            if schema == "runtime-40-comparison-v1":
                observed_mtimes: list[float] = []
                def read_source(relative: str) -> bytes:
                    data, mtime = self._read_meta(relative)
                    observed_mtimes.append(mtime)
                    return data
                live_document = build_live_cohort(read_source, document)
                if observed_mtimes:
                    live_document["updated_at"] = datetime.fromtimestamp(max(observed_mtimes), UTC).isoformat()
                projected = project_context_comparison(json.dumps(live_document, ensure_ascii=False, allow_nan=False).encode())
                projected.update(source_mode="runtime_records", data_updated_at=live_document["updated_at"],
                                 checked_at=datetime.now(UTC).isoformat(), source_revision=live_document["source_revision"])
            elif schema == "longbench-context-optimization-v1" and document.get("protocol") == FOCUS_PROTOCOL:
                observed_mtimes = []
                record_mtimes = []
                def read_focus_source(relative: str) -> bytes:
                    data, mtime = self._read_meta(relative)
                    observed_mtimes.append(mtime)
                    if "/records/validation/" in relative:
                        record_mtimes.append(mtime)
                    return data
                live_document = build_live_focus(read_focus_source, self._names, document)
                stamp = datetime.fromtimestamp(max(observed_mtimes), UTC).isoformat()
                live_document["updated_at"] = stamp
                live_document["completed_at"] = datetime.fromtimestamp(max(record_mtimes), UTC).isoformat() if live_document["collection_complete"] else None
                projected = project_context_optimization(json.dumps(live_document, ensure_ascii=False, allow_nan=False).encode())
                projected.update(source_mode="runtime_records", data_updated_at=stamp,
                                 checked_at=datetime.now(UTC).isoformat(), source_revision=live_document["source_revision"])
            else:
                projected = (project_context_optimization(summary) if schema == "longbench-context-optimization-v1"
                             else project_longbench_summary(summary))
            if projected is not None and projected["id"] != name:
                raise ValueError("LongBench summary identity does not match directory")
            return projected
        if prefix == "memory-bm25-full-":
            data = self._read(name + "/public-summary.json")
            report = json.loads(data)
            if not isinstance(report, dict) or report.get("schema") != "memory-rerank-comparison-v1":
                raise ValueError("invalid reranking summary")
            count = report.get("sample_count")
            if type(count) is not int or count <= 0 or type(report.get("completed_count")) is not int or report.get("completed_count") != count or report.get("complete") is not True:
                return None
            if report.get("candidate_pool") != 50 or report.get("storage_backend") != "isolated-sqlite-inmemory":
                raise ValueError("unknown reranking configuration")
            timestamp = report.get("completed_at")
            if not isinstance(timestamp, str) or datetime.fromisoformat(timestamp.replace("Z", "+00:00")).tzinfo is None:
                raise ValueError("missing completion timestamp")
            metrics = []
            for key, zh, en in (("recall_any@5", "会话召回率 @5", "Session recall @5"), ("recall_any@10", "会话召回率 @10", "Session recall @10"), ("ndcg_any@10", "会话 NDCG @10", "Session NDCG @10")):
                values = [_finite_public_number(_mapping(report.get(arm)).get(key)) for arm in ("baseline", "candidate")]
                if any(value is None or not 0 <= value <= 1 for value in values):
                    raise ValueError("invalid reranking metric")
                metrics.append({"key": key, "label_zh": zh, "label_en": en, "baseline": values[0], "candidate": values[1], "unit": "score" if key.startswith("ndcg") else "rate"})
            cost_known, actual, known, _, _ = _cost_projection(report.get("cost_known"), report.get("actual_cost_usd"), report.get("known_cost_usd"))
            return {"id": "memory-bm25-" + suffix, "kind": "retrieval-comparison",
                    "title_zh": "LongMemEval 排序对照 · 50 候选", "title_en": "LongMemEval reranking · 50 candidates",
                    "completed_at": timestamp, "sample_count": count, "completed_count": count,
                    "collection_complete": True, "quality_valid": _bool(report.get("quality_valid")), "acceptance_passed": None,
                    "cost_known": cost_known, "actual_cost_usd": actual, "known_cost_usd": known,
                    "baseline_label": "Legacy", "candidate_label": "BM25", "metrics": metrics,
                    "limitations_zh": ["相同真实嵌入和候选池的配对对照，使用隔离 SQLite 与内存向量库；未改动原题及评分。", "此表展示 50 候选排序结果；线上 3/5 条、600 token 的检索配置另行验证。"],
                    "limitations_en": ["Paired comparison using the same real embeddings and candidate pool in isolated SQLite and in-memory vector storage; questions and scoring are unchanged.", "This table evaluates 50 candidates; production requests for 3/5 items with a 600-token limit are evaluated separately."],
                    "source_sha256": hashlib.sha256(data).hexdigest()}
        if prefix.startswith("longmemeval"):
            canonical = "longmemeval-final-20261107/"
            candidate = self._first([name + "/recovery-summary.json", name + "/live-summary.json", name + "/live/summary.json"])
            raw[canonical + "live-summary.json"] = candidate
            raw[canonical + "lexical-summary.json"] = self._first([
                name + "/lexical-summary.json", name + "/lexical-fixed/summary.json", name + "/lexical/summary.json",
                "longmemeval-livecheck-" + suffix + "/lexical-fixed/summary.json",
            ])
            for file in ("comparison.json", "final-acceptance.json"):
                raw[canonical + file] = self._read(name + "/" + file)
            projected = _project_documents(raw, memory_identity="longmemeval-" + suffix)
            if not projected:
                return None
            row = projected[0]
            row["id"] = "longmemeval-" + suffix
            return row
        canonical = "paired-final-20261107/"
        raw[canonical + "paired-completion-final.json"] = self._read(name + "/paired-completion-final.json")
        # Supplemental provenance affects the digest only, and is never sent to clients.
        for canonical_name, alternatives in (
            ("analysis-notes.json", [name + "/analysis-notes.json", name + "/analysis-revision.json"]),
            ("source-provenance.json", [name + "/source-provenance.json", name + "/source-receipt.json"]),
        ):
            try:
                raw[canonical + canonical_name] = self._first(alternatives)
            except FileNotFoundError:
                raw[canonical + canonical_name] = b"{}"
        projected = _project_documents(raw, paired_identity="paired-policy-" + suffix)
        if not projected:
            return None
        row = projected[0]
        row["id"] = "paired-policy-" + suffix
        # Newer producers may include actual timestamps; never infer them from mtime or name.
        report = json.loads(raw[canonical + "paired-completion-final.json"])
        for field in ("completed_at", "evaluated_on", "reported_on"):
            if isinstance(report.get(field), str):
                row[field] = report[field]
        return row

    def get(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if self._cached is not None and now - self._last_read < self.ttl_s:
                return self._cached
            errors: set[str] = set()
            seen: set[str] = set()
            try:
                if self.root.is_symlink():
                    raise ValueError("symlink root")
                with os.scandir(self.root) as entries:
                    candidates = []
                    for entry in entries:
                        match = RUN_DIRECTORY.fullmatch(entry.name)
                        if match:
                            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                                errors.add("experiment_unsafe_path")
                                continue
                            candidates.append((entry.name, *match.groups()))
                            if len(candidates) > MAX_RUN_DIRECTORIES:
                                raise ValueError("too many experiment directories")
                for name, prefix, suffix in sorted(candidates):
                    identity = ("longmemeval-" if prefix.startswith("longmemeval") else "memory-bm25-" if prefix == "memory-bm25-full-" else "longbench-context-" if prefix == "longbench-context-" else "paired-policy-") + suffix
                    seen.add(identity)
                    try:
                        row = self._bundle(name, prefix, suffix)
                        if row is None:
                            if identity in self._good:
                                errors.add("experiment_incomplete")
                            continue
                        # Check that the projection contains only finite JSON before retaining it.
                        json.dumps(row, allow_nan=False)
                        self._good[identity] = row
                    except FileNotFoundError:
                        if identity in self._good:
                            errors.add("experiment_missing")
                    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
                        errors.add("experiment_invalid")
                if set(self._good) - seen:
                    errors.add("experiment_missing")
            except (OSError, ValueError):
                errors.add("experiments_unavailable")
            self._last_read = now
            self._cached = {
                "items": list(self._good.values()), "source": "filesystem", "stale": bool(errors),
                "status": "stale" if errors and self._good else "unavailable" if errors else "ready",
                "checked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "errors": sorted(errors),
            }
            return self._cached
