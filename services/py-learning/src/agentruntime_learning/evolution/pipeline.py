"""End-to-end Wave 2 self-evolution cycle orchestrator (MAT-LEARN-EVOL)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentruntime_core.providers.telemetry import LogLevel, TelemetryProvider

from agentruntime_learning.evolution.clustering import FailureCluster, cluster_failures
from agentruntime_learning.evolution.isolation import EVOLUTION_PLATFORM_PURPOSE, IsolatedEvolutionRegistry, SYSTEM_EVOLUTION_TENANT
from agentruntime_learning.evolution.matrix import SkillMatrixReport, SkillMatrixRunner, SkillVariant, write_matrix_reports
from agentruntime_learning.evolution.proposer import SkillProposal, propose_skill_patch
from agentruntime_learning.evolution.store import CandidateRecord, EvolutionStore

SKILL_SUITE_MAP: dict[str, tuple[str, ...]] = {
    "explore-repo": ("swe_engineering",),
    "coding-engineering": ("swe_engineering",),
    "bugfix-tdd": ("swe_engineering",),
    "docs-architecture": ("doc_architecture",),
    "deliver-files": ("doc_architecture",),
}


@dataclass(frozen=True)
class EvolutionCycleResult:
    status: str  # "pending_approval" | "rejected_by_gate" | "no_clusters" | "no_patch_needed"
    clusters: tuple[FailureCluster, ...]
    proposal: SkillProposal | None
    matrix_report: SkillMatrixReport | None
    candidate: CandidateRecord | None
    report_json_path: Path | None
    report_md_path: Path | None

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "tenant": SYSTEM_EVOLUTION_TENANT,
            "clusters": len(self.clusters),
            "target_skill": self.proposal.target_name if self.proposal else None,
            "candidate_id": self.candidate.candidate_id if self.candidate else None,
            "staged_skill_name": self.candidate.staged_skill_name if self.candidate else None,
            "gate_passed": self.matrix_report.gate.get("passed") if self.matrix_report else None,
            "report_json": str(self.report_json_path) if self.report_json_path else None,
            "report_md": str(self.report_md_path) if self.report_md_path else None,
        }


def _render_evolution_markdown(result_summary: Mapping[str, Any], clusters: Sequence[FailureCluster], proposal: SkillProposal | None, matrix: SkillMatrixReport | None) -> str:
    lines = [
        "# Wave 2 Self-Evolution Cycle Report",
        "",
        f"- Status: `{result_summary['status']}`",
        f"- Isolated Tenant: `{SYSTEM_EVOLUTION_TENANT}`",
        f"- Candidate ID: `{result_summary['candidate_id']}`",
        f"- Staged Registry Skill: `{result_summary['staged_skill_name']}`",
        f"- Matrix Gate Passed: `{result_summary['gate_passed']}`",
        "",
        "## 1. Failure Clusters",
        "",
    ]
    if not clusters:
        lines.append("- None met `min_support`.")
    else:
        for c in clusters:
            lines.append(f"- **{c.cluster_kind}** -> `{c.target_skill}` (support={c.support}, families={list(c.task_families)}): {c.diagnosis}")
    if proposal is not None:
        lines += [
            "",
            f"## 2. Proposed Diff (`{proposal.target_name}`)",
            "",
            f"- Rationale: {proposal.rationale}",
            "",
            "```diff",
            proposal.unified_diff.rstrip(),
            "```",
        ]
    if matrix is not None:
        gate = matrix.gate
        lines += [
            "",
            "## 3. Baseline vs Candidate Matrix Gate",
            "",
            f"- Mode: `{matrix.mode}` (`is_offline_fixture={matrix.is_offline_fixture}`)",
            f"- Pass rate: baseline `{gate.get('baseline_pass_rate')}` -> candidate `{gate.get('candidate_pass_rate')}` (delta `{gate.get('pass_rate_delta')}`)",
            f"- Blind read ratio: `{gate.get('baseline_blind_read_ratio')}` -> `{gate.get('candidate_blind_read_ratio')}`",
            f"- Tool error rate: `{gate.get('baseline_tool_error_rate')}` -> `{gate.get('candidate_tool_error_rate')}`",
        ]
        if gate.get("reasons"):
            for r in gate["reasons"]:
                lines.append(f"- Gate rejection reason: {r}")
    return "\n".join(lines) + "\n"


async def run_evolution_cycle(
    *,
    lines: Iterable[str],
    fmt: str = "console",
    skills_by_name: Mapping[str, SkillVariant],
    out_dir: Path,
    store: EvolutionStore | None = None,
    registry: Any | None = None,
    telemetry: TelemetryProvider | None = None,
    gateway: Any | None = None,
    model_tiers: Sequence[str] = ("strong@medium", "mid@medium"),
    mode: str = "fixture",
    min_support: int = 2,
    budget_usd: float = 0.0,
    extra_guardrails: Mapping[str, tuple[str, ...]] | None = None,
    now: datetime | None = None,
) -> EvolutionCycleResult:
    """Cluster failure traces -> draft diff -> stage in sys-evolution -> run matrix -> store candidate."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    clusters = tuple(cluster_failures(lines, fmt=fmt, min_support=min_support))
    if telemetry is not None:
        await telemetry.bind_platform(EVOLUTION_PLATFORM_PURPOSE).log(
            LogLevel.INFO,
            "evolution.cluster_complete",
            attributes={"evolution.tenant": SYSTEM_EVOLUTION_TENANT, "evolution.clusters": len(clusters)},
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    if not clusters:
        summary = {"status": "no_clusters", "tenant": SYSTEM_EVOLUTION_TENANT, "clusters": 0, "candidate_id": None, "staged_skill_name": None, "gate_passed": None}
        jp, mp = _write_cycle_files(out_dir, summary, clusters, None, None)
        return EvolutionCycleResult("no_clusters", clusters, None, None, None, jp, mp)

    proposal: SkillProposal | None = None
    chosen_cluster: FailureCluster | None = None
    for cl in clusters:
        proposal = propose_skill_patch(cl, skills_by_name=skills_by_name, extra_guardrails=extra_guardrails)
        if proposal is not None:
            chosen_cluster = cl
            break

    if proposal is None or chosen_cluster is None:
        summary = {"status": "no_patch_needed", "tenant": SYSTEM_EVOLUTION_TENANT, "clusters": len(clusters), "candidate_id": None, "staged_skill_name": None, "gate_passed": None}
        jp, mp = _write_cycle_files(out_dir, summary, clusters, None, None)
        return EvolutionCycleResult("no_patch_needed", clusters, None, None, None, jp, mp)

    staged_name = ""
    if registry is not None:
        iso = IsolatedEvolutionRegistry(registry)
        staged_name, _ = await iso.stage_candidate_skill(
            proposal.target_name,
            proposal.candidate_document,
            change_note=proposal.rationale,
        )

    base_variant = skills_by_name[proposal.target_name]
    baseline = SkillVariant(
        variant_id="baseline",
        skill_name=proposal.target_name,
        version=base_variant.version,
        instructions=proposal.baseline_document["instructions"],
        description=proposal.baseline_document["description"],
        mode_tag="baseline",
    )
    candidate_v = SkillVariant(
        variant_id="candidate",
        skill_name=proposal.target_name,
        version=staged_name or "candidate/draft",
        instructions=proposal.candidate_document["instructions"],
        description=proposal.candidate_document["description"],
        mode_tag="candidate",
    )

    suites = SKILL_SUITE_MAP.get(proposal.target_name, ("swe_engineering",))
    runner = SkillMatrixRunner(gateway=gateway, budget_usd=budget_usd)
    matrix_report = await runner.run(
        variants=(baseline, candidate_v),
        model_tiers=model_tiers,
        suites=suites,
        mode=mode,
        now=stamp,
    )
    write_matrix_reports(matrix_report, out_dir)

    gate_passed = bool(matrix_report.gate.get("passed"))
    status = "pending_approval" if gate_passed else "rejected_by_gate"

    record: CandidateRecord | None = None
    if store is not None:
        await store.migrate()
        record = await store.create_candidate(
            target_kind="skill",
            target_name=proposal.target_name,
            baseline_version=proposal.baseline_version,
            baseline_document=proposal.baseline_document,
            candidate_document=proposal.candidate_document,
            unified_diff=proposal.unified_diff,
            cluster_summary=chosen_cluster.to_json(),
            matrix_report=matrix_report.to_json(),
            status=status,
            staged_skill_name=staged_name,
        )

    if telemetry is not None:
        await telemetry.bind_platform(EVOLUTION_PLATFORM_PURPOSE).log(
            LogLevel.INFO,
            "evolution.run_cycle",
            attributes={
                "evolution.tenant": SYSTEM_EVOLUTION_TENANT,
                "evolution.status": status,
                "evolution.target_skill": proposal.target_name,
                "evolution.gate_passed": gate_passed,
            },
        )

    summary = {
        "status": status,
        "tenant": SYSTEM_EVOLUTION_TENANT,
        "clusters": len(clusters),
        "target_skill": proposal.target_name,
        "candidate_id": record.candidate_id if record else None,
        "staged_skill_name": staged_name,
        "gate_passed": gate_passed,
    }
    jp, mp = _write_cycle_files(out_dir, summary, clusters, proposal, matrix_report)
    return EvolutionCycleResult(status, clusters, proposal, matrix_report, record, jp, mp)


def _write_cycle_files(
    out_dir: Path,
    summary: Mapping[str, Any],
    clusters: Sequence[FailureCluster],
    proposal: SkillProposal | None,
    matrix: SkillMatrixReport | None,
) -> tuple[Path, Path]:
    json_path = out_dir / "evolution-report.json"
    md_path = out_dir / "evolution-report.md"
    payload = {
        **summary,
        "clusters_detail": [c.to_json() for c in clusters],
        "proposal": None
        if proposal is None
        else {
            "target_name": proposal.target_name,
            "cluster_kind": proposal.cluster_kind,
            "baseline_version": proposal.baseline_version,
            "unified_diff": proposal.unified_diff,
            "rationale": proposal.rationale,
        },
        "matrix": matrix.to_json() if matrix else None,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    md_path.write_text(_render_evolution_markdown(summary, clusters, proposal, matrix), "utf-8")
    return json_path, md_path
