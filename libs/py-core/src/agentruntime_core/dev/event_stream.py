"""内存 EventStream（开发实现，单分区）。语义见 agentruntime_core.providers.event_stream。"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from agentruntime_core.clock import Clock, SystemClock
from agentruntime_core.dev._common import check_positive, scope_id
from agentruntime_core.errors import InvalidError, NotFoundError
from agentruntime_core.providers.event_stream import ConsumedRecord, OutgoingRecord, Position
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope
from agentruntime_core.testing import FaultInjector

_TOPIC = re.compile(r"[a-z][a-z0-9._-]{0,199}")
_RESERVED_HEADERS = {"tenant_id", "scope"}


def _check_topic(topic: str) -> None:
    if not isinstance(topic, str) or not _TOPIC.fullmatch(topic):
        raise InvalidError("invalid_topic", "主题名必须是小写字母开头的 1 到 200 个小写字母、数字、点、下划线或连字符")


@dataclass(frozen=True)
class _Stored:
    offset: int
    owner: str
    record_id: str
    key: str
    value: bytes
    headers: dict[str, str]
    timestamp: datetime


@dataclass
class _Topic:
    retention: timedelta
    records: list[_Stored] = field(default_factory=list)
    next_offset: int = 0
    appended: asyncio.Event = field(default_factory=asyncio.Event)


class InMemoryEventStreamProvider:
    def __init__(self, *, clock: Clock | None = None, faults: FaultInjector | None = None) -> None:
        self._clock = clock or SystemClock()
        self._faults = faults or FaultInjector()
        self._topics: dict[str, _Topic] = {}
        # (作用域, 主题, 消费组) → 下一条要读的 offset
        self._committed: dict[tuple[str, str, str], int] = {}

    def bind(self, tenant: TenantContext) -> InMemoryEventStream:
        return InMemoryEventStream(self, tenant_scope(tenant))

    def bind_platform(self, purpose: str) -> InMemoryEventStream:
        return InMemoryEventStream(self, platform_scope(purpose))

    async def ensure_topic(self, topic: str, *, retention: timedelta) -> None:
        _check_topic(topic)
        check_positive(retention, "retention")
        existing = self._topics.get(topic)
        if existing is None:
            self._topics[topic] = _Topic(retention=retention)
        else:
            existing.retention = retention

    async def aclose(self) -> None:
        self._topics.clear()

    def _topic(self, topic: str) -> _Topic:
        _check_topic(topic)
        t = self._topics.get(topic)
        if t is None:
            raise NotFoundError("unknown_topic", "主题不存在，先用 ensure_topic 创建")
        cutoff = self._clock.now() - t.retention
        t.records = [r for r in t.records if r.timestamp > cutoff]
        return t


class InMemoryEventStream:
    def __init__(self, provider: InMemoryEventStreamProvider, scope: Scope) -> None:
        self._provider = provider
        self._owner = scope_id(scope)

    async def append(self, topic: str, records: Sequence[OutgoingRecord]) -> list[Position]:
        self._provider._faults.check("append")
        _check_topic(topic)
        for record in records:
            if set(record.headers) & _RESERVED_HEADERS:
                raise InvalidError("reserved_header", "tenant_id 与 scope 是保留的头部键")
        t = self._provider._topic(topic)
        positions = []
        now = self._provider._clock.now()
        for record in records:
            stored = _Stored(
                offset=t.next_offset,
                owner=self._owner,
                record_id=record.record_id or uuid.uuid4().hex,
                key=record.key,
                value=bytes(record.value),
                headers=dict(record.headers),
                timestamp=now,
            )
            t.records.append(stored)
            t.next_offset += 1
            positions.append(Position(partition=0, offset=stored.offset))
        t.appended.set()
        t.appended = asyncio.Event()
        return positions

    def consumer(self, topic: str, *, group: str) -> InMemoryConsumer:
        _check_topic(topic)
        if not group:
            raise InvalidError("invalid_argument", "消费组不能为空")
        return InMemoryConsumer(self._provider, self._owner, topic, group)


class InMemoryConsumer:
    def __init__(self, provider: InMemoryEventStreamProvider, owner: str, topic: str, group: str) -> None:
        self._provider = provider
        self._owner = owner
        self._topic = topic
        self._group_key = (owner, topic, group)
        self._position = provider._committed.get(self._group_key, 0)
        self._closed = False

    def _ready(self, max_records: int) -> list[ConsumedRecord]:
        t = self._provider._topic(self._topic)
        batch: list[ConsumedRecord] = []
        for r in t.records:
            if r.offset < self._position or r.owner != self._owner:
                continue
            batch.append(
                ConsumedRecord(
                    record_id=r.record_id,
                    key=r.key,
                    value=r.value,
                    headers=dict(r.headers),
                    timestamp=r.timestamp,
                    position=Position(partition=0, offset=r.offset),
                )
            )
            if len(batch) == max_records:
                break
        # 跳过别的作用域与已过期的记录：位置推进到最后看过的 offset 之后。
        if batch:
            self._position = batch[-1].position.offset + 1
        return batch

    async def poll(self, *, max_records: int = 100, timeout: timedelta = timedelta(seconds=1)) -> list[ConsumedRecord]:
        self._provider._faults.check("poll")
        if self._closed:
            raise InvalidError("closed", "消费者已关闭")
        if max_records < 1:
            raise InvalidError("invalid_argument", "max_records 至少为 1")
        appended = self._provider._topic(self._topic).appended
        batch = self._ready(max_records)
        if batch:
            return batch
        try:
            await asyncio.wait_for(appended.wait(), timeout=max(timeout.total_seconds(), 0))
        except TimeoutError:
            return []
        return self._ready(max_records)

    async def commit(self) -> None:
        self._provider._faults.check("commit")
        self._provider._committed[self._group_key] = self._position

    async def seek_to_beginning(self) -> None:
        self._provider._faults.check("seek_to_beginning")
        self._position = 0

    async def close(self) -> None:
        self._closed = True
