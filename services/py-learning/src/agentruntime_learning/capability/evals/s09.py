"""S-09 的 Planner 用例当能力矩阵的题（docs/plan/capability-matrix-evals.md 第 3 节）：一条 Task 用例一道题，评分复用 evals.scoring。

- `make`（role plan）：标为 full、replan 的用例（这些才调用规划模型，ROLE_PLAN）；一道题通过 = S-09 的规划决策全部通过
  （score_planner：结构、规则、Triage、形状）。分诊模型照常选档，不钉。
- `route`（role route）：由分诊模型定级的用例（上一轮的 Verdict 已经定级的不算：分诊模型根本不被调用）；一道题通过 = Triage
  与标注相同。只看分诊，所以规划角色的调用在本地就拒掉（不发到网关、不花钱）：Planner 回退到保守规划，Triage 照样是分诊定的级别。

默认两个划分（dev、holdout）都用：矩阵给模型打分，不调提示。留出集的题只记失败的维度与代码（不引用用例内容）。

MX-Q：`adapted` 给出 evalsets/public-adapted-drafts 下的套名（natural-plan、travelplanner、swebench），这些改编题也加进来，
题号是 `adapted:<套>/<用例>`，各题用自己那套的 Catalog；同一道原题派生的追问、replan 只取原题（那套 README 第 8 条：
不重复计为独立样本）。草稿的标签没经人确认，记录里写 confirmed 与 adapted；评测集版本把这几套算进去。
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agentruntime.common.v1 import enums_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_worker.executor.model_gateway import ModelGateway
from agentruntime_worker.planner.triage import reviewer_decision

from agentruntime_learning.capability.base import Item, ItemResult
from agentruntime_learning.evals.cases import DEFAULT_EVAL_SET, EvalSet, TaskCase, load_eval_set
from agentruntime_learning.evals.model import PlannerInput
from agentruntime_learning.evals.scoring import DecisionScore, score_planner
from agentruntime_learning.evals.worker_subject import WorkerPlannerSubject, planning_context

SPLITS = ("all", "dev", "holdout")
ADAPTED_ROOT = DEFAULT_EVAL_SET.parent / "public-adapted-drafts"
ADAPTED_SETS = ("natural-plan", "travelplanner", "swebench")
ADAPTED_PREFIX = "adapted:"


def source_groups(root: Path) -> dict[str, tuple[str, bool]]:
    """改编题那套的 sources.jsonl：用例 id（swebench 那套写作 case_id）→ (同一道原题的分组, 是不是派生的)。没有 group_id 的自成一组。"""
    out: dict[str, tuple[str, bool]] = {}
    for line in (root / "sources.jsonl").read_text("utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        case_id = record.get("id") or record.get("case_id")
        if case_id:
            out[str(case_id)] = (str(record.get("group_id") or case_id), "derived_from" in record)
    return out


def pick(ids: Sequence[str], n: int, seed: int) -> list[str]:
    """按 seed 打乱后取前 n 个：同一个 seed 选出同样的题，而且前缀稳定（多取 20 道时前 n 道不变）。"""
    order = sorted(ids)
    random.Random(seed).shuffle(order)
    return order[: max(0, n)]


def select_split(eval_set: EvalSet, split: str) -> EvalSet:
    if split not in SPLITS:
        raise ValueError(f"split 只能是 {'、'.join(SPLITS)}，得到 {split!r}")
    return eval_set if split == "all" else eval_set.only(split)


def planner_input(case: TaskCase, eval_set: EvalSet) -> PlannerInput:
    inp = case.planner_input
    return PlannerInput(
        message=inp["message"],
        task_state=copy.deepcopy(inp.get("task_state")),
        attachments=list(inp.get("attachments", [])),
        context=copy.deepcopy(inp["context"]),
        catalog=eval_set.catalog,
        last_round=copy.deepcopy(inp.get("last_round")),
    )


def failures(score: DecisionScore) -> list[dict[str, str]]:
    return [{"dimension": f.dimension, "code": f.code, "message": f.message} for f in score.failures]


class PlanningSkipped(RuntimeError):
    """route 格子不调用规划模型。"""


class _RefusePlanning:
    """规划角色的调用在本地拒掉（route 格子只看分诊，不花强模型的钱）；其他调用原样转发。"""

    def __init__(self, inner: ModelGateway) -> None:
        self.inner = inner

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        if request.call.role == enums_pb2.ROLE_PLAN:
            raise PlanningSkipped("route 格子不调用规划模型")
        return await self.inner.generate(request)

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        return await self.inner.embed(request)


class _S09:
    role = ""
    name = ""
    est_tokens: tuple[int, int] = (0, 0)
    est_other_usd = 0.0

    def __init__(self, eval_set: str | Path | None = None, split: str = "all", adapted: Sequence[str] | None = None) -> None:
        self.eval_set = load_eval_set(Path(eval_set) if eval_set else DEFAULT_EVAL_SET)
        self.split = split
        selected = select_split(self.eval_set, split)
        self.cases = {c.id: c for c in selected.task_cases if self._wanted(c, self.eval_set)}
        # 每道题出自哪一套（改编题各用自己那套的 Catalog）；套名为空的是主评测集。
        self.sets: dict[str, EvalSet] = {"": self.eval_set}
        self.origin = dict.fromkeys(self.cases, "")
        versions = []
        for name in adapted or ():
            if name not in ADAPTED_SETS:
                raise ValueError(f"没有改编题 {name!r}（可选：{'、'.join(ADAPTED_SETS)}）")
            es = load_eval_set(ADAPTED_ROOT / name)
            self.sets[name] = es
            versions.append(f"{name}:{es.version}")
            for case in self._originals(select_split(es, split), es, source_groups(ADAPTED_ROOT / name)):
                item_id = f"{ADAPTED_PREFIX}{name}/{case.id}"
                self.cases[item_id], self.origin[item_id] = case, name
        if versions:
            self.version = hashlib.sha256("\n".join([self.eval_set.version, *versions]).encode()).hexdigest()[:12]
        else:
            self.version = self.eval_set.version[:12]

    def _originals(self, selected: EvalSet, es: EvalSet, groups: dict[str, tuple[str, bool]]) -> list[TaskCase]:
        """这一格要的题，同一道原题只留一道：有原题就取原题，否则取派生题里 id 最小的。"""
        chosen: dict[str, TaskCase] = {}
        for case in sorted((c for c in selected.task_cases if self._wanted(c, es)), key=lambda c: (groups.get(c.id, (c.id, False))[1], c.id)):
            chosen.setdefault(groups.get(case.id, (case.id, False))[0], case)
        return list(chosen.values())

    def _wanted(self, case: TaskCase, eval_set: EvalSet) -> bool:
        raise NotImplementedError

    def items(self, n: int, seed: int) -> list[Item]:
        out = []
        for i in pick(list(self.cases), n, seed):
            payload: dict[str, Any] = {"split": self.cases[i].split}
            if self.origin[i]:
                payload["adapted"] = self.origin[i]
            out.append(Item(id=i, payload=payload))
        return out

    async def _plan(self, item_id: str, gateway: ModelGateway) -> tuple[DecisionScore, Any, WorkerPlannerSubject]:
        case, es = self.cases[item_id], self.sets[self.origin[item_id]]
        subject = WorkerPlannerSubject(gateway)
        output = await subject.plan(planner_input(case, es))
        return score_planner(case, output, es.catalog), output, subject

    def _record(self, item_id: str, score: DecisionScore, output: Any, subject: WorkerPlannerSubject) -> dict[str, Any]:
        case = self.cases[item_id]
        record = {
            "split": case.split,
            "triage_expected": case.planner_expect["triage"],
            "triage": output.triage_level,
            "fallback": output.fallback,
            "failures": failures(score),
            "model_call_failures": dict(subject.model_call_failures),
            "confirmed": case.confirmed,
        }
        if self.origin[item_id]:
            record["adapted"] = self.origin[item_id]
        return record


class S09Plan(_S09):
    """role plan：full、replan 的用例，S-09 的规划决策全部通过记 1。"""

    name = "s09"
    role = "plan"
    # S-09 实测约 0.008 美元/题（2026-11-02）：按强档的单价约 2.5k 输入、300 输出；分诊另计。
    est_tokens = (2500, 300)
    est_other_usd = 0.0003

    def _wanted(self, case: TaskCase, eval_set: EvalSet) -> bool:
        return case.planner_expect["triage"] in ("full", "replan")

    async def run_item(self, item: Item, gateway: ModelGateway, *, candidate: str) -> ItemResult:
        score, output, subject = await self._plan(item.id, gateway)
        record = self._record(item.id, score, output, subject)
        return ItemResult(item.id, score=1.0 if score.passed else 0.0, cost_usd=score.cost_usd, record=record)


class S09Route(_S09):
    """role route：由分诊模型定级的用例，Triage 与标注相同记 1。"""

    name = "s09-route"
    role = "route"
    est_tokens = (1500, 100)

    def _wanted(self, case: TaskCase, eval_set: EvalSet) -> bool:
        context = planning_context(planner_input(case, eval_set))
        return reviewer_decision(case.planner_input.get("last_round"), context=context) is None

    async def run_item(self, item: Item, gateway: ModelGateway, *, candidate: str) -> ItemResult:
        case = self.cases[item.id]
        score, output, subject = await self._plan(item.id, _RefusePlanning(gateway))
        record = self._record(item.id, score, output, subject)
        record["failures"] = [f for f in record["failures"] if f["dimension"] == "triage"]
        record.pop("fallback")  # 规划角色被本地拒掉，回退是必然的，不说明什么
        record["model_call_failures"].pop(PlanningSkipped.__name__, None)
        passed = output.triage_level == case.planner_expect["triage"]
        return ItemResult(item.id, score=1.0 if passed else 0.0, cost_usd=score.cost_usd, record=record)


def make(eval_set: str | None = None, split: str = "all", adapted: Sequence[str] | None = None) -> S09Plan:
    return S09Plan(eval_set, split, adapted)


def route(eval_set: str | None = None, split: str = "all", adapted: Sequence[str] | None = None) -> S09Route:
    return S09Route(eval_set, split, adapted)
