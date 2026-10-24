"""故障注入与恢复套件（计划 S-07）的 fixture。

整个目录共用端到端环境（tests/e2e/conftest.py 的 stack）。每个用例开始前把 Worker 换成故障 Worker
（e2e_support.fault_worker），目录之外的用例由 tests/e2e/conftest.py 换回标准 Worker，与用例的先后顺序无关。
每个用例开始与结束时清空故障规则，并把环境恢复成健康状态：Worker 活着，task-service 用默认配置运行。
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from e2e_support.api import TaskApi
from e2e_support.fake_openai import FakeOpenAI
from e2e_support.fault_gateway import FAKE_KEY, FAULT_PROFILES
from e2e_support.gateway import DEFAULT_KEY_NAME, Gateway, build_gateway, dev_secret
from e2e_support.stack import WRITE_TENANT, Stack


@pytest.fixture(scope="session")
def fstack(stack: Stack) -> Stack:
    return stack


@pytest.fixture(scope="session")
def fault_upstream() -> Iterator[FakeOpenAI]:
    upstream = FakeOpenAI()
    upstream.start()
    try:
        yield upstream
    finally:
        upstream.stop()


@pytest.fixture(scope="session")
def fault_gateway(tmp_path_factory: pytest.TempPathFactory, fault_upstream: FakeOpenAI) -> Iterator[Gateway]:
    root = tmp_path_factory.mktemp("fault-gateway")
    binary = build_gateway(root)
    secrets = root / "secrets.json"
    assert dev_secret(binary, secrets, DEFAULT_KEY_NAME, FAKE_KEY).returncode == 0
    profiles = root / "profiles.json"
    profiles.write_text(json.dumps(FAULT_PROFILES), "utf-8")
    gateway = Gateway(binary=binary, root=root / "serve", profiles_file=profiles, secrets_file=secrets, base_url=fault_upstream.base_url, secrets=[FAKE_KEY])
    gateway.start()
    try:
        yield gateway
    finally:
        gateway.stop()


@pytest.fixture
def gateway_fstack(fstack: Stack, fault_gateway: Gateway, fault_upstream: FakeOpenAI, monkeypatch: pytest.MonkeyPatch) -> Iterator[Stack]:
    """故障 Worker 设了 AR_MODEL_GATEWAY_ADDRESS：model 节点经真 Model Gateway（GrpcModelGateway）而不是替身。用例结束换回替身。"""
    fault_upstream.reset()
    monkeypatch.setenv("AR_MODEL_GATEWAY_ADDRESS", fault_gateway.address)
    fstack.restart_worker()
    try:
        yield fstack
    finally:
        fault_upstream.reset()
        monkeypatch.delenv("AR_MODEL_GATEWAY_ADDRESS", raising=False)
        fstack.restart_worker()


@pytest.fixture(autouse=True)
def _healthy(request: pytest.FixtureRequest) -> Iterator[None]:
    if "fstack" not in request.fixturenames:
        yield
        return
    s: Stack = request.getfixturevalue("fstack")
    s.ensure_healthy()
    s.use_worker("fault")
    yield
    s.ensure_healthy()


@pytest.fixture(scope="session")
def writer(fstack: Stack) -> TaskApi:
    """允许写操作的租户（effect 上限 write_irreversible）。"""
    return TaskApi(fstack.http_url, fstack.token(WRITE_TENANT, "uw"))
