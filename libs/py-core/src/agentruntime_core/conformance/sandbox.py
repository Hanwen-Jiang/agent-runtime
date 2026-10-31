"""Sandbox 的一致性测试。语义见 agentruntime_core.providers.sandbox。

开发实现（本地子进程）不提供网络隔离与资源上限，这里不测这两项；接入真正的隔离后端时另加用例。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import tarfile
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

import pytest

from agentruntime_core.conformance.common import (
    TENANT_A_ID,
    TENANT_B_ID,
    BindingRules,
    Harness,
    StandardRules,
    expect_error,
    tenant_a,
    tenant_b,
)
from agentruntime_core.errors import ErrorCategory, InvalidError, NotFoundError
from agentruntime_core.providers.sandbox import FileEntry, Sandbox, SandboxProvider, SandboxSpec

SandboxHarness = Harness[SandboxProvider]
TIMEOUT = timedelta(seconds=30)

# 命令和它的子进程各往一个文件里不停地追加，文件不再变长说明进程停了。到时间自己退出：取消没有杀掉它们时（测试失败时）不留在机器上。
_BEAT = """
import sys, time
end = time.monotonic() + 30
while time.monotonic() < end:
    with open(sys.argv[1], "a") as f:
        f.write(".")
    time.sleep(0.05)
"""
_TREE = f"""
import subprocess, sys
BEAT = {_BEAT!r}
subprocess.Popen([sys.executable, "-c", BEAT, "child.beat"])
sys.argv = ["", "parent.beat"]
exec(BEAT)
"""

MALFORMED_PATHS = ["../escape.txt", "/etc/passwd", "a/../../escape.txt", "C:\\Windows\\x", "a\\b", ""]


class SandboxConformance(StandardRules):
    probe_operation = "create"

    def _probe_envs(self) -> dict[str, str]:
        return self.__dict__.setdefault("_envs", {})

    async def write_probe(self, client: Sandbox, name: str) -> None:
        env = await client.create(SandboxSpec())
        self._probe_envs()[name] = env.env_id
        await client.write_file(env.env_id, "probe.txt", name.encode())

    async def probe_visible(self, client: Sandbox, name: str) -> bool:
        try:
            return await client.read_file(self._probe_envs()[name], "probe.txt") == name.encode()
        except NotFoundError:
            return False

    @pytest.fixture
    def sandbox(self, harness: SandboxHarness) -> Sandbox:
        return harness.provider.bind(tenant_a())

    async def test_exec_runs_code(self, sandbox: Sandbox) -> None:
        env = await sandbox.create(SandboxSpec())
        result = await sandbox.exec(env.env_id, ["python", "-c", "print(6 * 7)"], timeout=TIMEOUT)
        assert result.exit_code == 0 and result.timed_out is False
        assert result.stdout.strip() == b"42"

    async def test_exec_returns_non_zero_exit_code(self, sandbox: Sandbox) -> None:
        env = await sandbox.create(SandboxSpec())
        result = await sandbox.exec(
            env.env_id, ["python", "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"], timeout=TIMEOUT
        )
        assert result.exit_code == 3
        assert b"bad" in result.stderr

    async def test_exec_passes_stdin(self, sandbox: Sandbox) -> None:
        env = await sandbox.create(SandboxSpec())
        result = await sandbox.exec(
            env.env_id, ["python", "-c", "import sys; print(sys.stdin.read().upper())"], timeout=TIMEOUT, stdin=b"abc"
        )
        assert result.stdout.strip() == b"ABC"

    async def test_exec_env_is_only_for_that_command(self, sandbox: Sandbox) -> None:
        env = await sandbox.create(SandboxSpec())
        code = ["python", "-c", "import os; print(os.environ.get('AR_TEST_EXEC_ENV', 'unset'))"]
        first = await sandbox.exec(env.env_id, code, timeout=TIMEOUT, env={"AR_TEST_EXEC_ENV": "given"})
        assert first.stdout.strip() == b"given"
        second = await sandbox.exec(env.env_id, code, timeout=TIMEOUT)
        assert second.stdout.strip() == b"unset", "env 只对那一条命令有效"
        for bad in ({"AGENT_TOKEN": "x"}, {"BAD-NAME": "x"}):
            with expect_error(ErrorCategory.INVALID):
                await sandbox.exec(env.env_id, code, timeout=TIMEOUT, env=bad)

    async def test_exec_timeout_is_reported_not_raised(self, sandbox: Sandbox) -> None:
        env = await sandbox.create(SandboxSpec())
        result = await sandbox.exec(env.env_id, ["python", "-c", "import time; time.sleep(30)"], timeout=timedelta(milliseconds=500))
        assert result.timed_out is True
        assert result.exit_code is None

    async def test_cancelling_exec_stops_the_command_and_its_children(self, sandbox: Sandbox) -> None:
        """打断或取消 Task 时（docs/plan/running-task-interaction.md 第 2 节）Worker 这边的协程被取消，命令要真的停，子进程也要停。"""
        env = await sandbox.create(SandboxSpec())
        call = asyncio.create_task(sandbox.exec(env.env_id, ["python", "-c", _TREE], timeout=timedelta(seconds=60)))

        async def beats(name: str) -> int:
            try:
                return len(await sandbox.read_file(env.env_id, name))
            except NotFoundError:
                return 0

        for _ in range(400):
            if await beats("parent.beat") and await beats("child.beat"):
                break
            assert not call.done(), f"命令提前结束了：{call.result()}"
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("命令没有跑起来")
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(call, 20)
        await asyncio.sleep(0.5)
        before = await beats("parent.beat"), await beats("child.beat")
        await asyncio.sleep(1.0)
        after = await beats("parent.beat"), await beats("child.beat")
        assert after[0] == before[0], "取消之后命令还在跑"
        assert after[1] == before[1], "取消之后命令的子进程还在跑"

    async def test_files_roundtrip_and_are_visible_to_code(self, sandbox: Sandbox) -> None:
        env = await sandbox.create(SandboxSpec())
        await sandbox.write_file(env.env_id, "data/input.txt", "退款".encode())
        assert await sandbox.read_file(env.env_id, "data/input.txt") == "退款".encode()
        code = "open('data/out.txt', 'w', encoding='utf-8').write(open('data/input.txt', encoding='utf-8').read() * 2)"
        assert (await sandbox.exec(env.env_id, ["python", "-c", code], timeout=TIMEOUT)).exit_code == 0
        assert await sandbox.read_file(env.env_id, "data/out.txt") == "退款退款".encode()

    async def test_environments_do_not_share_files(self, sandbox: Sandbox) -> None:
        first = await sandbox.create(SandboxSpec())
        second = await sandbox.create(SandboxSpec())
        await sandbox.write_file(first.env_id, "only-here.txt", b"x")
        with expect_error(ErrorCategory.NOT_FOUND):
            await sandbox.read_file(second.env_id, "only-here.txt")

    @pytest.mark.parametrize("path", MALFORMED_PATHS, ids=range(len(MALFORMED_PATHS)))
    async def test_paths_cannot_escape_workdir(self, sandbox: Sandbox, path: str) -> None:
        env = await sandbox.create(SandboxSpec())
        with expect_error(ErrorCategory.INVALID):
            await sandbox.write_file(env.env_id, path, b"x")
        with expect_error(ErrorCategory.INVALID):
            await sandbox.read_file(env.env_id, path)

    async def test_read_missing_file_is_not_found(self, sandbox: Sandbox) -> None:
        env = await sandbox.create(SandboxSpec())
        with expect_error(ErrorCategory.NOT_FOUND):
            await sandbox.read_file(env.env_id, "missing.txt")

    async def test_destroyed_environment_is_gone(self, sandbox: Sandbox) -> None:
        env = await sandbox.create(SandboxSpec())
        await sandbox.destroy(env.env_id)
        with expect_error(ErrorCategory.NOT_FOUND):
            await sandbox.exec(env.env_id, ["python", "-c", "print(1)"], timeout=TIMEOUT)
        await sandbox.destroy(env.env_id)

    async def test_other_tenant_cannot_use_or_destroy_environment(self, harness: SandboxHarness, sandbox: Sandbox) -> None:
        env = await sandbox.create(SandboxSpec())
        b = harness.provider.bind(tenant_b())
        with expect_error(ErrorCategory.NOT_FOUND):
            await b.exec(env.env_id, ["python", "-c", "print(1)"], timeout=TIMEOUT)
        with expect_error(ErrorCategory.NOT_FOUND):
            await b.write_file(env.env_id, "x.txt", b"x")
        with expect_error(ErrorCategory.NOT_FOUND):
            await b.destroy(env.env_id)
        assert (await sandbox.exec(env.env_id, ["python", "-c", "print(1)"], timeout=TIMEOUT)).exit_code == 0

    async def test_unknown_environment_is_not_found(self, sandbox: Sandbox) -> None:
        with expect_error(ErrorCategory.NOT_FOUND):
            await sandbox.exec("env-does-not-exist", ["python", "-c", "print(1)"], timeout=TIMEOUT)


class WorkspaceHarness(Protocol):
    def workspace_path(self, tenant_id: str, workspace_id: str) -> Path:
        """宿主一侧看到的 workspace 目录（<工作区根>/<tenant_id>/<workspace_id>），测试在这里放文件、查文件。"""
        ...


class SandboxWorkspaceConformance(BindingRules):
    """spec.workspace_id 的规则（providers.sandbox 的语义最后一条）。支持绑定 workspace 的实现用下面的
    SandboxWithWorkspaceConformance（两组一起）；harness 要实现 WorkspaceHarness。目录由宿主一侧先建好（同 Worker 的工作区类工具的做法）。"""

    @staticmethod
    def _prepare(harness: Any, tenant_id: str, workspace_id: str) -> Path:
        path = harness.workspace_path(tenant_id, workspace_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def test_workspace_is_shared_with_the_host(self, harness: Any) -> None:
        sandbox = harness.provider.bind(tenant_a())
        host = self._prepare(harness, TENANT_A_ID, "w-share")
        (host / "seed.txt").write_text("从宿主写入", encoding="utf-8")
        env = await sandbox.create(SandboxSpec(workspace_id="w-share"))
        code = "open('made.txt', 'w', encoding='utf-8').write(open('seed.txt', encoding='utf-8').read() + '，沙箱读到了')"
        assert (await sandbox.exec(env.env_id, ["python", "-c", code], timeout=TIMEOUT)).exit_code == 0
        assert (host / "made.txt").read_text(encoding="utf-8") == "从宿主写入，沙箱读到了"
        assert await sandbox.read_file(env.env_id, "seed.txt") == "从宿主写入".encode()

    async def test_destroy_keeps_workspace_files(self, harness: Any) -> None:
        sandbox = harness.provider.bind(tenant_a())
        self._prepare(harness, TENANT_A_ID, "w-keep")
        env = await sandbox.create(SandboxSpec(workspace_id="w-keep"))
        await sandbox.write_file(env.env_id, "kept.txt", b"x")
        await sandbox.destroy(env.env_id)
        assert (harness.workspace_path(TENANT_A_ID, "w-keep") / "kept.txt").read_bytes() == b"x"

    async def test_same_workspace_id_differs_per_tenant(self, harness: Any) -> None:
        a = harness.provider.bind(tenant_a())
        b = harness.provider.bind(tenant_b())
        self._prepare(harness, TENANT_A_ID, "w-same")
        self._prepare(harness, TENANT_B_ID, "w-same")
        env_a = await a.create(SandboxSpec(workspace_id="w-same"))
        await a.write_file(env_a.env_id, "only-a.txt", b"a")
        env_b = await b.create(SandboxSpec(workspace_id="w-same"))
        with expect_error(ErrorCategory.NOT_FOUND):
            await b.read_file(env_b.env_id, "only-a.txt")

    async def test_platform_scope_has_no_workspace(self, harness: Any) -> None:
        with expect_error(ErrorCategory.INVALID):
            await harness.provider.bind_platform("conformance").create(SandboxSpec(workspace_id="w-1"))

    @pytest.mark.parametrize("workspace_id", ["../w", "W-1", "w/1", "-w", "w" * 64])
    async def test_malformed_workspace_id_is_invalid(self, harness: Any, workspace_id: str) -> None:
        with expect_error(ErrorCategory.INVALID):
            await harness.provider.bind(tenant_a()).create(SandboxSpec(workspace_id=workspace_id))


class SandboxWithWorkspaceConformance(SandboxConformance, SandboxWorkspaceConformance):
    """支持绑定 workspace 的实现跑的全套：通用规则加 workspace 的规则。"""


def tar_of(*items: tuple[str, str, bytes | str]) -> bytes:
    """测试用的 tar：(名字, 类型 file|dir|symlink|hardlink|char, 内容或链接目标)。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, kind, payload in items:
            info = tarfile.TarInfo(name)
            info.mode = 0o755 if kind == "dir" else 0o644
            if kind == "file":
                data = payload if isinstance(payload, bytes) else payload.encode()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
                continue
            info.type = {"dir": tarfile.DIRTYPE, "symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE, "char": tarfile.CHRTYPE}[kind]
            if kind in ("symlink", "hardlink"):
                info.linkname = str(payload)
            tar.addfile(info)
    return buf.getvalue()


def shape(entries: list[FileEntry]) -> list[tuple[str, str, int, str, str]]:
    """比较两份清单用：路径、类型、大小、内容、链接。权限位不比（Windows 上的开发实现表示不了），由各实现自己的测试覆盖。"""
    return [(e.path, e.type, e.size, e.sha256, e.link) for e in entries]


HOSTILE_ARCHIVES = {
    "absolute": [("/etc/passwd", "file", "x")],
    "dotdot": [("../escape.txt", "file", "x")],
    "nested-dotdot": [("a/../../escape.txt", "file", "x")],
    "backslash": [("a\\b.txt", "file", "x")],
    "hardlink": [("a.txt", "file", "x"), ("b.txt", "hardlink", "a.txt")],
    "device": [("dev", "char", "")],
    "escaping-link": [("out", "symlink", "../../etc")],
    "absolute-link": [("out", "symlink", "/etc")],
    "duplicate": [("a.txt", "file", "x"), ("a.txt", "file", "y")],
    "file-then-dir": [("a", "file", "x"), ("a/b.txt", "file", "y")],
}


class SandboxArchiveConformance(BindingRules):
    """工作区的存档模式（D-06）：SandboxArchive 与 SandboxBrowse 的语义（providers.sandbox）。支持的实现（开发实现、K8s、
    Cloudflare）把这一组与通用的一组一起加进各自的测试类。"""

    @pytest.fixture
    def box(self, harness: SandboxHarness) -> Any:
        return harness.provider.bind(tenant_a())

    @staticmethod
    async def _fresh(box: Any) -> str:
        return (await box.create(SandboxSpec())).env_id

    async def test_export_then_import_into_a_fresh_environment_keeps_everything(self, box: Any) -> None:
        src = await self._fresh(box)
        files = {"README.md": "# 退款\n".encode(), "src/app/main.py": b"print('hi')\n", "empty.txt": b"", ".git/HEAD": b"ref: main\n", "a.txt": b"a"}
        for path, data in files.items():
            await box.write_file(src, path, data)
        assert (await box.exec(src, ["python", "-c", "import os; os.makedirs('empty/dir')"], timeout=TIMEOUT)).exit_code == 0
        tmp = io.BytesIO()
        assert await box.export_archive(src, tmp, max_bytes=1 << 20) > 0
        dst = await self._fresh(box)
        data = tmp.getvalue()
        await box.import_archive(dst, io.BytesIO(data), size=len(data), max_bytes=1 << 20)
        want, got = await box.manifest(src), await box.manifest(dst)
        assert shape(got) == shape(want)
        readme = next(e for e in got if e.path == "README.md")
        assert readme.type == "file" and readme.size == len("# 退款\n".encode()) and readme.sha256 == hashlib.sha256("# 退款\n".encode()).hexdigest()
        assert {e.path for e in got} >= {"src", "src/app", "src/app/main.py", ".git", ".git/HEAD", "empty.txt", "empty", "empty/dir", "a.txt"}
        assert [e.path for e in got] == sorted(e.path for e in got), "清单按路径的字符串顺序排序"
        assert await box.read_file(dst, "src/app/main.py") == b"print('hi')\n"

    async def test_import_needs_an_empty_workdir_and_respects_the_limits(self, box: Any) -> None:
        env = await self._fresh(box)
        await box.write_file(env, "already.txt", b"x")
        small = tar_of(("a.txt", "file", "a"))
        with expect_error(ErrorCategory.INVALID, "not_empty"):
            await box.import_archive(env, io.BytesIO(small), size=len(small), max_bytes=1024)
        big = tar_of(("big.bin", "file", b"x" * 4096))
        with expect_error(ErrorCategory.INVALID):
            await box.import_archive(await self._fresh(box), io.BytesIO(big), size=len(big), max_bytes=1024)
        await box.write_file(env, "big.bin", b"y" * 4096)
        with expect_error(ErrorCategory.INVALID, "too_large"):
            await box.export_archive(env, io.BytesIO(), max_bytes=1024)

    @pytest.mark.parametrize("name", sorted(HOSTILE_ARCHIVES))
    async def test_hostile_archives_are_refused(self, box: Any, name: str) -> None:
        env = await self._fresh(box)
        data = tar_of(*HOSTILE_ARCHIVES[name])
        with expect_error(ErrorCategory.INVALID):
            await box.import_archive(env, io.BytesIO(data), size=len(data), max_bytes=1 << 20)
        assert not any(e.path.endswith("escape.txt") for e in await box.manifest(env))

    async def test_symlinks_inside_the_workdir_roundtrip(self, box: Any) -> None:
        data = tar_of(("pkg", "dir", ""), ("pkg/real.py", "file", "x = 1\n"), ("pkg/alias.py", "symlink", "real.py"), ("top", "symlink", "pkg"))
        env = await self._fresh(box)
        try:
            await box.import_archive(env, io.BytesIO(data), size=len(data), max_bytes=1 << 20)
        except InvalidError as err:
            if err.code == "symlink_unsupported":
                pytest.skip("这个后端所在的系统建不了符号链接（Windows 上的开发实现）")
            raise
        links = {e.path: e.link for e in await box.manifest(env) if e.type == "symlink"}
        assert links == {"pkg/alias.py": "real.py", "top": "pkg"}
        tmp = io.BytesIO()
        await box.export_archive(env, tmp, max_bytes=1 << 20)
        again = await self._fresh(box)
        await box.import_archive(again, io.BytesIO(tmp.getvalue()), size=len(tmp.getvalue()), max_bytes=1 << 20)
        assert shape(await box.manifest(again)) == shape(await box.manifest(env))

    async def test_other_tenants_cannot_export_import_or_browse(self, harness: SandboxHarness, box: Any) -> None:
        env = await self._fresh(box)
        await box.write_file(env, "secret.txt", b"a")
        other: Any = harness.provider.bind(tenant_b())
        data = tar_of(("x.txt", "file", "x"))
        with expect_error(ErrorCategory.NOT_FOUND):
            await other.export_archive(env, io.BytesIO(), max_bytes=1 << 20)
        with expect_error(ErrorCategory.NOT_FOUND):
            await other.import_archive(env, io.BytesIO(data), size=len(data), max_bytes=1 << 20)
        with expect_error(ErrorCategory.NOT_FOUND):
            await other.manifest(env)
        with expect_error(ErrorCategory.NOT_FOUND):
            await other.list_dir(env)
        with expect_error(ErrorCategory.NOT_FOUND):
            await other.grep(env, "a")

    async def test_list_and_grep_run_inside_the_environment(self, box: Any) -> None:
        env = await self._fresh(box)
        files = {
            "a.py": b"def refund():\n    return 1\n", "pkg/b.py": b"x = 1\r\n# refund policy\r\n", "pkg/c.txt": b"nothing\n",
            ".git/config": b"refund\n", "pkg/.git/x": b"refund\n", "bin.dat": b"refund\x00\x01",
        }
        for path, data in files.items():
            await box.write_file(env, path, data)
        assert [(e.name, e.type) for e in await box.list_dir(env)] == [(".git", "dir"), ("a.py", "file"), ("bin.dat", "file"), ("pkg", "dir")]
        sub = await box.list_dir(env, "pkg")
        assert [e.name for e in sub] == [".git", "b.py", "c.txt"] and sub[1].size == len(files["pkg/b.py"])
        with expect_error(ErrorCategory.NOT_FOUND):
            await box.list_dir(env, "missing")
        with expect_error(ErrorCategory.INVALID):
            await box.list_dir(env, "../escape")
        matches, truncated = await box.grep(env, "refund")
        assert [(m.path, m.line, m.text) for m in matches] == [("a.py", 1, "def refund():"), ("pkg/b.py", 2, "# refund policy")], "跳过 .git 与二进制"
        assert not truncated
        assert [m.path for m in (await box.grep(env, "refund", path="pkg"))[0]] == ["pkg/b.py"]
        assert (await box.grep(env, "re.und", max_matches=1))[1] is True
        with expect_error(ErrorCategory.INVALID):
            await box.grep(env, "(")
