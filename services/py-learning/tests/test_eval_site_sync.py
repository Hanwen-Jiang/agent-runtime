"""评测网站（docs/plan/eval-site.md 第 3 节）：题目从仓库 main 动态同步，不在发布时生成。

agentruntime_learning.evals.site_sync.SiteSync 每 interval_s 用只读令牌取一次仓库的 tarball（GitHub 的 tarball 接口），
只解出 evalsets 目录，换进缓存目录；只用标准库。守住的规则：

- 只解出 `<顶层目录>/services/py-learning/evalsets/` 下的普通文件与目录；绝对路径、`..`、符号链接、硬链接、设备文件一律跳过，
  解出的文件都在缓存目录里。整包超过上限时整次作废。
- 取不到或包坏了：保留上一份，status 标 stale 与一个错误代码；第一次就失败时 current() 为 None。
- 换新是整体替换：读的一方要么看到旧的一份，要么看到新的一份。
- 令牌只放在请求头里：不进地址、日志、status 与异常信息。
- 没到时间不重复取；refresh(force=True) 立即取。
"""

from __future__ import annotations

import io
import json
import logging
import tarfile
from pathlib import Path
from typing import Any

import pytest

from agentruntime_learning.evals.site_sync import SiteSync

TOKEN = "github_pat_TEST_TOKEN_should_never_show_0123456789"
TOP = "Hanwen-Jiang-agent-runtime-1a2b3c4"
SUB = "services/py-learning/evalsets"


def tarball(files: dict[str, bytes], *, extra: list[tarfile.TarInfo] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info, io.BytesIO(b"x" * info.size) if info.isfile() else None)
    return buf.getvalue()


def good_tarball(marker: str = "v1") -> bytes:
    return tarball({
        f"{TOP}/{SUB}/site.json": json.dumps({"marker": marker}).encode(),
        f"{TOP}/{SUB}/task-loop/task_cases/a.json": b"{}",
        f"{TOP}/README.md": b"outside evalsets",
        f"{TOP}/services/py-worker/secret.py": b"outside evalsets",
    })


class FakeGitHub:
    def __init__(self, bodies: list[bytes | Exception]) -> None:
        self.bodies = bodies
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str]) -> bytes:
        self.calls.append((url, headers))
        body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        if isinstance(body, Exception):
            raise body
        return body


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def make(tmp_path: Path, fetch: FakeGitHub, clock: Clock | None = None, **kw: Any) -> SiteSync:
    return SiteSync(repo="Hanwen-Jiang/agent-runtime", ref="main", token=TOKEN, cache_dir=tmp_path / "cache",
                    fetch=fetch, clock=clock or Clock(), interval_s=300, **kw)


def test_only_the_evalsets_directory_is_extracted(tmp_path: Path) -> None:
    sync = make(tmp_path, FakeGitHub([good_tarball()]))
    assert sync.refresh(force=True) is True
    root = sync.current()
    assert root is not None
    assert json.loads((root / "site.json").read_text("utf-8")) == {"marker": "v1"}
    assert (root / "task-loop" / "task_cases" / "a.json").is_file()
    files = {p.relative_to(tmp_path / "cache").as_posix() for p in (tmp_path / "cache").rglob("*") if p.is_file()}
    assert not [f for f in files if f.endswith(("README.md", "secret.py"))], "evalsets 以外的文件一概不落盘"


def test_the_request_uses_the_tarball_api_with_the_token_in_a_header(tmp_path: Path) -> None:
    fetch = FakeGitHub([good_tarball()])
    make(tmp_path, fetch).refresh(force=True)
    [(url, headers)] = fetch.calls
    assert url == "https://api.github.com/repos/Hanwen-Jiang/agent-runtime/tarball/main"
    assert TOKEN not in url
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers.get("User-Agent"), "GitHub 要求 User-Agent"


@pytest.mark.parametrize("bad", ["abs", "dotdot", "symlink", "hardlink", "device"])
def test_unsafe_members_are_skipped(tmp_path: Path, bad: str) -> None:
    infos: list[tarfile.TarInfo] = []
    if bad == "abs":
        i = tarfile.TarInfo("/etc/evil"); i.size = 1; infos.append(i)
    elif bad == "dotdot":
        i = tarfile.TarInfo(f"{TOP}/{SUB}/../../../../evil"); i.size = 1; infos.append(i)
    elif bad == "symlink":
        i = tarfile.TarInfo(f"{TOP}/{SUB}/link"); i.type = tarfile.SYMTYPE; i.linkname = "/etc/passwd"; infos.append(i)
    elif bad == "hardlink":
        i = tarfile.TarInfo(f"{TOP}/{SUB}/hard"); i.type = tarfile.LNKTYPE; i.linkname = f"{TOP}/README.md"; infos.append(i)
    else:
        i = tarfile.TarInfo(f"{TOP}/{SUB}/dev"); i.type = tarfile.CHRTYPE; infos.append(i)
    body = tarball({f"{TOP}/{SUB}/site.json": b"{}"}, extra=infos)
    sync = make(tmp_path, FakeGitHub([body]))
    assert sync.refresh(force=True) is True
    cache = (tmp_path / "cache").resolve()
    for p in (tmp_path).rglob("*"):
        if p.is_symlink():
            pytest.fail(f"解出了链接：{p}")
    assert not (tmp_path / "evil").exists() and not Path("/etc/evil").exists()
    for p in cache.rglob("*"):
        assert p.resolve().is_relative_to(cache)
    root = sync.current()
    assert root is not None and not (root / "link").exists() and not (root / "hard").exists() and not (root / "dev").exists()


def test_an_oversized_tarball_is_rejected_whole(tmp_path: Path) -> None:
    big = tarball({f"{TOP}/{SUB}/site.json": b"{}", f"{TOP}/{SUB}/big.bin": b"0" * 4096})
    sync = make(tmp_path, FakeGitHub([big]), max_bytes=1024)
    assert sync.refresh(force=True) is False
    assert sync.current() is None
    assert sync.status()["error"] == "too_large"


def test_a_failure_keeps_the_previous_copy_and_says_so(tmp_path: Path) -> None:
    clock = Clock()
    fetch = FakeGitHub([good_tarball("v1"), OSError("connection refused"), good_tarball("v2")])
    sync = make(tmp_path, fetch, clock)
    assert sync.refresh(force=True) is True
    first_at = sync.status()["synced_at"]
    clock.now += 301
    assert sync.refresh() is False
    root = sync.current()
    assert root is not None and json.loads((root / "site.json").read_text("utf-8")) == {"marker": "v1"}
    status = sync.status()
    assert status["stale"] is True and status["error"] == "unreachable" and status["synced_at"] == first_at
    clock.now += 301
    assert sync.refresh() is True
    assert json.loads((sync.current() / "site.json").read_text("utf-8")) == {"marker": "v2"}  # type: ignore[operator]
    assert sync.status()["stale"] is False and sync.status()["error"] is None


def test_a_broken_tarball_is_a_failure_not_a_crash(tmp_path: Path) -> None:
    sync = make(tmp_path, FakeGitHub([b"not a tarball"]))
    assert sync.refresh(force=True) is False
    assert sync.current() is None and sync.status()["error"] == "bad_archive"


def test_a_tarball_without_evalsets_is_a_failure(tmp_path: Path) -> None:
    sync = make(tmp_path, FakeGitHub([tarball({f"{TOP}/README.md": b"x"})]))
    assert sync.refresh(force=True) is False and sync.status()["error"] == "no_evalsets"


def test_it_does_not_fetch_again_before_the_interval(tmp_path: Path) -> None:
    clock = Clock()
    fetch = FakeGitHub([good_tarball()])
    sync = make(tmp_path, fetch, clock)
    sync.refresh(force=True)
    clock.now += 120
    sync.refresh()
    assert len(fetch.calls) == 1
    clock.now += 200
    sync.refresh()
    assert len(fetch.calls) == 2


def test_old_copies_are_cleaned_up(tmp_path: Path) -> None:
    clock = Clock()
    sync = make(tmp_path, FakeGitHub([good_tarball("v1"), good_tarball("v2"), good_tarball("v3")]), clock)
    for _ in range(3):
        sync.refresh(force=True)
        clock.now += 301
    copies = [p for p in (tmp_path / "cache").iterdir() if p.is_dir()]
    assert len(copies) <= 2, "只留当前与上一份（正在读旧一份的请求不受影响）"


def test_the_token_never_shows_in_logs_status_or_errors(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    err = OSError(f"401 for https://api.github.com/... Authorization: Bearer {TOKEN}")
    sync = make(tmp_path, FakeGitHub([err]))
    sync.refresh(force=True)
    assert TOKEN not in caplog.text
    assert TOKEN not in json.dumps(sync.status())
    assert TOKEN not in repr(sync)


def test_without_a_token_it_still_works_for_a_public_repo(tmp_path: Path) -> None:
    fetch = FakeGitHub([good_tarball()])
    sync = SiteSync(repo="o/r", ref="main", token=None, cache_dir=tmp_path / "cache", fetch=fetch, clock=Clock(), interval_s=300)
    assert sync.refresh(force=True) is True
    assert "Authorization" not in fetch.calls[0][1]


def test_a_local_directory_can_stand_in_for_github(tmp_path: Path) -> None:
    """本机与测试：不设仓库时直接读一个目录（例如检出里的 evalsets），不联网。"""
    local = tmp_path / "evalsets"
    (local / "x").mkdir(parents=True)
    (local / "site.json").write_text("{}", "utf-8")
    sync = SiteSync.local(local)
    assert sync.current() == local and sync.status()["source"] == "local"
