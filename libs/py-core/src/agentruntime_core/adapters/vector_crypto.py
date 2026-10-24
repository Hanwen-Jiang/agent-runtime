"""向量的保距变换（附录 I.7“向量按租户密钥加密”；计划 S-12b，经人确认的做法）。

嵌入与原文同等敏感，但加密后的向量没法做近邻检索。做法是按租户密钥做保距变换：

- 稠密向量：由密钥派生的正交变换（置换、符号翻转与若干个 Householder 反射的乘积）。正交变换保持内积、
  范数与余弦，租户内的检索结果不变；没有密钥就恢复不出原向量，不同租户的同一个向量变换后也不同。
- 稀疏向量：由密钥派生的可逆下标置换（32 位空间上的 Feistel 网络），值不变，点积保持不变。后端的下标空间更小时
  （Milvus 要求下标小于 2**32 - 1），用循环行走把置换限制在 [0, index_space) 内，仍是这个空间上的双射。

参数由 HMAC-SHA256 的计数器流派生，纯 Python 实现，不依赖 numpy。变换由（密钥、维度）唯一决定。
"""

from __future__ import annotations

import hashlib
import hmac
import math
import struct
from collections.abc import Iterator, Sequence

from agentruntime_core.providers.vector_store import SparseVector

HOUSEHOLDER_ROUNDS = 8
INDEX_SPACE = 2**32
FEISTEL_ROUNDS = 4


def _stream(key: bytes, label: bytes) -> Iterator[int]:
    """HMAC-SHA256 计数器流，每次给出一个 64 位无符号整数。"""
    counter = 0
    while True:
        block = hmac.new(key, label + struct.pack(">Q", counter), hashlib.sha256).digest()
        counter += 1
        for i in range(0, 32, 8):
            yield struct.unpack(">Q", block[i : i + 8])[0]


def _uniform(source: Iterator[int]) -> float:
    """[0, 1) 上的均匀数（53 位精度）。"""
    return (next(source) >> 11) / float(1 << 53)


class DenseTransform:
    def __init__(self, key: bytes, dims: int) -> None:
        if dims < 1:
            raise ValueError("维度至少为 1")
        self.key = key
        self.dims = dims
        source = _stream(key, b"dense:" + str(dims).encode())
        perm = list(range(dims))
        for i in range(dims - 1, 0, -1):  # Fisher–Yates
            j = next(source) % (i + 1)
            perm[i], perm[j] = perm[j], perm[i]
        self._perm = perm
        self._signs = [1.0 if next(source) & 1 else -1.0 for _ in range(dims)]
        self._reflections: list[list[float]] = []
        if dims > 1:
            for _ in range(HOUSEHOLDER_ROUNDS):
                # Box–Muller 得到各向同性的方向，再归一化。
                v = []
                for _ in range(dims):
                    u1 = max(_uniform(source), 1e-300)
                    u2 = _uniform(source)
                    v.append(math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2))
                n = math.sqrt(sum(x * x for x in v))
                self._reflections.append([x / n for x in v])

    def _check(self, vector: Sequence[float]) -> list[float]:
        if len(vector) != self.dims:
            raise ValueError(f"向量维度 {len(vector)} 与变换的维度 {self.dims} 不一致")
        return [float(x) for x in vector]

    @staticmethod
    def _reflect(u: list[float], y: list[float]) -> list[float]:
        d = 2.0 * sum(a * b for a, b in zip(u, y, strict=True))
        return [b - d * a for a, b in zip(u, y, strict=True)]

    def forward(self, vector: Sequence[float]) -> tuple[float, ...]:
        x = self._check(vector)
        y = [x[self._perm[i]] * self._signs[i] for i in range(self.dims)]
        for u in self._reflections:
            y = self._reflect(u, y)
        return tuple(y)

    def inverse(self, vector: Sequence[float]) -> tuple[float, ...]:
        y = self._check(vector)
        for u in reversed(self._reflections):  # Householder 反射自身就是逆
            y = self._reflect(u, y)
        x = [0.0] * self.dims
        for i in range(self.dims):
            x[self._perm[i]] = y[i] * self._signs[i]
        return tuple(x)


class SparseTransform:
    def __init__(self, key: bytes, *, index_space: int = INDEX_SPACE) -> None:
        # 循环行走平均要置换 2**32 / index_space 次，只用于略小于 2**32 的空间。
        if not INDEX_SPACE // 2 <= index_space <= INDEX_SPACE:
            raise ValueError("index_space 必须在 2**31 到 2**32 之间")
        self.key = key
        self.index_space = index_space
        self._round_keys = [hmac.new(key, b"sparse-round:" + bytes([r]), hashlib.sha256).digest() for r in range(FEISTEL_ROUNDS)]

    def _f(self, r: int, half: int) -> int:
        return int.from_bytes(hmac.new(self._round_keys[r], half.to_bytes(2, "big"), hashlib.sha256).digest()[:2], "big")

    def _feistel(self, index: int) -> int:
        left, right = index >> 16, index & 0xFFFF
        for r in range(FEISTEL_ROUNDS):
            left, right = right, left ^ self._f(r, right)
        return (left << 16) | right

    def _feistel_inverse(self, index: int) -> int:
        left, right = index >> 16, index & 0xFFFF
        for r in reversed(range(FEISTEL_ROUNDS)):
            left, right = right ^ self._f(r, left), left
        return (left << 16) | right

    def _permute(self, index: int) -> int:
        if not 0 <= index < self.index_space:
            raise ValueError(f"稀疏下标必须在 0 到 {self.index_space - 1} 之间")
        index = self._feistel(index)
        while index >= self.index_space:  # 循环行走：落到空间外就再置换一次
            index = self._feistel(index)
        return index

    def _unpermute(self, index: int) -> int:
        index = self._feistel_inverse(index)
        while index >= self.index_space:
            index = self._feistel_inverse(index)
        return index

    @staticmethod
    def _sorted(pairs: list[tuple[int, float]]) -> SparseVector:
        pairs.sort()
        return SparseVector(indices=tuple(i for i, _ in pairs), values=tuple(v for _, v in pairs))

    def forward(self, vector: SparseVector) -> SparseVector:
        return self._sorted([(self._permute(i), float(v)) for i, v in zip(vector.indices, vector.values, strict=True)])

    def inverse(self, vector: SparseVector) -> SparseVector:
        return self._sorted([(self._unpermute(i), float(v)) for i, v in zip(vector.indices, vector.values, strict=True)])
