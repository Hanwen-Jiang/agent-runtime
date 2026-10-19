"""内存 Cache（开发实现）。语义见 agentruntime_core.providers.cache。

单进程、单事件循环内有效；各操作内部没有 await，因此在 asyncio 下是原子的。
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from agentruntime_core.clock import Clock, SystemClock
from agentruntime_core.dev._common import check_name, check_positive, scope_id
from agentruntime_core.errors import InvalidError
from agentruntime_core.providers.cache import EventPage, Lock, RateDecision, StreamEvent
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope
from agentruntime_core.testing import FaultInjector


@dataclass
class _Stream:
    next_id: int = 1
    # (id, data, expires_at)
    events: list[tuple[int, bytes, datetime]] = field(default_factory=list)
    trimmed_max_id: int = 0
    changed: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _ScopeState:
    values: dict[str, tuple[bytes, datetime]] = field(default_factory=dict)
    counters: dict[str, tuple[int, datetime]] = field(default_factory=dict)
    # 每个限流键：[(时间, cost)]，按时间递增。
    windows: dict[str, list[tuple[datetime, int]]] = field(default_factory=dict)
    locks: dict[str, tuple[str, datetime]] = field(default_factory=dict)
    streams: dict[str, _Stream] = field(default_factory=dict)


class InMemoryCacheProvider:
    def __init__(self, *, clock: Clock | None = None, faults: FaultInjector | None = None) -> None:
        self._clock = clock or SystemClock()
        self._faults = faults or FaultInjector()
        self._scopes: dict[str, _ScopeState] = {}

    def bind(self, tenant: TenantContext) -> InMemoryCache:
        return InMemoryCache(self, tenant_scope(tenant))

    def bind_platform(self, purpose: str) -> InMemoryCache:
        return InMemoryCache(self, platform_scope(purpose))

    async def aclose(self) -> None:
        self._scopes.clear()

    def _state(self, scope: Scope) -> _ScopeState:
        return self._scopes.setdefault(scope_id(scope), _ScopeState())


class InMemoryCache:
    def __init__(self, provider: InMemoryCacheProvider, scope: Scope) -> None:
        self._provider = provider
        self._scope = scope

    @property
    def _now(self) -> datetime:
        return self._provider._clock.now()

    def _enter(self, operation: str) -> _ScopeState:
        self._provider._faults.check(operation)
        return self._provider._state(self._scope)

    # --- 键值 ---

    async def get(self, key: str) -> bytes | None:
        state = self._enter("get")
        check_name(key, "键")
        entry = state.values.get(key)
        if entry is None or entry[1] <= self._now:
            state.values.pop(key, None)
            return None
        return entry[0]

    async def set(self, key: str, value: bytes, *, ttl: timedelta) -> None:
        state = self._enter("set")
        check_name(key, "键")
        check_positive(ttl, "ttl")
        state.values[key] = (bytes(value), self._now + ttl)

    async def set_if_absent(self, key: str, value: bytes, *, ttl: timedelta) -> bool:
        state = self._enter("set_if_absent")
        check_name(key, "键")
        check_positive(ttl, "ttl")
        entry = state.values.get(key)
        if entry is not None and entry[1] > self._now:
            return False
        state.values[key] = (bytes(value), self._now + ttl)
        return True

    async def delete(self, key: str) -> bool:
        state = self._enter("delete")
        check_name(key, "键")
        entry = state.values.pop(key, None)
        return entry is not None and entry[1] > self._now

    # --- 计数与限流 ---

    async def incr(self, key: str, *, ttl: timedelta, by: int = 1) -> int:
        state = self._enter("incr")
        check_name(key, "键")
        check_positive(ttl, "ttl")
        now = self._now
        current = state.counters.get(key)
        if current is None or current[1] <= now:
            current = (0, now + ttl)
        value = current[0] + by
        state.counters[key] = (value, current[1])
        return value

    async def rate_limit(self, key: str, *, limit: int, window: timedelta, cost: int = 1) -> RateDecision:
        state = self._enter("rate_limit")
        check_name(key, "键")
        check_positive(window, "window")
        if limit < 1 or cost < 1:
            raise InvalidError("invalid_argument", "limit 与 cost 必须至少为 1")
        now = self._now
        entries = [(t, c) for t, c in state.windows.get(key, []) if t > now - window]
        used = sum(c for _, c in entries)
        if used + cost <= limit:
            entries.append((now, cost))
            state.windows[key] = entries
            return RateDecision(allowed=True, remaining=limit - used - cost, retry_after=timedelta(0))
        state.windows[key] = entries
        # 从最早的请求开始滑出窗口，直到腾出足够的额度。
        freed = 0
        retry_after = window
        for t, c in entries:
            freed += c
            if used - freed + cost <= limit:
                retry_after = t + window - now
                break
        return RateDecision(allowed=False, remaining=max(limit - used, 0), retry_after=max(retry_after, timedelta(microseconds=1)))

    # --- 锁 ---

    async def acquire_lock(self, name: str, *, ttl: timedelta) -> Lock | None:
        state = self._enter("acquire_lock")
        check_name(name, "锁名")
        check_positive(ttl, "ttl")
        now = self._now
        held = state.locks.get(name)
        if held is not None and held[1] > now:
            return None
        token = secrets.token_hex(16)
        state.locks[name] = (token, now + ttl)
        return Lock(name=name, token=token, expires_at=now + ttl)

    async def extend_lock(self, lock: Lock, *, ttl: timedelta) -> Lock | None:
        state = self._enter("extend_lock")
        check_name(lock.name, "锁名")
        check_positive(ttl, "ttl")
        now = self._now
        held = state.locks.get(lock.name)
        if held is None or held[0] != lock.token or held[1] <= now:
            return None
        state.locks[lock.name] = (lock.token, now + ttl)
        return Lock(name=lock.name, token=lock.token, expires_at=now + ttl)

    async def release_lock(self, lock: Lock) -> bool:
        state = self._enter("release_lock")
        check_name(lock.name, "锁名")
        held = state.locks.get(lock.name)
        if held is None or held[0] != lock.token or held[1] <= self._now:
            return False
        del state.locks[lock.name]
        return True

    # --- 推流 ---

    def _stream(self, state: _ScopeState, stream: str) -> _Stream:
        s = state.streams.setdefault(stream, _Stream())
        now = self._now
        # 每个事件的保留期可以不同，逐个检查。
        expired = [e[0] for e in s.events if e[2] <= now]
        if expired:
            s.trimmed_max_id = max(s.trimmed_max_id, *expired)
            s.events = [e for e in s.events if e[2] > now]
        return s

    async def append_event(self, stream: str, data: bytes, *, retention: timedelta) -> int:
        state = self._enter("append_event")
        check_name(stream, "流名")
        check_positive(retention, "retention")
        s = self._stream(state, stream)
        event_id = s.next_id
        s.next_id += 1
        s.events.append((event_id, bytes(data), self._now + retention))
        s.changed.set()
        s.changed = asyncio.Event()
        return event_id

    def _read(self, state: _ScopeState, stream: str, after: int, limit: int) -> EventPage:
        s = self._stream(state, stream)
        events = tuple(StreamEvent(id=i, data=d) for i, d, _ in s.events if i > after)[:limit]
        return EventPage(events=events, gap=s.trimmed_max_id > after)

    async def read_events(self, stream: str, *, after: int, limit: int = 100) -> EventPage:
        state = self._enter("read_events")
        check_name(stream, "流名")
        if after < 0 or limit < 1:
            raise InvalidError("invalid_argument", "after 不能为负，limit 至少为 1")
        return self._read(state, stream, after, limit)

    def subscribe(self, stream: str, *, after: int) -> AsyncIterator[StreamEvent]:
        state = self._enter("subscribe")
        check_name(stream, "流名")
        if after < 0:
            raise InvalidError("invalid_argument", "after 不能为负")
        return self._follow(state, stream, after)

    async def _follow(self, state: _ScopeState, stream: str, after: int) -> AsyncIterator[StreamEvent]:
        last = after
        while True:
            changed = self._stream(state, stream).changed
            page = self._read(state, stream, last, 1000)
            for event in page.events:
                yield event
                last = event.id
            if not page.events:
                await changed.wait()
