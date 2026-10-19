"""Sandbox 的一致性测试。语义见 agentruntime_core.providers.sandbox。

开发实现（本地子进程）不提供网络隔离与资源上限，这里不测这两项；接入真正的隔离后端时另加用例。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from agentruntime_core.conformance.common import Harness, StandardRules, expect_error, tenant_a, tenant_b
from agentruntime_core.errors import ErrorCategory, NotFoundError
from agentruntime_core.providers.sandbox import Sandbox, SandboxProvider, SandboxSpec

SandboxHarness = Harness[SandboxProvider]
TIMEOUT = timedelta(seconds=30)

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

    async def test_exec_timeout_is_reported_not_raised(self, sandbox: Sandbox) -> None:
        env = await sandbox.create(SandboxSpec())
        result = await sandbox.exec(env.env_id, ["python", "-c", "import time; time.sleep(30)"], timeout=timedelta(milliseconds=500))
        assert result.timed_out is True
        assert result.exit_code is None

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
