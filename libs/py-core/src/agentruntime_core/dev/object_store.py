"""本地磁盘 ObjectStore（开发实现）。语义见 agentruntime_core.providers.object_store。

布局：<root>/<作用域>/data/<键的 sha256>，元数据在同名的 .json 里。用键的哈希作文件名，避开 Windows 不区分大小写
与保留文件名的问题。临时地址是本地签名的 devobj:// 地址，只能用 fetch_url、upload_url 访问。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from agentruntime_core.clock import Clock, SystemClock
from agentruntime_core.dev._common import PLATFORM_ID, check_positive, scope_id
from agentruntime_core.errors import ConflictError, DeniedError, InvalidError, NotFoundError
from agentruntime_core.providers.object_store import LifecycleRule, ObjectData, ObjectInfo, ObjectPage
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope
from agentruntime_core.testing import FaultInjector

_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_URI = re.compile(r"obj://([^/]+)/(.+)", re.DOTALL)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def check_key(key: str) -> str:
    if not isinstance(key, str) or not 1 <= len(key) <= 1024:
        raise InvalidError("invalid_key", "键长度必须在 1 到 1024 之间")
    if key.startswith("/") or "\\" in key or _CONTROL.search(key):
        raise InvalidError("invalid_key", "键不得以 / 开头，不得含反斜杠或控制字符")
    if any(segment in ("", ".", "..") for segment in key.split("/")):
        raise InvalidError("invalid_key", "键不得有空段、. 或 .. 段")
    return key


def key_of_uri(uri: str, owner: str) -> str:
    """解析 obj://<作用域>/<键>：格式不对 invalid，属于别的作用域 denied。各实现共用。"""
    match = _URI.fullmatch(uri) if isinstance(uri, str) else None
    if match is None:
        raise InvalidError("invalid_uri", "引用必须形如 obj://<租户>/<键>")
    scope, key = match.groups()
    if scope != PLATFORM_ID and not _SLUG.fullmatch(scope):
        raise InvalidError("invalid_uri", "引用里的作用域格式不对")
    check_key(key)
    if scope != owner:
        raise DeniedError("cross_scope_reference", "引用属于别的作用域")
    return key


class LocalDiskObjectStoreProvider:
    def __init__(
        self,
        root: Path,
        *,
        signing_key: bytes,
        clock: Clock | None = None,
        faults: FaultInjector | None = None,
    ) -> None:
        self._root = Path(root)
        self._signing_key = signing_key
        self._clock = clock or SystemClock()
        self._faults = faults or FaultInjector()
        self._rules: dict[str, list[LifecycleRule]] = {}

    def bind(self, tenant: TenantContext) -> LocalDiskObjectStore:
        return LocalDiskObjectStore(self, tenant_scope(tenant))

    def bind_platform(self, purpose: str) -> LocalDiskObjectStore:
        return LocalDiskObjectStore(self, platform_scope(purpose))

    async def aclose(self) -> None:
        return None

    # --- 开发专用：临时地址与生命周期 ---

    def _sign(self, owner: str, key: str, method: str, expires: int) -> str:
        message = f"{owner}\n{key}\n{method}\n{expires}".encode()
        return hmac.new(self._signing_key, message, hashlib.sha256).hexdigest()

    def _url(self, owner: str, key: str, method: str, expires: int) -> str:
        encoded = base64.urlsafe_b64encode(key.encode()).decode()
        return f"devobj://{owner}/{encoded}?method={method}&expires={expires}&sig={self._sign(owner, key, method, expires)}"

    def _verify(self, url: str, method: str) -> tuple[str, str]:
        try:
            parts = urlsplit(url)
            query = {k: v[0] for k, v in parse_qs(parts.query).items()}
            owner = parts.netloc
            key = base64.urlsafe_b64decode(parts.path.lstrip("/").encode()).decode()
            expires = int(query["expires"])
            signature = query["sig"]
            signed_method = query["method"]
        except (KeyError, ValueError, UnicodeDecodeError) as error:
            raise DeniedError("invalid_url", "临时地址无效") from error
        if parts.scheme != "devobj" or signed_method != method:
            raise DeniedError("invalid_url", "临时地址无效")
        if not hmac.compare_digest(signature, self._sign(owner, key, method, expires)):
            raise DeniedError("invalid_url", "临时地址签名不对")
        if expires <= int(self._clock.now().timestamp()):
            raise DeniedError("expired_url", "临时地址已过期")
        return owner, key

    async def fetch_url(self, url: str) -> bytes:
        owner, key = self._verify(url, "GET")
        return _Files(self, owner).read(key).data

    async def upload_url(self, url: str, data: bytes) -> None:
        owner, key = self._verify(url, "PUT")
        _Files(self, owner).write(key, data, "application/octet-stream", {}, if_absent=False)

    async def run_lifecycle(self) -> None:
        if not self._root.exists():
            return
        for directory in self._root.iterdir():
            if directory.is_dir():
                _Files(self, directory.name).expire()


class _Files:
    """一个作用域下的对象文件。"""

    def __init__(self, provider: LocalDiskObjectStoreProvider, owner: str) -> None:
        self._provider = provider
        self._owner = owner
        self._dir = provider._root / owner / "data"

    def _paths(self, key: str) -> tuple[Path, Path]:
        name = hashlib.sha256(key.encode()).hexdigest()
        return self._dir / name, self._dir / f"{name}.json"

    def _info(self, meta: Mapping[str, object]) -> ObjectInfo:
        return ObjectInfo(
            key=str(meta["key"]),
            uri=f"obj://{self._owner}/{meta['key']}",
            size=int(meta["size"]),  # type: ignore[arg-type]
            sha256=str(meta["sha256"]),
            content_type=str(meta["content_type"]),
            created_at=datetime.fromisoformat(str(meta["created_at"])),
            metadata=dict(meta["metadata"]),  # type: ignore[arg-type]
        )

    def _expired(self, info: ObjectInfo) -> bool:
        now = self._provider._clock.now()
        for rule in self._provider._rules.get(self._owner, []):
            if info.key.startswith(rule.prefix) and info.created_at + rule.expire_after <= now:
                return True
        return False

    def _load(self, key: str) -> ObjectInfo | None:
        _, meta_path = self._paths(key)
        if not meta_path.exists():
            return None
        info = self._info(json.loads(meta_path.read_text("utf-8")))
        if self._expired(info):
            self.remove(key)
            return None
        return info

    def head(self, key: str) -> ObjectInfo:
        info = self._load(key)
        if info is None:
            raise NotFoundError("not_found", "对象不存在")
        return info

    def read(self, key: str) -> ObjectData:
        info = self.head(key)
        data_path, _ = self._paths(key)
        return ObjectData(info=info, data=data_path.read_bytes())

    def write(self, key: str, data: bytes, content_type: str, metadata: Mapping[str, str], *, if_absent: bool) -> ObjectInfo:
        if if_absent and self._load(key) is not None:
            raise ConflictError("already_exists", "对象已存在")
        self._dir.mkdir(parents=True, exist_ok=True)
        data_path, meta_path = self._paths(key)
        meta = {
            "key": key,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "content_type": content_type,
            "created_at": self._provider._clock.now().astimezone(UTC).isoformat(),
            "metadata": dict(metadata),
        }
        _atomic_write(data_path, bytes(data))
        _atomic_write(meta_path, json.dumps(meta, ensure_ascii=False).encode())
        return self._info(meta)

    def remove(self, key: str) -> bool:
        data_path, meta_path = self._paths(key)
        existed = meta_path.exists()
        for path in (meta_path, data_path):
            path.unlink(missing_ok=True)
        return existed

    def all(self) -> list[ObjectInfo]:
        if not self._dir.exists():
            return []
        infos = []
        for meta_path in self._dir.glob("*.json"):
            info = self._load(json.loads(meta_path.read_text("utf-8"))["key"])
            if info is not None:
                infos.append(info)
        return sorted(infos, key=lambda i: i.key)

    def expire(self) -> None:
        self.all()  # _load 会顺带清理过期对象


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class LocalDiskObjectStore:
    def __init__(self, provider: LocalDiskObjectStoreProvider, scope: Scope) -> None:
        self._provider = provider
        self._owner = scope_id(scope)
        self._files = _Files(provider, self._owner)

    def _enter(self, operation: str) -> None:
        self._provider._faults.check(operation)

    def uri(self, key: str) -> str:
        return f"obj://{self._owner}/{check_key(key)}"

    def key_of(self, uri: str) -> str:
        return key_of_uri(uri, self._owner)

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: Mapping[str, str] | None = None,
        if_absent: bool = False,
    ) -> ObjectInfo:
        self._enter("put")
        check_key(key)
        return self._files.write(key, data, content_type, metadata or {}, if_absent=if_absent)

    async def get(self, key: str) -> ObjectData:
        self._enter("get")
        return self._files.read(check_key(key))

    async def head(self, key: str) -> ObjectInfo:
        self._enter("head")
        return self._files.head(check_key(key))

    async def delete(self, key: str) -> bool:
        self._enter("delete")
        check_key(key)
        return self._files._load(key) is not None and self._files.remove(key)

    async def list(self, *, prefix: str = "", limit: int = 1000, cursor: str | None = None) -> ObjectPage:
        self._enter("list")
        if limit < 1:
            raise InvalidError("invalid_argument", "limit 至少为 1")
        after = base64.urlsafe_b64decode(cursor.encode()).decode() if cursor else None
        items = [i for i in self._files.all() if i.key.startswith(prefix) and (after is None or i.key > after)]
        page = items[:limit]
        more = len(items) > limit
        next_cursor = base64.urlsafe_b64encode(page[-1].key.encode()).decode() if more else None
        return ObjectPage(items=tuple(page), next_cursor=next_cursor)

    async def temporary_url(self, key: str, *, ttl: timedelta, method: Literal["GET", "PUT"] = "GET") -> str:
        self._enter("temporary_url")
        check_key(key)
        check_positive(ttl, "ttl")
        if method not in ("GET", "PUT"):
            raise InvalidError("invalid_argument", "method 只能是 GET 或 PUT")
        expires = int((self._provider._clock.now() + ttl).timestamp())
        return self._provider._url(self._owner, key, method, expires)

    async def set_lifecycle(self, rules: Sequence[LifecycleRule]) -> None:
        self._enter("set_lifecycle")
        for rule in rules:
            check_positive(rule.expire_after, "expire_after")
        self._provider._rules[self._owner] = list(rules)
