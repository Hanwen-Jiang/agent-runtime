"""能力矩阵的分数折算（capability.scoring，设计稿第 4、7 节）。"""

from __future__ import annotations

from typing import Any

import pytest

from agentruntime_learning.capability import scoring as sc


def rows(candidate: str, scores: list[list[float]], *, eval_name: str = "e", contaminated: set[tuple[int, int]] = frozenset(), version: str = "v1") -> list[dict[str, Any]]:
    """scores[i][j] 是第 i 道题第 j 次的分数；contaminated 里的 (i, j) 记为被污染。"""
    return [
        {"candidate": candidate, "eval": eval_name, "version": version, "item": f"{eval_name}-{i}", "repeat": j, "score": s, "contaminated": (i, j) in contaminated, "cost_usd": 0.01}
        for i, item in enumerate(scores)
        for j, s in enumerate(item)
    ]


def binary(n: int, p: float, r: int = 1) -> list[list[float]]:
    return [[1.0 if i < round(n * p) else 0.0] * r for i in range(n)]


def test_shrinkage_pulls_small_samples_towards_the_prior() -> None:
    assert sc.shrink(1.0, 0) == 0.5
    assert sc.shrink(0.9, 40) == pytest.approx((40 * 0.9 + 10 * 0.5) / 50)
    assert sc.shrink(0.9, 10) < sc.shrink(0.9, 100) < 0.9
    with pytest.raises(ValueError):
        sc.shrink(0.5, 0, k=0)


def test_an_item_is_the_mean_of_its_clean_repeats() -> None:
    data = rows("c", [[1.0, 0.0, 1.0], [0.0, 0.0, 1.0]], contaminated={(1, 2)})
    assert sc.item_means(data) == {"e": {"e-0": pytest.approx(2 / 3), "e-1": 0.0}}
    everything = rows("c", [[1.0]], contaminated={(0, 0)})
    assert sc.item_means(everything) == {}, "全被污染的题不算"


def test_the_bootstrap_resamples_items_not_repeats() -> None:
    """同一道题的 r 次不是独立样本：r = 3 次完全一样的结果，区间和 r = 1 一样宽。"""
    once = sc.score_candidate("c", rows("c", binary(40, 0.75, r=1)))
    thrice = sc.score_candidate("c", rows("c", binary(40, 0.75, r=3)))
    assert once.ci == thrice.ci and once.items == thrice.items == 40 and thrice.repeats == 3
    assert once.ci == sc.score_candidate("c", rows("c", binary(40, 0.75))).ci, "seed 固定，结果可复现"


def test_bootstrap_interval_and_raw_score() -> None:
    s = sc.score_candidate("c", rows("c", binary(100, 0.8)))
    assert s.p == pytest.approx(0.8) and s.score == pytest.approx(sc.shrink(0.8, 100))
    lo, hi = s.ci
    assert lo < 0.8 < hi and 0.05 < s.half_width < 0.1  # 二项分布 n = 100 时约 ±0.078
    assert s.status == sc.OK and s.publishable


def test_wide_intervals_are_not_published() -> None:
    small = sc.score_candidate("c", rows("c", binary(12, 0.5)))
    assert small.half_width > sc.MAX_HALF_WIDTH
    assert small.status == sc.INSUFFICIENT and not small.publishable and "样本不足" in sc.STATUS_TEXT[small.status]
    assert small.score is not None, "收缩分照算，只供参考"


def test_more_than_five_percent_contamination_invalidates_the_run() -> None:
    clean = binary(100, 0.8)
    few = sc.score_candidate("c", rows("c", clean, contaminated={(i, 0) for i in range(5)}))
    many = sc.score_candidate("c", rows("c", clean, contaminated={(i, 0) for i in range(6)}))
    assert few.status == sc.OK and few.items == 95, "被污染的不计分"
    assert many.status == sc.CONTAMINATED and not many.publishable and many.contamination_rate == pytest.approx(0.06)


def test_several_evals_are_weighted_by_item_count() -> None:
    data = rows("c", binary(80, 1.0), eval_name="a") + rows("c", binary(20, 0.0), eval_name="b")
    s = sc.score_candidate("c", data)
    assert s.p == pytest.approx(0.8) and s.by_eval == {"a": 80, "b": 20}
    lo, hi = s.ci
    assert lo == hi == pytest.approx(0.8), "按评测分层重抽样：每组内结果都一样时区间退化成一点"


def test_overlapping_candidates_are_flagged_indistinguishable() -> None:
    data = rows("a", binary(100, 0.80)) + rows("b", binary(100, 0.78)) + rows("c", binary(100, 0.30))
    cell = sc.score_cell(data, ["a", "b", "c"])
    assert cell.leader == "a"
    assert cell.candidates["a"].indistinguishable == ["b"] and cell.candidates["c"].indistinguishable == []
    assert sc.close_to_leader(cell) == ["a", "b"]


def test_a_far_ahead_leader_needs_no_more_samples() -> None:
    cell = sc.score_cell(rows("a", binary(100, 0.95)) + rows("b", binary(100, 0.4)), ["a", "b"])
    assert sc.close_to_leader(cell) == []


def test_the_tournament_stops_expensive_candidates_that_cannot_lead() -> None:
    data = rows("leader", binary(40, 0.9, r=3)) + rows("weak", binary(20, 0.2)) + rows("strong", binary(20, 0.9))
    cell = sc.score_cell(data, ["leader", "weak", "strong"], expensive=["weak", "strong"])
    assert cell.candidates["weak"].status == sc.NOT_LEADING and "淘汰赛" in cell.candidates["weak"].reason
    assert cell.candidates["strong"].status != sc.NOT_LEADING
    assert cell.leader in ("leader", "strong") and cell.leader != "weak"


def test_mixed_eval_versions_make_the_cell_unpublishable() -> None:
    data = rows("a", binary(50, 0.8), version="v1") + rows("b", binary(50, 0.8), version="v2")
    cell = sc.score_cell(data, ["a", "b"])
    assert cell.problems and all(s.status == sc.INCONSISTENT for s in cell.candidates.values())


def test_a_candidate_without_results_has_no_score() -> None:
    cell = sc.score_cell(rows("a", binary(50, 0.8)), ["a", "b"])
    assert cell.candidates["b"].status == sc.NO_RESULTS and cell.candidates["b"].score is None
    assert cell.to_json()["candidates"][1]["publishable"] is False
