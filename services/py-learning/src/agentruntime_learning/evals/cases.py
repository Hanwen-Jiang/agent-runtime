"""读取并校验评测集（计划 S-08）。

目录结构：

    <评测集>/catalog.json              评测用的 Node Catalog
    <评测集>/task_cases/<id>.json      Task 用例（kind = task）
    <评测集>/reviewer_samples/<id>.json  Reviewer 专用样本（kind = reviewer）

每个文件按 case.schema.json 校验，其中的 Stage、Task Plan、Task State、Verdict 按 /schemas 校验；另外检查
JSON Schema 表达不了的：id 与文件名一致且全局唯一，Stage 的 DAG 合法（复用 Stage Runner 的检查），
Stage 结果只涉及 Stage 里的 Node，参照输出与标注一致，引用的 Node Template、Recipe、工具都在 Catalog 里。
Stage 里写操作的幂等键由代码填入（与运行时相同），评测集里不用写。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from agentruntime_learning.evals import schemas, stages
from agentruntime_learning.evals.model import Catalog, TemplateEntry

# 仓库里默认的评测集。
DEFAULT_EVAL_SET = Path(__file__).resolve().parents[3] / "evalsets" / "task-loop"

CATEGORIES = ("ci_fix", "research_report", "batch_processing", "naming", "multi_day_dev", "booking_payment")
"""附录 B.3 的六类常见任务；用例还可以是 general（不属于六类的闲聊与简单问答）。"""

SYMPTOMS = (
    "fanout_conflict",
    "early_replans",
    "long_tool_loop",
    "revise_same_error",
    "cosmetic_reflection",
    "ensemble_divergence",
    "debate_no_convergence",
    "judge_bias",
    "context_pollution",
)
"""附录 B.7 的九种典型症状。"""

_DIRS = {"task": "task_cases", "reviewer": "reviewer_samples"}


class EvalSetError(ValueError):
    """评测集不合格；problems 是“文件：问题”的列表。"""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("评测集不合格：\n" + "\n".join(f"  {p}" for p in problems))
        self.problems = problems


@dataclass(frozen=True)
class Checkpoint:
    """一个 Round 结束时 Reviewer 的判定点。"""

    task_state: dict[str, Any]
    stage: dict[str, Any]
    results: dict[str, dict[str, Any]]
    history: list[dict[str, Any]]
    expect: dict[str, Any]


@dataclass(frozen=True)
class TaskCase:
    id: str
    split: str
    category: str
    review: dict[str, Any]
    description: str
    planner_input: dict[str, Any]
    planner_expect: dict[str, Any]
    rounds: list[Checkpoint]
    rubric: list[str] = field(default_factory=list)

    @property
    def confirmed(self) -> bool:
        return self.review.get("status") == "confirmed"


@dataclass(frozen=True)
class ReviewerSample:
    id: str
    split: str
    category: str | None
    review: dict[str, Any]
    description: str
    symptom: str | None
    checkpoint: Checkpoint
    rubric: list[str] = field(default_factory=list)

    @property
    def confirmed(self) -> bool:
        return self.review.get("status") == "confirmed"


@dataclass(frozen=True)
class EvalSet:
    name: str
    root: Path
    version: str
    """评测集内容的 sha256（按文件名排序后逐个计算），写进报告，用来判断两份报告是否可比。"""
    catalog: Catalog
    task_cases: list[TaskCase]
    reviewer_samples: list[ReviewerSample]

    def only(self, split: str) -> EvalSet:
        """只留某个划分（dev 或 holdout）的用例。"""
        return replace(
            self,
            task_cases=[c for c in self.task_cases if c.split == split],
            reviewer_samples=[s for s in self.reviewer_samples if s.split == split],
        )

    def unconfirmed(self) -> list[str]:
        """标注还没经人确认的用例 id。"""
        return sorted(i.id for i in [*self.task_cases, *self.reviewer_samples] if not i.confirmed)


def _fill_keys(data: dict[str, Any]) -> dict[str, Any]:
    """评测集里的 Stage 补上代码生成的幂等键，再按 schema 校验。"""
    if data.get("kind") == "task":
        reference = (((data.get("planner") or {}).get("expect") or {}).get("reference")) or {}
        if "stage" in reference:
            reference["stage"] = stages.with_idempotency_keys(reference["stage"])
        last = ((data.get("planner") or {}).get("input") or {}).get("last_round")
        if isinstance(last, dict) and "stage" in last:
            last["stage"] = stages.with_idempotency_keys(last["stage"])
        for cp in data.get("rounds") or []:
            if isinstance(cp, dict) and "stage" in cp:
                cp["stage"] = stages.with_idempotency_keys(cp["stage"])
    elif data.get("kind") == "reviewer":
        cp = data.get("checkpoint")
        if isinstance(cp, dict) and "stage" in cp:
            cp["stage"] = stages.with_idempotency_keys(cp["stage"])
    return data


def _stage_problems(where: str, stage: dict[str, Any], catalog: Catalog) -> list[str]:
    return [f"{where}：{p}" for p in [*stages.dag_problems(stage), *stages.catalog_problems(stage, catalog)]]


def _checkpoint(data: dict[str, Any]) -> Checkpoint:
    return Checkpoint(
        task_state=data["task_state"],
        stage=data["stage"],
        results=data["results"],
        history=data.get("history", []),
        expect=data["expect"],
    )


def _checkpoint_problems(where: str, cp: dict[str, Any], catalog: Catalog) -> list[str]:
    found = _stage_problems(f"{where}.stage", cp["stage"], catalog)
    node_ids = {n.get("id") for n in stages.nodes(cp["stage"])}
    found += [f"{where}.results 里的 {nid} 不在 Stage 里" for nid in sorted(set(cp["results"]) - node_ids)]
    expect = cp["expect"]
    if expect["verdict"] not in expect["acceptable"]:
        found.append(f"{where}：acceptable 不含标注的 {expect['verdict']}")
    if expect["reference"]["verdict"] != expect["verdict"]:
        found.append(f"{where}：参照 Verdict 是 {expect['reference']['verdict']}，标注是 {expect['verdict']}")
    return found


def _task_case_problems(data: dict[str, Any], catalog: Catalog) -> list[str]:
    expect = data["planner"]["expect"]
    reference = expect["reference"]
    found = _stage_problems("planner.expect.reference.stage", reference["stage"], catalog)
    plan = reference.get("task_plan")
    if plan is None and expect["triage"] != "fast":
        found.append(f"planner.expect.reference：Triage 是 {expect['triage']} 时要有 Task Plan")
    if plan is not None:
        level = plan["triage"]["level"]
        if level != expect["triage"]:
            found.append(f"planner.expect.reference.task_plan 的 Triage 是 {level}，标注是 {expect['triage']}")
        if expect["triage"] == "light" and plan.get("recipe") != expect.get("recipe"):
            found.append(f"planner.expect.reference.task_plan 的 Recipe 是 {plan.get('recipe')}，标注是 {expect.get('recipe')}")
    last = data["planner"]["input"].get("last_round")
    if last:
        found += _stage_problems("planner.input.last_round.stage", last["stage"], catalog)
        node_ids = {n.get("id") for n in stages.nodes(last["stage"])}
        found += [f"planner.input.last_round.results 里的 {nid} 不在上一轮的 Stage 里" for nid in sorted(set(last["results"]) - node_ids)]
    for i, cp in enumerate(data["rounds"]):
        found += _checkpoint_problems(f"rounds[{i}]", cp, catalog)
    return found


def _load_catalog(path: Path, problems: list[str]) -> Catalog:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(f"catalog.json：读不出来（{exc}）")
        return Catalog()
    errors = schemas.problems(schemas.case_def_validator("catalog"), data)
    if errors:
        problems += [f"catalog.json：{e}" for e in errors]
        return Catalog()
    return Catalog(
        templates={k: TemplateEntry(executor=v["executor"], effect=v["effect"]) for k, v in data["templates"].items()},
        recipes={k: v["task_family"] for k, v in data["recipes"].items()},
        tools=dict(data["tools"]),
    )


def load_eval_set(root: Path = DEFAULT_EVAL_SET) -> EvalSet:
    """读取并校验评测集；不合格时抛 EvalSetError，列出全部问题。"""
    root = Path(root)
    if not root.is_dir():
        raise EvalSetError([f"{root}：评测集目录不存在"])
    problems: list[str] = []
    catalog = _load_catalog(root / "catalog.json", problems)
    files = sorted(
        [root / "catalog.json", *(root / "task_cases").glob("*.json"), *(root / "reviewer_samples").glob("*.json")],
        key=lambda p: p.relative_to(root).as_posix(),
    )
    digest = hashlib.sha256()
    task_cases: list[TaskCase] = []
    samples: list[ReviewerSample] = []
    seen: dict[str, str] = {}
    case_validator = schemas.validator("case")
    for path in files:
        rel = path.relative_to(root).as_posix()
        raw = path.read_bytes() if path.exists() else b""
        digest.update(rel.encode() + b"\0" + raw + b"\0")
        if path.name == "catalog.json" and path.parent == root:
            continue
        try:
            data = _fill_keys(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            problems.append(f"{rel}：读不出来（{exc}）")
            continue
        errors = schemas.problems(case_validator, data)
        if errors:
            problems += [f"{rel}：{e}" for e in errors]
            continue
        kind, case_id = data["kind"], data["id"]
        if path.parent.name != _DIRS[kind]:
            problems.append(f"{rel}：kind 是 {kind}，应放在 {_DIRS[kind]}/")
            continue
        if case_id != path.stem:
            problems.append(f"{rel}：id {case_id} 与文件名不一致")
        if case_id in seen:
            problems.append(f"{rel}：id {case_id} 重复（{seen[case_id]}）")
        seen.setdefault(case_id, rel)
        if kind == "task":
            problems += [f"{rel}：{p}" for p in _task_case_problems(data, catalog)]
            task_cases.append(
                TaskCase(
                    id=case_id,
                    split=data["split"],
                    category=data["category"],
                    review=data["review"],
                    description=data["description"],
                    planner_input=data["planner"]["input"],
                    planner_expect=data["planner"]["expect"],
                    rounds=[_checkpoint(cp) for cp in data["rounds"]],
                    rubric=data.get("rubric", []),
                )
            )
        else:
            problems += [f"{rel}：{p}" for p in _checkpoint_problems("checkpoint", data["checkpoint"], catalog)]
            samples.append(
                ReviewerSample(
                    id=case_id,
                    split=data["split"],
                    category=data.get("category"),
                    review=data["review"],
                    description=data["description"],
                    symptom=data.get("symptom"),
                    checkpoint=_checkpoint(data["checkpoint"]),
                    rubric=data.get("rubric", []),
                )
            )
    if problems:
        raise EvalSetError(problems)
    return EvalSet(
        name=root.name,
        root=root,
        version=digest.hexdigest(),
        catalog=catalog,
        task_cases=sorted(task_cases, key=lambda c: c.id),
        reviewer_samples=sorted(samples, key=lambda s: s.id),
    )
