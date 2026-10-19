"""VectorStore（设计书 4.4、附录 I.6）：按租户写入与删除向量；稠密 + 稀疏混合查询并融合排序；按字段过滤。

只在 Python 侧（Memory Service）使用。

语义：
- 数据按语料（corpus）组织，例如 memory、knowledge。语料名：小写字母开头，1 到 63 个小写字母、数字或下划线，否则 invalid。
- 租户过滤由客户端的作用域决定，查询时由实现拼接，永远不由调用方或模型给出（设计书 3.4.1）。
  字段名 tenant_id 与 scope 是保留字：写入或过滤里出现即 invalid。
- upsert 按 id 覆盖，重复执行结果相同。同一语料、同一 embedding_version 下稠密向量维度必须一致，否则 invalid。
- query 只返回 embedding_version 相同的记录（按版本双写迁移，附录 I.6）。dense 与 sparse 至少给一个；
  只给 dense 按余弦相似度排序，只给 sparse 按点积排序；两者都给时用 RRF 融合（k = 60）。
- Filter 的条件全部满足才算命中：值为标量时要求字段相等；值为 AnyOf 时要求字段等于其中之一，
  或字段是列表且与之有交集。
- delete 返回实际删除的条数；删除后的记录不再被 get 或 query 返回。
- 向量按租户密钥加密存储（附录 I.7）由生产实现负责，接口不可见（S-12b）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from agentruntime_core.providers.base import Bindable

FieldValue = str | int | float | bool | tuple[str, ...]


@dataclass(frozen=True)
class SparseVector:
    indices: tuple[int, ...]
    values: tuple[float, ...]


@dataclass(frozen=True)
class VectorRecord:
    id: str
    dense: tuple[float, ...]
    embedding_version: str
    sparse: SparseVector | None = None
    fields: Mapping[str, FieldValue] = field(default_factory=dict)


@dataclass(frozen=True)
class AnyOf:
    values: tuple[str | int, ...]


@dataclass(frozen=True)
class Filter:
    must: Mapping[str, FieldValue | AnyOf]


@dataclass(frozen=True)
class Hit:
    id: str
    score: float
    fields: Mapping[str, FieldValue]


class VectorStore(Protocol):
    async def upsert(self, corpus: str, records: Sequence[VectorRecord]) -> None: ...

    async def get(self, corpus: str, ids: Sequence[str]) -> list[VectorRecord]:
        """按 id 取回；不存在的 id 直接省略。"""
        ...

    async def delete(self, corpus: str, ids: Sequence[str]) -> int: ...

    async def query(
        self,
        corpus: str,
        *,
        embedding_version: str,
        top_k: int,
        dense: Sequence[float] | None = None,
        sparse: SparseVector | None = None,
        filter: Filter | None = None,
    ) -> list[Hit]: ...

    async def count(self, corpus: str, *, filter: Filter | None = None) -> int: ...


class VectorStoreProvider(Bindable[VectorStore], Protocol):
    pass
