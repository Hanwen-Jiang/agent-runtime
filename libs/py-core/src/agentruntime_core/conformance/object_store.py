"""ObjectStore 的一致性测试。语义见 agentruntime_core.providers.object_store。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import timedelta
from typing import Protocol

import pytest

from agentruntime_core.conformance.common import (
    FaultInjectionUnsupported,
    Harness,
    StandardRules,
    TENANT_A_ID,
    expect_error,
    tenant_a,
    tenant_b,
)
from agentruntime_core.errors import ErrorCategory, NotFoundError, ProviderError
from agentruntime_core.providers.object_store import (
    MAX_SIGNED_UPLOAD_TTL,
    LifecycleRule,
    ObjectStore,
    ObjectStoreProvider,
    SignedUpload,
)


class ObjectStoreHarness(Harness[ObjectStoreProvider], Protocol):
    async def fetch(self, url: str) -> bytes:
        """用 temporary_url 生成的 GET 地址取对象；地址过期或无效时抛 DeniedError。"""
        ...

    async def upload(self, url: str, data: bytes) -> None:
        """用 temporary_url 生成的 PUT 地址上传；地址过期或无效时抛 DeniedError。"""
        ...

    async def upload_signed(self, url: str, data: bytes, headers: Mapping[str, str]) -> None:
        """用 signed_upload 给的地址 PUT data，带上 headers（Content-Length 由 data 定）。被拒时抛 ProviderError：
        地址过期或签名不对 DeniedError，内容与签的哈希不符 InvalidError，对象已存在 ConflictError。"""
        ...

    async def run_lifecycle(self) -> None:
        """立即执行一次生命周期清理；做不到时抛 FaultInjectionUnsupported。"""
        ...


MALFORMED_KEYS = ["", "/abs", "a//b", "a/../b", "./a", "a/.", "a\\b", "a/\nb", "x" * 1025]


class ObjectStoreConformance(StandardRules):
    probe_operation = "put"

    async def write_probe(self, client: ObjectStore, name: str) -> None:
        await client.put(f"probes/{name}", b"probe")

    async def probe_visible(self, client: ObjectStore, name: str) -> bool:
        try:
            await client.head(f"probes/{name}")
        except NotFoundError:
            return False
        return True

    @pytest.fixture
    def store(self, harness: ObjectStoreHarness) -> ObjectStore:
        return harness.provider.bind(tenant_a())

    async def test_put_then_get(self, store: ObjectStore) -> None:
        data = "华东退款明细".encode() + b"\x00\xff"
        info = await store.put("reports/q3.csv", data, content_type="text/csv", metadata={"source": "q_region"})
        assert info.key == "reports/q3.csv"
        assert info.size == len(data)
        assert info.sha256 == hashlib.sha256(data).hexdigest()
        assert info.content_type == "text/csv"
        got = await store.get("reports/q3.csv")
        assert got.data == data
        assert got.info.sha256 == info.sha256
        assert dict(got.info.metadata) == {"source": "q_region"}

    async def test_empty_object_is_allowed(self, store: ObjectStore) -> None:
        await store.put("empty", b"")
        assert (await store.get("empty")).data == b""

    async def test_uri_carries_tenant_prefix(self, store: ObjectStore) -> None:
        info = await store.put("reports/q3.csv", b"x")
        assert info.uri == f"obj://{TENANT_A_ID}/reports/q3.csv"
        assert store.uri("reports/q3.csv") == info.uri
        assert store.key_of(info.uri) == "reports/q3.csv"

    async def test_platform_uri_uses_reserved_prefix(self, harness: ObjectStoreHarness) -> None:
        platform = harness.provider.bind_platform("conformance")
        assert platform.uri("evals/set.json") == "obj://_platform/evals/set.json"

    async def test_key_of_other_tenant_uri_is_denied(self, harness: ObjectStoreHarness) -> None:
        a = harness.provider.bind(tenant_a())
        b = harness.provider.bind(tenant_b())
        uri = (await a.put("secret.txt", b"x")).uri
        with expect_error(ErrorCategory.DENIED):
            b.key_of(uri)
        with expect_error(ErrorCategory.DENIED):
            a.key_of("obj://_platform/evals/set.json")

    @pytest.mark.parametrize("uri", ["s3://bucket/key", "obj://", "obj://t-alpha", "obj://t-alpha/", "obj://T1/x"])
    async def test_key_of_malformed_uri_is_invalid(self, store: ObjectStore, uri: str) -> None:
        with expect_error(ErrorCategory.INVALID):
            store.key_of(uri)

    async def test_get_missing_is_not_found(self, store: ObjectStore) -> None:
        with expect_error(ErrorCategory.NOT_FOUND):
            await store.get("missing")
        with expect_error(ErrorCategory.NOT_FOUND):
            await store.head("missing")

    async def test_put_same_content_twice_is_idempotent(self, store: ObjectStore) -> None:
        first = await store.put("k", b"same")
        second = await store.put("k", b"same")
        assert first.sha256 == second.sha256
        assert (await store.list()).items[0].key == "k"
        assert len((await store.list()).items) == 1

    async def test_put_if_absent_conflicts_on_existing_key(self, store: ObjectStore) -> None:
        await store.put("k", b"first", if_absent=True)
        with expect_error(ErrorCategory.CONFLICT):
            await store.put("k", b"second", if_absent=True)
        assert (await store.get("k")).data == b"first"

    async def test_delete_reports_existence_and_is_repeatable(self, store: ObjectStore) -> None:
        await store.put("k", b"x")
        assert await store.delete("k") is True
        with expect_error(ErrorCategory.NOT_FOUND):
            await store.get("k")
        assert await store.delete("k") is False

    @pytest.mark.parametrize("key", MALFORMED_KEYS, ids=range(len(MALFORMED_KEYS)))
    async def test_malformed_key_is_invalid(self, store: ObjectStore, key: str) -> None:
        with expect_error(ErrorCategory.INVALID):
            await store.put(key, b"x")

    async def test_list_by_prefix_with_pagination(self, store: ObjectStore) -> None:
        for key in ["a/3", "a/1", "b/1", "a/2"]:
            await store.put(key, b"x")
        page = await store.list(prefix="a/", limit=2)
        assert [i.key for i in page.items] == ["a/1", "a/2"]
        assert page.next_cursor is not None
        page = await store.list(prefix="a/", limit=2, cursor=page.next_cursor)
        assert [i.key for i in page.items] == ["a/3"]
        assert page.next_cursor is None

    async def test_list_is_isolated_per_tenant(self, harness: ObjectStoreHarness) -> None:
        a = harness.provider.bind(tenant_a())
        b = harness.provider.bind(tenant_b())
        await a.put("shared-name", b"x")
        assert (await b.list()).items == ()

    async def test_temporary_get_url_expires(self, harness: ObjectStoreHarness, store: ObjectStore) -> None:
        await store.put("k", b"payload")
        url = await store.temporary_url("k", ttl=timedelta(seconds=60))
        assert await harness.fetch(url) == b"payload"
        await harness.advance(timedelta(seconds=61))
        with expect_error(ErrorCategory.DENIED):
            await harness.fetch(url)

    async def test_temporary_put_url_uploads(self, harness: ObjectStoreHarness, store: ObjectStore) -> None:
        url = await store.temporary_url("uploads/x", ttl=timedelta(seconds=60), method="PUT")
        await harness.upload(url, b"uploaded")
        assert (await store.get("uploads/x")).data == b"uploaded"

    async def test_temporary_url_requires_positive_ttl(self, store: ObjectStore) -> None:
        await store.put("k", b"x")
        with expect_error(ErrorCategory.INVALID):
            await store.temporary_url("k", ttl=timedelta(0))

    # --- signed_upload（STOR-SEC，docs/plan/workspace-read-latency-and-disk-2026-11-03.md 第 8.2 节）---
    # 拿到地址的一方（环境里的 agent，也就是用户代码）只能把内容正确、长度正确的这一个对象写一次。

    async def _signed(self, store: ObjectStore, key: str, data: bytes, ttl: timedelta = timedelta(minutes=5)) -> SignedUpload:
        return await store.signed_upload(key, ttl=ttl, sha256=hashlib.sha256(data).hexdigest(), size=len(data))

    async def test_signed_upload_writes_the_signed_content(self, harness: ObjectStoreHarness, store: ObjectStore) -> None:
        data = b"block \x00\xff content"
        signed = await self._signed(store, "blocks/aa", data)
        assert signed.url and signed.headers
        await harness.upload_signed(signed.url, data, signed.headers)
        assert (await store.get("blocks/aa")).data == data

    @pytest.mark.parametrize("other", [b"BLOCK \x00\xff CONTENT", b"block \x00\xff content!", b"short"], ids=["same-size", "longer", "shorter"])
    async def test_signed_upload_rejects_other_content(self, harness: ObjectStoreHarness, store: ObjectStore, other: bytes) -> None:
        signed = await self._signed(store, "blocks/aa", b"block \x00\xff content")
        with pytest.raises(ProviderError):
            await harness.upload_signed(signed.url, other, signed.headers)
        with expect_error(ErrorCategory.NOT_FOUND):
            await store.head("blocks/aa")

    async def test_signed_upload_needs_its_headers(self, harness: ObjectStoreHarness, store: ObjectStore) -> None:
        data = b"block"
        signed = await self._signed(store, "blocks/aa", data)
        with pytest.raises(ProviderError):
            await harness.upload_signed(signed.url, data, {})
        with expect_error(ErrorCategory.NOT_FOUND):
            await store.head("blocks/aa")

    async def test_signed_upload_never_overwrites(self, harness: ObjectStoreHarness, store: ObjectStore) -> None:
        await store.put("blocks/aa", b"original")
        data = b"replaced"
        signed = await self._signed(store, "blocks/aa", data)
        with pytest.raises(ProviderError):
            await harness.upload_signed(signed.url, data, signed.headers)
        assert (await store.get("blocks/aa")).data == b"original"

    async def test_signed_upload_expires(self, harness: ObjectStoreHarness, store: ObjectStore) -> None:
        data = b"block"
        signed = await self._signed(store, "blocks/aa", data, ttl=timedelta(minutes=5))
        await harness.advance(timedelta(minutes=6))
        with expect_error(ErrorCategory.DENIED):
            await harness.upload_signed(signed.url, data, signed.headers)

    @pytest.mark.parametrize(
        ("sha256", "size", "ttl"),
        [
            ("not-hex", 1, timedelta(minutes=1)),
            ("A" * 64, 1, timedelta(minutes=1)),
            ("a" * 64, -1, timedelta(minutes=1)),
            ("a" * 64, 1, timedelta(0)),
            ("a" * 64, 1, MAX_SIGNED_UPLOAD_TTL + timedelta(seconds=1)),
        ],
        ids=["not-hex", "upper-case", "negative-size", "zero-ttl", "ttl-too-long"],
    )
    async def test_signed_upload_rejects_bad_arguments(self, store: ObjectStore, sha256: str, size: int, ttl: timedelta) -> None:
        with expect_error(ErrorCategory.INVALID):
            await store.signed_upload("blocks/aa", ttl=ttl, sha256=sha256, size=size)

    async def test_lifecycle_rule_expires_old_objects(self, harness: ObjectStoreHarness, store: ObjectStore) -> None:
        await store.set_lifecycle([LifecycleRule(prefix="tmp/", expire_after=timedelta(hours=1))])
        await store.put("tmp/old", b"x")
        await store.put("keep/old", b"x")
        await harness.advance(timedelta(hours=2))
        await store.put("tmp/new", b"x")
        try:
            await harness.run_lifecycle()
        except FaultInjectionUnsupported:
            pytest.skip("该实现无法立即执行生命周期清理")
        with expect_error(ErrorCategory.NOT_FOUND):
            await store.get("tmp/old")
        assert (await store.get("keep/old")).data == b"x"
        assert (await store.get("tmp/new")).data == b"x"
