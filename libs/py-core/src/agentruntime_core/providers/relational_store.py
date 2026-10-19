"""RelationalStore（设计书 4.3）：事务读写、按租户分区的表、行级安全。

两种语言共用：Go 侧见 libs/go-core/providers/relational，方法一一对应，语义一致。

语义：
- 租户隔离由存储负责，应用 SQL 不写租户条件，也不出现租户列。开发实现（SQLite）每个作用域一个库；
  生产实现（Postgres）用行级安全，事务开始时设置租户会话变量、连接归还时清除（S-12a）。
- SQL 用可移植子集：参数占位符写 $1、$2…；列类型只用 TEXT、BIGINT、DOUBLE PRECISION，取回的值为
  str、int、float 或 None。布尔、JSON、时间由调用方编码（0/1、JSON 文本、ISO 8601 UTC 文本）。
- 事务：上下文管理器正常退出时提交；有异常抛出时回滚，异常原样抛出。read_only 事务里写入：invalid。
- 错误分类：唯一约束冲突 conflict；语法错误、表或列不存在、query_one 得到多行 invalid；
  死锁、序列化失败、连接中断 transient。
- migrate 对所有作用域生效（已有的与以后绑定的），按 version 递增执行，已执行过的版本跳过。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from agentruntime_core.providers.base import Bindable

Row = Mapping[str, Any]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


class Transaction(Protocol):
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """返回受影响的行数。"""
        ...

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]: ...

    async def query_one(self, sql: str, params: Sequence[Any] = ()) -> Row | None: ...


class RelationalStore(Protocol):
    def transaction(self, *, read_only: bool = False) -> AbstractAsyncContextManager[Transaction]: ...


class RelationalStoreProvider(Bindable[RelationalStore], Protocol):
    async def migrate(self, migrations: Sequence[Migration]) -> None: ...
