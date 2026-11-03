"""ENV-02：云环境配置的执行链（人工验收 Q16–37；docs/plan/env-02-implementation-2026-11-03.md）。

Runtime 一侧的验收：
1. 环境与仓库无关：同一份环境配置绑定到两个工作区，各自准备；配置按（租户，工作区）快照保存，只有密钥的名字。
2. 新的沙箱环境建好、工作区就位之后，先跑 setup、再跑 start，之后才执行业务命令；同一个环境复用时不重跑。
3. setup 的缓存键是 sha256（脚本内容 + 基础镜像 digest）；只缓存成功的结果（经环境镜像），失败不缓存、下次重跑。
4. setup 失败或超时：这个环境不启动（业务命令不执行）、错误码稳定（environment_setup_failed / environment_setup_timeout），
   环境表记下失败并销毁环境。
5. start 只启动交互运行时：失败只记录，不阻止业务命令。
6. .env：一行一个 KEY=value，交给 setup、start 与每条命令；有个数与字节上限；不能盖掉平台变量；像机密的名字被拒。
   网络密钥只有名字，值不进沙箱的环境变量。
7. 删除环境配置：选用它的工作区不再准备新的环境（environment_deleted），已有的环境由对账在闲着时销毁。
8. 环境表是状态的权威：setup、start 的结果记在 sandbox_envs 的行上，不从日志推断。
"""

from __future__ import annotations

import hashlib
import io
import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.errors import DeniedError, InvalidError
from agentruntime_core.providers.sandbox import SandboxEnv, SandboxSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.sandbox.broker import SandboxBroker
from agentruntime_worker.sandbox.env_config import EnvironmentConfig, parse_dotenv, setup_key
from agentruntime_worker.sandbox.ledger import SandboxLedger
from agentruntime_worker.sandbox.reconcile import reconcile_once
from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools
from agentruntime_worker.tools.sandboxes import Backend, Sandboxes
from agentruntime_worker.workspace.archive import WorkspaceArchiver
from agentruntime_worker.workspace.operations import WorkspaceOperations

S1 = "ses_01HZY2J5K3M4N5P6Q7R8S9T0VW"
S2 = "ses_01HZY2J5K3M4N5P6Q7R8S9T0VX"
BASE = "ghcr.io/agentruntime/sandbox-universal:0.1.0"
BASE_DIGEST = "sha256:" + "ab" * 32


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def environment(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "environment_id": "env-py",
        "setup_script": "echo setup >> order.txt\n",
        "start_command": "echo start >> order.txt\n",
        "env": "# 普通变量\nGREETING=hello world\nMODE=a=b\n",
    }
    doc.update(overrides)
    return doc


class ImageSandboxes:
    """包一层开发实现：接受 spec.image（集群里的 K8s 后端用环境镜像），记下每次建环境用的镜像，建出来时去掉它。"""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.images: list[str] = []

    def bind(self, tenant: TenantContext) -> Any:
        return _ImageBox(self, self.inner.bind(tenant))

    def bind_platform(self, purpose: str) -> Any:
        return _ImageBox(self, self.inner.bind_platform(purpose))


class _ImageBox:
    def __init__(self, owner: ImageSandboxes, box: Any) -> None:
        self._owner, self._box = owner, box

    async def create(self, spec: SandboxSpec) -> SandboxEnv:
        self._owner.images.append(spec.image)
        env = await self._box.create(replace(spec, image=""))
        return replace(env, image=spec.image or BASE_DIGEST)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._box, name)


class Setup:
    def __init__(self, tmp_path: Path, *, sessions: bool = True, images: bool = False, setup_timeout_s: float = 30) -> None:
        self.clock = Clock()
        self.workspaces = tmp_path / "workspaces"
        for ws in ("w1", "w2"):
            (self.workspaces / "t1" / ws).mkdir(parents=True, exist_ok=True)
        spec: dict[str, Any] = {"kind": "local", "isolation": "none"}
        if images:
            spec["custom_images"] = True
        self.boxes = Sandboxes({"local": spec}, "local", workspace_root=self.workspaces)
        store = SQLiteRelationalStoreProvider(tmp_path / "relational")
        self.ledger = SandboxLedger(store, clock=self.clock)
        self.archiver = WorkspaceArchiver(store, LocalDiskObjectStoreProvider(tmp_path / "objects"), clock=self.clock)
        self.broker = SandboxBroker(self.boxes, self.ledger, archiver=self.archiver)
        self.broker.setup_timeout = timedelta(seconds=setup_timeout_s)
        self.images: ImageSandboxes | None = None
        self.built: list[tuple[str, str]] = []
        if images:
            from agentruntime_worker.sandbox.environments import EnvironmentManager

            async def starter(owner: str, key: str) -> None:
                self.built.append((owner, key))

            # 构建器与镜像仓库不在这里测（test_sandbox_environments 测过）：starter 只记下要构建的键，测试里直接把行标成 ready。
            self.broker.environments = EnvironmentManager(self.ledger, base_image=BASE, base_digest=BASE_DIGEST, starter=starter)
        self.telemetry = ConsoleTelemetryProvider(stream=io.StringIO())
        self.tools = LocalToolDriverProvider()
        register_platform_tools(
            self.tools, PlatformToolsConfig(workspace_root=self.workspaces, shell_sandbox="sandbox", session_sandboxes=sessions),
            sandboxes=self.broker, telemetry=self.telemetry,
        )

    async def wrap_images(self) -> ImageSandboxes:
        backend = await self.boxes.backend("local")
        self.images = ImageSandboxes(backend.provider)
        self.boxes._backends["local"] = Backend(name="local", kind="local", isolation="none", provider=self.images)  # noqa: SLF001
        return self.images

    async def bind(self, workspace: str = "w1", doc: Mapping[str, Any] | None = None, *, user: str = "u1") -> EnvironmentConfig:
        config = EnvironmentConfig.from_args(dict(doc if doc is not None else environment()))
        await self.archiver.ensure("t1", workspace, (await self.boxes.policy_for("t1")).workspace)
        await self.archiver.set_environment("t1", workspace, config, owner_user=user)
        return config

    async def shell(self, argv: list[str], *, session: str = S1, workspace: str = "w1") -> dict[str, Any]:
        tenant = TenantContext(tenant_id="t1", user_id="u1", workspace_id=workspace, session_id=session)
        result = await self.tools.bind(tenant).invoke("shell_run", "v1", {"argv": argv}, idempotency_key=f"idk-{os.urandom(4).hex()}", task_id="tsk_env02")
        return result.output

    def order(self, workspace: str = "w1") -> list[str]:
        path = self.workspaces / "t1" / workspace / "order.txt"
        return path.read_text().split() if path.exists() else []


APPEND = ["bash", "-c", "echo cmd >> order.txt"]


# --- 配置的格式 ---


def test_dotenv_is_one_key_value_per_line_with_limits() -> None:
    assert parse_dotenv("# 注释\n\nA=1\r\nC=a=b=c\nD=\n  E=x y  \n") == {"A": "1", "C": "a=b=c", "D": "", "E": "x y"}
    for bad, code in (
        ("no-equals-sign", "invalid_env_line"),
        ("1BAD=x", "invalid_env_line"),
        ("B = x", "invalid_env_line"),
        ("A=1\nA=2", "invalid_env_line"),
        ("HTTPS_PROXY=http://evil", "env_reserved_name"),
        ("SSL_CERT_FILE=/tmp/x", "env_reserved_name"),
        ("AGENT_X=1", "env_reserved_name"),
        ("GITHUB_TOKEN=ghp_x", "env_secret_name"),
        ("MY_API_KEY=x", "env_secret_name"),
        ("DB_PASSWORD=x", "env_secret_name"),
        ("A=" + "x" * (16 * 1024), "env_too_large"),
        ("\n".join(f"V{i}=1" for i in range(65)), "env_too_large"),
        ("A=a\x00b", "invalid_env_line"),
    ):
        with pytest.raises(InvalidError) as err:
            parse_dotenv(bad)
        assert err.value.code == code, (bad, err.value.code)
        assert "ghp_x" not in err.value.message, "错误信息不带值"


def test_environment_config_validation_and_snapshot_without_secret_values() -> None:
    config = EnvironmentConfig.from_args(environment(
        network={"mode": "NETWORK_MODE_RESTRICTED", "hosts": [{"host": "pypi.org"}, {"host": "github.com", "include_subdomains": True}]},
        secret_headers=[{"host": "api.example.com", "secret_name": "example-token"}, {"host": "x.example.com", "secret_name": "x", "header": "x-api-key"}],
    ))
    assert config.network_mode == "restricted"
    assert [(h.host, h.include_subdomains) for h in config.hosts] == [("pypi.org", False), ("github.com", True)]
    assert config.secret_headers[0].header == "authorization" and config.secret_headers[1].header == "x-api-key"
    assert config.variables == {"GREETING": "hello world", "MODE": "a=b"}
    again = EnvironmentConfig.from_json(config.to_json())
    assert again == config
    assert "secret_value" not in config.to_json()
    for doc, code in (
        (environment(environment_id="bad id"), "invalid_environment"),
        (environment(setup_script="x" * (64 * 1024 + 1)), "environment_script_too_large"),
        (environment(network={"mode": "NETWORK_MODE_RESTRICTED", "hosts": [{"host": "10.0.0.1"}]}), "invalid_network_host"),
        (environment(network={"mode": "NETWORK_MODE_RESTRICTED", "hosts": [{"host": "*"}]}), "invalid_network_host"),
        (environment(secret_headers=[{"host": "api.example.com", "secret_name": "../other-user/t"}]), "invalid_secret_header"),
        (environment(secret_headers=[{"host": "api.example.com", "secret_name": "t", "header": "Host"}]), "invalid_secret_header"),
        (environment(secret_headers=[{"host": "api.example.com", "secret_name": "t", "header": "Proxy-Authorization"}]), "invalid_secret_header"),
        (environment(secret_headers=[{"host": "api.example.com", "secret_name": "t", "query": "token"}]), "invalid_environment"),
        (environment(unknown_field=1), "invalid_environment"),
    ):
        with pytest.raises(InvalidError) as err:
            EnvironmentConfig.from_args(doc)
        assert err.value.code == code, (doc, err.value.code)


def test_setup_cache_key_is_script_content_plus_base_image_digest() -> None:
    key = setup_key("apt-get install -y jq\n", BASE_DIGEST)
    assert key == hashlib.sha256(b"agentruntime-setup-v1\x00" + hashlib.sha256(b"apt-get install -y jq\n").digest() + b"\x00" + BASE_DIGEST.encode()).hexdigest()
    assert setup_key("apt-get install -y jq\n", "sha256:" + "cd" * 32) != key
    assert setup_key("apt-get install -y yq\n", BASE_DIGEST) != key
    assert setup_key("apt-get install -y jq\n", BASE_DIGEST) == key


# --- 绑定 ---


async def test_workspace_create_binds_a_repo_independent_environment(tmp_path: Path) -> None:
    from agentruntime.common.v1 import tenant_pb2
    from agentruntime.workflow.v1 import workspace_pb2
    from google.protobuf import struct_pb2

    s = Setup(tmp_path)

    async def no_secret() -> bytes | None:
        return None

    ops = WorkspaceOperations(s.broker, secret=no_secret)
    for ws in ("w3", "w4"):
        args = struct_pb2.Struct()
        args.update({"environment": environment(secret_headers=[{"host": "api.example.com", "secret_name": "tok"}])})
        inp = workspace_pb2.WorkspaceOperationInput(
            tenant=tenant_pb2.TenantContext(tenant_id="t1", user_id="u1"), operation_id=f"op-{ws}", operation=workspace_pb2.WORKSPACE_OPERATION_CREATE,
            workspace_id=ws, args=args, requested_by="u1",
        )
        result = await ops.run(inp)
        assert not result.HasField("error"), result.error
        assert result.result["workspace"]["environment_id"] == "env-py"
        bound = await s.archiver.environment("t1", ws)
        assert bound is not None and bound.config.environment_id == "env-py" and bound.owner_user == "u1" and not bound.deleted
    # 一份环境配置用于两个工作区：各自一份快照（之后编辑环境只影响新建的工作区）。
    assert (await s.archiver.environment("t1", "w3")).config == (await s.archiver.environment("t1", "w4")).config  # type: ignore[union-attr]
    bad = struct_pb2.Struct()
    bad.update({"environment": environment(env="GITHUB_TOKEN=ghp_x")})
    inp = workspace_pb2.WorkspaceOperationInput(
        tenant=tenant_pb2.TenantContext(tenant_id="t1", user_id="u1"), operation_id="op-bad", operation=workspace_pb2.WORKSPACE_OPERATION_CREATE,
        workspace_id="w5", args=bad, requested_by="u1",
    )
    result = await ops.run(inp)
    assert result.error.code == "env_secret_name" and "ghp_x" not in result.error.message
    assert await s.archiver.get("t1", "w5") is None, "配置不对时不建工作区"


# --- setup 与 start ---


async def test_setup_then_start_then_command_once_per_environment(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    await s.bind()
    first = await s.shell(APPEND)
    assert first["exit_code"] == 0
    assert s.order() == ["setup", "start", "cmd"], "setup、start 在业务命令之前，各一次"
    assert first["environment_config"] == {"environment_id": "env-py", "setup": "succeeded", "start": "succeeded"}
    second = await s.shell(APPEND)
    assert s.order() == ["setup", "start", "cmd", "cmd"], "同一个环境复用时不重跑"
    assert "environment_config" not in second
    [record] = await s.ledger.active("t1")
    assert record.environment_id == "env-py" and record.setup_state == "succeeded" and record.start_state == "succeeded"
    assert record.setup_key == setup_key("echo setup >> order.txt\n", record.image or "") and record.setup_finished_at


async def test_dotenv_reaches_setup_start_and_commands_but_secret_values_do_not(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    await s.bind(doc=environment(
        setup_script='echo "setup:$GREETING" >> seen.txt\n', start_command='echo "start:$MODE" >> seen.txt\n',
        secret_headers=[{"host": "api.example.com", "secret_name": "example-token"}],
    ))
    out = await s.shell(["bash", "-c", 'echo "cmd:$GREETING"; env'])
    assert (s.workspaces / "t1" / "w1" / "seen.txt").read_text().splitlines() == ["setup:hello world", "start:a=b"]
    assert "cmd:hello world" in out["stdout"]
    assert "example-token" not in out["stdout"], "网络密钥的名字与值都不进命令的环境变量"


async def test_setup_failure_blocks_the_start_and_is_not_cached(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    await s.bind(doc=environment(setup_script="echo setup >> order.txt\necho 'apt failed' >&2\nexit 3\n"))
    with pytest.raises(InvalidError) as err:
        await s.shell(APPEND)
    assert err.value.code == "environment_setup_failed" and "3" in err.value.message and "apt failed" in err.value.message
    assert s.order() == ["setup"], "setup 失败：start 与业务命令都不执行"
    assert await s.ledger.active("t1") == [], "失败的环境不留着"
    records = await _all_records(s)
    assert [(r.state, r.setup_state, r.setup_error) for r in records] == [("destroyed", "failed", "environment_setup_failed")]
    # 失败不缓存：下一次照样重跑 setup（仍然失败）。
    with pytest.raises(InvalidError):
        await s.shell(APPEND)
    assert s.order() == ["setup", "setup"]


async def test_setup_timeout_blocks_the_start(tmp_path: Path) -> None:
    s = Setup(tmp_path, setup_timeout_s=1)
    await s.bind(doc=environment(setup_script="sleep 5\n"))
    with pytest.raises(InvalidError) as err:
        await s.shell(APPEND)
    assert err.value.code == "environment_setup_timeout"
    assert s.order() == []
    [record] = await _all_records(s)
    assert record.setup_state == "failed" and record.setup_error == "environment_setup_timeout"


async def test_start_failure_is_recorded_but_does_not_block(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    await s.bind(doc=environment(start_command="echo start >> order.txt\nexit 7\n"))
    out = await s.shell(APPEND)
    assert out["exit_code"] == 0 and s.order() == ["setup", "start", "cmd"]
    assert out["environment_config"]["start"] == "failed"
    [record] = await s.ledger.active("t1")
    assert record.setup_state == "succeeded" and record.start_state == "failed"


async def test_one_environment_used_for_two_workspaces_prepares_each(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    await s.bind("w1")
    await s.bind("w2")
    await s.shell(APPEND, workspace="w1")
    await s.shell(APPEND, workspace="w2", session=S2)
    assert s.order("w1") == ["setup", "start", "cmd"] and s.order("w2") == ["setup", "start", "cmd"]


async def test_workspaces_without_environment_keep_the_old_behaviour(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    out = await s.shell(APPEND)
    assert out["exit_code"] == 0 and s.order() == ["cmd"] and "environment_config" not in out
    [record] = await s.ledger.active("t1")
    assert record.setup_state == "none" and record.environment_id == ""


async def test_successful_setup_is_cached_by_script_and_base_image(tmp_path: Path) -> None:
    s = Setup(tmp_path, sessions=False, images=True)
    images = await s.wrap_images()
    await s.bind()
    await s.shell(APPEND)
    key = setup_key("echo setup >> order.txt\n", BASE_DIGEST)
    assert s.built == [("t1", key)], "setup 成功后按缓存键起一次构建"
    manager = s.broker.environments
    assert manager is not None
    # 构建好了（构建器与推送在 test_sandbox_environments 里测）：下一个新环境用缓存的镜像，不再跑 setup。
    await manager.store.mark_ready("t1", key, image="127.0.0.1:30500/envs/t1/env@sha256:" + "ee" * 32, digest="sha256:" + "ee" * 32, size_bytes=1, build_ms=1)
    out = await s.shell(APPEND)
    assert s.order() == ["setup", "start", "cmd", "start", "cmd"], "命中缓存：setup 不重跑，start 照跑"
    assert out["environment_config"]["setup"] == "cached"
    assert images.images[-1].endswith("sha256:" + "ee" * 32)
    # 脚本变了：缓存不中，重跑。
    await s.bind(doc=environment(setup_script="echo setup2 >> order.txt\n"))
    await s.shell(APPEND)
    assert s.order()[-3:] == ["setup2", "start", "cmd"]


async def test_failed_setup_never_starts_a_cache_build(tmp_path: Path) -> None:
    s = Setup(tmp_path, sessions=False, images=True)
    await s.wrap_images()
    await s.bind(doc=environment(setup_script="exit 1\n"))
    with pytest.raises(InvalidError):
        await s.shell(APPEND)
    assert s.built == [], "只缓存成功的结果"


# --- 删除 ---


async def test_deleted_environment_stops_new_environments_and_reconcile_cleans_up(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    await s.bind()
    await s.shell(APPEND)
    assert len(await s.ledger.active("t1")) == 1
    assert await s.archiver.delete_environment("t1", "env-py", owner_user="other-user") == 0, "只认选用它的用户"
    assert await s.archiver.delete_environment("t1", "env-py", owner_user="u1") == 1
    assert await s.archiver.delete_environment("t1", "env-py", owner_user="u1") == 0, "幂等"
    report = await reconcile_once(s.broker)
    assert report.reaped == 1 and await s.ledger.active("t1") == [], "已有的环境闲着时由对账销毁"
    [record] = await _all_records(s)
    assert "environment deleted" in record.reason
    with pytest.raises(DeniedError) as err:
        await s.shell(APPEND, session=S2)
    assert err.value.code == "environment_deleted"
    assert await s.archiver.get("t1", "w1") is not None, "不删工作区"


async def _all_records(s: Setup) -> list[Any]:
    from agentruntime_worker.sandbox.ledger import _COLUMNS, EnvRecord

    async with s.ledger.bind("t1").transaction(read_only=True) as tx:
        rows = await tx.query(f"SELECT {', '.join(_COLUMNS)} FROM sandbox_envs ORDER BY created_at")
    return [EnvRecord.from_row("t1", r) for r in rows]
