"""测试里拉起临时的 Postgres 实例（计划 S-12b；与 Go 的 libs/go-core/adapters/postgres/pgtest 一致）。

二进制取自 AR_POSTGRES_BIN 指定的目录，或 PATH 上的 initdb、pg_ctl、psql（见 CLAUDE.md 的常用命令），
不让测试自行下载。找不到时测试失败；设 AR_ALLOW_MISSING_TOOLS=1 改为跳过。

管理操作与原始 SQL 都经 psql 执行，核对用的查询不经过被测的适配器。
"""

from __future__ import annotations

import itertools
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

APP_ROLE = "agentruntime_app"
"""在线服务用的数据库角色：能登录，不是超级用户，没有 BYPASSRLS。"""

ENV_BIN = "AR_POSTGRES_BIN"
ENV_ALLOW_MISSING = "AR_ALLOW_MISSING_TOOLS"


class MissingTool(RuntimeError):
    """找不到 Postgres 的二进制。"""


def _tool(name: str) -> str:
    exe = name + (".exe" if os.name == "nt" else "")
    directory = os.environ.get(ENV_BIN, "")
    if directory:
        path = Path(directory) / exe
        if not path.exists():
            raise MissingTool(f"{ENV_BIN} 里没有 {exe}")
        return str(path)
    found = shutil.which(name)
    if found is None:
        raise MissingTool(f"找不到 {name}（Postgres 的二进制），见 CLAUDE.md 的常用命令")
    return found


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _run(args: list[str]) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    out = (proc.stdout + proc.stderr).replace("\r\n", "\n")
    if proc.returncode != 0:
        raise RuntimeError(f"{Path(args[0]).name} 失败（退出码 {proc.returncode}）：{out}")
    return out


class Cluster:
    """一个临时实例（trust 认证，只监听 127.0.0.1）。"""

    def __init__(self) -> None:
        initdb = _tool("initdb")
        self.bin = str(Path(initdb).parent)
        self.dir = Path(tempfile.mkdtemp(prefix="agentruntime-pg-"))
        self.port = _free_port()
        self._counter = itertools.count(1)
        self._roles: set[str] = set()
        self._lock = threading.Lock()
        data = self.dir / "data"
        _run([initdb, "-D", str(data), "-U", "postgres", "-A", "trust", "-E", "UTF8", "--no-locale"])
        opts = f"-p {self.port} -c listen_addresses=127.0.0.1 -c fsync=off -c max_connections=200"
        if os.name != "nt":
            opts += f" -c unix_socket_directories={self.dir}"
        # pg_ctl start 拉起的服务进程会继承输出句柄：输出交给文件，pg_ctl 一退出就返回。
        with open(self.dir / "pg_ctl.out", "w", encoding="utf-8") as out:
            proc = subprocess.run(
                [_tool("pg_ctl"), "-D", str(data), "-l", str(self.dir / "postgres.log"), "-o", opts, "-w", "start"],
                stdout=out,
                stderr=out,
                check=False,
            )
        if proc.returncode != 0:
            log = (self.dir / "postgres.log").read_text("utf-8", errors="replace") if (self.dir / "postgres.log").exists() else ""
            raise RuntimeError(f"pg_ctl start 失败：{(self.dir / 'pg_ctl.out').read_text('utf-8', errors='replace')}\n{log}")

    def stop(self) -> None:
        subprocess.run([_tool("pg_ctl"), "-D", str(self.dir / "data"), "-m", "immediate", "-w", "stop"], capture_output=True, check=False)
        shutil.rmtree(self.dir, ignore_errors=True)

    def dsn(self, role: str, db: str) -> str:
        return f"postgresql://{role}@127.0.0.1:{self.port}/{db}?sslmode=disable"

    def psql(self, role: str, db: str, sql: str) -> str:
        """以 role 身份在 db 里执行 SQL，返回无对齐、| 分隔的输出（每行一条记录）。"""
        out = _run([_tool("psql"), "-h", "127.0.0.1", "-p", str(self.port), "-U", role, "-d", db, "-v", "ON_ERROR_STOP=1", "-X", "-q", "-A", "-t", "-F", "|", "-c", sql])
        return out.strip()

    def ensure_role(self, name: str, attrs: str) -> None:
        with self._lock:
            if name in self._roles:
                return
            self.psql("postgres", "postgres", f"CREATE ROLE {name} LOGIN {attrs}")
            self._roles.add(name)

    def new_database(self, prefix: str = "s12b") -> str:
        """新建一个属于在线服务角色的库，返回库名。"""
        self.ensure_role(APP_ROLE, "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE")
        db = f"{prefix}_{next(self._counter)}"
        self.psql("postgres", "postgres", f"CREATE DATABASE {db} OWNER {APP_ROLE}")
        return db

    def drop_database(self, db: str) -> None:
        try:
            self.psql("postgres", "postgres", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        except RuntimeError:
            pass


def allow_missing() -> bool:
    return os.environ.get(ENV_ALLOW_MISSING) == "1"
