"""每套进程一个业务库（e2e_support/business_db.py）。

2026-10-24 夜间任务的 Postgres 步骤：所有 Stack 继承同一个 AR_RELATIONAL_DSN，test_dev_stack 种下的开发种子出现在
test_registry 的固定版本里。修复后每套进程在同一个库里用自己的 schema：
- 连接串的改写（URL 与 key=value 两种写法）不需要 Postgres；
- 设了 AR_RELATIONAL_DSN 时（夜间任务的第二步），task-service 经改写后的连接串把表建在这套进程自己的 schema 里，
  另一套进程的 schema 里看不到。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from e2e_support.business_db import ENV_DSN, BusinessDb, psql, with_search_path
from e2e_support.stack import REPO_ROOT, Stack


def test_search_path_is_added_to_a_url_dsn() -> None:
    dsn = with_search_path("postgres://app@127.0.0.1:55432/e2e?sslmode=disable", "e2e_ab12")
    parts = urlsplit(dsn)
    assert (parts.scheme, parts.netloc, parts.path) == ("postgres", "app@127.0.0.1:55432", "/e2e")
    assert parse_qsl(parts.query) == [("sslmode", "disable"), ("options", "-csearch_path=e2e_ab12")]


def test_search_path_is_added_to_a_keyword_dsn() -> None:
    assert with_search_path("host=127.0.0.1 dbname=e2e", "e2e_ab12") == "host=127.0.0.1 dbname=e2e options='-csearch_path=e2e_ab12'"


@pytest.mark.parametrize("dsn", ["postgres://app@h/e2e?options=-cstatement_timeout%3D5", "host=h options='-cwork_mem=4MB'"])
def test_a_dsn_that_already_has_options_is_refused(dsn: str) -> None:
    with pytest.raises(ValueError, match="options"):
        with_search_path(dsn, "e2e_ab12")


@pytest.mark.parametrize("schema", ["", "E2E", "e2e-1", "e2e; DROP SCHEMA public"])
def test_a_bad_schema_name_is_refused(schema: str) -> None:
    with pytest.raises(ValueError):
        with_search_path("postgres://app@h/e2e", schema)


def test_sqlite_needs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_DSN, raising=False)
    db = BusinessDb()
    assert db.env() == {}
    db.drop()


def tables(schema: str) -> set[str]:
    out = psql(os.environ[ENV_DSN], f"SELECT table_name FROM information_schema.tables WHERE table_schema = '{schema}'")
    return {line for line in out.splitlines() if line}


@pytest.mark.e2e
@pytest.mark.skipif(not os.environ.get(ENV_DSN), reason="只在业务库换成 Postgres 时运行（夜间任务的第二步）")
def test_each_stack_writes_its_own_schema(tmp_path: Path) -> None:
    mine, other = Stack(root=tmp_path / "mine"), Stack(root=tmp_path / "other")
    try:
        mine._build_task_service()
        assert mine.task_service_bin is not None
        proc = subprocess.run(
            [str(mine.task_service_bin), "registry-seed", str(REPO_ROOT / "deploy" / "dev" / "registry-seed.json")],
            env=mine.cli_env({"AR_SCHEMAS_DIR": str(REPO_ROOT / "schemas")}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        assert proc.returncode == 0, f"registry-seed 失败：\n{proc.stderr}"
        other.business_db.env()
        assert mine.business_db.schema != other.business_db.schema
        assert "agentruntime_migrations" in tables(mine.business_db.schema), "task-service 的表建在这套进程自己的 schema 里"
        assert tables(other.business_db.schema) == set(), "另一套进程的 schema 里看不到"
    finally:
        mine.stop()
        other.stop()
    assert tables(mine.business_db.schema) == set(), "停下之后删掉自己的 schema"
