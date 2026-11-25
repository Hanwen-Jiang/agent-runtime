"""OTel/console trace failure clustering for Wave 2 self-evolution (MAT-LEARN-EVOL)."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from agentruntime_learning.pipeline.traces import SpanRecord, read_spans

CLUSTER_SKILL_MAP: dict[str, tuple[str, str]] = {
    "readonly_churn": ("explore-repo", "excessive read-only steps / read_only_reminder triggered before targeted edits"),
    "edit_or_patch_conflict": ("coding-engineering", "frequent repo_edit/apply_patch match conflicts or blind edits without line inspection"),
    "unverified_completion": ("bugfix-tdd", "reviewer blocked completion for missing post-edit verification or unresolved test failures"),
    "undelivered_artifact": ("deliver-files", "reviewer blocked completion because expected deliverable was not submitted via deliver_file"),
    "doc_or_mermaid_defect": ("docs-architecture", "documentation check failed on unquoted Mermaid labels, HTML tags, or unverified paths"),
}


@dataclass(frozen=True)
class FailureCluster:
    cluster_kind: str
    target_skill: str
    support: int
    task_families: tuple[str, ...]
    task_ids: tuple[str, ...]
    sample_metrics: dict[str, Any]
    diagnosis: str

    def to_json(self) -> dict[str, Any]:
        return {
            "cluster_kind": self.cluster_kind,
            "target_skill": self.target_skill,
            "support": self.support,
            "task_families": list(self.task_families),
            "task_ids": list(self.task_ids),
            "sample_metrics": self.sample_metrics,
            "diagnosis": self.diagnosis,
        }


def _task_family(span: SpanRecord) -> str:
    return str(span.attributes.get("runtime.task_family") or span.attributes.get("task_family") or "general.coding")


def _task_id(span: SpanRecord) -> str:
    return str(span.attributes.get("task_id") or span.attributes.get("runtime.task_id") or span.trace_id or span.span_id)


def _as_list(val: Any) -> list[str]:
    if isinstance(val, str):
        return [x.strip() for x in val.split(",") if x.strip()]
    if isinstance(val, (list, tuple)):
        return [str(x) for x in val if x]
    return []


def cluster_failures(
    lines: Iterable[str],
    *,
    fmt: str = "console",
    min_support: int = 2,
) -> list[FailureCluster]:
    """Cluster structural OTel/console trace anomalies into actionable Skill improvement targets.

    Reads only span structure, tool names, error codes, and reviewer verdict reasons; never
    captures prompt text, credentials, or tenant user payloads.
    """
    spans = read_spans(lines, fmt=fmt)
    by_kind_tasks: dict[str, set[str]] = defaultdict(set)
    by_kind_families: dict[str, set[str]] = defaultdict(set)
    by_kind_details: dict[str, Counter[str]] = defaultdict(Counter)

    for span in spans:
        attrs: Mapping[str, Any] = span.attributes
        tid = _task_id(span)
        fam = _task_family(span)

        floor_reasons = set(_as_list(attrs.get("floor_reasons") or attrs.get("runtime.floor_reasons")))
        signals = set(_as_list(attrs.get("signals") or attrs.get("runtime.signals")))
        error_code = str(attrs.get("error.code") or attrs.get("tool.error_code") or "")
        tool_name = str(attrs.get("gen_ai.tool.name") or attrs.get("tool.name") or "")
        tool_status = str(attrs.get("tool.status") or attrs.get("status") or "")
        read_reminder = bool(attrs.get("read_only_reminder") or attrs.get("runtime.read_only_reminder"))

        if read_reminder or "excessive_reads" in signals:
            by_kind_tasks["readonly_churn"].add(tid)
            by_kind_families["readonly_churn"].add(fam)
            by_kind_details["readonly_churn"]["read_only_reminder"] += 1

        if error_code in ("edit_no_match", "edit_ambiguous", "patch_conflict") or (
            tool_name in ("repo_edit", "apply_patch") and tool_status == "error"
        ):
            by_kind_tasks["edit_or_patch_conflict"].add(tid)
            by_kind_families["edit_or_patch_conflict"].add(fam)
            by_kind_details["edit_or_patch_conflict"][error_code or tool_name] += 1

        if "rule_done_unverified" in floor_reasons or error_code == "rule_done_unverified":
            by_kind_tasks["unverified_completion"].add(tid)
            by_kind_families["unverified_completion"].add(fam)
            by_kind_details["unverified_completion"]["rule_done_unverified"] += 1

        if "rule_done_undelivered" in floor_reasons or error_code == "rule_done_undelivered":
            by_kind_tasks["undelivered_artifact"].add(tid)
            by_kind_families["undelivered_artifact"].add(fam)
            by_kind_details["undelivered_artifact"]["rule_done_undelivered"] += 1

        if error_code in ("mermaid_syntax_error", "doc_check_failed") or "doc_defect" in signals:
            by_kind_tasks["doc_or_mermaid_defect"].add(tid)
            by_kind_families["doc_or_mermaid_defect"].add(fam)
            by_kind_details["doc_or_mermaid_defect"][error_code or "doc_defect"] += 1

    clusters: list[FailureCluster] = []
    for kind, task_ids in by_kind_tasks.items():
        if len(task_ids) < min_support:
            continue
        target_skill, diagnosis = CLUSTER_SKILL_MAP[kind]
        clusters.append(
            FailureCluster(
                cluster_kind=kind,
                target_skill=target_skill,
                support=len(task_ids),
                task_families=tuple(sorted(by_kind_families[kind])),
                task_ids=tuple(sorted(task_ids)),
                sample_metrics=dict(sorted(by_kind_details[kind].items())),
                diagnosis=diagnosis,
            )
        )
    clusters.sort(key=lambda c: (-c.support, c.cluster_kind))
    return clusters
