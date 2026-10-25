"""端到端测试的业务库隔离：每套进程一个业务库，SQLite 与 Postgres 下都一样。

SQLite（默认）下每套进程的 task-service 有自己的 AR_DATA_DIR，业务库天然分开。夜间任务的 Postgres 步骤设了
AR_RELATIONAL_DSN，子进程都继承同一个连接串：会话级的 Stack、故障注入套件的 Stack、dev_stack.py 拉起的整套、单独跑
registry-seed 的用例连的是同一个库，Registry 这类平台作用域的数据互相看得见（2026-10-24 夜间任务：dev_stack 的开发种子
出现在 test_registry 的固定版本里）。

所以设了 AR_RELATIONAL_DSN 时，每套进程第一次要业务库时建一个自己的 schema，连接串加上 options=-csearch_path=<schema>
（libpq 与 pgx 都认）：Postgres 适配器的迁移与查询都在 current_schema() 里，行级安全不受影响。schema 用 psql 建与删
（PATH 上，或 AR_POSTGRES_BIN 指定的目录里，与 Go 侧 Postgres 测试的约定相同）。没设 AR_RELATIONAL_DSN 时什么都不做。
报错里不带连接串（可能含口令）。
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ENV_DSN = "AR_RELATIONAL_DSN"
ENV_POSTGRES_BIN = "AR_POSTGRES_BIN"
SCHEMA_PREFIX = "e2e_"
_SCHEMA_NAME = re.compile(r"[a-z_][a-z0-9_]*")


def with_search_path(dsn: str, schema: str) -> str:
    """在连接串（URL 或 key=value 写法）上加 options=-csearch_path=<schema>。已经带 options 的连接串不支持。"""
    if not _SCHEMA_NAME.fullmatch(schema):
        raise ValueError(f"schema 名不合法：{schema!r}")
    option = f"-csearch_path={schema}"
    if "://" in dsn:
        parts = urlsplit(dsn)
        query = parse_qsl(parts.query, keep_blank_values=True)
        if any(key == "options" for key, _ in query):
            raise ValueError("AR_RELATIONAL_DSN 里已经有 options，不能再加 search_path")
        return urlunsplit(parts._replace(query=urlencode([*query, ("options", option)])))
    if re.search(r"(^|\s)options\s*=", dsn):
        raise ValueError("AR_RELATIONAL_DSN 里已经有 options，不能再加 search_path")
    return f"{dsn} options='{option}'"


def psql(dsn: str, sql: str) -> str:
    """执行 sql，返回不对齐、不带表头的输出。"""
    bin_dir = os.environ.get(ENV_POSTGRES_BIN)
    exe = shutil.which("psql", path=bin_dir) if bin_dir else shutil.which("psql")
    if exe is None:
        raise RuntimeError(f"找不到 psql（PATH 上或 {ENV_POSTGRES_BIN} 指定的目录里）：业务库换成 Postgres 时要用它给每套进程建 schema")
    proc = subprocess.run(
        [exe, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", dsn, "-c", sql],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql 执行失败（{sql}）：\n{proc.stderr.replace(dsn, '<AR_RELATIONAL_DSN>')}")
    return proc.stdout


class BusinessDb:
    """一套进程的业务库。env() 是给这套进程的环境变量：Postgres 时是指向自己 schema 的连接串，SQLite 时为空。"""

    def __init__(self) -> None:
        self._base = os.environ.get(ENV_DSN, "")
        self._schema = ""
        self._dropped = False

    @property
    def schema(self) -> str:
        return self._schema

    def env(self) -> dict[str, str]:
        if not self._base:
            return {}
        if not self._schema:
            schema = SCHEMA_PREFIX + secrets.token_hex(6)
            psql(self._base, f"CREATE SCHEMA {schema}")
            self._schema = schema
        return {ENV_DSN: with_search_path(self._base, self._schema)}

    def drop(self) -> None:
        """删掉这套进程的 schema（进程都停了之后）。尽力而为：夜间任务的库用完即弃。之后 env() 不再新建。"""
        if not self._schema or self._dropped:
            return
        self._dropped = True
        try:
            psql(self._base, f"SET lock_timeout = '10s'; DROP SCHEMA IF EXISTS {self._schema} CASCADE")
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
