"""Cache（设计书 4.5）：带过期的键值、计数与限流、分布式锁、推流。

两种语言共用：Go 侧见 libs/go-core/providers/cache，方法一一对应，语义一致。

只存可丢失或可重建的数据；不能丢的状态放在 DurableEngine 或 RelationalStore。

语义：
- 键、锁名、流名：1 到 512 个字符，不含空白与控制字符，否则 invalid。每个作用域各有一套命名空间。
- ttl、window、retention 必须大于 0，否则 invalid。所有写入都必须带过期时间。
- incr：键不存在时从 0 开始，并在创建时设 ttl；之后的 incr 不改 ttl。
- rate_limit：滑动窗口，任意 window 时长内累计 cost 不超过 limit。被拒绝的请求不计入。
- 锁：同一作用域内同名锁同一时刻只有一个持有者；token 随机。过期后别人可以拿到；
  持有者用过期的 token 续期或释放都失败（返回 None 或 False），不会影响新的持有者。
- 推流：同一个流里 append_event 返回的 id 从 1 开始严格递增，供 SSE 的 Last-Event-ID 续传。
  超过 retention 的事件被清理；read_events 若发现 after 之后有事件已被清理，返回 gap=True。
  subscribe 先补发 id 大于 after 的已有事件，再推送新事件。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from agentruntime_core.providers.base import Bindable


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    # 本窗口内还剩多少额度。
    remaining: int
    # 被拒绝时，最早什么时候可能放行；放行时为 0。
    retry_after: timedelta


@dataclass(frozen=True)
class Lock:
    name: str
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class StreamEvent:
    id: int
    data: bytes


@dataclass(frozen=True)
class EventPage:
    events: tuple[StreamEvent, ...]
    # after 之后有事件已过保留期被清理，客户端漏掉了事件。
    gap: bool


class Cache(Protocol):
    async def get(self, key: str) -> bytes | None: ...

    async def set(self, key: str, value: bytes, *, ttl: timedelta) -> None: ...

    async def set_if_absent(self, key: str, value: bytes, *, ttl: timedelta) -> bool:
        """键不存在时写入并返回 True；已存在时不改值，返回 False。"""
        ...

    async def delete(self, key: str) -> bool:
        """返回删除前键是否存在。"""
        ...

    async def incr(self, key: str, *, ttl: timedelta, by: int = 1) -> int: ...

    async def rate_limit(self, key: str, *, limit: int, window: timedelta, cost: int = 1) -> RateDecision: ...

    async def acquire_lock(self, name: str, *, ttl: timedelta) -> Lock | None:
        """拿到锁返回 Lock；已被别人持有返回 None。"""
        ...

    async def extend_lock(self, lock: Lock, *, ttl: timedelta) -> Lock | None:
        """续期成功返回新的 Lock；锁已过期或易主返回 None。"""
        ...

    async def release_lock(self, lock: Lock) -> bool:
        """释放成功返回 True；锁已过期或易主返回 False。"""
        ...

    async def append_event(self, stream: str, data: bytes, *, retention: timedelta) -> int: ...

    async def read_events(self, stream: str, *, after: int, limit: int = 100) -> EventPage: ...

    def subscribe(self, stream: str, *, after: int) -> AsyncIterator[StreamEvent]: ...


class CacheProvider(Bindable[Cache], Protocol):
    pass
