"""ObjectStore（设计书 4.7）：按租户前缀读写对象、临时访问地址、生命周期策略。

两种语言共用：Go 侧见 libs/go-core/providers/objectstore，方法一一对应，语义一致。

语义：
- 键：1 到 1024 个字符，用 / 分段；不得以 / 开头，不得有空段、. 或 .. 段、反斜杠与控制字符，否则 invalid。
- 对象的引用是 obj://<tenant_id>/<键>；平台作用域为 obj://_platform/<键>。key_of 解析引用，
  引用属于别的作用域时报 denied。
- put 同一个键、同样内容可以重复执行，结果相同；if_absent=True 且键已存在时报 conflict。
- get、head 不存在的键：not_found。delete 返回删除前是否存在，重复删除不报错。
- temporary_url 生成限时地址，过期后不可用。
- set_lifecycle 替换本作用域的生命周期规则：前缀下创建超过 expire_after 的对象被清理。
  对象的生命周期必须长于 Temporal 的 retention（计划 S-06），由部署配置保证。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol

from agentruntime_core.providers.base import Bindable


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

    async def set_lifecycle(self, rules: Sequence[LifecycleRule]) -> None: ...


class ObjectStoreProvider(Bindable[ObjectStore], Protocol):
    pass
