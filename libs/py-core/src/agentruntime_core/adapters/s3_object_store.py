"""ObjectStore 的 S3 兼容实现：Cloudflare R2、AWS S3、MinIO（设计书 4.7；docs/plan/cloud-deploy.md D-09）。语义见
agentruntime_core.providers.object_store；Go 侧见 libs/go-core/adapters/s3objects，两边布局与语义一致。只用标准库（签名见 adapters.sigv4）。

- 布局：一个桶，对象名是 [前缀]<作用域>/<键>（作用域是租户 ID 或 _platform；前缀可选，几个环境共用一个桶时用，
  各环境要么都带前缀、要么都不带，否则命名空间会重叠）；路径式地址 <endpoint>/<bucket>/<对象名>。
- 元数据：内容的 sha256 放 x-amz-meta-ar-sha256，调用方的 metadata 编成 base64 的 JSON 放 x-amz-meta-ar-metadata
  （S3 的用户元数据名不区分大小写、只认 ASCII，这样原样保留）；创建时间取 Last-Modified。
- if_absent 用条件写（If-None-Match: *），已存在时 412 → conflict。R2、S3、MinIO 都支持。
- delete 先 head 再删（S3 的 DELETE 不说删之前在不在）；list 用 ListObjectsV2，再逐个 head 补全元数据。
- temporary_url 是 SigV4 预签名地址（最长 7 天）；经它上传的对象没有 sha256 元数据，head 时 sha256 为空串，get 时按内容算。
- list 的 limit 超过 1000（S3 一页的上限）时按 1000 取。
- set_lifecycle 改写桶的生命周期配置里本作用域的规则（规则 ID 是 ar-<前缀与作用域的哈希>-<序号>，别的作用域的规则不动）；
  S3 的过期只按整天算，不足一天按一天。清理由存储服务每天做，不能立即触发。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal

from agentruntime_core.adapters import sigv4
from agentruntime_core.clock import Clock, SystemClock
from agentruntime_core.dev._common import scope_id
from agentruntime_core.dev.object_store import check_key, key_of_uri
from agentruntime_core.errors import ConflictError, DeniedError, InvalidError, NotFoundError, ProviderError, TransientError
from agentruntime_core.providers.object_store import LifecycleRule, ObjectData, ObjectInfo, ObjectPage
from agentruntime_core.secrets import Secret
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope
from agentruntime_core.testing import FaultInjector

META_SHA = "x-amz-meta-ar-sha256"
META_USER = "x-amz-meta-ar-metadata"
_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
MAX_PRESIGN = 7 * 24 * 3600

# 进程配置（task-service、Worker 相同，Go 侧见 s3objects.OptionsFromEnv）：设了 AR_S3_ENDPOINT 就用这个实现，
# 不再用 AR_OBJECT_STORE_DIR。密钥给 AR_S3_SECRET_ACCESS_KEY，或者 AR_S3_SECRET_ACCESS_KEY_FILE（K8s Secret 挂成的文件）。
ENV_ENDPOINT = "AR_S3_ENDPOINT"
ENV_BUCKET = "AR_S3_BUCKET"
ENV_REGION = "AR_S3_REGION"
ENV_PREFIX = "AR_S3_PREFIX"
ENV_ACCESS_KEY_ID = "AR_S3_ACCESS_KEY_ID"
ENV_SECRET_ACCESS_KEY = "AR_S3_SECRET_ACCESS_KEY"
ENV_SECRET_ACCESS_KEY_FILE = "AR_S3_SECRET_ACCESS_KEY_FILE"


@dataclass(frozen=True)
class S3Settings:
    endpoint: str
    bucket: str
    access_key_id: str
    # 不参与比较与哈希：Secret 不可哈希，而 Worker 拿配置当缓存的键。
    secret_access_key: Secret = field(compare=False)
    region: str = "auto"
    prefix: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> S3Settings | None:
        """没设 AR_S3_ENDPOINT 时返回 None；缺别的项时抛 ValueError（只提变量名，不带取值）。"""
        endpoint = env.get(ENV_ENDPOINT, "").strip()
        if not endpoint:
            return None
        secret: str = env.get(ENV_SECRET_ACCESS_KEY, "")
        if not secret and env.get(ENV_SECRET_ACCESS_KEY_FILE, "").strip():
            try:
                secret = Path(env[ENV_SECRET_ACCESS_KEY_FILE].strip()).read_text("utf-8").strip()
            except OSError:
                raise ValueError(f"读不了 {ENV_SECRET_ACCESS_KEY_FILE} 指向的文件") from None
        missing = [n for n, v in ((ENV_BUCKET, env.get(ENV_BUCKET, "")), (ENV_ACCESS_KEY_ID, env.get(ENV_ACCESS_KEY_ID, "")), (ENV_SECRET_ACCESS_KEY, secret)) if not v.strip()]
        if missing:
            raise ValueError(f"设了 {ENV_ENDPOINT} 还要设 {'、'.join(missing)}")
        return cls(
            endpoint=endpoint, bucket=env[ENV_BUCKET].strip(), access_key_id=env[ENV_ACCESS_KEY_ID].strip(),
            secret_access_key=Secret(secret), region=env.get(ENV_REGION, "").strip() or "auto", prefix=env.get(ENV_PREFIX, "").strip(),
        )

    def provider(self, *, clock: Clock | None = None) -> S3ObjectStoreProvider:
        return S3ObjectStoreProvider(
            endpoint=self.endpoint, bucket=self.bucket, access_key_id=self.access_key_id, secret_access_key=self.secret_access_key,
            region=self.region, prefix=self.prefix, clock=clock,
        )


class S3ObjectStoreProvider:
    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: Secret | str,
        region: str = "auto",
        prefix: str = "",
        clock: Clock | None = None,
        faults: FaultInjector | None = None,
        timeout_s: float = 60,
    ) -> None:
        if not endpoint.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError("S3 的地址要是 https（本机测试除外）")
        if prefix and (not prefix.endswith("/") or prefix.startswith("/") or "\\" in prefix):
            raise ValueError("前缀要以 / 结尾，不得以 / 开头，不得含反斜杠")
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.prefix = prefix
        self._access_key = access_key_id
        self._secret = secret_access_key if isinstance(secret_access_key, Secret) else Secret(secret_access_key)
        self.region = region
        self._clock = clock or SystemClock()
        self._faults = faults or FaultInjector()
        self._timeout = timeout_s

    def bind(self, tenant: TenantContext) -> S3ObjectStore:
        return S3ObjectStore(self, tenant_scope(tenant))

    def bind_platform(self, purpose: str) -> S3ObjectStore:
        return S3ObjectStore(self, platform_scope(purpose))

    async def aclose(self) -> None:
        return None

    # --- HTTP ---

    def url(self, name: str = "", query: str = "") -> str:
        path = f"/{self.bucket}" + (f"/{sigv4.quote(name, path=True)}" if name else "")
        return f"{self.endpoint}{path}" + (f"?{query}" if query else "")

    def _now(self) -> datetime:
        return self._clock.now().astimezone(UTC)

    async def request(self, method: str, url: str, body: bytes = b"", headers: Mapping[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        headers = dict(headers or {})
        payload_hash = hashlib.sha256(body).hexdigest()
        secret = self._secret.reveal()
        signed = sigv4.sign(
            method, url, headers, payload_hash=payload_hash, access_key=self._access_key,
            secret=secret if isinstance(secret, str) else secret.decode(), region=self.region, now=self._now(),
        )
        request = urllib.request.Request(url, data=body if method in ("PUT", "POST") else None, method=method)
        for k, v in {**headers, **signed}.items():
            request.add_header(k, v)

        def send() -> tuple[int, dict[str, str], bytes]:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(request, timeout=self._timeout) as resp:
                    return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
            except urllib.error.HTTPError as err:
                return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
                raise TransientError("object_store_unavailable", f"连不上对象存储（{type(err).__name__}）") from None

        return await asyncio.to_thread(send)


def _error(status: int, body: bytes, *, what: str) -> ProviderError:
    code = ""
    try:
        code = ET.fromstring(body).findtext("Code") or ""
    except ET.ParseError:
        pass
    if status == 404:
        return NotFoundError("not_found", f"{what}不存在")
    if status == 412:
        return ConflictError("already_exists", "键已存在")
    if status in (401, 403):
        return DeniedError("object_store_denied", f"对象存储拒绝了请求（{code or status}）")
    if status in (400, 405, 411, 413):
        return InvalidError("object_store_invalid", f"对象存储认为请求不对（{code or status}）")
    return TransientError("object_store_unavailable", f"对象存储暂时不可用（{code or status}）")


class S3ObjectStore:
    def __init__(self, provider: S3ObjectStoreProvider, scope: Scope) -> None:
        self._p = provider
        self._owner = scope_id(scope)
        self._base = f"{provider.prefix}{self._owner}/"

    def _name(self, key: str) -> str:
        return self._base + check_key(key)

    def uri(self, key: str) -> str:
        return f"obj://{self._owner}/{check_key(key)}"

    def key_of(self, uri: str) -> str:
        return key_of_uri(uri, self._owner)

    def _info(self, key: str, headers: Mapping[str, str], data: bytes | None = None) -> ObjectInfo:
        sha = headers.get(META_SHA) or (hashlib.sha256(data).hexdigest() if data is not None else "")
        metadata: dict[str, str] = {}
        if headers.get(META_USER):
            try:
                metadata = json.loads(base64.b64decode(headers[META_USER]))
            except (ValueError, TypeError):
                metadata = {}
        try:
            created = parsedate_to_datetime(headers["last-modified"]).astimezone(UTC)
        except (KeyError, TypeError, ValueError):
            created = self._p._now()
        size = int(headers.get("content-length") or (len(data) if data is not None else 0))
        return ObjectInfo(
            key=key, uri=self.uri(key), size=size, sha256=sha, content_type=headers.get("content-type") or "application/octet-stream",
            created_at=created, metadata=metadata,
        )

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: Mapping[str, str] | None = None,
        if_absent: bool = False,
    ) -> ObjectInfo:
        self._p._faults.check("put")
        name = self._name(key)
        sha = hashlib.sha256(data).hexdigest()
        headers = {"Content-Type": content_type or "application/octet-stream", META_SHA: sha}
        if metadata:
            headers[META_USER] = base64.b64encode(json.dumps(dict(metadata), ensure_ascii=False).encode()).decode()
        if if_absent:
            headers["If-None-Match"] = "*"
        status, _, body = await self._p.request("PUT", self._p.url(name), data, headers)
        if status not in (200, 201):
            raise _error(status, body, what="对象")
        return ObjectInfo(
            key=key, uri=self.uri(key), size=len(data), sha256=sha, content_type=headers["Content-Type"],
            created_at=self._p._now(), metadata=dict(metadata or {}),
        )

    async def get(self, key: str) -> ObjectData:
        self._p._faults.check("get")
        status, headers, body = await self._p.request("GET", self._p.url(self._name(key)))
        if status != 200:
            raise _error(status, body, what="对象")
        return ObjectData(info=self._info(key, headers, body), data=body)

    async def head(self, key: str) -> ObjectInfo:
        self._p._faults.check("head")
        status, headers, body = await self._p.request("HEAD", self._p.url(self._name(key)))
        if status != 200:
            raise _error(status, body, what="对象")
        return self._info(key, headers)

    async def delete(self, key: str) -> bool:
        self._p._faults.check("delete")
        name = self._name(key)
        try:
            await self.head(key)
        except NotFoundError:
            return False
        status, _, body = await self._p.request("DELETE", self._p.url(name))
        if status not in (200, 204, 404):
            raise _error(status, body, what="对象")
        return True

    async def list(self, *, prefix: str = "", limit: int = 1000, cursor: str | None = None) -> ObjectPage:
        self._p._faults.check("list")
        if limit < 1:
            raise InvalidError("invalid_argument", "limit 至少为 1")
        if prefix and ("\\" in prefix or prefix.startswith("/")):
            raise InvalidError("invalid_key", "前缀不得以 / 开头，不得含反斜杠")
        params = {"list-type": "2", "prefix": self._base + prefix, "max-keys": str(min(limit, 1000))}
        if cursor:
            params["continuation-token"] = cursor
        query = "&".join(f"{sigv4.quote(k)}={sigv4.quote(v)}" for k, v in sorted(params.items()))
        status, _, body = await self._p.request("GET", self._p.url(query=query))
        if status != 200:
            raise _error(status, body, what="桶")
        root = ET.fromstring(body)
        names = [el.findtext(f"{_NS}Key") or el.findtext("Key") or "" for el in [*root.iter(f"{_NS}Contents"), *root.iter("Contents")]]
        keys = [n[len(self._base):] for n in names if n.startswith(self._base)]
        truncated = (root.findtext(f"{_NS}IsTruncated") or root.findtext("IsTruncated") or "").lower() == "true"
        token = root.findtext(f"{_NS}NextContinuationToken") or root.findtext("NextContinuationToken")
        infos = await asyncio.gather(*(self._head_or_none(k) for k in keys))
        return ObjectPage(items=tuple(i for i in infos if i is not None), next_cursor=token if truncated and token else None)

    async def _head_or_none(self, key: str) -> ObjectInfo | None:
        try:
            return await self.head(key)
        except NotFoundError:
            return None  # 列出之后被删掉了

    async def temporary_url(self, key: str, *, ttl: timedelta, method: Literal["GET", "PUT"] = "GET") -> str:
        self._p._faults.check("temporary_url")
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
            raise InvalidError("invalid_argument", "ttl 必须大于 0")
        if method not in ("GET", "PUT"):
            raise InvalidError("invalid_argument", "method 只能是 GET 或 PUT")
        seconds = min(MAX_PRESIGN, max(1, math.ceil(ttl.total_seconds())))
        secret = self._p._secret.reveal()
        return sigv4.presign(
            method, self._p.url(self._name(key)), expires=seconds, access_key=self._p._access_key,
            secret=secret if isinstance(secret, str) else secret.decode(), region=self._p.region, now=self._p._now(),
        )

    async def set_lifecycle(self, rules: Sequence[LifecycleRule]) -> None:
        self._p._faults.check("set_lifecycle")
        for rule in rules:
            if rule.expire_after <= timedelta(0):
                raise InvalidError("invalid_argument", "expire_after 必须大于 0")
            if rule.prefix and ("\\" in rule.prefix or rule.prefix.startswith("/")):
                raise InvalidError("invalid_key", "前缀不得以 / 开头，不得含反斜杠")
        url = self._p.url(query="lifecycle=")
        mine = rule_id_prefix(self._base)
        status, _, body = await self._p.request("GET", url)
        existing: list[ET.Element] = []
        if status == 200:
            root = ET.fromstring(body)
            for rule in [*root.iter(f"{_NS}Rule"), *root.iter("Rule")]:
                rule_id = rule.findtext(f"{_NS}ID") or rule.findtext("ID") or ""
                if not rule_id.startswith(mine):
                    existing.append(rule)
        elif status != 404:
            raise _error(status, body, what="桶的生命周期配置")
        config = ET.Element("LifecycleConfiguration", xmlns=_NS[1:-1])
        for rule in existing:
            config.append(_strip_ns(rule))
        for i, rule in enumerate(rules):
            el = ET.SubElement(config, "Rule")
            ET.SubElement(el, "ID").text = f"{mine}{i}"
            ET.SubElement(ET.SubElement(el, "Filter"), "Prefix").text = self._base + rule.prefix
            ET.SubElement(el, "Status").text = "Enabled"
            days = max(1, math.ceil(rule.expire_after.total_seconds() / 86400))
            ET.SubElement(ET.SubElement(el, "Expiration"), "Days").text = str(days)
        data = ET.tostring(config, xml_declaration=True, encoding="utf-8")
        # S3 要求生命周期配置带 Content-MD5（只是完整性校验，不是安全用途）。
        headers = {"Content-Type": "application/xml", "Content-MD5": base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()}
        if not list(config):
            status, _, body = await self._p.request("DELETE", url)
            if status not in (200, 204, 404):
                raise _error(status, body, what="桶的生命周期配置")
            return
        status, _, body = await self._p.request("PUT", url, data, headers)
        if status not in (200, 204):
            raise _error(status, body, what="桶的生命周期配置")


def rule_id_prefix(base: str) -> str:
    """本作用域的生命周期规则 ID 前缀。用哈希而不是作用域本身：租户 t-a 的前缀不能是 t-a-b 的前缀。Go 侧算法相同。"""
    return f"ar-{hashlib.sha256(base.encode()).hexdigest()[:24]}-"


def _strip_ns(el: ET.Element) -> ET.Element:
    out = ET.Element(el.tag.replace(_NS, ""), el.attrib)
    out.text, out.tail = el.text, None
    for child in el:
        out.append(_strip_ns(child))
    return out
