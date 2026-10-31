"""docs/plan/memory-live.md M-02 验收：真 embedding 是 3072 维，保距变换是纯 Python，要核对余弦不变与耗时。"""

from __future__ import annotations

import math
import random
import time

from agentruntime_core.adapters.vector_crypto import DenseTransform

DIMS = 3072


def cosine(a: list[float] | tuple[float, ...], b: list[float] | tuple[float, ...]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def vector(seed: int) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(0, 1) for _ in range(DIMS)]


def test_cosine_survives_at_3072_dimensions() -> None:
    t = DenseTransform(b"k" * 32, DIMS)
    a, b = vector(1), vector(2)
    assert math.isclose(cosine(t.forward(a), t.forward(b)), cosine(a, b), abs_tol=1e-9)
    assert cosine(t.forward(a), a) < 0.5, "变换后不能还像原向量"


def test_the_transform_is_fast_enough_for_a_search() -> None:
    """一次检索变换一个查询、取回十条。"""
    t = DenseTransform(b"k" * 32, DIMS)
    v = vector(3)
    started = time.perf_counter()
    for _ in range(11):
        t.forward(v)
    assert time.perf_counter() - started < 1.0
