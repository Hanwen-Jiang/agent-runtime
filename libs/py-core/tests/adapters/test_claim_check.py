"""S-06 验收：Claim-Check 编解码（Python 侧；设计书 4.7、附录 K.3、L.8；计划 S-06）。

- 阈值上下的载荷各一组：不超过阈值原样保留，超过的卸载到 ObjectStore、历史里只留引用，解码还原出逐字节相同的载荷。
- 跨语言一致：引用格式、加密方式、元数据键按测试向量（tests/contract/fixtures/claimcheck/vector.json），
  Go 侧用同一份向量；跨语言往返在 tests/contract/test_claim_check_crosslang.py。
- 密钥错误时解码失败，报错明确；对象被篡改、不存在时同样。
- 租户隔离：按租户的数据密钥加密，对象在租户自己的前缀下；租户 A 的引用不能用租户 B 的上下文解码。
- 生命周期：对象的保留期必须长于 Temporal 的 retention。
- 可迁移：引用带版本号，不认识的版本明确拒绝。
- ObjectStore 暂时不可用：本地有限次重试，仍不行报 transient（S-07 场景 10 的端到端用例在 tests/e2e/faults）。

依赖用 S-02 的开发实现：本地磁盘 ObjectStore、本地文件 SecretStore。
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio.api.common.v1 import Payload
from temporalio.converter import WorkflowSerializationContext

from agentruntime_core.adapters import claim_check as cc
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.errors import DeniedError, ErrorCategory, InvalidError, NotFoundError, TransientError
from agentruntime_core.tenancy import TenantContext
from agentruntime_core.testing import FaultInjector

T1 = TenantContext(tenant_id="t1")
T2 = TenantContext(tenant_id="t2")
THRESHOLD = cc.DEFAULT_THRESHOLD
VECTOR = Path(__file__).resolve().parents[4] / "tests" / "contract" / "fixtures" / "claimcheck" / "vector.json"


class CountingSecrets:
    """SecretStore 工厂的包装：数 generate_data_key 调了几次。"""

    def __init__(self, inner: LocalFileSecretStoreProvider) -> None:
        self.inner = inner
        self.generated = 0

    def bind(self, tenant: TenantContext) -> Any:
        outer = self
        store = self.inner.bind(tenant)

        class _Store:
            def __getattr__(self, name: str) -> Any:
                return getattr(store, name)

            async def generate_data_key(self, purpose: str) -> Any:
                outer.generated += 1
                return await store.generate_data_key(purpose)

        return _Store()

    def bind_platform(self, purpose: str) -> Any:
        return self.inner.bind_platform(purpose)

    async def aclose(self) -> None:
        await self.inner.aclose()


@dataclass
class Fixture:
    root: Path
    object_faults: FaultInjector
    secret_faults: FaultInjector
    objects: LocalDiskObjectStoreProvider
    secrets: CountingSecrets
    claim_check: cc.ClaimCheck

    def with_threshold(self, threshold: int) -> cc.ClaimCheck:
        return cc.ClaimCheck(self.objects, self.secrets, threshold=threshold, backoff=timedelta(milliseconds=1))


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    object_faults, secret_faults = FaultInjector(), FaultInjector()
    objects = LocalDiskObjectStoreProvider(tmp_path / "objects", signing_key=b"claim-check-test", faults=object_faults)
    secrets = CountingSecrets(LocalFileSecretStoreProvider(tmp_path / "secrets.json", faults=secret_faults))
    claim_check = cc.ClaimCheck(objects, secrets, backoff=timedelta(milliseconds=1))
    return Fixture(tmp_path, object_faults, secret_faults, objects, secrets, claim_check)


def size_of(payload: Payload) -> int:
    return len(payload.SerializeToString(deterministic=True))


def payload_of_size(size: int, *, fill: bytes = b"x") -> Payload:
    """确定性序列化后恰好 size 字节的载荷。"""
    payload = Payload(metadata={"encoding": b"json/plain"}, data=b"")
    data_len = max(0, size - size_of(payload) - 4)
    while True:
        payload = Payload(metadata={"encoding": b"json/plain"}, data=fill * data_len)
        actual = size_of(payload)
        if actual == size:
            return payload
        data_len += size - actual


async def stored_keys(objects: LocalDiskObjectStoreProvider, tenant: TenantContext) -> list[str]:
    page = await objects.bind(tenant).list(prefix=cc.KEY_PREFIX)
    return [item.key for item in page.items]


# --- 阈值上下 ---


@pytest.mark.parametrize("size", [64, THRESHOLD - 1, THRESHOLD])
async def test_payloads_up_to_the_threshold_pass_through(fx: Fixture, size: int) -> None:
    payload = payload_of_size(size)
    out = await fx.claim_check.bind(T1).encode([payload])
    assert out == [payload], "不超过阈值的载荷原样保留"
    assert not cc.is_reference(out[0])
    assert await stored_keys(fx.objects, T1) == []
    assert fx.secrets.generated == 0, "不卸载就不生成数据密钥"


@pytest.mark.parametrize("size", [THRESHOLD + 1, 1024 * 1024])
async def test_payloads_above_the_threshold_are_offloaded_and_restored(fx: Fixture, size: int) -> None:
    payload = payload_of_size(size)
    [ref] = await fx.claim_check.bind(T1).encode([payload])

    assert cc.is_reference(ref)
    assert ref.metadata["encoding"] == cc.ENCODING
    assert ref.metadata[cc.VERSION_KEY] == str(cc.VERSION).encode()
    assert size_of(ref) < 1024, "历史里只留引用"
    parsed = cc.parse_reference(ref)
    digest = hashlib.sha256(cc.serialize(payload)).hexdigest()
    assert parsed == cc.Reference(version=1, uri=f"obj://t1/{cc.KEY_PREFIX}{digest}", sha256=digest, size=size)

    assert await stored_keys(fx.objects, T1) == [f"{cc.KEY_PREFIX}{digest}"]
    stored = (await fx.objects.bind(T1).get(f"{cc.KEY_PREFIX}{digest}")).data
    assert b"x" * 64 not in stored, "对象里是密文"

    [restored] = await fx.claim_check.bind(T1).decode([ref])
    assert restored == payload
    assert cc.serialize(restored) == cc.serialize(payload), "解码还原出逐字节相同的载荷"


async def test_threshold_is_configurable(fx: Fixture) -> None:
    small = fx.with_threshold(1024)
    [kept, offloaded] = await small.bind(T1).encode([payload_of_size(1024), payload_of_size(1025)])
    assert not cc.is_reference(kept) and cc.is_reference(offloaded)


async def test_a_batch_keeps_its_order_and_only_large_payloads_change(fx: Fixture) -> None:
    batch = [payload_of_size(100), payload_of_size(THRESHOLD + 10, fill=b"a"), payload_of_size(200), payload_of_size(THRESHOLD * 2, fill=b"b")]
    out = await fx.claim_check.bind(T1).encode(batch)
    assert [cc.is_reference(p) for p in out] == [False, True, False, True]
    assert out[0] == batch[0] and out[2] == batch[2]
    assert await fx.claim_check.bind(T1).decode(out) == batch
    assert await fx.claim_check.bind(T1).decode([batch[0]]) == [batch[0]], "不是引用的载荷解码时原样通过"


async def test_reencoding_the_same_payload_reuses_the_stored_object(fx: Fixture) -> None:
    """重放时工作流会再编码一次命令里的载荷：内容寻址，不重复上传、不重复生成数据密钥。"""
    payload = payload_of_size(THRESHOLD * 2)
    first = await fx.claim_check.bind(T1).encode([payload])
    second = await fx.claim_check.bind(T1).encode([payload])
    assert first == second
    assert len(await stored_keys(fx.objects, T1)) == 1
    assert fx.secrets.generated == 1


# --- 跨语言一致的格式（测试向量）---


def test_format_follows_the_shared_vector() -> None:
    vector = json.loads(VECTOR.read_text("utf-8"))
    inp, exp = vector["inputs"], vector["expected"]
    spec = vector["spec"]
    assert (spec["reference_encoding"].encode(), spec["reference_version_key"], spec["reference_version"]) == (cc.ENCODING, cc.VERSION_KEY, cc.VERSION)
    assert (spec["object_key_prefix"], spec["data_key_purpose"], spec["algorithm"]) == (cc.KEY_PREFIX, cc.DATA_KEY_PURPOSE, cc.ALGORITHM)

    payload = Payload(metadata={k: v.encode() for k, v in inp["payload"]["metadata"].items()}, data=inp["payload"]["data_utf8"].encode())
    plaintext = cc.serialize(payload)
    assert plaintext.hex() == inp["plaintext_hex"], "确定性序列化与 Go 侧字节相同"
    assert hashlib.sha256(plaintext).hexdigest() == exp["plaintext_sha256"]

    sealed = cc.seal(
        data_key=bytes.fromhex(inp["data_key_hex"]),
        nonce=bytes.fromhex(inp["nonce_hex"]),
        wrapped_key=bytes.fromhex(inp["wrapped_key_hex"]),
        tenant_id=inp["tenant_id"],
        object_key=exp["object_key"],
        plaintext=plaintext,
    )
    assert sealed.hex() == exp["sealed_hex"]
    header, _ = cc.parse_sealed(sealed)
    assert header == cc.SealedHeader(version=1, algorithm=cc.ALGORITHM, wrapped_key=bytes.fromhex(inp["wrapped_key_hex"]), nonce=bytes.fromhex(inp["nonce_hex"]))
    assert cc.open_sealed(sealed, data_key=bytes.fromhex(inp["data_key_hex"]), tenant_id=inp["tenant_id"], object_key=exp["object_key"]) == plaintext


async def test_reference_bytes_follow_the_shared_vector(fx: Fixture) -> None:
    vector = json.loads(VECTOR.read_text("utf-8"))
    inp, exp = vector["inputs"], vector["expected"]
    payload = Payload(metadata={k: v.encode() for k, v in inp["payload"]["metadata"].items()}, data=inp["payload"]["data_utf8"].encode())
    [ref] = await fx.with_threshold(0).bind(TenantContext(tenant_id=inp["tenant_id"])).encode([payload])
    assert {k: v.decode() for k, v in ref.metadata.items()} == exp["reference_metadata"]
    assert ref.data.decode() == exp["reference_data"], "引用的 JSON 与 Go 侧逐字节相同"


@pytest.mark.parametrize(
    "tamper",
    [
        lambda data: data[:-1] + bytes([data[-1] ^ 1]),  # 密文或 tag
        lambda data: data[:4] + bytes([2]) + data[5:],  # 版本
        lambda data: b"XXXX" + data[4:],  # 魔数
    ],
)
def test_open_sealed_rejects_tampering(tamper: Any) -> None:
    vector = json.loads(VECTOR.read_text("utf-8"))
    inp, exp = vector["inputs"], vector["expected"]
    with pytest.raises(InvalidError) as err:
        cc.open_sealed(tamper(bytes.fromhex(exp["sealed_hex"])), data_key=bytes.fromhex(inp["data_key_hex"]), tenant_id="t1", object_key=exp["object_key"])
    assert err.value.code == "claim_check_decrypt_failed"


def test_open_sealed_binds_the_tenant_and_object_key() -> None:
    """AAD 带租户与对象键：把对象挪到别的租户或别的键下都解不开。"""
    vector = json.loads(VECTOR.read_text("utf-8"))
    inp, exp = vector["inputs"], vector["expected"]
    sealed, key = bytes.fromhex(exp["sealed_hex"]), bytes.fromhex(inp["data_key_hex"])
    for tenant_id, object_key in (("t2", exp["object_key"]), ("t1", cc.KEY_PREFIX + "0" * 64)):
        with pytest.raises(InvalidError):
            cc.open_sealed(sealed, data_key=key, tenant_id=tenant_id, object_key=object_key)


# --- 报错明确 ---


async def test_wrong_key_fails_with_a_clear_error(fx: Fixture, tmp_path: Path) -> None:
    [ref] = await fx.claim_check.bind(T1).encode([payload_of_size(THRESHOLD * 2)])
    # 同一个 ObjectStore，换一份密钥文件：t1 的密钥加密密钥不同了。
    other_secrets = LocalFileSecretStoreProvider(tmp_path / "other-secrets.json")
    await other_secrets.bind(T1).generate_data_key(cc.DATA_KEY_PURPOSE)
    wrong = cc.ClaimCheck(fx.objects, other_secrets)
    with pytest.raises(InvalidError) as err:
        await wrong.bind(T1).decode([ref])
    assert err.value.code == "claim_check_key_mismatch"
    uri = cc.parse_reference(ref).uri
    assert uri in str(err.value), "报错带出是哪一个引用"
    kek = json.loads((tmp_path / "other-secrets.json").read_text("utf-8"))["scopes"]["t1"]["kek"]
    assert kek not in str(err.value) and base64.b64decode(kek).hex() not in str(err.value), "报错里没有密钥"


async def test_tampered_object_fails_to_decrypt(fx: Fixture) -> None:
    [ref] = await fx.claim_check.bind(T1).encode([payload_of_size(THRESHOLD * 2)])
    key = fx.objects.bind(T1).key_of(cc.parse_reference(ref).uri)
    data = (await fx.objects.bind(T1).get(key)).data
    await fx.objects.bind(T1).put(key, data[:-1] + bytes([data[-1] ^ 1]), content_type=cc.CONTENT_TYPE)
    with pytest.raises(InvalidError) as err:
        await fx.claim_check.bind(T1).decode([ref])
    assert err.value.code == "claim_check_decrypt_failed" and cc.parse_reference(ref).uri in str(err.value)


async def test_missing_object_is_reported_as_not_found(fx: Fixture) -> None:
    [ref] = await fx.claim_check.bind(T1).encode([payload_of_size(THRESHOLD * 2)])
    await fx.objects.bind(T1).delete(fx.objects.bind(T1).key_of(cc.parse_reference(ref).uri))
    with pytest.raises(NotFoundError) as err:
        await fx.claim_check.bind(T1).decode([ref])
    assert err.value.code == "claim_check_missing" and cc.parse_reference(ref).uri in str(err.value)


@pytest.mark.parametrize(
    "data",
    [
        b"not json",
        json.dumps({"v": 2, "uri": "obj://t1/claim-check/ab", "sha256": "ab", "size": 1}).encode(),
        json.dumps({"v": 1, "uri": "obj://t1/claim-check/ab"}).encode(),
    ],
)
async def test_bad_or_unknown_reference_versions_are_rejected(fx: Fixture, data: bytes) -> None:
    ref = Payload(metadata={"encoding": cc.ENCODING, cc.VERSION_KEY: b"1"}, data=data)
    with pytest.raises(InvalidError) as err:
        await fx.claim_check.bind(T1).decode([ref])
    assert err.value.code == "claim_check_bad_reference"


# --- 租户隔离 ---


async def test_a_reference_from_another_tenant_cannot_be_decoded(fx: Fixture) -> None:
    [ref] = await fx.claim_check.bind(T1).encode([payload_of_size(THRESHOLD * 2)])
    with pytest.raises(DeniedError) as err:
        await fx.claim_check.bind(T2).decode([ref])
    assert err.value.code == "claim_check_cross_tenant"


async def test_a_forged_reference_cannot_reach_another_tenants_object(fx: Fixture) -> None:
    """把 t1 的对象键写进 t2 的引用：t2 的作用域里没有这个对象，也拿不到 t1 的数据密钥。"""
    [ref] = await fx.claim_check.bind(T1).encode([payload_of_size(THRESHOLD * 2)])
    parsed = cc.parse_reference(ref)
    forged_data = json.dumps(
        {"sha256": parsed.sha256, "size": parsed.size, "uri": parsed.uri.replace("obj://t1/", "obj://t2/"), "v": 1}, sort_keys=True, separators=(",", ":")
    ).encode()
    forged = Payload(metadata=dict(ref.metadata), data=forged_data)
    with pytest.raises((NotFoundError, DeniedError)):
        await fx.claim_check.bind(T2).decode([forged])


async def test_objects_live_under_the_tenant_prefix_with_tenant_data_keys(fx: Fixture) -> None:
    [ref] = await fx.claim_check.bind(T1).encode([payload_of_size(THRESHOLD * 2)])
    uri = cc.parse_reference(ref).uri
    assert uri.startswith("obj://t1/claim-check/")
    assert await stored_keys(fx.objects, T2) == []
    with pytest.raises(DeniedError):
        fx.objects.bind(T2).key_of(uri)
    header, _ = cc.parse_sealed((await fx.objects.bind(T1).get(fx.objects.bind(T1).key_of(uri))).data)
    with pytest.raises(DeniedError):
        await fx.secrets.inner.bind(T2).unwrap_data_key(header.wrapped_key, purpose=cc.DATA_KEY_PURPOSE)
    assert await fx.secrets.inner.bind(T1).unwrap_data_key(header.wrapped_key, purpose=cc.DATA_KEY_PURPOSE)


# --- ObjectStore 暂时不可用 ---


async def test_transient_object_store_errors_are_retried(fx: Fixture) -> None:
    payload = payload_of_size(THRESHOLD * 2)
    fx.object_faults.fail_next("put", ErrorCategory.TRANSIENT, times=2)
    [ref] = await fx.claim_check.bind(T1).encode([payload])
    fx.object_faults.fail_next("get", ErrorCategory.TRANSIENT, times=2)
    assert await fx.claim_check.bind(T1).decode([ref]) == [payload]


async def test_a_persistent_outage_is_reported_as_unavailable(fx: Fixture) -> None:
    payload = payload_of_size(THRESHOLD * 2)
    fx.object_faults.fail_next("put", ErrorCategory.TRANSIENT, times=10)
    with pytest.raises(TransientError) as err:
        await fx.claim_check.bind(T1).encode([payload])
    assert err.value.code == "claim_check_unavailable" and "ObjectStore" in str(err.value)

    # 另起一个没有故障的 ObjectStore 写入对象，再让 SecretStore 一直不可用。
    fresh = cc.ClaimCheck(LocalDiskObjectStoreProvider(fx.root / "objects", signing_key=b"claim-check-test"), fx.secrets)
    [ref] = await fresh.bind(T1).encode([payload])
    fx.secret_faults.fail_next("unwrap_data_key", ErrorCategory.TRANSIENT, times=10)
    with pytest.raises(TransientError) as err:
        await fx.claim_check.bind(T1).decode([ref])
    assert err.value.code == "claim_check_unavailable" and "SecretStore" in str(err.value)


# --- 生命周期 ---


def test_lifecycle_rule_outlives_the_temporal_retention(fx: Fixture) -> None:
    rule = fx.claim_check.lifecycle_rule(retention=timedelta(days=30))
    assert rule.prefix == cc.KEY_PREFIX
    assert rule.expire_after == timedelta(days=30) + cc.DEFAULT_LIFECYCLE_MARGIN
    with pytest.raises(InvalidError):
        fx.claim_check.lifecycle_rule(retention=timedelta(days=30), margin=timedelta(0))


# --- 接入 Temporal 的 PayloadCodec ---


class RecordingResolver:
    def __init__(self, tenant: TenantContext | None) -> None:
        self.tenant = tenant
        self.contexts: list[Any] = []

    async def tenant_for(self, context: Any) -> TenantContext | None:
        self.contexts.append(context)
        return self.tenant


async def test_codec_resolves_the_tenant_only_when_something_is_offloaded(fx: Fixture) -> None:
    resolver = RecordingResolver(T1)
    context = WorkflowSerializationContext(namespace="default", workflow_id="tsk_x")
    codec = fx.claim_check.codec(resolver).with_context(context)
    small, large = payload_of_size(100), payload_of_size(THRESHOLD * 2)
    assert await codec.encode([small]) == [small]
    assert resolver.contexts == [], "全是小载荷时不去确定租户"
    [kept, ref] = await codec.encode([small, large])
    assert kept == small and cc.is_reference(ref)
    assert resolver.contexts == [context], "确定租户时用的是这次操作的序列化上下文"
    assert await codec.decode([kept, ref]) == [small, large]


async def test_codec_refuses_to_offload_or_restore_without_a_tenant(fx: Fixture) -> None:
    large = payload_of_size(THRESHOLD * 2)
    with pytest.raises(DeniedError) as err:
        await fx.claim_check.codec(RecordingResolver(None)).encode([large])
    assert err.value.code == "claim_check_no_tenant"
    [ref] = await fx.claim_check.bind(T1).encode([large])
    with pytest.raises(DeniedError):
        await fx.claim_check.codec(RecordingResolver(None)).decode([ref])
    with pytest.raises(DeniedError) as err:
        await fx.claim_check.codec(RecordingResolver(T2)).decode([ref])
    assert err.value.code == "claim_check_cross_tenant"


async def test_static_tenant_resolver() -> None:
    assert await cc.StaticTenant(T1).tenant_for(None) == T1
    assert await cc.StaticTenant(T1).tenant_for(WorkflowSerializationContext(namespace="default", workflow_id="x")) == T1
