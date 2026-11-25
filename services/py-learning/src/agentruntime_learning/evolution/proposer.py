"""Skill/Prompt patch diff synthesis with schema & safety validation (MAT-LEARN-EVOL)."""

from __future__ import annotations

import copy
import difflib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agentruntime_core.errors import InvalidError

from agentruntime_learning.evals.schemas import problems, validator
from agentruntime_learning.evolution.clustering import FailureCluster
from agentruntime_learning.evolution.matrix import SkillVariant

MAX_SKILL_JSON_BYTES = 256 << 10
_UNSAFE_PATTERNS = (
    "跳过测试",
    "skip tests",
    "disable tests",
    "忽略 effect_ceiling",
    "ignore effect_ceiling",
    "bypass sandbox",
    "绕过沙箱",
    "绕过 ssrf",
    "伪造通过",
    "fabricate",
)

# Concrete, deterministic operational enhancements keyed by cluster_kind.
_CLUSTER_APPENDICES: dict[str, tuple[str, ...]] = {
    "readonly_churn": (
        "- 高效定位防盲读：先使用 `file_inspect` 看行数与符号索引，再以 `repo_read(with_line_numbers=true, start_line=..., max_lines=...)` 先定位再切片；避免连续全文件盲读超过 2 轮仍不收敛。",
    ),
    "edit_or_patch_conflict": (
        "- 冲突与原子修改守则：多处改动优先用 `apply_patch`；若遇 `edit_no_match`/`edit_ambiguous`/`patch_conflict`，先重读最新行号与 `repo_git(subcommand=\"diff\")` 核对上下文再重试，禁止盲目重复原补丁。",
    ),
    "unverified_completion": (
        "- 完成前硬核验：任何写操作后必须通过 `shell_run` 运行受影响模块回归测试并取得 exit_code 0；严禁在未核验时声称通过。",
    ),
    "undelivered_artifact": (
        "- 交付闭环校验：生成报告或导出文件后必须显式调用 `deliver_file` 并核对返回 artifact 状态，避免只停留在工作区。",
    ),
    "doc_or_mermaid_defect": (
        "- Mermaid 与引用防错：含括号或方括号的 Mermaid 节点标签一律加双引号，不使用 HTML 标签；每个引用路径须先经 `repo_read` 核实存在。",
    ),
}


@dataclass(frozen=True)
class SkillProposal:
    target_name: str
    cluster_kind: str
    baseline_version: str
    baseline_document: dict[str, Any]
    candidate_document: dict[str, Any]
    unified_diff: str
    rationale: str


def validate_candidate_safety(document: Mapping[str, Any]) -> None:
    """Validate candidate skill document against skill.schema.json, size ceiling, and safety guardrails."""
    errs = problems(validator("skill"), dict(document))
    if errs:
        raise InvalidError("invalid_skill_candidate", f"候选 Skill 未通过 Schema 校验：{'; '.join(errs)}")
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_SKILL_JSON_BYTES:
        raise InvalidError("skill_too_large", f"候选 Skill 体积 {len(encoded)} 字节超过 256 KiB 上限")
    low = json.dumps(document, ensure_ascii=False).lower()
    for banned in _UNSAFE_PATTERNS:
        if banned in low:
            raise InvalidError("unsafe_skill_candidate", f"候选 Skill 包含越权/作弊指令：{banned!r}")


def unified_instruction_diff(skill_name: str, old_text: str, new_text: str) -> str:
    old_norm = old_text.rstrip() + "\n"
    new_norm = new_text.rstrip() + "\n"
    return "".join(
        difflib.unified_diff(
            old_norm.splitlines(keepends=True),
            new_norm.splitlines(keepends=True),
            fromfile=f"a/skills/{skill_name}/instructions.md",
            tofile=f"b/skills/{skill_name}/instructions.md",
        )
    )


def propose_skill_patch(
    cluster: FailureCluster,
    *,
    skills_by_name: Mapping[str, SkillVariant],
    extra_guardrails: Mapping[str, tuple[str, ...]] | None = None,
) -> SkillProposal | None:
    """Draft a non-duplicative Skill patch addressing a FailureCluster; returns None if already covered."""
    base = skills_by_name.get(cluster.target_skill)
    if base is None:
        return None
    lines_map = extra_guardrails or _CLUSTER_APPENDICES
    additions = [line for line in lines_map.get(cluster.cluster_kind, ()) if line not in base.instructions]
    if not additions:
        return None

    old_doc = {
        "description": base.description or f"{base.skill_name} skill",
        "instructions": base.instructions,
    }
    new_instructions = base.instructions.rstrip() + "\n\n【自进化补充检查项（针对聚类：" + cluster.cluster_kind + "）】\n" + "\n".join(additions) + "\n"
    new_doc = copy.deepcopy(old_doc)
    new_doc["instructions"] = new_instructions
    validate_candidate_safety(new_doc)

    diff = unified_instruction_diff(base.skill_name, base.instructions, new_instructions)
    if not diff:
        return None

    return SkillProposal(
        target_name=base.skill_name,
        cluster_kind=cluster.cluster_kind,
        baseline_version=base.version,
        baseline_document=old_doc,
        candidate_document=new_doc,
        unified_diff=diff,
        rationale=f"针对 {cluster.cluster_kind}（support={cluster.support}）：{cluster.diagnosis}",
    )
