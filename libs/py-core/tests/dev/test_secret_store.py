"""开发实现：本地文件 SecretStore。"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest

from agentruntime_core.conformance.secret_store import SecretStoreConformance
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.errors import ErrorCategory, InvalidError
from agentruntime_core.tenancy import TenantContext
from agentruntime_core.testing import FakeClock, FaultInjector


class Harness:
    max_ttl = timedelta(hours=1)

    def __init__(self, path: Path) -> None:
        self.clock = FakeClock()
        self.faults = FaultInjector()
        self.provider = LocalFileSecretStoreProvider(path, max_ttl=self.max_ttl, clock=self.clock, faults=self.faults)

    async def advance(self, delta: timedelta) -> None:
        self.clock.advance(delta)
        await asyncio.sleep(0)

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)

    def seed_secret(self, tenant_id: str | None, name: str, value: str) -> None:
        self.provider.seed_secret(tenant_id, name, value)

    def grant_role(self, tenant_id: str | None, role: str) -> None:
        self.provider.grant_role(tenant_id, role)


class TestLocalFileSecretStore(SecretStoreConformance):
    @pytest.fixture
    def harness(self, tmp_path: Path) -> Harness:
        return Harness(tmp_path / "secrets.json")


# --- 开发实现专有：多个进程共用一份密钥文件（S-06：task-service 与 Worker 互相解开对方的 Claim-Check 数据密钥）---

T1 = TenantContext(tenant_id="t1")


async def test_a_kek_created_by_another_process_is_picked_up(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    early = LocalFileSecretStoreProvider(path)  # 先启动的进程，读文件时还没有 t1 的密钥加密密钥
    late = LocalFileSecretStoreProvider(path)
    key = await late.bind(T1).generate_data_key("claim_check")
    assert await early.bind(T1).unwrap_data_key(key.wrapped, purpose="claim_check") == key.plaintext, "缺密钥加密密钥时重新读文件"


async def test_concurrent_processes_never_overwrite_each_others_kek(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    a, b = LocalFileSecretStoreProvider(path), LocalFileSecretStoreProvider(path)
    key_a = await a.bind(T1).generate_data_key("claim_check")
    key_b = await b.bind(T1).generate_data_key("claim_check")  # b 读文件时没有 t1 的，生成前要重新读，沿用 a 的
    fresh = LocalFileSecretStoreProvider(path)
    assert await fresh.bind(T1).unwrap_data_key(key_a.wrapped, purpose="claim_check") == key_a.plaintext
    assert await fresh.bind(T1).unwrap_data_key(key_b.wrapped, purpose="claim_check") == key_b.plaintext


# --- 集群里（D-04）：静态密钥来自只读的种子文件，kek 写在共用的可写文件里 ---


async def test_merge_seed_keeps_generated_keks_and_refreshes_secrets(tmp_path: Path) -> None:
    path, seed = tmp_path / "store" / "secrets.json", tmp_path / "seed.json"
    seed.write_text(json.dumps({"version": 1, "scopes": {"_platform": {"secrets": {"model-provider/api-key": "old"}, "roles": ["r1"]}}}), "utf-8")
    LocalFileSecretStoreProvider(path).merge_seed(seed)
    key = await LocalFileSecretStoreProvider(path).bind(T1).generate_data_key("claim_check")

    # 轮换：种子里的值变了；kek 不能被种子换掉（种子里带了别的 kek 也不行）。
    seed.write_text(json.dumps({"version": 1, "scopes": {
        "_platform": {"secrets": {"model-provider/api-key": "new"}, "roles": ["r2"]},
        "t1": {"secrets": {}, "roles": [], "kek": "A" * 43 + "="},
    }}), "utf-8")
    LocalFileSecretStoreProvider(path).merge_seed(seed)
    fresh = LocalFileSecretStoreProvider(path)
    assert (await fresh.bind_platform("seed").read("model-provider/api-key")).reveal() == "new"
    assert await fresh.bind(T1).unwrap_data_key(key.wrapped, purpose="claim_check") == key.plaintext
    assert json.loads(path.read_text("utf-8"))["scopes"]["_platform"]["roles"] == ["r1", "r2"]
    with pytest.raises(InvalidError):
        LocalFileSecretStoreProvider(path).merge_seed(tmp_path / "missing.json")
