"""Exact ranking equivalence to the pre-optimization MMR and a work bound."""
from __future__ import annotations

import copy
import math
import random

import pytest

from agentruntime_memory import service


def original(scored, top_k):
    # Frozen original loop, independent of the incremental implementation.
    selected = [s for s in scored if s[0]][:top_k]
    pool = [s for s in scored if not s[0]]
    best = max((s[1] for s in pool), default=1.0) or 1.0
    while pool and len(selected) < top_k:
        choice, value = None, -math.inf
        for s in pool:
            similarity = max((service._cosine(s[4], t[4]) for t in selected), default=0.0)
            if similarity >= service.REDUNDANT:
                continue
            mmr = service.MMR_LAMBDA * s[1] / best - (1 - service.MMR_LAMBDA) * similarity
            if mmr > value:
                choice, value = s, mmr
        if choice is None:
            break
        selected.append(choice)
        pool.remove(choice)
    return selected


def candidates(vectors, *, exact=(), scores=None):
    rows = [(i in exact, scores[i] if scores else 1.0, (i % 2, f"item-{i:04}"),
             {"id": f"item-{i:04}"}, tuple(vector)) for i, vector in enumerate(vectors)]
    return sorted(rows, key=lambda s: (not s[0], -s[1], s[2]))


def ids(rows):
    return [s[2] for s in rows]


@pytest.mark.parametrize("seed", range(32))
def test_random_rankings_match_original_exactly(seed):
    rng = random.Random(seed)
    size, dimension = rng.randrange(1, 65), rng.randrange(1, 33)
    vectors = [tuple(rng.uniform(-1, 1) for _ in range(dimension)) for _ in range(size)]
    scores = [rng.choice((0.2, 0.5, 0.75, 1.0, rng.random())) for _ in range(size)]
    scored = candidates(vectors, exact={i for i in range(size) if rng.random() < 0.08}, scores=scores)
    untouched = copy.deepcopy(scored)
    for top_k in (1, 2, 10, 50):
        assert ids(service._mmr_select(scored, top_k)) == ids(original(scored, top_k))
    assert scored == untouched


@pytest.mark.parametrize("exact", [(), (0,), (0, 1), (0, 1, 2)])
@pytest.mark.parametrize("top_k", [1, 3, 10])
def test_negative_zero_duplicate_unequal_and_nan_vectors_keep_order(exact, top_k):
    scored = candidates([
        (1.0, 0.0), (-1.0, 0.0), (0.0, -1.0), (0.0, 0.0),
        (1.0, 0.0), (), (1.0,), (float("nan"), 0.0), (-0.0, 0.0),
    ], exact=exact)
    assert ids(service._mmr_select(scored, top_k)) == ids(original(scored, top_k))


def test_first_negative_similarity_is_not_clamped_to_zero():
    rows = candidates([(1, 0), (-1, 0), (0, 1)], scores=[1.0, 0.6, 0.9])
    assert ids(service._mmr_select(rows, 2)) == [(0, "item-0000"), (1, "item-0001")]
    assert ids(service._mmr_select(rows, 2)) == ids(original(rows, 2))


def test_redundancy_boundary_and_ties_match(monkeypatch):
    values = {(1, 0): 0.9, (2, 0): math.nextafter(0.9, 0), (3, 0): -0.0}
    # _mmr_select passes each vector's cached norm (norm_a, norm_b); the stand-in ignores them.
    monkeypatch.setattr(service, "_cosine", lambda a, b, **_: values.get((a[0], b[0]), 0.0))
    rows = candidates([(0,), (1,), (2,), (3,)], scores=[1.0, 0.7, 0.7, 0.7])
    assert ids(service._mmr_select(rows, 4)) == ids(original(rows, 4))


def test_cosine_work_is_linear_in_selected_count(monkeypatch):
    # Orthogonal candidates make every selected position reachable. Count the
    # expensive operation instead of relying on a noisy wall-clock assertion.
    rows = candidates([tuple(float(i == j) for j in range(250)) for i in range(250)])
    cosine = service._cosine
    calls = 0
    def counted(a, b, **norms):
        nonlocal calls
        calls += 1
        return cosine(a, b, **norms)
    monkeypatch.setattr(service, "_cosine", counted)
    optimized = service._mmr_select(rows, 50)
    optimized_calls, calls = calls, 0
    reference = original(rows, 50)
    assert ids(optimized) == ids(reference)
    assert optimized_calls <= 250 * 50
    assert calls > 20 * optimized_calls


def test_empty_candidates_and_exact_only():
    assert service._mmr_select([], 50) == []
    rows = candidates([(1,), (1,), (1,)], exact=(0, 1, 2))
    assert ids(service._mmr_select(rows, 2)) == ids(original(rows, 2))
