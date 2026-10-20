"""Claim-Check：Temporal 大载荷卸载到 ObjectStore，事件历史里只留引用（设计书 4.7、附录 K.3、L.8；计划 S-06）。

两种语言共用同一种格式：Go 侧见 libs/go-core/adapters/temporal/claimcheck，测试向量见
tests/contract/fixtures/claimcheck/vector.json。

格式（版本 1）：
- 只卸载“确定性序列化后超过阈值”的载荷（默认 64 KiB，可配置）；不超过的原样保留。
- 对象键：claim-check/<原载荷 sha256 十六进制>，在租户自己的 ObjectStore 作用域里（obj://<租户>/…）。
  内容寻址：同一载荷再编码（例如重放）复用已有对象，不重复上传、不重复生成数据密钥。
- 对象内容：b"ARCC" | u8 版本 | u32 头部长度（大端）| 头部 JSON（alg、key、nonce）| 密文。
  每个对象一把数据密钥（SecretStore.generate_data_key("claim_check")，按租户包装），AES-256-GCM 加密，
  AAD = "agentruntime/claim-check/v1" 0x00 租户 ID 0x00 对象键。
- 引用：metadata {"encoding": "binary/agentruntime-claim-check", "agentruntime-claim-check-version": "1"}，
  data 为 JSON {"sha256", "size", "uri", "v": 1}。版本号为以后迁移到 Temporal External Storage 留出路径。

租户：
- TenantClaimCheck 绑定一个租户；解码别的租户的引用一律 denied（引用的租户、ObjectStore 的作用域、
  数据密钥的作用域三处都核对）。
- 接入 Temporal 时由 TenantResolver 按序列化上下文确定租户：客户端调用用 StaticTenant（租户来自认证身份），
  Worker 用 WorkflowMemoResolver（工作流 ID 的根 → describe → memo 里的 agentruntime_scope）。确定不了租户时拒绝，
  不会用别的租户或平台密钥代替。

错误（统一错误模型）：
- 跨租户、确定不了租户：denied（claim_check_cross_tenant、claim_check_no_tenant）。
- 数据密钥解不开（密钥不对）：invalid（claim_check_key_mismatch）；解密或校验失败（对象被篡改）：
  invalid（claim_check_decrypt_failed、claim_check_digest_mismatch）；引用格式不对或版本不支持：invalid（claim_check_bad_reference）。
- 对象不存在（例如已过保留期）：not_found（claim_check_missing）。
- ObjectStore 或 SecretStore 暂时不可用：先在本地重试，仍不行报 transient（claim_check_unavailable）。
错误信息带引用的 uri，不带任何密钥或载荷内容。

生命周期：对象必须活得比 Temporal 的 retention 长，lifecycle_rule 给出 claim-check/ 前缀的规则并做校验。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import struct
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol, TypeVar

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from temporalio.api.common.v1 import Memo, Payload
from temporalio.client import Client, WorkflowHistory
from temporalio.converter import DataConverter, PayloadCodec, SerializationContext, WithSerializationContext
from temporalio.service import RPCError, RPCStatusCode
from typing_extensions import Self

from agentruntime_core.adapters.temporal import SCOPE_MEMO_KEY
from agentruntime_core.dev._common import PLATFORM_ID
from agentruntime_core.errors import ConflictError, DeniedError, InvalidError, NotFoundError, TransientError
from agentruntime_core.providers.object_store import LifecycleRule, ObjectStoreProvider
from agentruntime_core.providers.secret_store import SecretStoreProvider
from agentruntime_core.secrets import Secret
from agentruntime_core.tenancy import TenantContext

DEFAULT_THRESHOLD = 64 * 1024
ENCODING = b"binary/agentruntime-claim-check"
VERSION_KEY = "agentruntime-claim-check-version"
VERSION = 1
KEY_PREFIX = "claim-check/"
DATA_KEY_PURPOSE = "claim_check"
ALGORITHM = "AES-256-GCM"
CONTENT_TYPE = "application/vnd.agentruntime.claim-check"
# 对象的生命周期比 Temporal 的 retention 至少多出这么久。
DEFAULT_LIFECYCLE_MARGIN = timedelta(days=7)

_MAGIC = b"ARCC"
_AAD_PREFIX = b"agentruntime/claim-check/v1\x00"
_NONCE_SIZE = 12
_URI = re.compile(r"obj://([a-z0-9][a-z0-9_-]{0,62})/(claim-check/[0-9a-f]{64})")
_SHA256 = re.compile(r"[0-9a-f]{64}")

T = TypeVar("T")


@dataclass(frozen=True)
class Reference:
    version: int
    uri: str
    sha256: str
    size: int


@dataclass(frozen=True)
class SealedHeader:
    version: int
    algorithm: str
    wrapped_key: bytes
    nonce: bytes


def _canonical(obj: Any) -> bytes:
    """键排序、无空白的 JSON，与 Go 的 json.Marshal（map）逐字节相同。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _key_bytes(secret: Secret) -> bytes:
    value = secret.reveal()
    return value if isinstance(value, bytes) else value.encode()


def _aad(tenant_id: str, object_key: str) -> bytes:
    return _AAD_PREFIX + tenant_id.encode() + b"\x00" + object_key.encode()


def is_reference(payload: Payload) -> bool:
    """载荷是不是 Claim-Check 的引用。"""
    return payload.metadata.get("encoding") == ENCODING


def parse_reference(payload: Payload) -> Reference:
    """解析引用；格式不对或版本不支持报 invalid（claim_check_bad_reference）。"""
    version = payload.metadata.get(VERSION_KEY, b"").decode(errors="replace")
    if version != str(VERSION):
        raise InvalidError("claim_check_bad_reference", f"不支持的 Claim-Check 版本：{version or '缺失'}")
    try:
        raw = json.loads(payload.data)
    except (ValueError, UnicodeDecodeError):
        raise InvalidError("claim_check_bad_reference", "Claim-Check 引用不是合法的 JSON") from None
    if not isinstance(raw, dict) or raw.get("v") != VERSION:
        raise InvalidError("claim_check_bad_reference", f"不支持的 Claim-Check 引用版本：{raw.get('v') if isinstance(raw, dict) else raw!r}")
    uri, digest, size = raw.get("uri"), raw.get("sha256"), raw.get("size")
    if not isinstance(uri, str) or not _URI.fullmatch(uri):
        raise InvalidError("claim_check_bad_reference", "Claim-Check 引用的 uri 格式不对")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest) or not uri.endswith(digest):
        raise InvalidError("claim_check_bad_reference", f"Claim-Check 引用的 sha256 不对：{uri}")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise InvalidError("claim_check_bad_reference", f"Claim-Check 引用的 size 不对：{uri}")
    return Reference(version=VERSION, uri=uri, sha256=digest, size=size)


def _reference_payload(uri: str, digest: str, size: int) -> Payload:
    return Payload(
        metadata={"encoding": ENCODING, VERSION_KEY: str(VERSION).encode()},
        data=_canonical({"sha256": digest, "size": size, "uri": uri, "v": VERSION}),
    )


def serialize(payload: Payload) -> bytes:
    """原载荷的确定性序列化（阈值、sha256 与加密都基于它，两种语言字节相同）。"""
    return payload.SerializeToString(deterministic=True)


def seal(*, data_key: bytes, nonce: bytes, wrapped_key: bytes, tenant_id: str, object_key: str, plaintext: bytes) -> bytes:
    """按格式加密并拼出对象内容（测试向量用固定的密钥与 nonce）。"""
    header = _canonical({"alg": ALGORITHM, "key": base64.b64encode(wrapped_key).decode(), "nonce": base64.b64encode(nonce).decode()})
    ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, _aad(tenant_id, object_key))
    return _MAGIC + bytes([VERSION]) + struct.pack(">I", len(header)) + header + ciphertext


def parse_sealed(data: bytes) -> tuple[SealedHeader, bytes]:
    """拆出头部与密文；格式不对报 invalid（claim_check_decrypt_failed）。"""
    bad = InvalidError("claim_check_decrypt_failed", "Claim-Check 对象的格式不对")
    if len(data) < 9 or data[:4] != _MAGIC:
        raise bad
    if data[4] != VERSION:
        raise InvalidError("claim_check_decrypt_failed", f"不支持的 Claim-Check 对象版本：{data[4]}")
    (header_len,) = struct.unpack(">I", data[5:9])
    if len(data) < 9 + header_len:
        raise bad
    try:
        header = json.loads(data[9 : 9 + header_len])
        wrapped = base64.b64decode(header["key"], validate=True)
        nonce = base64.b64decode(header["nonce"], validate=True)
        algorithm = header["alg"]
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise bad from None
    if algorithm != ALGORITHM or len(nonce) != _NONCE_SIZE:
        raise bad
    return SealedHeader(version=VERSION, algorithm=algorithm, wrapped_key=wrapped, nonce=nonce), data[9 + header_len :]


def open_sealed(data: bytes, *, data_key: bytes, tenant_id: str, object_key: str) -> bytes:
    """解密对象内容；密钥不对或被篡改报 invalid（claim_check_decrypt_failed）。"""
    header, ciphertext = parse_sealed(data)
    try:
        return AESGCM(data_key).decrypt(header.nonce, ciphertext, _aad(tenant_id, object_key))
    except (InvalidTag, ValueError):
        raise InvalidError("claim_check_decrypt_failed", "Claim-Check 对象解密失败：数据密钥不对或对象被篡改") from None


def tenant_from_memo(memo: Memo) -> TenantContext | None:
    """工作流 memo 里的作用域（agentruntime_scope）对应的租户；没有、或是平台作用域时返回 None。"""
    field = memo.fields.get(SCOPE_MEMO_KEY)
    if field is None:
        return None
    try:
        [owner] = DataConverter.default.payload_converter.from_payloads([field])
    except Exception:  # noqa: BLE001 — memo 格式不对等同于没有作用域
        return None
    if not isinstance(owner, str) or not owner or owner == PLATFORM_ID:
        return None
    return TenantContext(tenant_id=owner)


def tenant_from_history(history: WorkflowHistory) -> TenantContext | None:
    """事件历史第一个事件（WorkflowExecutionStarted）的 memo 里的租户；回放工具按它绑定编解码。"""
    if not history.events:
        return None
    first = history.events[0]
    if not first.HasField("workflow_execution_started_event_attributes"):
        return None
    return tenant_from_memo(first.workflow_execution_started_event_attributes.memo)


class TenantClaimCheck:
    """绑定了一个租户的 Claim-Check 编解码。"""

    def __init__(self, claim_check: ClaimCheck, tenant: TenantContext) -> None:
        self._cc = claim_check
        self._tenant_id = tenant.tenant_id
        self._objects = claim_check.objects.bind(tenant)
        self._secrets = claim_check.secrets.bind(tenant)

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        out = []
        for payload in payloads:
            plaintext = serialize(payload)
            out.append(payload if len(plaintext) <= self._cc.threshold else await self._offload(plaintext))
        return out

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [await self._restore(p) if is_reference(p) else p for p in payloads]

    async def _offload(self, plaintext: bytes) -> Payload:
        digest = hashlib.sha256(plaintext).hexdigest()
        key = KEY_PREFIX + digest
        uri = self._objects.uri(key)
        if not await self._retry("ObjectStore", uri, lambda: self._exists(key)):
            data_key = await self._retry("SecretStore", uri, lambda: self._secrets.generate_data_key(DATA_KEY_PURPOSE))
            sealed = seal(
                data_key=_key_bytes(data_key.plaintext),
                nonce=os.urandom(_NONCE_SIZE),
                wrapped_key=data_key.wrapped,
                tenant_id=self._tenant_id,
                object_key=key,
                plaintext=plaintext,
            )
            try:
                await self._retry("ObjectStore", uri, lambda: self._objects.put(key, sealed, content_type=CONTENT_TYPE, if_absent=True))
            except ConflictError:
                pass  # 同一载荷别人刚写过：内容寻址，用已有的对象
        return _reference_payload(uri, digest, len(plaintext))

    async def _exists(self, key: str) -> bool:
        try:
            await self._objects.head(key)
        except NotFoundError:
            return False
        return True

    async def _restore(self, payload: Payload) -> Payload:
        ref = parse_reference(payload)
        match = _URI.fullmatch(ref.uri)
        assert match is not None  # parse_reference 已校验
        if match.group(1) != self._tenant_id:
            raise DeniedError("claim_check_cross_tenant", f"Claim-Check 引用 {ref.uri} 属于别的租户")
        key = match.group(2)
        try:
            obj = await self._retry("ObjectStore", ref.uri, lambda: self._objects.get(key))
        except NotFoundError:
            raise NotFoundError("claim_check_missing", f"Claim-Check 对象 {ref.uri} 不存在（可能已过保留期）") from None
        try:
            header, _ = parse_sealed(obj.data)
        except InvalidError as err:
            raise InvalidError(err.code, f"{err.message}：{ref.uri}") from None
        try:
            data_key = await self._retry("SecretStore", ref.uri, lambda: self._secrets.unwrap_data_key(header.wrapped_key, purpose=DATA_KEY_PURPOSE))
        except DeniedError:
            raise DeniedError("claim_check_cross_tenant", f"Claim-Check 对象 {ref.uri} 的数据密钥属于别的租户") from None
        except InvalidError:
            raise InvalidError("claim_check_key_mismatch", f"解不开 Claim-Check 对象 {ref.uri} 的数据密钥：租户的密钥加密密钥不对") from None
        try:
            plaintext = open_sealed(obj.data, data_key=_key_bytes(data_key), tenant_id=self._tenant_id, object_key=key)
        except InvalidError as err:
            raise InvalidError(err.code, f"{err.message}：{ref.uri}") from None
        if len(plaintext) != ref.size or hashlib.sha256(plaintext).hexdigest() != ref.sha256:
            raise InvalidError("claim_check_digest_mismatch", f"Claim-Check 对象 {ref.uri} 的内容与引用不符")
        return Payload.FromString(plaintext)

    async def _retry(self, store: str, uri: str, op: Callable[[], Awaitable[T]]) -> T:
        """依赖暂时不可用时在本地重试几次（指数退避），仍不行报 transient（claim_check_unavailable）。"""
        last: TransientError | None = None
        for attempt in range(self._cc.attempts):
            try:
                return await op()
            except TransientError as err:
                last = err
                if attempt + 1 < self._cc.attempts:
                    await asyncio.sleep(self._cc.backoff.total_seconds() * (2**attempt))
        assert last is not None
        raise TransientError(
            "claim_check_unavailable", f"{store} 暂时不可用（{uri}），已重试 {self._cc.attempts} 次：{last.code}", retry_after=last.retry_after
        )


class TenantResolver(Protocol):
    async def tenant_for(self, context: SerializationContext | None) -> TenantContext | None:
        """按 Temporal 的序列化上下文确定租户；确定不了返回 None。"""
        ...


class StaticTenant:
    """固定的租户：客户端按认证身份绑定租户后发起的调用用它。"""

    def __init__(self, tenant: TenantContext) -> None:
        self.tenant = tenant

    async def tenant_for(self, context: SerializationContext | None) -> TenantContext | None:
        return self.tenant


class WorkflowMemoResolver:
    """Worker 一侧：工作流 ID 的根（第一个 / 之前，即 Task 或 Session 的 ID）→ describe → memo 里的作用域。

    结果按根 ID 缓存。describe 用不带 Claim-Check 的客户端，免得解码 memo 时又回到这里。
    """

    def __init__(self, client: Client) -> None:
        self.client = client
        self._cache: dict[str, TenantContext] = {}

    async def tenant_for(self, context: SerializationContext | None) -> TenantContext | None:
        workflow_id = getattr(context, "workflow_id", None)
        if not workflow_id:
            return None
        root = workflow_id.split("/", 1)[0]
        cached = self._cache.get(root)
        if cached is not None:
            return cached
        try:
            description = await self.client.get_workflow_handle(root).describe()
        except RPCError as err:
            if err.status == RPCStatusCode.NOT_FOUND:
                return None
            raise TransientError("claim_check_unavailable", f"查不到工作流 {root} 的作用域：{err.status.name}") from None
        tenant = tenant_from_memo(description.raw_description.workflow_execution_info.memo)
        if tenant is not None:
            self._cache[root] = tenant
        return tenant


class ClaimCheckCodec(PayloadCodec, WithSerializationContext):
    """接入 Temporal 的 PayloadCodec：按序列化上下文确定租户，再交给 TenantClaimCheck。

    不超过阈值的载荷、不是引用的载荷原样通过，不会去确定租户。
    """

    def __init__(self, claim_check: ClaimCheck, resolver: TenantResolver, context: SerializationContext | None = None) -> None:
        self.claim_check = claim_check
        self.resolver = resolver
        self.context = context

    def with_context(self, context: SerializationContext) -> Self:
        return type(self)(self.claim_check, self.resolver, context)

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        if all(len(serialize(p)) <= self.claim_check.threshold for p in payloads):
            return list(payloads)
        return await (await self._bound("卸载")).encode(payloads)

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        if not any(is_reference(p) for p in payloads):
            return list(payloads)
        return await (await self._bound("取回")).decode(payloads)

    async def _bound(self, action: str) -> TenantClaimCheck:
        tenant = await self.resolver.tenant_for(self.context)
        if tenant is None:
            workflow_id = getattr(self.context, "workflow_id", None) or "未知"
            raise DeniedError("claim_check_no_tenant", f"无法确定载荷所属的租户（工作流 {workflow_id}），拒绝{action}")
        return self.claim_check.bind(tenant)


class ClaimCheck:
    def __init__(
        self,
        objects: ObjectStoreProvider,
        secrets: SecretStoreProvider,
        *,
        threshold: int = DEFAULT_THRESHOLD,
        attempts: int = 3,
        backoff: timedelta = timedelta(milliseconds=200),
    ) -> None:
        if threshold < 0:
            raise InvalidError("invalid_threshold", "Claim-Check 的阈值不能是负数")
        if attempts < 1:
            raise InvalidError("invalid_attempts", "Claim-Check 至少尝试一次")
        self.objects = objects
        self.secrets = secrets
        self.threshold = threshold
        self.attempts = attempts
        self.backoff = backoff

    def bind(self, tenant: TenantContext) -> TenantClaimCheck:
        return TenantClaimCheck(self, tenant)

    def codec(self, resolver: TenantResolver) -> ClaimCheckCodec:
        return ClaimCheckCodec(self, resolver)

    def lifecycle_rule(self, *, retention: timedelta, margin: timedelta = DEFAULT_LIFECYCLE_MARGIN) -> LifecycleRule:
        """claim-check/ 前缀的生命周期规则：expire_after = retention + margin；margin 不大于 0 报 invalid。"""
        if retention <= timedelta(0):
            raise InvalidError("invalid_lifecycle", "Temporal 的 retention 必须大于 0")
        if margin <= timedelta(0):
            raise InvalidError("invalid_lifecycle", "Claim-Check 对象的生命周期必须长于 Temporal 的 retention")
        return LifecycleRule(prefix=KEY_PREFIX, expire_after=retention + margin)
