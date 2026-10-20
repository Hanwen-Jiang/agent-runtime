"""S-03 验收：心跳与超时（计划 S-03 难点“心跳”：heartbeat 超时短于 start-to-close 超时，Worker 崩溃后
activity 才能被及时接手），以及 Worker 进程的配置。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from agentruntime_worker.config import (
    ENV_CLAIM_CHECK_THRESHOLD,
    ENV_DATA_DIR,
    ENV_OBJECT_STORE_DIR,
    ENV_SECRETS_FILE,
    ENV_STUB_CALL_LOG,
    ENV_STUB_GATE_DIR,
    ENV_STUB_NODE_TIMEOUT_S,
    ENV_TEMPORAL_ADDRESS,
    ENV_TEMPORAL_NAMESPACE,
    WorkerConfig,
)
from agentruntime_worker.workflows import timeouts


@pytest.mark.parametrize("timeout_s", [1, 1.5, 2, 3, 4, 10, 20, 40, 60, 600, 3600, 86400])
def test_heartbeat_is_shorter_than_start_to_close(timeout_s: float) -> None:
    start_to_close, heartbeat = timeouts.model_node_timeouts(timeout_s)
    assert start_to_close == timedelta(seconds=timeout_s)
    assert timedelta(0) < heartbeat < start_to_close
    assert heartbeat <= timeouts.MAX_HEARTBEAT


def test_heartbeat_detects_a_crash_within_a_few_seconds_for_short_nodes() -> None:
    # 端到端测试把桩节点的 timeout_s 设为 40，Worker 被杀后要在几秒内被接手。
    _, heartbeat = timeouts.model_node_timeouts(40)
    assert heartbeat <= timedelta(seconds=3)


def test_missing_node_timeout_uses_the_default() -> None:
    start_to_close, heartbeat = timeouts.model_node_timeouts(None)
    assert start_to_close == timeouts.DEFAULT_NODE_TIMEOUT
    assert timedelta(0) < heartbeat < start_to_close


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_node_timeout_is_rejected(bad: float) -> None:
    with pytest.raises(ValueError):
        timeouts.model_node_timeouts(bad)


def test_decision_and_finalize_timeouts_are_positive() -> None:
    assert timeouts.DECISION_START_TO_CLOSE > timedelta(0)
    assert timeouts.FINALIZE_START_TO_CLOSE > timedelta(0)


def test_config_defaults() -> None:
    config = WorkerConfig.from_env({})
    assert config.temporal_address == "localhost:7233"
    assert config.namespace == "default"
    assert config.call_log is None and config.gate_dir is None
    assert config.stub_node_timeout_s == 600


def test_config_from_env(tmp_path: Path) -> None:
    config = WorkerConfig.from_env(
        {
            ENV_TEMPORAL_ADDRESS: "127.0.0.1:7999",
            ENV_TEMPORAL_NAMESPACE: "ns1",
            ENV_STUB_CALL_LOG: str(tmp_path / "calls.jsonl"),
            ENV_STUB_GATE_DIR: str(tmp_path / "gates"),
            ENV_STUB_NODE_TIMEOUT_S: "40",
            ENV_DATA_DIR: str(tmp_path / "data"),
        }
    )
    assert config.data_dir == tmp_path / "data"
    assert config.temporal_address == "127.0.0.1:7999"
    assert config.namespace == "ns1"
    assert config.call_log == tmp_path / "calls.jsonl"
    assert config.gate_dir == tmp_path / "gates"
    assert config.stub_node_timeout_s == 40


@pytest.mark.parametrize("value", ["0", "-5", "abc", "1.5"])
def test_config_rejects_bad_node_timeout(value: str) -> None:
    with pytest.raises(ValueError):
        WorkerConfig.from_env({ENV_STUB_NODE_TIMEOUT_S: value})


# --- S-06：Claim-Check 用的 ObjectStore、SecretStore 与阈值 ---


def test_claim_check_config_defaults(tmp_path: Path) -> None:
    config = WorkerConfig.from_env({ENV_DATA_DIR: str(tmp_path / "data")})
    assert config.claim_check_threshold == 64 * 1024
    assert config.object_store_dir == tmp_path / "data" / "objects", "没单独配置时放在数据目录下"
    assert config.secrets_file == tmp_path / "data" / "secrets.json"


def test_claim_check_config_from_env(tmp_path: Path) -> None:
    config = WorkerConfig.from_env(
        {
            ENV_DATA_DIR: str(tmp_path / "data"),
            ENV_OBJECT_STORE_DIR: str(tmp_path / "shared" / "objects"),
            ENV_SECRETS_FILE: str(tmp_path / "shared" / "secrets.json"),
            ENV_CLAIM_CHECK_THRESHOLD: "1024",
        }
    )
    assert config.object_store_dir == tmp_path / "shared" / "objects"
    assert config.secrets_file == tmp_path / "shared" / "secrets.json"
    assert config.claim_check_threshold == 1024


@pytest.mark.parametrize("value", ["0", "-1", "abc", "2.5"])
def test_config_rejects_bad_claim_check_threshold(value: str) -> None:
    with pytest.raises(ValueError):
        WorkerConfig.from_env({ENV_CLAIM_CHECK_THRESHOLD: value})
