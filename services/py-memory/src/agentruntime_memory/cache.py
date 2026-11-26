"""Tenant-isolated TTL LRU cache for query embeddings in py-memory."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from agentruntime_memory.identity import Identity
from agentruntime_memory.metrics import MemoryMetricsRegistry, default_metrics

CacheKey = tuple[str, str, str, str]


@dataclass(frozen=True)
class _Entry:
    vector: tuple[float, ...]
    expires_at: float


class TenantQueryEmbeddingCache:
    """Bounded TTL + LRU cache keyed strictly by (tenant_id, workspace_id, embedding_version, query)."""

    def __init__(
        self,
        *,
        max_size: int = 512,
        ttl_seconds: float = 300.0,
        clock: Callable[[], float] | None = None,
        metrics: MemoryMetricsRegistry | None = None,
    ) -> None:
        if max_size < 0:
            raise ValueError("max_size must be >= 0")
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be >= 0")
        self.max_size = int(max_size)
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock or time.monotonic
        self._metrics = metrics or default_metrics()
        self._entries: OrderedDict[CacheKey, _Entry] = OrderedDict()
        self._lock = asyncio.Lock()

    def _key(self, identity: Identity, version: str, query: str) -> CacheKey:
        return (identity.tenant_id, identity.workspace_id, version, query)

    def get(self, identity: Identity, version: str, query: str) -> tuple[float, ...] | None:
        if self.max_size == 0 or self.ttl_seconds == 0:
            return None
        key = self._key(identity, version, query)
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() >= entry.expires_at:
            self._entries.pop(key, None)
            return None
        self._entries.move_to_end(key)
        return entry.vector

    def put(self, identity: Identity, version: str, query: str, vector: Sequence[float]) -> tuple[float, ...]:
        tup = tuple(float(x) for x in vector)
        if self.max_size == 0 or self.ttl_seconds == 0:
            return tup
        key = self._key(identity, version, query)
        expires = self._clock() + self.ttl_seconds
        if key in self._entries:
            self._entries.move_to_end(key)
        self._entries[key] = _Entry(vector=tup, expires_at=expires)
        while len(self._entries) > self.max_size:
            self._entries.popitem(last=False)
        return tup

    async def get_or_compute(
        self,
        identity: Identity,
        version: str,
        query: str,
        compute: Callable[[], Awaitable[Sequence[float]]],
    ) -> tuple[tuple[float, ...], bool]:
        """Return (vector, cache_hit) while preserving strict tenant/workspace key isolation."""
        async with self._lock:
            cached = self.get(identity, version, query)
            if cached is not None:
                self._metrics.inc_cache_hit()
                return cached, True
        self._metrics.inc_cache_miss()
        computed = tuple(float(x) for x in await compute())
        async with self._lock:
            self.put(identity, version, query, computed)
        return computed, False
