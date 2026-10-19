"""EventStream 的一致性测试。语义见 agentruntime_core.providers.event_stream。"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from agentruntime_core.conformance.common import Harness, StandardRules, expect_error, tenant_a, tenant_b
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.providers.event_stream import ConsumedRecord, Consumer, EventStream, EventStreamProvider, OutgoingRecord

TOPIC = "conformance.events"
RETENTION = timedelta(days=7)

EventStreamHarness = Harness[EventStreamProvider]


def out(key: str, value: str, **headers: str) -> OutgoingRecord:
    return OutgoingRecord(key=key, value=value.encode(), headers=headers)


async def drain(consumer: Consumer, *, rounds: int = 3) -> list[ConsumedRecord]:
    """连续 poll，直到一轮没有新记录。"""
    records: list[ConsumedRecord] = []
    empty = 0
    while empty < rounds:
        batch = await consumer.poll(max_records=100, timeout=timedelta(milliseconds=200))
        if batch:
            records.extend(batch)
            empty = 0
        else:
            empty += 1
    return records


def _group(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:8]}"


class EventStreamConformance(StandardRules):
    probe_operation = "append"

    async def write_probe(self, client: EventStream, name: str) -> None:
        await client.append(TOPIC, [out(name, "probe")])

    async def probe_visible(self, client: EventStream, name: str) -> bool:
        consumer = client.consumer(TOPIC, group=_group("probe"))
        try:
            return name in {r.key for r in await drain(consumer)}
        finally:
            await consumer.close()

    @pytest.fixture(autouse=True)
    async def _topic(self, harness: EventStreamHarness) -> None:
        await harness.provider.ensure_topic(TOPIC, retention=RETENTION)

    @pytest.fixture
    def stream(self, harness: EventStreamHarness) -> EventStream:
        return harness.provider.bind(tenant_a())

    async def test_records_are_read_in_order_per_key(self, stream: EventStream) -> None:
        await stream.append(TOPIC, [out("task-1", f"r{i}") for i in range(5)])
        consumer = stream.consumer(TOPIC, group=_group("order"))
        records = await drain(consumer)
        await consumer.close()
        assert [r.value for r in records if r.key == "task-1"] == [f"r{i}".encode() for i in range(5)]

    async def test_record_id_is_kept_or_assigned(self, stream: EventStream) -> None:
        await stream.append(TOPIC, [OutgoingRecord(key="k", value=b"x", record_id="rec-1"), out("k", "y")])
        consumer = stream.consumer(TOPIC, group=_group("ids"))
        records = await drain(consumer)
        await consumer.close()
        assert records[0].record_id == "rec-1"
        assert records[1].record_id and records[1].record_id != "rec-1"

    async def test_headers_roundtrip(self, stream: EventStream) -> None:
        await stream.append(TOPIC, [out("k", "x", span="execute_tool")])
        consumer = stream.consumer(TOPIC, group=_group("headers"))
        [record] = await drain(consumer)
        await consumer.close()
        assert record.headers["span"] == "execute_tool"

    @pytest.mark.parametrize("header", ["tenant_id", "scope"])
    async def test_reserved_headers_are_invalid(self, stream: EventStream, header: str) -> None:
        with expect_error(ErrorCategory.INVALID):
            await stream.append(TOPIC, [out("k", "x", **{header: "t-beta"})])

    @pytest.mark.parametrize("topic", ["", "Upper", "has space", "x" * 201])
    async def test_malformed_topic_is_invalid(self, stream: EventStream, topic: str) -> None:
        with expect_error(ErrorCategory.INVALID):
            await stream.append(topic, [out("k", "x")])

    async def test_groups_are_independent(self, stream: EventStream) -> None:
        await stream.append(TOPIC, [out("k", f"r{i}") for i in range(3)])
        for label in ("learning", "memory"):
            consumer = stream.consumer(TOPIC, group=_group(label))
            assert len(await drain(consumer)) == 3
            await consumer.close()

    async def test_committed_records_are_not_redelivered(self, stream: EventStream) -> None:
        group = _group("commit")
        await stream.append(TOPIC, [out("k", "r1"), out("k", "r2")])
        first = stream.consumer(TOPIC, group=group)
        assert [r.value for r in await drain(first)] == [b"r1", b"r2"]
        await first.commit()
        await first.close()
        await stream.append(TOPIC, [out("k", "r3")])
        second = stream.consumer(TOPIC, group=group)
        assert [r.value for r in await drain(second)] == [b"r3"]
        await second.close()

    async def test_uncommitted_records_are_redelivered(self, stream: EventStream) -> None:
        group = _group("at-least-once")
        await stream.append(TOPIC, [out("k", "r1")])
        first = stream.consumer(TOPIC, group=group)
        assert len(await drain(first)) == 1
        await first.close()  # 没有 commit
        second = stream.consumer(TOPIC, group=group)
        assert [r.value for r in await drain(second)] == [b"r1"]
        await second.close()

    async def test_seek_to_beginning_replays(self, stream: EventStream) -> None:
        await stream.append(TOPIC, [out("k", "r1"), out("k", "r2")])
        consumer = stream.consumer(TOPIC, group=_group("replay"))
        assert len(await drain(consumer)) == 2
        await consumer.commit()
        await consumer.seek_to_beginning()
        assert [r.value for r in await drain(consumer)] == [b"r1", b"r2"]
        await consumer.close()

    async def test_records_expire_after_retention(self, harness: EventStreamHarness, stream: EventStream) -> None:
        await harness.provider.ensure_topic("conformance.short", retention=timedelta(hours=1))
        await stream.append("conformance.short", [out("k", "old")])
        await harness.advance(timedelta(hours=2))
        await stream.append("conformance.short", [out("k", "new")])
        consumer = stream.consumer("conformance.short", group=_group("retention"))
        assert [r.value for r in await drain(consumer)] == [b"new"]
        await consumer.close()

    async def test_consumers_only_see_own_tenant(self, harness: EventStreamHarness, stream: EventStream) -> None:
        b = harness.provider.bind(tenant_b())
        await stream.append(TOPIC, [out("k", "alpha-only")])
        await b.append(TOPIC, [out("k", "beta-only")])
        consumer = b.consumer(TOPIC, group=_group("isolation"))
        assert [r.value for r in await drain(consumer)] == [b"beta-only"]
        await consumer.close()
