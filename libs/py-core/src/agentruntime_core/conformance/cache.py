"""Cache 的一致性测试。语义见 agentruntime_core.providers.cache。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from agentruntime_core.conformance.common import Harness, StandardRules, expect_error, tenant_a, tenant_b
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.providers.cache import Cache, CacheProvider

TTL = timedelta(seconds=30)
LONG = timedelta(hours=1)

CacheHarness = Harness[CacheProvider]


class CacheConformance(StandardRules):
    probe_operation = "set"

    async def write_probe(self, client: Cache, name: str) -> None:
        await client.set(name, b"probe", ttl=LONG)

    async def probe_visible(self, client: Cache, name: str) -> bool:
        return await client.get(name) == b"probe"

    @pytest.fixture
    def cache(self, harness: CacheHarness) -> Cache:
        return harness.provider.bind(tenant_a())

    # --- 键值 ---

    async def test_get_missing_key_returns_none(self, cache: Cache) -> None:
        assert await cache.get("missing") is None

    async def test_set_then_get(self, cache: Cache) -> None:
        await cache.set("k", b"\x00\xffvalue", ttl=TTL)
        assert await cache.get("k") == b"\x00\xffvalue"

    async def test_set_overwrites(self, cache: Cache) -> None:
        await cache.set("k", b"v1", ttl=TTL)
        await cache.set("k", b"v2", ttl=TTL)
        assert await cache.get("k") == b"v2"

    async def test_value_expires_after_ttl(self, harness: CacheHarness, cache: Cache) -> None:
        await cache.set("k", b"v", ttl=timedelta(seconds=2))
        await harness.advance(timedelta(seconds=1))
        assert await cache.get("k") == b"v"
        await harness.advance(timedelta(seconds=2))
        assert await cache.get("k") is None

    async def test_set_if_absent_is_idempotent(self, cache: Cache) -> None:
        assert await cache.set_if_absent("k", b"first", ttl=TTL) is True
        assert await cache.set_if_absent("k", b"second", ttl=TTL) is False
        assert await cache.get("k") == b"first"

    async def test_set_if_absent_after_expiry(self, harness: CacheHarness, cache: Cache) -> None:
        assert await cache.set_if_absent("k", b"first", ttl=timedelta(seconds=1))
        await harness.advance(timedelta(seconds=2))
        assert await cache.set_if_absent("k", b"second", ttl=TTL) is True
        assert await cache.get("k") == b"second"

    async def test_delete_reports_existence_and_is_repeatable(self, cache: Cache) -> None:
        await cache.set("k", b"v", ttl=TTL)
        assert await cache.delete("k") is True
        assert await cache.get("k") is None
        assert await cache.delete("k") is False

    @pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1)], ids=["zero", "negative"])
    async def test_non_positive_ttl_is_invalid(self, cache: Cache, ttl: timedelta) -> None:
        with expect_error(ErrorCategory.INVALID):
            await cache.set("k", b"v", ttl=ttl)

    @pytest.mark.parametrize("key", ["", "has space", "line\nbreak", "x" * 513], ids=["empty", "space", "newline", "too_long"])
    async def test_malformed_key_is_invalid(self, cache: Cache, key: str) -> None:
        with expect_error(ErrorCategory.INVALID):
            await cache.set(key, b"v", ttl=TTL)

    # --- 计数与限流 ---

    async def test_incr_starts_at_zero_and_accumulates(self, cache: Cache) -> None:
        assert await cache.incr("n", ttl=TTL) == 1
        assert await cache.incr("n", ttl=TTL, by=5) == 6

    async def test_incr_is_atomic_under_concurrency(self, cache: Cache) -> None:
        await asyncio.gather(*(cache.incr("n", ttl=TTL) for _ in range(50)))
        assert await cache.incr("n", ttl=TTL, by=0) == 50

    async def test_counter_expires_from_creation(self, harness: CacheHarness, cache: Cache) -> None:
        await cache.incr("n", ttl=timedelta(seconds=10))
        await harness.advance(timedelta(seconds=6))
        await cache.incr("n", ttl=timedelta(seconds=10))  # 不刷新过期时间
        await harness.advance(timedelta(seconds=6))
        assert await cache.incr("n", ttl=timedelta(seconds=10)) == 1

    async def test_rate_limit_allows_up_to_limit(self, cache: Cache) -> None:
        window = timedelta(seconds=60)
        decisions = [await cache.rate_limit("api", limit=3, window=window) for _ in range(4)]
        assert [d.allowed for d in decisions] == [True, True, True, False]
        assert decisions[2].remaining == 0
        assert decisions[3].retry_after > timedelta(0)

    async def test_rejected_requests_do_not_consume_quota(self, harness: CacheHarness, cache: Cache) -> None:
        window = timedelta(seconds=10)
        assert (await cache.rate_limit("api", limit=1, window=window)).allowed
        for _ in range(5):
            assert not (await cache.rate_limit("api", limit=1, window=window)).allowed
        await harness.advance(timedelta(seconds=11))
        assert (await cache.rate_limit("api", limit=1, window=window)).allowed

    async def test_rate_limit_window_slides(self, harness: CacheHarness, cache: Cache) -> None:
        window = timedelta(seconds=10)
        assert (await cache.rate_limit("api", limit=2, window=window)).allowed
        await harness.advance(timedelta(seconds=6))
        assert (await cache.rate_limit("api", limit=2, window=window)).allowed
        assert not (await cache.rate_limit("api", limit=2, window=window)).allowed
        await harness.advance(timedelta(seconds=5))  # 第一个请求滑出窗口
        assert (await cache.rate_limit("api", limit=2, window=window)).allowed

    async def test_rate_limit_respects_cost(self, cache: Cache) -> None:
        window = timedelta(seconds=60)
        assert (await cache.rate_limit("tokens", limit=10, window=window, cost=7)).allowed
        assert not (await cache.rate_limit("tokens", limit=10, window=window, cost=4)).allowed
        assert (await cache.rate_limit("tokens", limit=10, window=window, cost=3)).allowed

    async def test_rate_limit_is_exact_under_concurrency(self, cache: Cache) -> None:
        decisions = await asyncio.gather(
            *(cache.rate_limit("burst", limit=5, window=timedelta(seconds=60)) for _ in range(20))
        )
        assert sum(d.allowed for d in decisions) == 5

    @pytest.mark.parametrize(("limit", "cost"), [(0, 1), (5, 0)], ids=["zero_limit", "zero_cost"])
    async def test_rate_limit_rejects_bad_arguments(self, cache: Cache, limit: int, cost: int) -> None:
        with expect_error(ErrorCategory.INVALID):
            await cache.rate_limit("api", limit=limit, window=timedelta(seconds=1), cost=cost)

    async def test_counters_and_limits_are_isolated_per_tenant(self, harness: CacheHarness) -> None:
        a = harness.provider.bind(tenant_a())
        b = harness.provider.bind(tenant_b())
        await a.incr("n", ttl=TTL, by=10)
        assert await b.incr("n", ttl=TTL) == 1
        window = timedelta(seconds=60)
        assert (await a.rate_limit("api", limit=1, window=window)).allowed
        assert (await b.rate_limit("api", limit=1, window=window)).allowed

    # --- 锁 ---

    async def test_lock_is_exclusive(self, cache: Cache) -> None:
        lock = await cache.acquire_lock("job", ttl=TTL)
        assert lock is not None
        assert await cache.acquire_lock("job", ttl=TTL) is None
        assert await cache.release_lock(lock) is True
        assert await cache.acquire_lock("job", ttl=TTL) is not None

    async def test_lock_is_exclusive_under_concurrency(self, cache: Cache) -> None:
        results = await asyncio.gather(*(cache.acquire_lock("job", ttl=TTL) for _ in range(20)))
        assert sum(r is not None for r in results) == 1

    async def test_expired_lock_can_be_taken_and_old_token_is_fenced(self, harness: CacheHarness, cache: Cache) -> None:
        old = await cache.acquire_lock("job", ttl=timedelta(seconds=5))
        assert old is not None
        await harness.advance(timedelta(seconds=6))
        new = await cache.acquire_lock("job", ttl=TTL)
        assert new is not None and new.token != old.token
        assert await cache.release_lock(old) is False, "过期的持有者释放了别人的锁"
        assert await cache.extend_lock(old, ttl=TTL) is None, "过期的持有者续期了别人的锁"
        assert await cache.acquire_lock("job", ttl=TTL) is None, "新持有者的锁被旧 token 影响"

    async def test_extend_lock_prolongs_it(self, harness: CacheHarness, cache: Cache) -> None:
        lock = await cache.acquire_lock("job", ttl=timedelta(seconds=5))
        assert lock is not None
        await harness.advance(timedelta(seconds=4))
        extended = await cache.extend_lock(lock, ttl=timedelta(seconds=5))
        assert extended is not None
        await harness.advance(timedelta(seconds=4))
        assert await cache.acquire_lock("job", ttl=TTL) is None

    async def test_release_is_repeatable(self, cache: Cache) -> None:
        lock = await cache.acquire_lock("job", ttl=TTL)
        assert lock is not None
        assert await cache.release_lock(lock) is True
        assert await cache.release_lock(lock) is False

    async def test_locks_are_isolated_per_tenant(self, harness: CacheHarness) -> None:
        a = harness.provider.bind(tenant_a())
        b = harness.provider.bind(tenant_b())
        assert await a.acquire_lock("job", ttl=TTL) is not None
        assert await b.acquire_lock("job", ttl=TTL) is not None

    # --- 推流 ---

    async def test_event_ids_start_at_one_and_increase(self, cache: Cache) -> None:
        ids = [await cache.append_event("task-1", f"e{i}".encode(), retention=LONG) for i in range(5)]
        assert ids[0] >= 1
        assert ids == sorted(ids) and len(set(ids)) == 5

    async def test_read_events_after_id_resumes(self, cache: Cache) -> None:
        ids = [await cache.append_event("task-1", f"e{i}".encode(), retention=LONG) for i in range(5)]
        page = await cache.read_events("task-1", after=ids[1])
        assert [e.id for e in page.events] == ids[2:]
        assert [e.data for e in page.events] == [b"e2", b"e3", b"e4"]
        assert page.gap is False

    async def test_read_events_honours_limit(self, cache: Cache) -> None:
        for i in range(5):
            await cache.append_event("task-1", f"e{i}".encode(), retention=LONG)
        page = await cache.read_events("task-1", after=0, limit=2)
        assert [e.data for e in page.events] == [b"e0", b"e1"]

    async def test_expired_events_are_reported_as_gap(self, harness: CacheHarness, cache: Cache) -> None:
        first = await cache.append_event("task-1", b"old", retention=timedelta(seconds=5))
        await harness.advance(timedelta(seconds=6))
        second = await cache.append_event("task-1", b"new", retention=timedelta(seconds=5))
        page = await cache.read_events("task-1", after=0)
        assert [e.id for e in page.events] == [second]
        assert page.gap is True
        page = await cache.read_events("task-1", after=first)
        assert page.gap is False

    async def test_subscribe_replays_then_streams_live(self, cache: Cache) -> None:
        first = await cache.append_event("task-1", b"a", retention=LONG)
        await cache.append_event("task-1", b"b", retention=LONG)
        received: list[bytes] = []

        async def consume() -> None:
            async for event in cache.subscribe("task-1", after=first):
                received.append(event.data)
                if len(received) == 2:
                    return

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        await cache.append_event("task-1", b"c", retention=LONG)
        await asyncio.wait_for(consumer, timeout=5)
        assert received == [b"b", b"c"]

    async def test_streams_are_isolated_per_tenant(self, harness: CacheHarness) -> None:
        a = harness.provider.bind(tenant_a())
        b = harness.provider.bind(tenant_b())
        await a.append_event("task-1", b"secret-progress", retention=LONG)
        page = await b.read_events("task-1", after=0)
        assert page.events == ()
