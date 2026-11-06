"""能力矩阵的调度（docs/plan/capability-matrix-evals.md 第 2、4、7、8 节）。

计划（JSON）列出格子、每格的评测与候选；调度对每个 候选 × 格子 × 评测 × 题 × 重复 造一个新的 PinningGateway
（包在 AR_EVAL_GATEWAY_FACTORY 给的 Model Gateway 外面，被测角色钉在候选上、改到候选所在的档），调用评测的 run_item，
把每次的 ItemResult（含 record）写成输出目录里 results.jsonl 的一行。同一个输出目录再跑是续跑：做完的（题、重复）跳过。

    {
      "name": "round1",                       # 写进 capability_scores 的 source
      "task_family": "general.unclassified",  # 第一版都写在这里（第 1 节）
      "n": 40, "r": 3, "seed": 0, "concurrency": 4,
      "item_usd_limit": 0.5,                  # 单题（一次重复）的花费上限，可省
      "extend": false,                        # true：区间与领先者重叠的候选追加样本（+20 道，最多 120）
      "cells": [
        {"role": "plan", "tier": "medium",    # tier 是 Profile 里的 Tier 名（不是 strong@medium）
         "evals": ["s09", {"eval": "bfcl", "n": 100, "r": 1, "options": {}, "est_usd_per_item": 0.002}],
         "candidates": ["a6api/claude-sonnet-5-5@2026-10", "a6api/gpt-6-astra@2026-11"],
         "expensive": ["a6api/gpt-6-astra@2026-11"]}   # 走淘汰赛（第 7 节）
      ]
    }

贵的候选（expensive）先跑 n = 20、r = 1 的淘汰赛：其他候选跑完之后，区间上沿够不着领先者下沿的就停（“不领先”），
其余补到完整的 n、r（淘汰赛的结果算作前 20 道题的第一次重复）。评测的 items 前缀稳定，所以追加样本与淘汰赛做的都是同一批题。

花费：每行记 cost_usd（PinningGateway 记的，含其他角色与裁判的调用）；单题超过 item_usd_limit 后再调模型就在本地拒掉，
这一次记 error = item_usd_limit、按被污染处理（不计分）；总花费到了 budget_usd 就不再开始新的题。边跑边发
runtime.eval.*（suite capability，每个候选一个 run），评测看板读它们。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentruntime_core.providers.telemetry import Telemetry
from agentruntime_worker.executor.model_gateway import ModelGateway

from agentruntime_learning.capability import publication, scoring
from agentruntime_learning.capability.base import Cell, CellEval, Item, ItemResult, PinningGateway
from agentruntime_learning.capability.evals import load_eval
from agentruntime_learning.evals.live import LiveRun
from agentruntime_learning.evals.budget import BudgetError, BudgetGateway

REPO = Path(__file__).resolve().parents[5]
DEFAULT_PROFILES = REPO / "deploy" / "dev" / "model-profiles.json"
DEFAULT_TASK_FAMILY = "general.unclassified"
RESULTS = "results.jsonl"
PLAN_COPY = "plan.json"
RUN_LOCK = "run-lock.json"
SCORES = "scores.json"
JUDGE_LOG = "judgments.jsonl"
SUITE = "capability"
_SLUG = re.compile(r"^[a-z][a-z0-9_]*$")


class PlanError(ValueError):
    """计划不合法（格子、候选、Tier、评测对不上）。"""


class ResumeMismatch(ValueError):
    """输出目录里的结果来自另一版评测集或另一个计划。"""


@dataclass(frozen=True)
class EvalEntry:
    spec: str
    options: Mapping[str, Any]
    n: int
    r: int
    est_usd_per_item: float | None = None


@dataclass(frozen=True)
class CellPlan:
    cell: Cell
    evals: tuple[EvalEntry, ...]
    candidates: tuple[str, ...]
    expensive: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.cell.task_family}/{self.cell.role}/{self.cell.tier}"


@dataclass(frozen=True)
class MatrixPlan:
    name: str
    seed: int
    concurrency: int
    cells: tuple[CellPlan, ...]
    extend: bool = False
    item_usd_limit: float | None = None
    tournament_n: int = scoring.TOURNAMENT_N
    extend_step: int = scoring.EXTEND_STEP
    max_n: int = scoring.MAX_N
    raw: Mapping[str, Any] = field(default_factory=dict)


def _positive(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PlanError(f"{what} 要是正整数，得到 {value!r}")
    return value


def parse_plan(data: Mapping[str, Any]) -> MatrixPlan:
    """校验并解析计划；只查计划本身，候选与 Profile 是否对得上见 check_profiles。"""
    if not isinstance(data, Mapping):
        raise PlanError("计划要是一个 JSON 对象")
    name = str(data.get("name") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        raise PlanError(f"计划要有 name（字母数字与 ._-，最长 64），得到 {name!r}")
    n, r = _positive(data.get("n", 40), "n"), _positive(data.get("r", 3), "r")
    seed = data.get("seed", 0)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise PlanError(f"seed 要是整数，得到 {seed!r}")
    family = str(data.get("task_family") or DEFAULT_TASK_FAMILY)
    limit = data.get("item_usd_limit")
    if limit is not None and (not isinstance(limit, int | float) or limit <= 0):
        raise PlanError(f"item_usd_limit 要是正数，得到 {limit!r}")
    cells: list[CellPlan] = []
    seen: set[str] = set()
    for i, raw in enumerate(data.get("cells") or []):
        where = f"cells[{i}]"
        try:
            cell = Cell(str(raw.get("task_family") or family), str(raw.get("role")), str(raw.get("tier")))
        except (ValueError, AttributeError) as exc:
            raise PlanError(f"{where}：{exc}") from exc
        if not _SLUG.match(cell.tier):
            raise PlanError(f"{where}：tier 是 Profile 里的 Tier 名（例如 medium），得到 {cell.tier!r}")
        if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", cell.task_family):
            raise PlanError(f"{where}：task_family 要形如 general.unclassified，得到 {cell.task_family!r}")
        entries = []
        for j, spec in enumerate(raw.get("evals") or []):
            spec = {"eval": spec} if isinstance(spec, str) else dict(spec)
            if not spec.get("eval"):
                raise PlanError(f"{where}.evals[{j}]：要写评测（<模块>[:<工厂函数>]）")
            est = spec.get("est_usd_per_item")
            entries.append(
                EvalEntry(
                    spec=str(spec["eval"]),
                    options=dict(spec.get("options") or {}),
                    n=_positive(spec.get("n", raw.get("n", n)), f"{where}.evals[{j}].n"),
                    r=_positive(spec.get("r", raw.get("r", r)), f"{where}.evals[{j}].r"),
                    est_usd_per_item=float(est) if est is not None else None,
                )
            )
        candidates = tuple(str(c) for c in raw.get("candidates") or [])
        expensive = tuple(str(c) for c in raw.get("expensive") or [])
        if not entries or not candidates:
            raise PlanError(f"{where}：要有 evals 与 candidates")
        if len(set(candidates)) != len(candidates):
            raise PlanError(f"{where}：candidates 有重复")
        if set(expensive) - set(candidates):
            raise PlanError(f"{where}：expensive 里的 {sorted(set(expensive) - set(candidates))} 不在 candidates 里")
        plan = CellPlan(cell, tuple(entries), candidates, expensive)
        if plan.key in seen:
            raise PlanError(f"{where}：格子 {plan.key} 重复")
        seen.add(plan.key)
        cells.append(plan)
    if not cells:
        raise PlanError("计划里没有格子（cells）")
    return MatrixPlan(
        name=name,
        seed=seed,
        concurrency=_positive(data.get("concurrency", 4), "concurrency"),
        cells=tuple(cells),
        extend=bool(data.get("extend", False)),
        item_usd_limit=float(limit) if limit is not None else None,
        tournament_n=_positive(data.get("tournament_n", scoring.TOURNAMENT_N), "tournament_n"),
        extend_step=_positive(data.get("extend_step", scoring.EXTEND_STEP), "extend_step"),
        max_n=_positive(data.get("max_n", scoring.MAX_N), "max_n"),
        raw=dict(data),
    )


def load_plan(path: Path) -> MatrixPlan:
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"读不出计划 {path}：{exc}") from exc
    return parse_plan(data)


def load_profiles(path: Path = DEFAULT_PROFILES) -> dict[str, dict[str, Any]]:
    data = json.loads(Path(path).read_text("utf-8"))
    return {str(p["id"]): p for p in data.get("profiles") or []}


def check_profiles(plan: MatrixPlan, profiles: Mapping[str, Mapping[str, Any]]) -> None:
    """每个候选都要在 Profile 里、不是嵌入模型，格子的 Tier 要属于它（capability_scores 的 Tier 必须属于该 Profile）。"""
    problems = []
    for cp in plan.cells:
        for c in cp.candidates:
            profile = profiles.get(c)
            if profile is None:
                problems.append(f"{cp.key}：候选 {c} 不在 Profile 里")
            elif profile.get("class") == "embed":
                problems.append(f"{cp.key}：{c} 是嵌入模型，不进矩阵")
            elif cp.cell.tier not in {t.get("name") for t in profile.get("tiers") or []}:
                problems.append(f"{cp.key}：{c} 没有 Tier {cp.cell.tier}")
    if problems:
        raise PlanError("计划与 Profile 对不上：\n" + "\n".join(f"  {p}" for p in problems))


def request_tier(profile: Mapping[str, Any], cell: Cell) -> str:
    """被测角色的调用改成的 Tier：<候选的 class>@<格子的 tier>。"""
    return f"{profile['class']}@{cell.tier}"


# --- 结果文件 ---


def row_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    return (str(row["task_family"]), str(row["role"]), str(row["tier"]), str(row["eval"]), str(row["candidate"]), str(row["item"]), str(row["repeat"]))


def _drop_partial_line(path: Path) -> None:
    """上次写到一半被打断时，最后一行没有换行：截掉它（这一次重跑），新的行才不会接在它后面。"""
    if not path.exists():
        return
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        with path.open("r+b") as f:
            f.truncate(data.rfind(b"\n") + 1)


def read_results(out_dir: Path) -> list[dict[str, Any]]:
    path = Path(out_dir) / RESULTS
    if not path.exists():
        return []
    rows = []
    for line in path.read_text("utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 写到一半被打断的最后一行：这一次重跑
    return rows


def cell_rows(rows: Sequence[Mapping[str, Any]], cell: Cell) -> list[Mapping[str, Any]]:
    return [r for r in rows if (r["task_family"], r["role"], r["tier"]) == (cell.task_family, cell.role, cell.tier)]


# --- 花费估算 ---


@dataclass
class Estimate:
    rows: list[dict[str, Any]]
    """每个 格子 × 评测 × 候选：runs（计划的次数）、done、per_run_usd（来源 basis）、remaining_usd。"""

    @property
    def remaining_usd(self) -> float:
        return sum(r["remaining_usd"] or 0.0 for r in self.rows)

    @property
    def unknown(self) -> int:
        return sum(1 for r in self.rows if r["per_run_usd"] is None and r["runs"] > r["done"])

    @property
    def spent_usd(self) -> float:
        return sum(r["spent_usd"] for r in self.rows)

    def summary(self) -> str:
        lines = ["花费估算（每格 ≈ 候选数 × n × r × 每题花费；追加样本另计）："]
        for r in self.rows:
            per = "未知" if r["per_run_usd"] is None else f"${r['per_run_usd']:.4f}/次（{r['basis']}）"
            extra = f"；淘汰赛后若晋级再跑约 {r['if_advancing_runs']} 次" if r.get("if_advancing_runs") else ""
            lines.append(
                f"  {r['cell']} · {r['eval']} · {r['candidate']}：{r['runs']} 次（已做 {r['done']}），{per}，"
                f"还要约 ${r['remaining_usd'] or 0:.2f}{extra}"
            )
        lines.append(f"合计还要约 ${self.remaining_usd:.2f}（已花 ${self.spent_usd:.2f}）" + (f"；{self.unknown} 项估不出（没有单价或 est_tokens）" if self.unknown else ""))
        return "\n".join(lines)


def _per_run(entry: EvalEntry, ev: CellEval, profile: Mapping[str, Any], done: Sequence[Mapping[str, Any]]) -> tuple[float | None, str]:
    if done:
        return sum(float(r.get("cost_usd") or 0) for r in done) / len(done), "已跑的平均"
    if entry.est_usd_per_item is not None:
        return entry.est_usd_per_item, "计划给的"
    tokens = getattr(ev, "est_tokens", None)
    cost = profile.get("cost") or {}
    if tokens and (tokens[0] or tokens[1]):
        usd = (tokens[0] * float(cost.get("in_per_m") or 0) + tokens[1] * float(cost.get("out_per_m") or 0)) / 1e6
        return usd + float(getattr(ev, "est_other_usd", 0.0) or 0.0), f"约 {tokens[0]}+{tokens[1]} token × 单价"
    return None, ""


def estimate(plan: MatrixPlan, profiles: Mapping[str, Mapping[str, Any]], evals: Mapping[tuple[str, str], CellEval], rows: Sequence[Mapping[str, Any]]) -> Estimate:
    """按计划（不含追加样本）估算还要花的钱；做过的按实际平均花费算。evals 是 (格子, 评测写法) → CellEval。"""
    done: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        done[(f"{r['task_family']}/{r['role']}/{r['tier']}", str(r["eval"]), str(r["candidate"]))].append(r)
    out = []
    for cp in plan.cells:
        for entry in cp.evals:
            ev = evals[(cp.key, entry.spec)]
            available = len(ev.items(entry.n, plan.seed))
            for c in cp.candidates:
                had = done.get((cp.key, ev.name, c), [])
                if c in cp.expensive:
                    runs = min(available, plan.tournament_n)
                    advancing = available * entry.r - runs
                else:
                    runs, advancing = available * entry.r, 0
                per, basis = _per_run(entry, ev, profiles[c], had)
                left = max(0, runs - len(had))
                out.append(
                    {
                        "cell": cp.key,
                        "eval": ev.name,
                        "candidate": c,
                        "runs": runs,
                        "done": len(had),
                        "per_run_usd": per,
                        "basis": basis,
                        "remaining_usd": None if per is None else per * left,
                        "spent_usd": sum(float(r.get("cost_usd") or 0) for r in had),
                        "if_advancing_runs": advancing,
                    }
                )
    return Estimate(out)


# --- 调度 ---


class ItemBudgetExceeded(BudgetError):
    """单题的花费到了 item_usd_limit，不再调模型。"""


class _ItemBudget:
    def __init__(self, inner: ModelGateway, limit: float | None, path: Path) -> None:
        self.guard = BudgetGateway(inner, path, limit) if limit is not None else None
        self.inner = self.guard or inner
        self.limit = limit
        self.spent = self.guard.state["spent_usd_micros"] / 1e6 if self.guard else 0.0
        self.exceeded = False
        self.cost_known = not (self.guard and self.guard.state["pending"])
        self.responses = 0

    async def _call(self, method: str, request: Any) -> Any:
        try:
            response = await getattr(self.inner, method)(request)
        except Exception as exc:
            if isinstance(exc, BudgetError):
                self.exceeded = True
            billed = getattr(exc, "billed_cost_usd_micros", None)
            if isinstance(billed, int) and billed >= 0:
                self.spent += billed / 1e6
            else:
                self.cost_known = False
            raise
        if response.HasField("usage"):
            self.spent += response.usage.cost_usd_micros / 1_000_000
        else:
            self.cost_known = False
        self.responses += 1
        return response

    def close(self) -> None:
        if self.guard:
            self.guard.close()

    async def generate(self, request: Any) -> Any:
        return await self._call("generate", request)

    async def embed(self, request: Any) -> Any:
        return await self._call("embed", request)


@dataclass(frozen=True)
class _Job:
    cell: CellPlan
    ev: CellEval
    candidate: str
    item: Item
    repeat: int


def _outcome(result: ItemResult) -> str:
    if result.contaminated or result.error:
        return "error"
    return "passed" if result.score >= 1.0 else "failed"


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-.")[:40] or "x"


class MatrixRunner:
    """跑一个计划。gateway 是基础的 Model Gateway（每次调用前再包 PinningGateway）；out_dir 是输出目录（续跑同一个）。"""

    def __init__(
        self,
        plan: MatrixPlan,
        gateway: ModelGateway,
        out_dir: Path,
        *,
        profiles: Mapping[str, Mapping[str, Any]] | None = None,
        evals: Mapping[tuple[str, str], CellEval] | None = None,
        telemetry: Telemetry | None = None,
        budget_usd: float | None = None,
        concurrency: int | None = None,
        run_id: str | None = None,
        frozen: Mapping[str, Any] | None = None,
    ) -> None:
        self.plan = plan
        self.gateway = gateway
        self.out_dir = Path(out_dir)
        self.profiles = profiles if profiles is not None else load_profiles()
        check_profiles(plan, self.profiles)
        self.evals = dict(evals) if evals is not None else load_evals(plan)
        self.telemetry = telemetry
        self.budget_usd = budget_usd
        self.concurrency = concurrency or plan.concurrency
        self.run_id = run_id or f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{_slug(plan.name)}"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        _drop_partial_line(self.out_dir / RESULTS)
        self.rows = read_results(self.out_dir)
        self.done = {row_key(r) for r in self.rows}
        self.spent = sum(float(r.get("cost_usd") or 0) for r in self.rows)
        self.new_runs = 0
        self.skipped_for_budget = 0
        self.not_run: list[dict[str, Any]] = []
        self._live: dict[str, LiveRun] = {}
        self._judge: Any = None
        self._total_budget: BudgetGateway | None = None
        self._lock = asyncio.Lock()
        self._check_versions()
        from agentruntime_learning.evals.deepeval_acceptance import provenance

        self.frozen = provenance(frozen) if frozen is not None else None
        self.run_lock = {
            "plan": dict(plan.raw),
            "profiles_sha256": _digest(self.profiles),
            "deployment": self.frozen,
            "budget_usd": budget_usd,
            "concurrency": self.concurrency,
            "evaluations": [
                {"cell": cp.key, "spec": entry.spec, "name": self.evals[(cp.key, entry.spec)].name,
                 "version": str(self.evals[(cp.key, entry.spec)].version),
                 "contract": getattr(self.evals[(cp.key, entry.spec)], "contract", None),
                 "items": [item.id for item in self.evals[(cp.key, entry.spec)].items(plan.max_n if plan.extend else entry.n, plan.seed)]}
                for cp in plan.cells for entry in cp.evals
            ],
        }
        lock_path = self.out_dir / RUN_LOCK
        if lock_path.exists():
            if json.loads(lock_path.read_text("utf-8")) != self.run_lock:
                raise ResumeMismatch("评测计划、题集、Profile或部署配置变了：换一个输出目录，不混合结果")
        elif self.rows:
            raise ResumeMismatch("旧结果没有不可变运行配置：保留旧结果，换一个输出目录")
        else:
            with lock_path.open("x", encoding="utf-8") as lock:
                json.dump(self.run_lock, lock, ensure_ascii=False, sort_keys=True, indent=2)
        self.experiment_digest = _digest(self.run_lock)

    def _check_versions(self) -> None:
        current = {(cp.key, ev.name): str(ev.version) for cp in self.plan.cells for ev in (self.evals[(cp.key, e.spec)] for e in cp.evals)}
        for r in self.rows:
            key = (f"{r['task_family']}/{r['role']}/{r['tier']}", str(r["eval"]))
            if key in current and str(r.get("version")) != current[key]:
                raise ResumeMismatch(f"{self.out_dir} 里 {key[0]} 的 {key[1]} 是版本 {r.get('version')}，现在是 {current[key]}：评测集变了，换一个输出目录")

    def estimate(self) -> Estimate:
        return estimate(self.plan, self.profiles, self.evals, self.rows)

    # --- 实时指标 ---

    async def _live_for(self, candidate: str) -> LiveRun:
        live = self._live.get(candidate)
        if live is None:
            live = LiveRun(self.telemetry, suite=SUITE, subject=self.plan.name, split="matrix", run_id=f"{self.run_id}-{_slug(candidate)}", model=candidate)
            self._live[candidate] = live
            had = [r for r in self.rows if r["candidate"] == candidate]
            await live.planned(0, carried=len(had), carried_cost_usd=sum(float(r.get("cost_usd") or 0) for r in had))
        return live

    # --- 跑题 ---

    def _jobs(self, cp: CellPlan, candidates: Sequence[str], sizes: Mapping[str, tuple[int, int]]) -> list[_Job]:
        """candidates × 评测 × 前 n 道题 × r 次里还没做的；sizes 是 评测写法 → (n, r)。"""
        jobs = []
        for entry in cp.evals:
            ev = self.evals[(cp.key, entry.spec)]
            n, r = sizes[entry.spec]
            items = ev.items(n, self.plan.seed)
            for c in candidates:
                for item in items:
                    for repeat in range(r):
                        key = (cp.cell.task_family, cp.cell.role, cp.cell.tier, ev.name, c, item.id, str(repeat))
                        if key not in self.done:
                            jobs.append(_Job(cp, ev, c, item, repeat))
        return jobs

    async def _run_jobs(self, jobs: Sequence[_Job]) -> None:
        if not jobs:
            return
        planned: dict[str, int] = defaultdict(int)
        for job in jobs:
            planned[job.candidate] += 1
        for candidate, count in sorted(planned.items()):
            await (await self._live_for(candidate)).planned(count)
        sem = asyncio.Semaphore(self.concurrency)

        async def one(job: _Job) -> None:
            async with sem:
                if self.budget_usd is not None and self.spent >= self.budget_usd:
                    self._skip(job, "experiment_budget_exhausted")
                    return
                if self._total_budget is not None and (
                    self._total_budget.state["pending"] and not self._total_budget.lock.locked()
                    or self._total_budget.state["spent_usd_micros"] >= self._total_budget.limit
                ):
                    self._skip(job, "experiment_budget_unavailable")
                    return
                await self._run_one(job)

        await asyncio.gather(*(one(j) for j in jobs))

    def _skip(self, job: _Job, reason: str) -> None:
        self.skipped_for_budget += 1
        self.not_run.append({"cell": job.cell.key, "eval": job.ev.name, "candidate": job.candidate,
                             "item": job.item.id, "repeat": job.repeat, "reason": reason})

    async def _run_one(self, job: _Job) -> None:
        cp, ev, cell = job.cell, job.ev, job.cell.cell
        base = self.gateway
        uses_judge = bool(getattr(ev, "uses_judge", False))
        identity = (cp.key, ev.name, job.candidate, job.item.id, job.repeat)
        item_key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        budget = _ItemBudget(base, self.plan.item_usd_limit, self.out_dir / "item-budgets" / f"{item_key}.json")
        base = budget
        if uses_judge:
            from agentruntime_learning.evals.judging import RecordingJudgeGateway

            # Every retry/fallback goes through both item and experiment caps.
            base = RecordingJudgeGateway(budget, self.out_dir / JUDGE_LOG)
        pin = PinningGateway(base, role=cell.role, candidate=job.candidate, tier=request_tier(self.profiles[job.candidate], cell))
        try:
            if uses_judge:
                from agentruntime_learning.evals.judging import judging

                with judging(eval=ev.name, case_id=job.item.id, repeat=job.repeat, candidate=job.candidate, subject_models=[job.candidate]):
                    result = await ev.run_item(job.item, pin, candidate=job.candidate)
            else:
                result = await ev.run_item(job.item, pin, candidate=job.candidate)
        except Exception as exc:  # noqa: BLE001 — 评测出错只记这一次（0 分），整轮继续
            result = ItemResult(job.item.id, score=0.0, error=f"{type(exc).__name__}: {exc}"[:300])
        finally:
            budget.close()
        total_unavailable = self._total_budget is not None and (
            self._total_budget.state["pending"] or self._total_budget.state["spent_usd_micros"] >= self._total_budget.limit
        )
        if budget.exceeded and total_unavailable and budget.responses == 0 and budget.spent == 0 and budget.cost_known:
            self._skip(job, "experiment_budget_unavailable")
            return  # No provider observation: must remain eligible for a reconciled resume.
        contaminated = bool(result.contaminated or pin.contaminated)
        error = result.error
        if budget.exceeded:
            error, contaminated = ("experiment_budget_exhausted" if total_unavailable else "item_usd_limit"), True
        cost = max(float(result.cost_usd or 0.0), pin.cost_usd, budget.spent)
        row = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "plan": self.plan.name,
            "plan_sha256": _digest(self.plan.raw),
            "experiment_sha256": self.experiment_digest,
            "deployment_frozen": self.frozen is not None,
            "task_family": cell.task_family,
            "role": cell.role,
            "tier": cell.tier,
            "eval": ev.name,
            "version": str(ev.version),
            "candidate": job.candidate,
            "item": job.item.id,
            "repeat": job.repeat,
            "score": float(result.score),
            "contaminated": contaminated,
            "error": error,
            "cost_usd": round(cost, 6),
            "cost_known": budget.cost_known,
            "calls": {f"{role}:{profile}": n for (role, profile), n in sorted(pin.calls.items())},
            "pinned_calls": pin.pinned_calls,
            "missed_calls": pin.missed_calls,
            "record": result.record,
        }
        async with self._lock:
            with (self.out_dir / RESULTS).open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.rows.append(row)
            self.done.add(row_key(row))
            self.spent += cost
            self.new_runs += 1
        await (await self._live_for(job.candidate)).case(_outcome(ItemResult(job.item.id, score=row["score"], contaminated=contaminated, error=error)), cost)

    # --- 一个格子 ---

    def score_cell(self, cp: CellPlan) -> scoring.CellScore:
        return scoring.score_cell(cell_rows(self.rows, cp.cell), cp.candidates, expensive=cp.expensive, tournament_n=self.plan.tournament_n, seed=self.plan.seed)

    async def run_cell(self, cp: CellPlan) -> scoring.CellScore:
        for entry in cp.evals:
            ev = self.evals[(cp.key, entry.spec)]
            if ev.role != cp.cell.role:
                raise PlanError(f"{cp.key}：评测 {entry.spec} 测的是 {ev.role}，格子是 {cp.cell.role}")
            if getattr(ev, "uses_judge", False) and cp.cell.role == "judge":
                raise PlanError(f"{cp.key}：被测角色是 judge 的格子不能再用固定的裁判（{entry.spec} 的 uses_judge）")
        full = {e.spec: (e.n, e.r) for e in cp.evals}
        regular = [c for c in cp.candidates if c not in cp.expensive]
        expensive = list(cp.expensive)
        await self._run_jobs(self._jobs(cp, regular, full))
        if expensive and regular:
            await self._run_jobs(self._jobs(cp, expensive, {e.spec: (min(e.n, self.plan.tournament_n), 1) for e in cp.evals}))
            cell = self.score_cell(cp)
            advancing = [c for c in expensive if cell.candidates[c].status != scoring.NOT_LEADING]
            await self._run_jobs(self._jobs(cp, advancing, full))
        elif expensive:
            await self._run_jobs(self._jobs(cp, expensive, full))
        sizes = dict(full)
        while self.plan.extend:
            cell = self.score_cell(cp)
            close = scoring.close_to_leader(cell)
            grown = {}
            for e in cp.evals:
                n, r = sizes[e.spec]
                target = min(n + self.plan.extend_step, self.plan.max_n)
                ev = self.evals[(cp.key, e.spec)]
                if target > n and len(ev.items(target, self.plan.seed)) > len(ev.items(n, self.plan.seed)):
                    grown[e.spec] = (target, r)
            if not close or not grown:
                break
            sizes.update(grown)
            await self._run_jobs(self._jobs(cp, close, sizes))
        return self.score_cell(cp)

    async def run(self) -> dict[str, Any]:
        original_gateway = self.gateway
        if self.budget_usd is not None:
            self._total_budget = BudgetGateway(self.gateway, self.out_dir / "model-spending.json", self.budget_usd, prior_cost_usd=self.spent)
            self.gateway = self._total_budget
        try:
            return await self._run()
        finally:
            if self._total_budget:
                self._total_budget.close()
            self.gateway = original_gateway
            self._judge = None

    async def _run(self) -> dict[str, Any]:
        (self.out_dir / PLAN_COPY).write_text(json.dumps(dict(self.plan.raw), ensure_ascii=False, indent=2), "utf-8")
        for cp in self.plan.cells:
            await self.run_cell(cp)
        report = score_results(self.plan, self.rows)
        if self._total_budget:
            report["budget"] = dict(self._total_budget.state)
        report["not_run"] = self.not_run
        write_scores(self.out_dir, report)
        for cell in report["cells"]:
            for c in cell["candidates"]:
                live = self._live.get(c["candidate"])
                if live is not None and c["score"] is not None:
                    await live.score(f"{cell['role']}.{cell['tier']}", c["score"])
        for live in self._live.values():
            await live.finished()
        return report


def load_evals(plan: MatrixPlan) -> dict[tuple[str, str], CellEval]:
    """(格子, 评测写法) → CellEval；同一个格子里两项评测的 name 不能相同。"""
    out: dict[tuple[str, str], CellEval] = {}
    for cp in plan.cells:
        names: set[str] = set()
        for entry in cp.evals:
            try:
                ev = load_eval(entry.spec, entry.options)
            except ValueError as exc:
                raise PlanError(f"{cp.key}：{exc}") from exc
            if ev.name in names:
                raise PlanError(f"{cp.key}：两项评测都叫 {ev.name}")
            names.add(ev.name)
            out[(cp.key, entry.spec)] = ev
    return out


# --- 汇总 ---


def score_results(plan: MatrixPlan, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """按计划把结果行折算成分数（scoring），每格一项；at 是这一格最后一个结果的日期（观测日期）。"""
    cells = []
    for cp in plan.cells:
        mine = cell_rows(rows, cp.cell)
        cell = scoring.score_cell(mine, cp.candidates, expensive=cp.expensive, tournament_n=plan.tournament_n, seed=plan.seed)
        evals = sorted({str(r["eval"]) for r in mine})
        versions = {str(r["eval"]): str(r.get("version", "")) for r in mine}
        at = max((str(r.get("at", ""))[:10] for r in mine), default="")
        cells.append(
            {
                "task_family": cp.cell.task_family,
                "role": cp.cell.role,
                "tier": cp.cell.tier,
                "evals": [{"name": e, "version": versions[e]} for e in evals],
                "at": at,
                **cell.to_json(),
            }
        )
        extras = eval_extras(mine)
        if extras:
            cells[-1]["extras"] = extras
        # Use resolved evaluator names (the rows carry these, not factory specs).
        expected_repeats = {
            load_eval(entry.spec, entry.options).name: entry.r
            for entry in cp.evals
        }
        publication.enforce(cells[-1], mine, expected_repeats=expected_repeats, allow_extended_samples=plan.extend)
        mismatched = (
            any(r.get("plan_sha256") and r["plan_sha256"] != _digest(plan.raw) for r in mine)
            or bool({r["candidate"] for r in mine} - set(cp.candidates))
            or len({r["experiment_sha256"] for r in mine if r.get("experiment_sha256")}) > 1
        )
        if mismatched:
            cells[-1]["problems"].append("原始记录与冻结计划/候选/实验不一致")
            for candidate in cells[-1]["candidates"]:
                candidate.update(publishable=False, status="inconsistent", reason="experiment_mismatch")
            cells[-1]["publication"]["coverage_ok"] = False
    return {"plan": plan.name, "seed": plan.seed, "scored_at": datetime.now(UTC).isoformat(timespec="seconds"), "cells": cells}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def verify_run_lock(out_dir: Path, plan: MatrixPlan, rows: Sequence[Mapping[str, Any]]) -> None:
    path = Path(out_dir) / RUN_LOCK
    if not path.exists():
        # Legacy results can be diagnosed but cannot be resumed. S09 and all
        # other publication gates still apply when revalidating old evidence.
        if any(row.get("experiment_sha256") for row in rows):
            raise ResumeMismatch("运行配置丢失，不能验证新结果")
        return
    frozen = json.loads(path.read_text("utf-8"))
    if frozen["plan"] != dict(plan.raw):
        raise ResumeMismatch("不能改写冻结计划再评分或发布")
    digest = _digest(frozen)
    if any(row.get("experiment_sha256") != digest for row in rows):
        raise ResumeMismatch("结果与冻结实验摘要不符")
    evaluations = {(entry["cell"], entry["name"]): entry for entry in frozen["evaluations"]}
    for row in rows:
        key = (f"{row['task_family']}/{row['role']}/{row['tier']}", row["eval"])
        entry = evaluations.get(key)
        if entry is None or row["version"] != entry["version"] or row["item"] not in entry["items"]:
            raise ResumeMismatch("结果的评测、版本或题目不属于冻结题集")
        if entry["contract"] is not None and (row.get("record") or {}).get("scoring_contract") != entry["contract"]:
            raise ResumeMismatch("规划评分证据与运行时冻结的实际提示/校验不符")


def eval_extras(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """评测自带的另一种口径，按候选汇总，只写进报告、不进分数：SimpleQA 的官方 F 分数与答错率（simpleqa.official_metrics）。出错的题不算。"""
    from agentruntime_learning.capability.evals import simpleqa

    grades: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        grade = (r.get("record") or {}).get("grade")
        if r.get("eval") == simpleqa.NAME and not r.get("error") and grade:
            grades[str(r["candidate"])].append(str(grade))
    return {simpleqa.NAME: {c: simpleqa.official_metrics(g) for c, g in sorted(grades.items())}} if grades else {}


def write_scores(out_dir: Path, report: Mapping[str, Any]) -> Path:
    path = Path(out_dir) / SCORES
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    return path


def scores_summary(report: Mapping[str, Any]) -> str:
    lines = [f"能力矩阵 {report['plan']}："]
    for cell in report["cells"]:
        evals = "+".join(e["name"] for e in cell["evals"]) or "—"
        lines.append(f"  {cell['task_family']} × {cell['role']} × {cell['tier']}（{evals}）领先：{cell['leader'] or '—'}")
        lines += [f"    ⚠ {p}" for p in cell["problems"]]
        for c in cell["candidates"]:
            if c["p"] is None:
                lines.append(f"    {c['candidate']}：{c['status_text']}（{c['reason']}）")
                continue
            ci = c["ci"]
            tag = "" if c["publishable"] else f"，不发布：{c['reason']}"
            same = f"；与 {'、'.join(c['indistinguishable'])} 不可分" if c["indistinguishable"] else ""
            lines.append(
                f"    {c['candidate']}：分数 {c['score']:.3f}（原始 {c['p']:.3f}，95% 区间 {ci[0]:.3f}–{ci[1]:.3f}，n = {c['items']}、r = {c['repeats']}，"
                f"污染 {c['contaminated']}/{c['runs']}，花费 ${c['cost_usd']:.2f}）{tag}{same}"
            )
    return "\n".join(lines)


__all__ = [
    "DEFAULT_PROFILES",
    "DEFAULT_TASK_FAMILY",
    "RESULTS",
    "SCORES",
    "CellPlan",
    "EvalEntry",
    "Estimate",
    "ItemBudgetExceeded",
    "MatrixPlan",
    "MatrixRunner",
    "PlanError",
    "ResumeMismatch",
    "check_profiles",
    "estimate",
    "load_evals",
    "load_plan",
    "load_profiles",
    "parse_plan",
    "read_results",
    "request_tier",
    "score_results",
    "scores_summary",
    "write_scores",
]
