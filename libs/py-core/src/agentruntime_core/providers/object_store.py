"""ObjectStore（设计书 4.7）：按租户前缀读写对象、临时访问地址、生命周期策略。

两种语言共用：Go 侧见 libs/go-core/providers/objectstore，方法一一对应，语义一致。

语义：
- 键：1 到 1024 个字符，用 / 分段；不得以 / 开头，不得有空段、. 或 .. 段、反斜杠与控制字符，否则 invalid。
- 对象的引用是 obj://<tenant_id>/<键>；平台作用域为 obj://_platform/<键>。key_of 解析引用，
  引用属于别的作用域时报 denied。
- put 同一个键、同样内容可以重复执行，结果相同；if_absent=True 且键已存在时报 conflict。
- get、head 不存在的键：not_found。delete 返回删除前是否存在，重复删除不报错。
- temporary_url 生成限时地址，过期后不可用。
- signed_upload 生成只能写一次、只能写指定内容的上传地址（STOR-SEC，docs/plan/workspace-read-latency-and-disk-2026-11-03.md
  第 8.2 节）：用 PUT，原样带上返回的 headers；内容的 sha256 与长度必须与签的一致，对象已存在时拒绝（不覆盖）。地址交给不受信任
  的一方（沙箱里的 agent，也就是用户代码）也无害：写不了别的键、别的内容，也写不了第二次。sha256 是 64 位小写十六进制，size 不能为负，
  ttl 在 0 与 MAX_SIGNED_UPLOAD_TTL 之间，否则 invalid。
- set_lifecycle 替换本作用域的生命周期规则：前缀下创建超过 expire_after 的对象被清理。
  对象的生命周期必须长于 Temporal 的 retention（计划 S-06），由部署配置保证。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol

from agentruntime_core.errors import InvalidError
from agentruntime_core.providers.base import Bindable

# signed_upload 的地址最长有效多久：按块分批签，每批几分钟（第 8.2 节“有效期几分钟”）。
MAX_SIGNED_UPLOAD_TTL = timedelta(minutes=15)
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class SignedUpload:
    """signed_upload 的结果：用 PUT 上传，原样带上 headers（它们都在签名里，少一个、改一个都被拒）。"""

    url: str
    headers: Mapping[str, str]


def check_signed_upload(sha256: str, size: int, ttl: timedelta) -> None:
    """signed_upload 的参数校验，各实现共用。"""
    if not isinstance(sha256, str) or not _SHA256_HEX.fullmatch(sha256):
        raise InvalidError("invalid_argument", "sha256 要是 64 位小写十六进制")
    if not isinstance(size, int) or size < 0:
        raise InvalidError("invalid_argument", "size 不能为负")
    if not isinstance(ttl, timedelta) or ttl <= timedelta(0) or ttl > MAX_SIGNED_UPLOAD_TTL:
        raise InvalidError("invalid_argument", f"ttl 要大于 0、不超过 {int(MAX_SIGNED_UPLOAD_TTL.total_seconds())} 秒")


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    uri: str
    size: int
    sha256: str
    content_type: str
    created_at: datetime
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ObjectData:
    info: ObjectInfo
    data: bytes


@dataclass(frozen=True)
class ObjectPage:
    items: tuple[ObjectInfo, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class LifecycleRule:
    prefix: str
    expire_after: timedelta


class ObjectStore(Protocol):
    def uri(self, key: str) -> str: ...

    def key_of(self, uri: str) -> str: ...

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: Mapping[str, str] | None = None,
        if_absent: bool = False,
    ) -> ObjectInfo: ...

    async def get(self, key: str) -> ObjectData: ...

    async def head(self, key: str) -> ObjectInfo: ...

    async def delete(self, key: str) -> bool: ...

    async def list(self, *, prefix: str = "", limit: int = 1000, cursor: str | None = None) -> ObjectPage: ...

    async def temporary_url(self, key: str, *, ttl: timedelta, method: Literal["GET", "PUT"] = "GET") -> str: ...

    async def signed_upload(self, key: str, *, ttl: timedelta, sha256: str, size: int) -> SignedUpload: ...

    async def set_lifecycle(self, rules: Sequence[LifecycleRule]) -> None: ...


class ObjectStoreProvider(Bindable[ObjectStore], Protocol):
    pass
