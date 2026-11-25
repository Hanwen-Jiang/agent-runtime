"""Wave 2 Model x Skill Evaluation Matrix & Isolated Self-Evolution Harness."""

from agentruntime_learning.evolution.clustering import FailureCluster, cluster_failures
from agentruntime_learning.evolution.isolation import (
    CANDIDATE_SKILL_PREFIX,
    EVOLUTION_PLATFORM_PURPOSE,
    SYSTEM_EVOLUTION_TENANT,
    SYSTEM_EVOLUTION_USER,
    IsolatedEvolutionRegistry,
    assert_evolution_tenant,
    candidate_skill_name,
    evolution_tenant_context,
    evolution_worker_tenant,
)
from agentruntime_learning.evolution.matrix import (
    MatrixCaseResult,
    MatrixCellResult,
    SkillMatrixReport,
    SkillMatrixRunner,
    SkillVariant,
    evaluate_matrix_gate,
    load_platform_skills_variants,
    score_doc_artifact,
    score_engineering_trajectory,
    validate_mermaid_blocks,
    write_matrix_reports,
)
from agentruntime_learning.evolution.pipeline import EvolutionCycleResult, run_evolution_cycle
from agentruntime_learning.evolution.proposer import SkillProposal, propose_skill_patch, validate_candidate_safety
from agentruntime_learning.evolution.store import CandidateRecord, EvolutionStore

__all__ = [
    "CANDIDATE_SKILL_PREFIX",
    "CandidateRecord",
    "EVOLUTION_PLATFORM_PURPOSE",
    "EvolutionCycleResult",
    "EvolutionStore",
    "FailureCluster",
    "IsolatedEvolutionRegistry",
    "MatrixCaseResult",
    "MatrixCellResult",
    "SYSTEM_EVOLUTION_TENANT",
    "SYSTEM_EVOLUTION_USER",
    "SkillMatrixReport",
    "SkillMatrixRunner",
    "SkillProposal",
    "SkillVariant",
    "assert_evolution_tenant",
    "candidate_skill_name",
    "cluster_failures",
    "evaluate_matrix_gate",
    "evolution_tenant_context",
    "evolution_worker_tenant",
    "load_platform_skills_variants",
    "propose_skill_patch",
    "run_evolution_cycle",
    "score_doc_artifact",
    "score_engineering_trajectory",
    "validate_candidate_safety",
    "validate_mermaid_blocks",
    "write_matrix_reports",
]
