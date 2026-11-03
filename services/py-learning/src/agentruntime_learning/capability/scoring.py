"""能力矩阵的分数折算（docs/plan/capability-matrix-evals.md 第 4、7 节）。

输入是调度器落盘的结果行（matrix.RESULTS，一道题跑一次一行），只看 eval、item、repeat、score、contaminated：

1. 题级结果：一道题 r 次里没被污染的那几次取平均；r 次全被污染的题不算。
2. 原始分 p：题级结果的平均（多个评测合成一个格子时按题数加权，即所有题一起平均）。
3. 置信区间：按题做 bootstrap（2000 次，按评测分层重抽样，seed 固定）；同一道题的 r 次不当成独立样本。
4. 收缩：score = (n·p + k·0.5) / (n + k)，k = 10，n 是题数。
5. 区间半宽 > 0.15：不写分数（样本不足）；被污染的次数超过 5%：整次运行无效，不写分数。
6. 两个候选的区间重叠：照写各自的收缩分，报告标“不可分”。
7. 追加样本：只对区间与领先者重叠的候选（连同领先者）每次加 20 道，最多到 120 道。
8. 淘汰赛（贵的模型）：先 n = 20、r = 1；区间上沿够不着领先者的下沿就停，写“不领先”。
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

K = 10
PRIOR = 0.5
RESAMPLES = 2000
LEVEL = 0.95
MAX_HALF_WIDTH = 0.15
MAX_CONTAMINATION = 0.05
TOURNAMENT_N = 20
EXTEND_STEP = 20
MAX_N = 120

OK = "ok"
NO_RESULTS = "no_results"
INSUFFICIENT = "insufficient"
CONTAMINATED = "contaminated"
NOT_LEADING = "not_leading"
INCONSISTENT = "inconsistent"
STATUS_TEXT = {
    OK: "可发布",
    NO_RESULTS: "没有结果",
    INSUFFICIENT: "样本不足",
    CONTAMINATED: "污染过多，运行无效",
    NOT_LEADING: "不领先（淘汰赛）",
    INCONSISTENT: "评测集版本不一致",
}


def shrink(p: float, n: int, *, k: float = K, prior: float = PRIOR) -> float:
    """向先验收缩：(n·p + k·prior) / (n + k)。"""
    if n < 0 or k < 0 or n + k == 0:
        raise ValueError(f"n、k 不能为负且不能同时为 0，得到 n={n}、k={k}")
    return (n * p + k * prior) / (n + k)


def item_means(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    """评测 → {题: 没被污染的那几次的平均}；全被污染的题不出现。"""
    scores: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows:
        if not row.get("contaminated"):
            scores[(str(row["eval"]), str(row["item"]))].append(float(row["score"]))
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for (name, item), values in sorted(scores.items()):
        out[name][item] = sum(values) / len(values)
    return dict(out)


def bootstrap_ci(groups: Mapping[str, Sequence[float]], *, resamples: int = RESAMPLES, seed: Any = 0, level: float = LEVEL) -> tuple[float, float]:
    """按题重抽样的百分位区间；groups 是评测 → 题级结果，每组在组内重抽样（按题数加权）。"""
    data = [list(v) for _, v in sorted(groups.items()) if v]
    total = sum(len(v) for v in data)
    if total == 0:
        raise ValueError("没有题，算不出区间")
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        acc = 0.0
        for values in data:
            m = len(values)
            acc += sum(values[rng.randrange(m)] for _ in range(m))
        means.append(acc / total)
    means.sort()
    alpha = (1 - level) / 2
    lo = means[max(0, math.floor(alpha * resamples))]
    hi = means[min(resamples - 1, math.ceil((1 - alpha) * resamples) - 1)]
    return lo, hi


@dataclass
class CandidateScore:
    candidate: str
    items: int = 0
    """有结果的题数（n）。"""
    repeats: int = 0
    """一道题最多跑了几次（r）。"""
    runs: int = 0
    contaminated: int = 0
    errors: int = 0
    cost_usd: float = 0.0
    p: float | None = None
    ci: tuple[float, float] | None = None
    half_width: float | None = None
    score: float | None = None
    """收缩后的分数；status 不是 ok 时只供参考，不发布。"""
    status: str = NO_RESULTS
    reason: str = ""
    by_eval: dict[str, int] = field(default_factory=dict)
    """评测 → 题数。"""
    indistinguishable: list[str] = field(default_factory=list)
    """区间与它重叠的其他候选（“不可分”）。"""

    @property
    def contamination_rate(self) -> float:
        return self.contaminated / self.runs if self.runs else 0.0

    @property
    def publishable(self) -> bool:
        return self.status == OK

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["ci"] = list(self.ci) if self.ci else None
        data["contamination_rate"] = self.contamination_rate
        data["publishable"] = self.publishable
        data["status_text"] = STATUS_TEXT.get(self.status, self.status)
        return data


def score_candidate(
    candidate: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    seed: Any = 0,
    resamples: int = RESAMPLES,
    k: float = K,
    max_half_width: float = MAX_HALF_WIDTH,
    max_contamination: float = MAX_CONTAMINATION,
) -> CandidateScore:
    """一个候选在一个格子里的分数（rows 只含这个候选、这个格子的结果行）。"""
    out = CandidateScore(candidate=candidate, runs=len(rows))
    if not rows:
        out.reason = "还没有结果"
        return out
    out.contaminated = sum(1 for r in rows if r.get("contaminated"))
    out.errors = sum(1 for r in rows if r.get("error") and not r.get("contaminated"))
    out.cost_usd = round(sum(float(r.get("cost_usd") or 0) for r in rows), 6)
    out.repeats = max(int(r.get("repeat", 0)) for r in rows) + 1
    means = item_means(rows)
    out.by_eval = {name: len(items) for name, items in means.items()}
    out.items = sum(out.by_eval.values())
    if out.items:
        out.p = sum(v for items in means.values() for v in items.values()) / out.items
        out.ci = bootstrap_ci({name: list(items.values()) for name, items in means.items()}, resamples=resamples, seed=f"{seed}:{candidate}")
        out.half_width = (out.ci[1] - out.ci[0]) / 2
        out.score = shrink(out.p, out.items, k=k)
    if out.contamination_rate > max_contamination:
        out.status = CONTAMINATED
        out.reason = f"被污染 {out.contaminated}/{out.runs} 次（{out.contamination_rate:.1%}），超过 {max_contamination:.0%}：钉的模型没用上，运行无效"
    elif not out.items:
        out.status = NO_RESULTS
        out.reason = "没有可计分的题"
    elif out.half_width is not None and out.half_width > max_half_width:
        out.status = INSUFFICIENT
        out.reason = f"区间半宽 {out.half_width:.3f} > {max_half_width}（n = {out.items}）：不写分数，路由照旧用 0.5 的先验"
    else:
        out.status = OK
    return out


def overlap(a: CandidateScore, b: CandidateScore) -> bool:
    if a.ci is None or b.ci is None:
        return False
    return a.ci[0] <= b.ci[1] and b.ci[0] <= a.ci[1]


def leader_of(scores: Iterable[CandidateScore]) -> CandidateScore | None:
    """领先者：有区间、没被判无效或淘汰的候选里收缩分最高的（同分取 id 小的）。"""
    eligible = [s for s in scores if s.score is not None and s.ci is not None and s.status not in (CONTAMINATED, NOT_LEADING, INCONSISTENT)]
    return min(eligible, key=lambda s: (-s.score, s.candidate)) if eligible else None


def eliminated(challenger: CandidateScore, leader: CandidateScore | None) -> bool:
    """淘汰赛：区间上沿够不着领先者的下沿。"""
    return leader is not None and challenger.ci is not None and leader.ci is not None and challenger.ci[1] < leader.ci[0]


@dataclass
class CellScore:
    candidates: dict[str, CandidateScore]
    leader: str | None = None
    problems: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "leader": self.leader,
            "problems": self.problems,
            "candidates": [self.candidates[c].to_json() for c in sorted(self.candidates)],
        }


def score_cell(
    rows: Sequence[Mapping[str, Any]],
    candidates: Sequence[str],
    *,
    expensive: Sequence[str] = (),
    tournament_n: int = TOURNAMENT_N,
    seed: Any = 0,
    **options: Any,
) -> CellScore:
    """一个格子里所有候选的分数，加上领先者、“不可分”与淘汰赛的判定。rows 是这个格子的全部结果行。"""
    by_candidate: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    versions: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        by_candidate[str(row["candidate"])].append(row)
        versions[str(row["eval"])].add(str(row.get("version", "")))
    cell = CellScore(candidates={c: score_candidate(c, by_candidate.get(c, []), seed=seed, **options) for c in candidates})
    mixed = sorted(name for name, v in versions.items() if len(v) > 1)
    if mixed:
        cell.problems.append(f"评测集版本不一致：{'、'.join(mixed)}（同一格里的候选要做同一批题，换一个输出目录重跑）")
        for s in cell.candidates.values():
            s.status, s.reason = INCONSISTENT, cell.problems[-1]
        return cell
    regular = [cell.candidates[c] for c in candidates if c not in expensive]
    regular_leader = leader_of(regular)
    for c in expensive:
        s = cell.candidates[c]
        in_tournament = s.repeats <= 1 and s.items <= tournament_n
        if s.runs and in_tournament and regular_leader is not None and eliminated(s, regular_leader):
            s.status = NOT_LEADING
            s.reason = f"淘汰赛（n = {s.items}、r = 1）：区间上沿 {s.ci[1]:.3f} 低于领先者 {regular_leader.candidate} 的下沿 {regular_leader.ci[0]:.3f}"
    leader = leader_of(cell.candidates.values())
    cell.leader = leader.candidate if leader else None
    scored = [s for s in cell.candidates.values() if s.ci is not None]
    for s in scored:
        s.indistinguishable = sorted(o.candidate for o in scored if o is not s and overlap(s, o))
    return cell


def close_to_leader(cell: CellScore) -> list[str]:
    """追加样本的对象：区间与领先者重叠的候选（连同领先者）；领先者遥遥领先时为空。"""
    if cell.leader is None:
        return []
    leader = cell.candidates[cell.leader]
    close = [
        s.candidate
        for s in cell.candidates.values()
        if s is not leader and s.status not in (CONTAMINATED, NOT_LEADING, INCONSISTENT) and overlap(s, leader)
    ]
    return sorted([leader.candidate, *close]) if close else []


__all__ = [
    "CONTAMINATED",
    "EXTEND_STEP",
    "INCONSISTENT",
    "INSUFFICIENT",
    "K",
    "MAX_CONTAMINATION",
    "MAX_HALF_WIDTH",
    "MAX_N",
    "NOT_LEADING",
    "NO_RESULTS",
    "OK",
    "RESAMPLES",
    "STATUS_TEXT",
    "TOURNAMENT_N",
    "CandidateScore",
    "CellScore",
    "bootstrap_ci",
    "close_to_leader",
    "eliminated",
    "item_means",
    "leader_of",
    "overlap",
    "score_candidate",
    "score_cell",
    "shrink",
]
