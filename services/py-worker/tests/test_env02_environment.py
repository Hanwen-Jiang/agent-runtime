"""ENV-02：云环境配置的执行链（人工验收 Q16–37；docs/plan/env-02-implementation-2026-11-03.md）。

Runtime 一侧的验收（SBX-01 起准备环境在 Sandbox Service 里，这里对着真的服务进程测 Worker 一侧的用法）：
1. 环境与仓库无关：同一份环境配置绑定到两个工作区，各自准备；配置按（租户，工作区）快照保存，只有密钥的名字。
2. 新的沙箱环境建好、工作区就位之后，先跑 setup、再跑 start，之后才执行业务命令；同一个环境复用时不重跑。
3. setup 的缓存键是 sha256（脚本内容 + 基础镜像 digest）；只缓存成功的结果（经环境镜像）——缓存在服务里，由
   services/go-sandbox-service 的 TestSuccessfulSetupIsCachedByScriptAndBaseImage、TestFailedSetupNeverStartsACacheBuild 覆盖。
4. setup 失败或超时：这个环境不启动（业务命令不执行）、错误码稳定（environment_setup_failed / environment_setup_timeout），
   环境表记下失败并销毁环境。
5. start 只启动交互运行时：失败只记录，不阻止业务命令。
6. .env：一行一个 KEY=value，交给 setup、start 与每条命令；有个数与字节上限；不能盖掉平台变量；像机密的名字被拒。
   网络密钥只有名字，值不进沙箱的环境变量。
7. 删除环境配置：选用它的工作区不再准备新的环境（environment_deleted），已有的环境不因此销毁。
8. 环境表是状态的权威：setup、start 的结果记在 sandbox_envs 的行上，不从日志推断。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agentruntime.workflow.v1 import workspace_pb2
from agentruntime_core.errors import DeniedError, InvalidError

from agentruntime_worker.sandbox.env_config import EnvironmentConfig, parse_dotenv, setup_key

from .service_setup import S1, S2, Setup

BASE_DIGEST = "sha256:" + "ab" * 32
CA_BUNDLE = Path("/tmp/agentruntime-ca.pem")  # 服务的 broker.CABundle（本地后端不隔离，就是本机的这个文件）
APPEND = ["bash", "-c", "echo cmd >> order.txt"]


def environment(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "environment_id": "env-py",
        "setup_script": "echo setup >> order.txt\n",
        "start_command": "echo start >> order.txt\n",
        "env": "# 普通变量\nGREETING=hello world\nMODE=a=b\n",
    }
    doc.update(overrides)
    return doc


def setup(tmp_path: Path, *, sessions: bool = True, env: Mapping[str, str] | None = None, secrets: Mapping[str, Any] | None = None) -> Setup:
    s = Setup(tmp_path, sessions=sessions, env=env, secrets=secrets)
    for ws in ("w1", "w2"):
        (s.host / "t1" / ws).mkdir(parents=True, exist_ok=True)
    return s


async def bind(s: Setup, workspace: str = "w1", doc: Mapping[str, Any] | None = None, *, user: str = "u1") -> EnvironmentConfig:
    config = EnvironmentConfig.from_args(dict(doc if doc is not None else environment()))
    await s.archiver.ensure("t1", workspace, (await s.policies.policy_for("t1")).workspace)
    await s.archiver.set_environment("t1", workspace, config, owner_user=user)
    return config


def _limits() -> Any:
    from agentruntime_worker.sandbox.policy import WorkspaceLimits

    return WorkspaceLimits()


async def shell(s: Setup, argv: list[str], *, session: str = S1, workspace: str = "w1") -> dict[str, Any]:
    return await s.call("shell_run", {"argv": argv}, session=session, workspace=workspace, task_id="tsk_env02")


def order(s: Setup, workspace: str = "w1") -> list[str]:
    path = s.host / "t1" / workspace / "order.txt"
    return path.read_text().split() if path.exists() else []


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
    s = setup(tmp_path)
    for ws in ("w3", "w4"):
        out = await s.run(workspace_pb2.WORKSPACE_OPERATION_CREATE, workspace=ws,
                          environment=environment(secret_headers=[{"host": "api.example.com", "secret_name": "tok"}]))
        assert out["workspace"]["environment_id"] == "env-py"
        bound = await s.archiver.environment("t1", ws)
        assert bound is not None and bound.config.environment_id == "env-py" and bound.owner_user == "u-alice" and not bound.deleted
    # 一份环境配置用于两个工作区：各自一份快照（之后编辑环境只影响新建的工作区）。
    assert (await s.archiver.environment("t1", "w3")).config == (await s.archiver.environment("t1", "w4")).config  # type: ignore[union-attr]
    err = await s.error(workspace_pb2.WORKSPACE_OPERATION_CREATE, workspace="w5", environment=environment(env="GITHUB_TOKEN=ghp_x"))
    assert err.code == "env_secret_name" and "ghp_x" not in err.message
    assert await s.archiver.get("t1", "w5") is None, "配置不对时不建工作区"


# --- setup 与 start ---


async def test_setup_then_start_then_command_once_per_environment(tmp_path: Path) -> None:
    s = setup(tmp_path)
    await bind(s)
    first = await shell(s, APPEND)
    assert first["exit_code"] == 0
    assert order(s) == ["setup", "start", "cmd"], "setup、start 在业务命令之前，各一次"
    assert first["environment_config"] == {"environment_id": "env-py", "setup": "succeeded", "start": "succeeded"}
    second = await shell(s, APPEND)
    assert order(s) == ["setup", "start", "cmd", "cmd"], "同一个环境复用时不重跑"
    assert "environment_config" not in second
    [record] = s.envs("t1")
    assert record["environment_id"] == "env-py" and record["setup_state"] == "succeeded" and record["start_state"] == "succeeded"
    assert record["setup_key"] == setup_key("echo setup >> order.txt\n", record["image"] or "") and record["setup_finished_at"]


async def test_dotenv_reaches_setup_start_and_commands_but_secret_values_do_not(tmp_path: Path) -> None:
    s = setup(tmp_path)
    await bind(s, doc=environment(
        setup_script='echo "setup:$GREETING" >> seen.txt\n', start_command='echo "start:$MODE" >> seen.txt\n',
        secret_headers=[{"host": "api.example.com", "secret_name": "example-token"}],
    ))
    out = await shell(s, ["bash", "-c", 'echo "cmd:$GREETING"; env'])
    assert (s.host / "t1" / "w1" / "seen.txt").read_text().splitlines() == ["setup:hello world", "start:a=b"]
    assert "cmd:hello world" in out["stdout"]
    assert "example-token" not in out["stdout"], "网络密钥的名字与值都不进命令的环境变量"


async def test_setup_failure_blocks_the_start_and_is_not_cached(tmp_path: Path) -> None:
    s = setup(tmp_path)
    await bind(s, doc=environment(setup_script="echo setup >> order.txt\necho 'apt failed' >&2\nexit 3\n"))
    with pytest.raises(InvalidError) as err:
        await shell(s, APPEND)
    assert err.value.code == "environment_setup_failed" and "3" in err.value.message and "apt failed" in err.value.message
    assert order(s) == ["setup"], "setup 失败：start 与业务命令都不执行"
    assert s.envs("t1") == [], "失败的环境不留着"
    assert [(r["state"], r["setup_state"], r["setup_error"]) for r in s.envs("t1", active=False)] == [("destroyed", "failed", "environment_setup_failed")]
    # 失败不缓存：下一次照样重跑 setup（仍然失败）。
    with pytest.raises(InvalidError):
        await shell(s, APPEND)
    assert order(s) == ["setup", "setup"]


async def test_setup_timeout_blocks_the_start(tmp_path: Path) -> None:
    s = setup(tmp_path, env={"AR_SANDBOX_SETUP_TIMEOUT_S": "1"})
    await bind(s, doc=environment(setup_script="sleep 5\n"))
    with pytest.raises(InvalidError) as err:
        await shell(s, APPEND)
    assert err.value.code == "environment_setup_timeout"
    assert order(s) == []
    [record] = s.envs("t1", active=False)
    assert record["setup_state"] == "failed" and record["setup_error"] == "environment_setup_timeout"


async def test_start_failure_is_recorded_but_does_not_block(tmp_path: Path) -> None:
    s = setup(tmp_path)
    await bind(s, doc=environment(start_command="echo start >> order.txt\nexit 7\n"))
    out = await shell(s, APPEND)
    assert out["exit_code"] == 0 and order(s) == ["setup", "start", "cmd"]
    assert out["environment_config"]["start"] == "failed"
    [record] = s.envs("t1")
    assert record["setup_state"] == "succeeded" and record["start_state"] == "failed"


async def test_one_environment_used_for_two_workspaces_prepares_each(tmp_path: Path) -> None:
    s = setup(tmp_path)
    await bind(s, "w1")
    await bind(s, "w2")
    await shell(s, APPEND, workspace="w1")
    await shell(s, APPEND, workspace="w2", session=S2)
    assert order(s, "w1") == ["setup", "start", "cmd"] and order(s, "w2") == ["setup", "start", "cmd"]


async def test_workspaces_without_environment_keep_the_old_behaviour(tmp_path: Path) -> None:
    s = setup(tmp_path)
    out = await shell(s, APPEND)
    assert out["exit_code"] == 0 and order(s) == ["cmd"] and "environment_config" not in out
    [record] = s.envs("t1")
    assert record["setup_state"] == "none" and record["environment_id"] == ""


# --- 删除 ---


async def test_deleting_the_config_keeps_the_session_snapshot_but_drops_its_secrets(tmp_path: Path) -> None:
    # 2026-11-03 用户定：还有会话在用时也可以删；会话保留建会话时的非机密快照，机密部分（.env、网络密钥）立即删掉。
    s = setup(tmp_path, sessions=False)
    await bind(s, doc=environment(
        setup_script='echo "setup:${GREETING:-none}" >> order.txt\n', start_command="",
        secret_headers=[{"host": "api.example.com", "secret_name": "example-token"}],
    ))
    await shell(s, ["bash", "-c", 'echo "cmd:${GREETING:-none}" >> order.txt'])
    assert order(s) == ["setup:hello", "world", "cmd:hello", "world"]
    assert await s.archiver.delete_environment("t1", "env-py", owner_user="other-user") == 0, "只认选用它的用户"
    assert await s.archiver.delete_environment("t1", "env-py", owner_user="u1") == 1
    assert await s.archiver.delete_environment("t1", "env-py", owner_user="u1") == 0, "幂等"
    bound = await s.archiver.environment("t1", "w1")
    assert bound is not None and bound.deleted and bound.config.setup_script and bound.config.env == "" and bound.config.secret_headers == ()
    # 会话照常：新的环境照样按快照跑 setup，只是没有了 .env 的变量。
    out = await shell(s, ["bash", "-c", 'echo "cmd:${GREETING:-none}" >> order.txt'])
    assert out["exit_code"] == 0 and order(s)[-2:] == ["setup:none", "cmd:none"]
    # 配置删除之后再交接：只收非机密的快照，带机密就拒绝。
    with pytest.raises(DeniedError) as err:
        await bind(s)
    assert err.value.code == "environment_deleted"
    await bind(s, doc=environment(env="", secret_headers=[]))
    assert (await s.archiver.environment("t1", "w1")).deleted  # type: ignore[union-attr]
    assert await s.archiver.get("t1", "w1") is not None, "不删工作区"


async def test_secret_headers_make_the_sandbox_trust_the_gateway_ca_without_any_value(tmp_path: Path) -> None:
    cert = "-----BEGIN CERTIFICATE-----\nGATEWAY-CA\n-----END CERTIFICATE-----\n"
    s = setup(tmp_path, env={"AR_EGRESS_GATEWAY_URL": "http://egress.test:8095"}, secrets={"_platform": {"sandbox/egress-ca-cert": cert}})
    await bind(s, doc=environment(secret_headers=[{"host": "api.example.com", "secret_name": "example-token"}]))
    out = await shell(s, ["bash", "-c", 'echo "$SSL_CERT_FILE $REQUESTS_CA_BUNDLE $NODE_EXTRA_CA_CERTS"'])
    assert out["stdout"].split() == [str(CA_BUNDLE)] * 3
    assert "GATEWAY-CA" in CA_BUNDLE.read_text(), "证书包里有网关 CA（只有证书）"
    assert "example-token" not in out["stdout"]


async def test_set_workspace_environment_binds_after_creation_and_clears(tmp_path: Path) -> None:
    s = setup(tmp_path)
    await s.archiver.ensure("t1", "w1", _limits())
    result = await s.run(workspace_pb2.WORKSPACE_OPERATION_SET_ENVIRONMENT, environment=environment())
    assert result["workspace"]["environment_id"] == "env-py"
    await shell(s, APPEND)
    assert order(s) == ["setup", "start", "cmd"]
    cleared = await s.run(workspace_pb2.WORKSPACE_OPERATION_SET_ENVIRONMENT)
    assert cleared["workspace"].get("environment_id", "") == "" and await s.archiver.environment("t1", "w1") is None
    assert (await s.error(workspace_pb2.WORKSPACE_OPERATION_SET_ENVIRONMENT, workspace="w-missing", environment=environment())).code == "workspace_not_found"
