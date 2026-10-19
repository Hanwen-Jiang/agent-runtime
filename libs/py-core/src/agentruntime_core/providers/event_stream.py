"""EventStream（设计书 4.6）：按主题追加、按消费组读取与回放、按保留期清理。

只在 Python 侧（Learning Pipeline、记忆提炼）使用。

语义：
- 主题名：小写字母开头，1 到 200 个小写字母、数字、点、下划线或连字符，否则 invalid。
- append 至少投递一次：消费者可能读到重复记录，按 record_id 去重。record_id 为空时由实现生成。
- 作用域写在记录上，由实现添加；头部键 tenant_id 与 scope 是保留字，调用方写入即 invalid。
  消费者只读到本作用域的记录。跨租户读取由 S-12 另行提供。
- 同一个 key 的记录按追加顺序读出。
- 消费组：不同组互不影响，各自读到全部记录。commit 提交本消费者已 poll 出的全部记录；
  同组的新消费者从上次提交之后开始；未提交的记录会被重新投递。seek_to_beginning 从头回放。
- 超过主题保留期的记录被清理，回放时不再出现。保留期由 ensure_topic 设置。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from agentruntime_core.providers.base import Bindable


@dataclass(frozen=True)
class OutgoingRecord:
    key: str
    value: bytes
    headers: Mapping[str, str] = field(default_factory=dict)
    record_id: str = ""


@dataclass(frozen=True)
class Position:
    partition: int
    offset: int


@dataclass(frozen=True)
class ConsumedRecord:
    record_id: str
    key: str
    value: bytes
    headers: Mapping[str, str]
    timestamp: datetime
    position: Position


class Consumer(Protocol):
    async def poll(self, *, max_records: int = 100, timeout: timedelta = timedelta(seconds=1)) -> list[ConsumedRecord]:
        """最多等 timeout；没有新记录时返回空列表。"""
        ...

    async def commit(self) -> None: ...

    async def seek_to_beginning(self) -> None: ...

    async def close(self) -> None: ...


class EventStream(Protocol):
    async def append(self, topic: str, records: Sequence[OutgoingRecord]) -> list[Position]: ...

    def consumer(self, topic: str, *, group: str) -> Consumer: ...


class EventStreamProvider(Bindable[EventStream], Protocol):
    async def ensure_topic(self, topic: str, *, retention: timedelta) -> None:
        """创建主题或更新保留期。平台运维操作，不按租户。"""
        ...
