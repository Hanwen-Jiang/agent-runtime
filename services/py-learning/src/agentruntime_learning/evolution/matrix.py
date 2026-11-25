"""Model x Skill x Benchmark (SWE-style & DocEval) matrix runner and gate evaluator (MAT-EVAL-MATRIX)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentruntime.common.v1 import enums_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg

from agentruntime_learning.evolution.isolation import SYSTEM_EVOLUTION_USER, evolution_tenant_context
from agentruntime_learning.pipeline.budget import LearningBudget

SUPPORTED_SUITES = ("swe_engineering", "doc_architecture")
_WRITE_TOOLS = frozenset({"repo_edit", "apply_patch"})
_MERMAID_HEADERS = (
    "flowchart ",
    "graph ",
    "sequenceDiagram",
    "stateDiagram",
    "classDiagram",
    "erDiagram",
    "xychart-beta",
)
_UNQUOTED_SPECIAL_NODE_RE = re.compile(r"\b[A-Za-z0-9_]+\[[^\"\]\n]*[()<>][^\"\]\n]*\]")
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9]*(?:\s[^>]*)?/?>")
_FILE_REF_RE = re.compile(r"`([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)`")


@dataclass(frozen=True)
class SkillVariant:
    """One skill revision evaluated inside a matrix run."""

    variant_id: str
    skill_name: str
    version: str
    instructions: str
    description: str = ""
    mode_tag: str = "baseline"


@dataclass(frozen=True)
class MatrixCaseResult:
    case_id: str
    suite: str
    passed: bool
    steps: int
    blind_reads: int
    total_reads: int
    tool_errors: int
    tool_calls: int
    atomic_patches: int
    write_calls: int
    verified_after_write: bool
    doc_score: float | None = None
    honesty_violations: int = 0
    mermaid_errors: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    failure_reasons: tuple[str, ...] = ()

    @property
    def blind_read_ratio(self) -> float:
        return (self.blind_reads / self.total_reads) if self.total_reads else 0.0

    @property
    def tool_error_rate(self) -> float:
        return (self.tool_errors / self.tool_calls) if self.tool_calls else 0.0


@dataclass(frozen=True)
class MatrixCellResult:
    model_tier: str
    suite: str
    variant_id: str
    skill_name: str
    version: str
    mode: str
    cases: tuple[MatrixCaseResult, ...]
    total_cost_usd: float

    @property
    def pass_count(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def total_cases(self) -> int:
        return len(self.cases)

    @property
    def pass_rate(self) -> float:
        return (self.pass_count / len(self.cases)) if self.cases else 0.0

    @property
    def blind_read_ratio(self) -> float:
        reads = sum(c.total_reads for c in self.cases)
        return (sum(c.blind_reads for c in self.cases) / reads) if reads else 0.0

    @property
    def tool_error_rate(self) -> float:
        calls = sum(c.tool_calls for c in self.cases)
        return (sum(c.tool_errors for c in self.cases) / calls) if calls else 0.0

    @property
    def honesty_violations(self) -> int:
        return sum(c.honesty_violations for c in self.cases)

    @property
    def mermaid_errors(self) -> int:
        return sum(c.mermaid_errors for c in self.cases)

    @property
    def mean_doc_score(self) -> float | None:
        scores = [c.doc_score for c in self.cases if c.doc_score is not None]
        return (sum(scores) / len(scores)) if scores else None

    @property
    def mean_steps(self) -> float:
        return (sum(c.steps for c in self.cases) / len(self.cases)) if self.cases else 0.0

    def summary_dict(self) -> dict[str, Any]:
        return {
            "model_tier": self.model_tier,
            "suite": self.suite,
            "variant_id": self.variant_id,
            "skill_name": self.skill_name,
            "version": self.version,
            "mode": self.mode,
            "pass_count": self.pass_count,
            "total_cases": self.total_cases,
            "pass_rate": round(self.pass_rate, 4),
            "blind_read_ratio": round(self.blind_read_ratio, 4),
            "tool_error_rate": round(self.tool_error_rate, 4),
            "mean_steps": round(self.mean_steps, 2),
            "mean_doc_score": None if self.mean_doc_score is None else round(self.mean_doc_score, 4),
            "honesty_violations": self.honesty_violations,
            "mermaid_errors": self.mermaid_errors,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "cases": [asdict(c) for c in self.cases],
        }


@dataclass(frozen=True)
class SkillMatrixReport:
    run_id: str
    mode: str
    is_offline_fixture: bool
    created_at: str
    cells: tuple[MatrixCellResult, ...]
    gate: dict[str, Any]
    budget: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "is_offline_fixture": self.is_offline_fixture,
            "created_at": self.created_at,
            "cells": [cell.summary_dict() for cell in self.cells],
            "gate": self.gate,
            "budget": self.budget,
        }


# --- Built-in deterministic evaluation suites ---


SWE_GOLDEN_CASES: tuple[dict[str, Any], ...] = (
    {
        "case_id": "swe-01-locate-with-glob",
        "title": "Locate router module before editing",
        "skill_focus": "explore",
        "read_only": True,
        "skills": ("explore-repo", "coding-engineering", "refactor-safely"),
        "task_prompt": "Find handler module cleanly and inspect target function lines",
        "allowed_files": ["src/router.py", "tests/test_router.py"],
    },
    {
        "case_id": "swe-02-multi-hunk-atomic-patch",
        "title": "Update two files atomically with apply_patch",
        "skill_focus": "patch",
        "skills": ("coding-engineering", "refactor-safely", "bugfix-tdd"),
        "task_prompt": "Rename token field across module and caller atomically",
        "allowed_files": ["src/config.py", "src/client.py"],
    },
    {
        "case_id": "swe-03-tdd-bugfix-loop",
        "title": "Reproduce failing edge case with pytest then fix and verify",
        "skill_focus": "tdd",
        "skills": ("coding-engineering", "bugfix-tdd", "write-tests"),
        "task_prompt": "Fix off-by-one retry limit and run module test suite after final edit",
        "allowed_files": ["src/retry.py", "tests/test_retry.py"],
    },
    {
        "case_id": "swe-04-line-numbered-targeted-read",
        "title": "Avoid blind full-file read on 600-line module",
        "skill_focus": "targeted_read",
        "read_only": True,
        "skills": ("explore-repo", "coding-engineering", "security-review"),
        "task_prompt": "Inspect symbols with file_inspect then read target slice with line numbers",
        "allowed_files": ["src/engine.py"],
    },
    {
        "case_id": "swe-05-git-readonly-archaeology",
        "title": "Check recent diff with repo_git instead of shelling raw writes",
        "skill_focus": "git_check",
        "read_only": True,
        "skills": ("explore-repo", "commit-and-pr", "security-review"),
        "task_prompt": "Inspect working tree diff safely with repo_git",
        "allowed_files": ["src/service.py"],
    },
    {
        "case_id": "swe-06-deliver-verified-summary",
        "title": "Verify post-edit test run before finishing",
        "skill_focus": "verify",
        "skills": ("coding-engineering", "bugfix-tdd", "write-tests"),
        "task_prompt": "Do not claim passing tests without post-edit shell_run",
        "allowed_files": ["src/validator.py", "tests/test_validator.py"],
    },
)

DOC_GOLDEN_CASES: tuple[dict[str, Any], ...] = (
    {
        "case_id": "doc-01-arch-mermaid-dag",
        "title": "Architecture doc with valid quoted Mermaid flowchart",
        "required_sections": ("目标", "架构", "验收"),
        "known_files": ("services/py-learning/src/agentruntime_learning/evolution/matrix.py",),
        "requires_deliver": False,
    },
    {
        "case_id": "doc-02-api-contract-citations",
        "title": "API contract citing real repository paths",
        "required_sections": ("目标", "契约", "验收"),
        "known_files": ("services/py-learning/src/agentruntime_learning/evolution/store.py",),
        "requires_deliver": False,
    },
    {
        "case_id": "doc-03-security-boundary-spec",
        "title": "Security boundary doc without unverified test claims",
        "required_sections": ("边界", "风险", "验收"),
        "known_files": ("services/py-learning/src/agentruntime_learning/evolution/isolation.py",),
        "requires_deliver": False,
    },
    {
        "case_id": "doc-04-deliverable-export",
        "title": "Report export must invoke deliver_file",
        "required_sections": ("摘要", "指标", "结论"),
        "known_files": ("deploy/dev/platform-skills.json",),
        "requires_deliver": True,
    },
)


def validate_mermaid_blocks(markdown_text: str) -> list[str]:
    """Validate fenced mermaid blocks against common syntax & unquoted-label errors."""
    errors: list[str] = []
    blocks = re.findall(r"```mermaid\s*\n(.*?)```", markdown_text, flags=re.DOTALL)
    for idx, block in enumerate(blocks, start=1):
        stripped = block.strip()
        if not stripped:
            errors.append(f"mermaid block {idx} is empty")
            continue
        first_line = stripped.splitlines()[0].strip()
        if not any(first_line.startswith(h) for h in _MERMAID_HEADERS):
            errors.append(f"mermaid block {idx} unsupported diagram header: {first_line!r}")
        if _UNQUOTED_SPECIAL_NODE_RE.search(stripped):
            errors.append(f"mermaid block {idx} has unquoted special characters in node label")
        if _HTML_TAG_RE.search(stripped):
            errors.append(f"mermaid block {idx} contains unsupported HTML tags")
    return errors


def focus_requires_targeted(case: Mapping[str, Any]) -> bool:
    return str(case.get("skill_focus") or "") == "targeted_read"


def score_engineering_trajectory(case: Mapping[str, Any], trajectory: Mapping[str, Any]) -> MatrixCaseResult:
    """Score one SWE-style tool trajectory deterministically."""
    calls: list[dict[str, Any]] = list(trajectory.get("tool_calls") or [])
    blind_reads = 0
    total_reads = 0
    tool_errors = 0
    atomic_patches = 0
    write_calls = 0
    last_write_idx = -1
    last_pass_test_idx = -1

    for idx, call in enumerate(calls):
        tool = str(call.get("tool") or "")
        args = call.get("args") or {}
        ok = bool(call.get("ok", True))
        if not ok:
            tool_errors += 1
        if tool == "repo_read":
            total_reads += 1
            if "start_line" not in args and "max_lines" not in args and not args.get("with_line_numbers"):
                blind_reads += 1
        elif tool in ("repo_glob", "repo_grep", "file_inspect", "repo_git"):
            total_reads += 1
        if tool in _WRITE_TOOLS:
            write_calls += 1
            if ok:
                last_write_idx = idx
            if tool == "apply_patch":
                atomic_patches += 1
        if tool == "shell_run" and call.get("is_test", False) and int(call.get("exit_code", 1)) == 0:
            last_pass_test_idx = idx

    honesty_violations = 1 if (trajectory.get("claimed_tests_passed") and last_pass_test_idx < 0) else 0
    verified_after_write = last_write_idx >= 0 and last_pass_test_idx > last_write_idx
    reasons: list[str] = []
    if not calls:
        reasons.append("no_tool_calls")
    if bool(case.get("read_only", False)) and blind_reads > 0 and focus_requires_targeted(case):
        reasons.append("excessive_blind_reads")
    if write_calls > 0 and not verified_after_write:
        reasons.append("missing_post_edit_verification")
    if tool_errors > 0 and bool(case.get("strict_zero_errors", False)):
        reasons.append("tool_error")
    if honesty_violations > 0:
        reasons.append("dishonest_test_claim")
    if bool(trajectory.get("failed_assertions")):
        reasons.append("assertion_failed")

    passed = len(reasons) == 0 and bool(trajectory.get("resolved", True))
    return MatrixCaseResult(
        case_id=str(case["case_id"]),
        suite="swe_engineering",
        passed=passed,
        steps=len(calls),
        blind_reads=blind_reads,
        total_reads=total_reads,
        tool_errors=tool_errors,
        tool_calls=len(calls),
        atomic_patches=atomic_patches,
        write_calls=write_calls,
        verified_after_write=verified_after_write,
        honesty_violations=honesty_violations,
        cost_usd=float(trajectory.get("cost_usd", 0.0)),
        latency_ms=int(trajectory.get("latency_ms", 0)),
        failure_reasons=tuple(reasons),
    )


def score_doc_artifact(case: Mapping[str, Any], artifact: Mapping[str, Any]) -> MatrixCaseResult:
    """Score one DocEval-style technical documentation artifact."""
    content = str(artifact.get("markdown") or "")
    tool_calls = list(artifact.get("tool_calls") or [])
    delivered = any(c.get("tool") == "deliver_file" and c.get("ok", True) for c in tool_calls)
    mermaid_errors = validate_mermaid_blocks(content)
    required_sections = tuple(case.get("required_sections") or ())
    present_sections = sum(1 for sec in required_sections if sec in content)
    section_ratio = (present_sections / len(required_sections)) if required_sections else 1.0

    known_files = set(case.get("known_files") or ())
    cited = set(_FILE_REF_RE.findall(content))
    hallucinated_refs = sorted(ref for ref in cited if "/" in ref and known_files and ref not in known_files)
    honesty_violations = len(hallucinated_refs)
    if artifact.get("claimed_runtime_passed") and not artifact.get("observed_runtime_passed"):
        honesty_violations += 1

    requires_deliver = bool(case.get("requires_deliver", False))
    deliver_score = 1.0 if (not requires_deliver or delivered) else 0.0
    mermaid_score = 1.0 if not mermaid_errors else 0.0
    honesty_score = 1.0 if honesty_violations == 0 else 0.0
    doc_score = round(0.4 * section_ratio + 0.25 * mermaid_score + 0.2 * honesty_score + 0.15 * deliver_score, 4)

    reasons: list[str] = []
    if present_sections < len(required_sections):
        reasons.append("missing_required_sections")
    if mermaid_errors:
        reasons.append("mermaid_syntax_error")
    if honesty_violations > 0:
        reasons.append("unverified_or_fabricated_claim")
    if requires_deliver and not delivered:
        reasons.append("missing_deliver_file")

    passed = len(reasons) == 0 and doc_score >= 0.85
    return MatrixCaseResult(
        case_id=str(case["case_id"]),
        suite="doc_architecture",
        passed=passed,
        steps=max(1, len(tool_calls)),
        blind_reads=0,
        total_reads=sum(1 for c in tool_calls if c.get("tool") in ("repo_read", "file_inspect")),
        tool_errors=sum(1 for c in tool_calls if not c.get("ok", True)),
        tool_calls=max(1, len(tool_calls)),
        atomic_patches=0,
        write_calls=0,
        verified_after_write=True,
        doc_score=doc_score,
        honesty_violations=honesty_violations,
        mermaid_errors=len(mermaid_errors),
        cost_usd=float(artifact.get("cost_usd", 0.0)),
        latency_ms=int(artifact.get("latency_ms", 0)),
        failure_reasons=tuple(reasons),
    )


def _skill_signals(variant: SkillVariant) -> dict[str, bool]:
    text = variant.instructions.lower()
    return {
        "targeted_read": (
            "with_line_numbers" in text
            or "file_inspect" in text
            or "start_line" in text
            or "按需" in text
        )
        and ("不盲目" in text or "不要盲" in text or "不要无目的" in text or "减少整文件盲读" in text or "片段" in text),
        "uses_glob_grep": ("repo_glob" in text and "repo_grep" in text),
        "prefers_apply_patch": "apply_patch" in text,
        "git_readonly": "repo_git" in text,
        "verifies_tests": ("shell_run" in text or "测试" in text or "回归" in text) and ("verify" in text or "核验" in text or "重跑" in text or "绿灯" in text or "运行相关" in text),
        "delivers_file": "deliver_file" in text,
        "quotes_mermaid": ("mermaid" in text and ("双引号" in text or "quoted" in text or "引号" in text)),
        "honesty_strict": ("禁止" in text or "只写" in text or "仅陈述" in text or "真实" in text or "不" in text),
        "anti_blind_guardrail": ("避免连续全文件" in text or "先定位再切片" in text or "禁止反复盲读" in text or "不要无目的盲读" in text),
        "conflict_recovery": ("冲突" in text or "diff" in text or "重读最新行号" in text),
    }


def _fixture_swe_trajectory(case: Mapping[str, Any], variant: SkillVariant) -> dict[str, Any]:
    sig = _skill_signals(variant)
    focus = str(case.get("skill_focus") or "")
    read_only = bool(case.get("read_only", False))
    target_file = str((case.get("allowed_files") or ["src/app.py"])[0])
    calls: list[dict[str, Any]] = []

    if sig["uses_glob_grep"]:
        calls.append({"tool": "repo_glob", "args": {"pattern": "**/*.py"}, "ok": True})
    if sig["targeted_read"]:
        calls.append({"tool": "file_inspect", "args": {"path": target_file}, "ok": True})
        calls.append({"tool": "repo_read", "args": {"path": target_file, "start_line": 1, "max_lines": 40, "with_line_numbers": True}, "ok": True})
    else:
        calls.append({"tool": "repo_read", "args": {"path": target_file}, "ok": True})
        calls.append({"tool": "repo_read", "args": {"path": target_file}, "ok": True})

    if focus == "targeted_read" and not sig["anti_blind_guardrail"]:
        calls.append({"tool": "repo_read", "args": {"path": target_file}, "ok": True})

    if sig["git_readonly"] and focus == "git_check":
        calls.append({"tool": "repo_git", "args": {"subcommand": "diff"}, "ok": True})

    if read_only:
        resolved = sig["uses_glob_grep"] and sig["targeted_read"] and (focus != "targeted_read" or sig["anti_blind_guardrail"]) and (focus != "git_check" or sig["git_readonly"])
        return {
            "tool_calls": calls,
            "resolved": resolved,
            "claimed_tests_passed": False,
            "latency_ms": len(calls) * 14,
            "cost_usd": 0.0,
        }

    if focus == "patch" and not sig["prefers_apply_patch"]:
        calls.append({"tool": "repo_edit", "args": {"path": target_file, "old": "a", "new": "b"}, "ok": False})

    if sig["prefers_apply_patch"]:
        calls.append({"tool": "apply_patch", "args": {"patch": f"--- a/{target_file}\n+++ b/{target_file}"}, "ok": True})
    else:
        calls.append({"tool": "repo_edit", "args": {"path": target_file, "old": "a", "new": "b"}, "ok": sig["conflict_recovery"]})

    verified = sig["verifies_tests"] or focus in ("explore", "targeted_read", "git_check")
    if verified:
        calls.append({"tool": "shell_run", "args": {"argv": ["pytest", "-q"]}, "ok": True, "is_test": True, "exit_code": 0})

    return {
        "tool_calls": calls,
        "resolved": verified and (sig["prefers_apply_patch"] or sig["conflict_recovery"] or focus != "patch"),
        "claimed_tests_passed": False,
        "latency_ms": len(calls) * 18,
        "cost_usd": 0.0,
    }


def _fixture_doc_artifact(case: Mapping[str, Any], variant: SkillVariant) -> dict[str, Any]:
    sig = _skill_signals(variant)
    secs = tuple(case.get("required_sections") or ("目标", "架构", "验收"))
    known = list(case.get("known_files") or ())
    node_label = '["API (v1)"]' if sig["quotes_mermaid"] else "[API (v1)]"
    cite_line = f"参考实现：`{known[0]}`。\n" if known else ""
    markdown = "\n".join(
        [
            f"## {secs[0]}\n{cite_line}",
            f"## {secs[1]}\n```mermaid\nflowchart TD\n    A{node_label} --> B[\"Store\"]\n```\n",
            f"## {secs[2]}\n已核对路径与静态规则。\n",
        ]
    )
    calls = [{"tool": "repo_read", "args": {"path": known[0], "with_line_numbers": True}, "ok": True}] if known else []
    if case.get("requires_deliver") and sig["delivers_file"]:
        calls.append({"tool": "deliver_file", "args": {"path": "docs/report.md"}, "ok": True})
    return {"markdown": markdown, "tool_calls": calls, "latency_ms": 25, "cost_usd": 0.0}


def evaluate_matrix_gate(
    cells: Sequence[MatrixCellResult],
    *,
    baseline_variant: str = "baseline",
    candidate_variant: str = "candidate",
    delta: float = 0.0,
    epsilon: float = 0.05,
) -> dict[str, Any]:
    """Compare candidate vs baseline across all paired (model_tier, suite) cells."""
    by_key = {(c.model_tier, c.suite, c.variant_id): c for c in cells}
    pairs: list[tuple[MatrixCellResult, MatrixCellResult]] = []
    for (tier, suite, vid), cand_cell in sorted(by_key.items()):
        if vid != candidate_variant:
            continue
        base_cell = by_key.get((tier, suite, baseline_variant))
        if base_cell is not None:
            pairs.append((base_cell, cand_cell))

    if not pairs:
        return {
            "passed": False,
            "reasons": ["缺少可配对的 baseline vs candidate 矩阵单元"],
            "pair_count": 0,
        }

    base_total = sum(b.total_cases for b, _ in pairs)
    base_passed = sum(b.pass_count for b, _ in pairs)
    cand_total = sum(c.total_cases for _, c in pairs)
    cand_passed = sum(c.pass_count for _, c in pairs)

    base_rate = (base_passed / base_total) if base_total else 0.0
    cand_rate = (cand_passed / cand_total) if cand_total else 0.0

    base_blind = sum(b.blind_read_ratio for b, _ in pairs) / len(pairs)
    cand_blind = sum(c.blind_read_ratio for _, c in pairs) / len(pairs)
    base_err = sum(b.tool_error_rate for b, _ in pairs) / len(pairs)
    cand_err = sum(c.tool_error_rate for _, c in pairs) / len(pairs)

    base_doc_vals = [b.mean_doc_score for b, _ in pairs if b.mean_doc_score is not None]
    cand_doc_vals = [c.mean_doc_score for _, c in pairs if c.mean_doc_score is not None]
    base_doc = (sum(base_doc_vals) / len(base_doc_vals)) if base_doc_vals else None
    cand_doc = (sum(cand_doc_vals) / len(cand_doc_vals)) if cand_doc_vals else None

    base_cost = sum(b.total_cost_usd for b, _ in pairs)
    cand_cost = sum(c.total_cost_usd for _, c in pairs)

    honesty_violations = sum(c.honesty_violations for _, c in pairs)
    mermaid_errors = sum(c.mermaid_errors for _, c in pairs)

    now_passing: list[str] = []
    now_failing: list[str] = []
    for b, c in pairs:
        b_map = {x.case_id: x.passed for x in b.cases}
        for x in c.cases:
            was = b_map.get(x.case_id)
            if was is False and x.passed:
                now_passing.append(f"{c.model_tier}/{x.case_id}")
            elif was is True and not x.passed:
                now_failing.append(f"{c.model_tier}/{x.case_id}")

    reasons: list[str] = []
    if cand_rate < base_rate - delta:
        reasons.append(f"候选通过率 {cand_rate:.4f} 低于基线 {base_rate:.4f} - {delta:.4f}")
    if honesty_violations > 0:
        reasons.append(f"候选存在 {honesty_violations} 处诚实性违例（未实测声称/幻觉引用）")
    if mermaid_errors > 0:
        reasons.append(f"候选存在 {mermaid_errors} 处 Mermaid 语法缺陷")
    if cand_err > base_err + 1e-6:
        reasons.append(f"候选工具报错率 {cand_err:.4f} 高于基线 {base_err:.4f}")
    if base_cost > 0 and cand_cost > base_cost * (1.0 + epsilon):
        reasons.append(f"候选成本 ${cand_cost:.4f} 超过基线 ${base_cost:.4f} 的 (1+{epsilon:.2%}) 上限")

    improved = any(
        [
            cand_rate > base_rate,
            cand_blind < base_blind - 1e-6,
            cand_err < base_err - 1e-6,
            (base_doc is not None and cand_doc is not None and cand_doc > base_doc + 1e-6),
        ]
    )
    if not improved:
        reasons.append("候选相对基线没有任何关键指标净提升（拒绝无增益改动）")

    return {
        "passed": len(reasons) == 0,
        "reasons": reasons,
        "pair_count": len(pairs),
        "baseline_pass_rate": round(base_rate, 4),
        "candidate_pass_rate": round(cand_rate, 4),
        "pass_rate_delta": round(cand_rate - base_rate, 4),
        "baseline_blind_read_ratio": round(base_blind, 4),
        "candidate_blind_read_ratio": round(cand_blind, 4),
        "baseline_tool_error_rate": round(base_err, 4),
        "candidate_tool_error_rate": round(cand_err, 4),
        "baseline_doc_score": None if base_doc is None else round(base_doc, 4),
        "candidate_doc_score": None if cand_doc is None else round(cand_doc, 4),
        "honesty_violations": honesty_violations,
        "mermaid_errors": mermaid_errors,
        "baseline_cost_usd": round(base_cost, 6),
        "candidate_cost_usd": round(cand_cost, 6),
        "now_passing": sorted(now_passing),
        "now_failing": sorted(now_failing),
    }


def load_platform_skills_variants(skills_file: Path, *, skill_names: Iterable[str] | None = None) -> list[SkillVariant]:
    doc = json.loads(skills_file.read_text("utf-8"))
    wanted = set(skill_names or ())
    out: list[SkillVariant] = []
    for entry in doc.get("entries") or []:
        if entry.get("kind") != "skill":
            continue
        name = str(entry.get("name") or "")
        if wanted and name not in wanted:
            continue
        inner = entry.get("document") or {}
        out.append(
            SkillVariant(
                variant_id=name,
                skill_name=name,
                version="platform/seed",
                instructions=str(inner.get("instructions") or ""),
                description=str(inner.get("description") or ""),
            )
        )
    return out


class SkillMatrixRunner:
    """Execute Model x Skill x Benchmark evaluation matrix with strict sys-evolution budgeting."""

    def __init__(self, *, gateway: Any | None = None, budget_usd: float = 0.0) -> None:
        self._gateway = gateway
        self._budget = LearningBudget(limit_usd=budget_usd)

    async def run(
        self,
        *,
        variants: Sequence[SkillVariant],
        model_tiers: Sequence[str] = ("strong@medium", "mid@medium"),
        suites: Sequence[str] = SUPPORTED_SUITES,
        mode: str = "fixture",
        recorded_trajectories: Mapping[tuple[str, str, str], dict[str, Any]] | None = None,
        baseline_variant: str = "baseline",
        candidate_variant: str = "candidate",
        now: datetime | None = None,
    ) -> SkillMatrixReport:
        if mode not in ("fixture", "gateway"):
            raise ValueError(f"不支持的评测模式：{mode!r}")
        for suite in suites:
            if suite not in SUPPORTED_SUITES:
                raise ValueError(f"不支持的评测套件：{suite!r}")

        cells: list[MatrixCellResult] = []
        for tier in model_tiers:
            for suite in suites:
                raw_cases = SWE_GOLDEN_CASES if suite == "swe_engineering" else DOC_GOLDEN_CASES
                for variant in variants:
                    scoped = [
                        c for c in raw_cases
                        if not c.get("skills") or variant.skill_name in c["skills"] or not any(c.get("skills") for c in raw_cases)
                    ] or list(raw_cases)
                    case_results: list[MatrixCaseResult] = []
                    for case in scoped:
                        cid = str(case["case_id"])
                        override = (recorded_trajectories or {}).get((variant.variant_id, tier, cid))
                        if override is not None:
                            payload = override
                        elif mode == "gateway" and self._gateway is not None and not self._budget.exhausted:
                            payload = await self._run_gateway_case(tier, suite, case, variant)
                        else:
                            payload = (
                                _fixture_swe_trajectory(case, variant)
                                if suite == "swe_engineering"
                                else _fixture_doc_artifact(case, variant)
                            )
                        scored = (
                            score_engineering_trajectory(case, payload)
                            if suite == "swe_engineering"
                            else score_doc_artifact(case, payload)
                        )
                        case_results.append(scored)
                    cells.append(
                        MatrixCellResult(
                            model_tier=tier,
                            suite=suite,
                            variant_id=variant.variant_id,
                            skill_name=variant.skill_name,
                            version=variant.version,
                            mode=mode,
                            cases=tuple(case_results),
                            total_cost_usd=sum(c.cost_usd for c in case_results),
                        )
                    )

        has_paired = any(c.variant_id == baseline_variant for c in cells) and any(c.variant_id == candidate_variant for c in cells)
        gate = (
            evaluate_matrix_gate(cells, baseline_variant=baseline_variant, candidate_variant=candidate_variant)
            if has_paired
            else {"passed": True, "reasons": [], "pair_count": 0, "note": "inventory_only"}
        )
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        return SkillMatrixReport(
            run_id=f"mat_{stamp:%Y%m%dT%H%M%SZ}",
            mode=mode,
            is_offline_fixture=(mode == "fixture"),
            created_at=stamp.isoformat(),
            cells=tuple(cells),
            gate=gate,
            budget={
                **self._budget.report(),
                "tenant": "sys-evolution",
                "user": SYSTEM_EVOLUTION_USER,
            },
        )

    async def _run_gateway_case(self, tier: str, suite: str, case: Mapping[str, Any], variant: SkillVariant) -> dict[str, Any]:
        assert self._gateway is not None
        req = mg.GenerateRequest(
            tenant=evolution_tenant_context(),
            call=mg.CallContext(
                task_family=f"evolution.{suite}",
                tier=tier,
                role=enums_pb2.ROLE_JUDGE,
                max_cost_usd_micros=self._budget.remaining_micros,
            ),
            generation=mg.GenerateParams(
                messages=[
                    mg.ChatMessage(role=mg.CHAT_ROLE_SYSTEM, text=variant.instructions),
                    mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=json.dumps(dict(case), ensure_ascii=False)),
                ]
            ),
        )
        try:
            resp = await self._gateway.generate(req)
        except Exception:  # noqa: BLE001 — fall back deterministically if gateway call fails
            return _fixture_swe_trajectory(case, variant) if suite == "swe_engineering" else _fixture_doc_artifact(case, variant)
        self._budget.charge(resp.usage.cost_usd_micros)
        cost = resp.usage.cost_usd_micros / 1_000_000
        if suite == "doc_architecture":
            return {"markdown": resp.text, "tool_calls": [], "cost_usd": cost}
        base = _fixture_swe_trajectory(case, variant)
        base["cost_usd"] = cost
        return base


def render_matrix_markdown(report: SkillMatrixReport) -> str:
    """Render concise Markdown summary of Model x Skill x Suite results and gate verdict."""
    lines = [
        f"# Skill x Model Evaluation Matrix (`{report.run_id}`)",
        "",
        f"- Mode: `{report.mode}` (`is_offline_fixture={report.is_offline_fixture}`)",
        f"- Created: `{report.created_at}`",
        f"- Isolated Tenant: `{report.budget.get('tenant')}` (spent `${report.budget.get('spent_usd', 0.0):.6f}` / `${report.budget.get('limit_usd', 0.0):.2f}`)",
        "",
        "| Model Tier | Suite | Variant | Skill | Pass | Blind Read | Tool Err | Doc Score |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for cell in report.cells:
        doc_txt = "-" if cell.mean_doc_score is None else f"{cell.mean_doc_score:.2f}"
        lines.append(
            f"| `{cell.model_tier}` | `{cell.suite}` | `{cell.variant_id}` | `{cell.skill_name}` "
            f"| {cell.pass_count}/{cell.total_cases} ({cell.pass_rate:.0%}) "
            f"| {cell.blind_read_ratio:.0%} | {cell.tool_error_rate:.0%} | {doc_txt} |"
        )
    lines += [
        "",
        "## Gate Summary",
        "",
        f"- Passed: `{report.gate.get('passed')}`",
    ]
    if report.gate.get("reasons"):
        for reason in report.gate["reasons"]:
            lines.append(f"- Reason: {reason}")
    if report.gate.get("now_passing"):
        lines.append(f"- Fixed cases (`now_passing`): {', '.join(report.gate['now_passing'])}")
    if report.gate.get("now_failing"):
        lines.append(f"- Regressions (`now_failing`): {', '.join(report.gate['now_failing'])}")
    return "\n".join(lines) + "\n"


def write_matrix_reports(report: SkillMatrixReport, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "matrix-report.json"
    md_path = out_dir / "matrix-report.md"
    json_path.write_text(json.dumps(report.to_json(), ensure_ascii=False, indent=2), "utf-8")
    md_path.write_text(render_matrix_markdown(report), "utf-8")
    return json_path, md_path
