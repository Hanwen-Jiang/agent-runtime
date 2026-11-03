"""评测网站的数据同步（docs/plan/eval-site.md 第 3 节）：从仓库 main 取 evalsets，不在发布时生成。只用标准库。

SiteSync 每 interval_s 用只读令牌取一次仓库的 tarball（GitHub 的 tarball 接口），只解出 ``<顶层目录>/<subdir>/`` 下的普通文件与目录，
解进缓存目录里新的一份，再整体换过去（读的一方要么看到旧的一份，要么看到新的一份）；只留当前与上一份。

- 绝对路径、``..``、符号链接、硬链接、设备文件一律跳过；解出的文件都在缓存目录里。解压后的总大小超过 max_bytes 时整次作废。
- 取不到或包坏了：保留上一份，status 标 stale 与错误代码（unreachable、bad_archive、no_evalsets、too_large）；
  异常信息不记（可能带地址或令牌）。
- 令牌只放在请求头里，不进地址、日志、status 与 repr。
- SiteSync.local(目录)：本机与测试直接读一个目录，不联网。
"""

from __future__ import annotations

import io
import logging
import shutil
import tarfile
import threading
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_SUBDIR = "services/py-learning/evalsets"
DEFAULT_INTERVAL_S = 300.0
DEFAULT_MAX_BYTES = 64 << 20
FETCH_TIMEOUT_S = 60.0
USER_AGENT = "agent-runtime-eval-site"
API = "https://api.github.com"

Fetch = Callable[[str, dict[str, str]], bytes]


class _Failure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _http_fetch(url: str, headers: dict[str, str]) -> bytes:
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 — 地址是写死的 GitHub API 加部署配置的仓库名
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:  # noqa: S310
        return bytes(resp.read())


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class SiteSync:
    def __init__(self, *, repo: str, ref: str = "main", token: str | None, cache_dir: Path, fetch: Fetch | None = None,
                 clock: Callable[[], float] = time.time, interval_s: float = DEFAULT_INTERVAL_S,
                 max_bytes: int = DEFAULT_MAX_BYTES, subdir: str = DEFAULT_SUBDIR) -> None:
        self.repo = repo
        self.ref = ref
        self._token = token or None
        self.cache_dir = Path(cache_dir)
        self.fetch = fetch or _http_fetch
        self.clock = clock
        self.interval_s = interval_s
        self.max_bytes = max_bytes
        self.subdir = subdir.strip("/")
        self._lock = threading.Lock()
        self._current: Path | None = None
        self._previous: Path | None = None
        self._attempted_at = float("-inf")
        self._synced_at: float | None = None
        self._error: str | None = None
        self._serial = 0
        self._source = "github"

    @classmethod
    def local(cls, root: Path) -> SiteSync:
        sync = cls(repo="", token=None, cache_dir=Path(root))
        sync._source = "local"
        sync._current = Path(root)
        sync._synced_at = time.time()
        return sync

    def __repr__(self) -> str:
        return f"SiteSync(source={self._source!r}, repo={self.repo!r}, ref={self.ref!r})"

    def current(self) -> Path | None:
        return self._current

    def status(self) -> dict[str, Any]:
        return {"source": self._source, "repo": self.repo, "ref": self.ref, "synced_at": _iso(self._synced_at),
                "stale": self._error is not None and self._source != "local", "error": self._error}

    def refresh(self, *, force: bool = False) -> bool:
        if self._source == "local":
            return True
        with self._lock:
            now = self.clock()
            if not force and now - self._attempted_at < self.interval_s:
                return False
            self._attempted_at = now
            try:
                body = self._download()
                target = self._extract(body)
            except _Failure as failure:
                self._error = failure.code
                log.warning("eval site sync failed: %s", failure.code)
                return False
            old_previous, self._previous, self._current = self._previous, self._current, target
            self._synced_at, self._error = now, None
            if old_previous is not None and old_previous not in (self._previous, self._current):
                shutil.rmtree(old_previous, ignore_errors=True)
            return True

    def _download(self) -> bytes:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            body = self.fetch(f"{API}/repos/{self.repo}/tarball/{self.ref}", headers)
        except Exception as exc:  # noqa: BLE001 — 不回显异常：可能带地址或令牌
            raise _Failure("unreachable") from exc
        if len(body) > self.max_bytes:
            raise _Failure("too_large")
        return body

    def _extract(self, body: bytes) -> Path:
        try:
            tar = tarfile.open(fileobj=io.BytesIO(body), mode="r:*")
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise _Failure("bad_archive") from exc
        sub = PurePosixPath(self.subdir)
        wanted: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
        total = 0
        try:
            with tar:
                for info in tar.getmembers():
                    rel = self._relative(info.name, sub)
                    if rel is None or not (info.isfile() or info.isdir()):
                        continue
                    total += info.size
                    if total > self.max_bytes:
                        raise _Failure("too_large")
                    wanted.append((info, rel))
                if not any(info.isfile() for info, _ in wanted):
                    raise _Failure("no_evalsets")
                self._serial += 1
                target = self.cache_dir / f"copy-{int(self.clock())}-{self._serial}"
                tmp = target.with_name(target.name + ".tmp")
                shutil.rmtree(tmp, ignore_errors=True)
                tmp.mkdir(parents=True)
                root = tmp.resolve()
                for info, rel in wanted:
                    dest = (tmp / rel.as_posix()).resolve()
                    if not dest.is_relative_to(root):
                        continue
                    if info.isdir():
                        dest.mkdir(parents=True, exist_ok=True)
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    src = tar.extractfile(info)
                    if src is None:
                        continue
                    with src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
        except _Failure:
            raise
        except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
            raise _Failure("bad_archive") from exc
        tmp.rename(target)
        return target

    @staticmethod
    def _relative(name: str, sub: PurePosixPath) -> PurePosixPath | None:
        """``<顶层>/<sub>/…`` 换成 ``…``；别的（绝对路径、带 ..、不在 sub 下）为 None。"""
        if not name or name.startswith("/") or "\\" in name:
            return None
        parts = PurePosixPath(name).parts
        if any(p in ("..", "") for p in parts) or len(parts) < 2:
            return None
        inner = PurePosixPath(*parts[1:])
        try:
            rel = inner.relative_to(sub)
        except ValueError:
            return None
        return None if rel == PurePosixPath(".") else rel
