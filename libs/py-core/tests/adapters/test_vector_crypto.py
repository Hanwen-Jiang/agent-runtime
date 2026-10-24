"""S-12b：向量的保距变换（附录 I.7“嵌入与原文同等敏感，向量按租户密钥加密”；经人确认的做法）。

正交变换保持内积、范数与余弦，租户内的检索结果不变；没有密钥恢复不出原向量。稀疏向量做可逆的下标置换。
"""

from __future__ import annotations

import math
import random

import pytest

from agentruntime_core.adapters.vector_crypto import DenseTransform, SparseTransform
from agentruntime_core.providers.vector_store import SparseVector

KEY_A = bytes(range(32))
KEY_B = bytes(range(1, 33))


def vec(dims: int, seed: int) -> tuple[float, ...]:
    rng = random.Random(seed)
    return tuple(rng.uniform(-1, 1) for _ in range(dims))


def dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def norm(a) -> float:
    return math.sqrt(dot(a, a))


@pytest.mark.parametrize("dims", [2, 3, 64, 1024])
def test_dense_transform_preserves_inner_products_and_norms(dims: int) -> None:
    t = DenseTransform(KEY_A, dims)
    a, b = vec(dims, 1), vec(dims, 2)
    fa, fb = t.forward(a), t.forward(b)
    assert len(fa) == dims
    assert norm(fa) == pytest.approx(norm(a), rel=1e-9)
    assert dot(fa, fb) == pytest.approx(dot(a, b), rel=1e-7, abs=1e-9)


@pytest.mark.parametrize("dims", [2, 64, 1024])
def test_dense_transform_round_trips(dims: int) -> None:
    t = DenseTransform(KEY_A, dims)
    a = vec(dims, 3)
    assert t.inverse(t.forward(a)) == pytest.approx(a, abs=1e-9)


def test_dense_transform_hides_the_vector_and_depends_on_the_key() -> None:
    dims = 64
    a = vec(dims, 4)
    fa = DenseTransform(KEY_A, dims).forward(a)
    assert DenseTransform(KEY_A, dims).forward(a) == fa, "同一把密钥、同一维度，变换确定"
    assert DenseTransform(KEY_B, dims).forward(a) != pytest.approx(fa, abs=1e-3), "不同租户的同一个向量变换后不同"
    assert fa != pytest.approx(a, abs=1e-3)
    # 不只是置换与翻转符号：坐标的绝对值分布也变了，按大小排序对不上原向量。
    assert sorted(abs(x) for x in fa) != pytest.approx(sorted(abs(x) for x in a), abs=1e-3)


def test_dense_transform_rejects_the_wrong_dimension() -> None:
    with pytest.raises(ValueError):
        DenseTransform(KEY_A, 3).forward((1.0, 0.0))


def sv(**weights: float) -> SparseVector:
    items = sorted((int(k[1:]), v) for k, v in weights.items())
    return SparseVector(indices=tuple(i for i, _ in items), values=tuple(v for _, v in items))


def sparse_dot(a: SparseVector, b: SparseVector) -> float:
    weights = dict(zip(a.indices, a.values, strict=True))
    return sum(weights.get(i, 0.0) * v for i, v in zip(b.indices, b.values, strict=True))


def test_sparse_transform_preserves_dot_products_and_round_trips() -> None:
    t = SparseTransform(KEY_A)
    a, b = sv(i0=1.0, i7=2.0, i42=0.5), sv(i7=3.0, i42=4.0, i99=1.0)
    fa, fb = t.forward(a), t.forward(b)
    assert sparse_dot(fa, fb) == pytest.approx(sparse_dot(a, b))
    assert list(fa.indices) == sorted(fa.indices), "下标保持有序"
    assert sorted(fa.values) == sorted(a.values), "值不变"
    assert set(fa.indices) != set(a.indices), "下标被置换"
    assert t.inverse(fa) == a


def test_sparse_transform_depends_on_the_key() -> None:
    a = sv(i3=1.0, i5=1.0)
    assert SparseTransform(KEY_A).forward(a) == SparseTransform(KEY_A).forward(a)
    assert SparseTransform(KEY_A).forward(a).indices != SparseTransform(KEY_B).forward(a).indices


def test_a_smaller_index_space_stays_inside_and_round_trips() -> None:
    """Milvus 要求稀疏下标小于 2**32 - 1：收窄空间后，结果都在空间内，仍可逆；碰到空间外的像时循环行走。"""
    top = 2**32 - 1
    narrow, full = SparseTransform(KEY_A, index_space=top), SparseTransform(KEY_A)
    walker = full._feistel_inverse(top)  # 它在全空间里的像正好是 2**32 - 1，收窄后要多走一步
    a = sv(i0=1.0, i7=2.0, **{f"i{walker}": 3.0, f"i{top - 1}": 4.0})
    fa = narrow.forward(a)
    assert all(0 <= i < top for i in fa.indices)
    assert narrow.inverse(fa) == a
    assert narrow._permute(walker) == full._permute(top) != top
    assert narrow._permute(7) == full._permute(7), "像在空间内的下标与全空间一致"
    with pytest.raises(ValueError):
        narrow.forward(sv(**{f"i{top}": 1.0}))
    with pytest.raises(ValueError):
        SparseTransform(KEY_A, index_space=1000)
