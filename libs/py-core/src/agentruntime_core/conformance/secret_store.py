"""SecretStore 的一致性测试。语义见 agentruntime_core.providers.secret_store。"""

from __future__ import annotations

import base64
import json
import logging
import pickle
from datetime import timedelta
from typing import Protocol

import pytest

from agentruntime_core.conformance.common import (
    TENANT_A_ID,
    TENANT_B_ID,
    Harness,
    StandardRules,
    expect_error,
    tenant_a,
    tenant_b,
)
from agentruntime_core.errors import ErrorCategory, NotFoundError, ProviderError
from agentruntime_core.providers.secret_store import LeaseStatus, SecretStore, SecretStoreProvider
from agentruntime_core.secrets import MASK

# harness 必须把这个角色授予租户 A、租户 B 与平台作用域。
PROBE_ROLE = "conformance-reader"
PLATFORM_SECRET_VALUE = "platform-db-password-7Q2"
TENANT_SECRET_VALUE = "tenant-byok-key-9X4"


class SecretStoreHarness(Harness[SecretStoreProvider], Protocol):
    @property
    def max_ttl(self) -> timedelta:
        """实现允许的最长凭据有效期。"""
        ...

    def seed_secret(self, tenant_id: str | None, name: str, value: str) -> None:
        """写入静态密钥；tenant_id 为 None 表示平台作用域。"""
        ...

    def grant_role(self, tenant_id: str | None, role: str) -> None:
        """把签发凭据的角色授予作用域；tenant_id 为 None 表示平台作用域。"""
        ...


def _forms(raw: str | bytes) -> list[str]:
    """明文可能被打印出来的几种形式。"""
    data = raw.encode() if isinstance(raw, str) else raw
    forms = [data.hex(), base64.b64encode(data).decode()]
    if isinstance(raw, str):
        forms.append(raw)
    return forms


class SecretStoreConformance(StandardRules):
    probe_operation = "issue_credential"

    @pytest.fixture(autouse=True)
    def _seed(self, harness: SecretStoreHarness) -> None:
        for tenant_id in (TENANT_A_ID, TENANT_B_ID, None):
            harness.grant_role(tenant_id, PROBE_ROLE)
        harness.seed_secret(None, "db-password", PLATFORM_SECRET_VALUE)
        harness.seed_secret(TENANT_A_ID, "model-api-key", TENANT_SECRET_VALUE)

    # pytest 为每个测试新建实例，探针名 → lease_id 的映射不会跨测试。
    def _probe_leases(self) -> dict[str, str]:
        return self.__dict__.setdefault("_leases", {})

    async def write_probe(self, client: SecretStore, name: str) -> None:
        credential = await client.issue_credential(PROBE_ROLE, ttl=timedelta(minutes=5))
        self._probe_leases()[name] = credential.lease_id

    async def probe_visible(self, client: SecretStore, name: str) -> bool:
        try:
            await client.lease_status(self._probe_leases()[name])
        except NotFoundError:
            return False
        return True

    @pytest.fixture
    def secrets(self, harness: SecretStoreHarness) -> SecretStore:
        return harness.provider.bind(tenant_a())

    # --- 静态密钥 ---

    async def test_read_returns_secret_of_own_scope(self, harness: SecretStoreHarness, secrets: SecretStore) -> None:
        assert (await secrets.read("model-api-key")).reveal() == TENANT_SECRET_VALUE
        platform = harness.provider.bind_platform("conformance")
        assert (await platform.read("db-password")).reveal() == PLATFORM_SECRET_VALUE

    async def test_read_is_isolated_between_scopes(self, harness: SecretStoreHarness, secrets: SecretStore) -> None:
        b = harness.provider.bind(tenant_b())
        with expect_error(ErrorCategory.NOT_FOUND):
            await b.read("model-api-key")
        with expect_error(ErrorCategory.NOT_FOUND):
            await secrets.read("db-password")  # 租户读不到基础设施密钥

    async def test_read_missing_is_not_found(self, secrets: SecretStore) -> None:
        with expect_error(ErrorCategory.NOT_FOUND):
            await secrets.read("missing")

    # --- 短期凭据 ---

    async def test_issue_credential_is_active_and_short_lived(self, harness: SecretStoreHarness, secrets: SecretStore) -> None:
        credential = await secrets.issue_credential(PROBE_ROLE, ttl=timedelta(minutes=5))
        assert credential.role == PROBE_ROLE
        assert credential.secret.reveal()
        assert await secrets.lease_status(credential.lease_id) is LeaseStatus.ACTIVE
        other = await secrets.issue_credential(PROBE_ROLE, ttl=timedelta(minutes=5))
        assert other.lease_id != credential.lease_id
        assert other.secret != credential.secret

    async def test_credential_expires(self, harness: SecretStoreHarness, secrets: SecretStore) -> None:
        credential = await secrets.issue_credential(PROBE_ROLE, ttl=timedelta(seconds=60))
        await harness.advance(timedelta(seconds=61))
        assert await secrets.lease_status(credential.lease_id) is LeaseStatus.EXPIRED

    async def test_revoke_is_immediate_and_repeatable(self, secrets: SecretStore) -> None:
        credential = await secrets.issue_credential(PROBE_ROLE, ttl=timedelta(minutes=5))
        await secrets.revoke(credential.lease_id)
        assert await secrets.lease_status(credential.lease_id) is LeaseStatus.REVOKED
        await secrets.revoke(credential.lease_id)

    async def test_revoke_unknown_lease_is_not_found(self, secrets: SecretStore) -> None:
        with expect_error(ErrorCategory.NOT_FOUND):
            await secrets.revoke("lease-does-not-exist")

    async def test_other_tenant_cannot_revoke(self, harness: SecretStoreHarness, secrets: SecretStore) -> None:
        credential = await secrets.issue_credential(PROBE_ROLE, ttl=timedelta(minutes=5))
        b = harness.provider.bind(tenant_b())
        with expect_error(ErrorCategory.NOT_FOUND):
            await b.revoke(credential.lease_id)
        assert await secrets.lease_status(credential.lease_id) is LeaseStatus.ACTIVE

    async def test_role_not_granted_is_denied(self, secrets: SecretStore) -> None:
        with expect_error(ErrorCategory.DENIED):
            await secrets.issue_credential("crm-admin", ttl=timedelta(minutes=5))

    async def test_ttl_must_be_positive_and_bounded(self, harness: SecretStoreHarness, secrets: SecretStore) -> None:
        with expect_error(ErrorCategory.INVALID):
            await secrets.issue_credential(PROBE_ROLE, ttl=timedelta(0))
        with expect_error(ErrorCategory.INVALID):
            await secrets.issue_credential(PROBE_ROLE, ttl=harness.max_ttl + timedelta(seconds=1))

    # --- 数据密钥 ---

    async def test_data_key_roundtrip(self, secrets: SecretStore) -> None:
        key = await secrets.generate_data_key("claim_check")
        plaintext = key.plaintext.reveal()
        assert isinstance(plaintext, bytes) and len(plaintext) == 32
        assert (await secrets.unwrap_data_key(key.wrapped, purpose="claim_check")).reveal() == plaintext
        other = await secrets.generate_data_key("claim_check")
        assert other.plaintext != key.plaintext
        assert other.key_id != key.key_id or other.wrapped != key.wrapped

    async def test_other_tenant_cannot_unwrap(self, harness: SecretStoreHarness, secrets: SecretStore) -> None:
        key = await secrets.generate_data_key("claim_check")
        b = harness.provider.bind(tenant_b())
        with expect_error(ErrorCategory.DENIED):
            await b.unwrap_data_key(key.wrapped, purpose="claim_check")

    async def test_unwrap_with_other_purpose_is_denied(self, secrets: SecretStore) -> None:
        key = await secrets.generate_data_key("claim_check")
        with expect_error(ErrorCategory.DENIED):
            await secrets.unwrap_data_key(key.wrapped, purpose="vector_encryption")

    async def test_corrupted_wrapped_key_is_invalid(self, secrets: SecretStore) -> None:
        key = await secrets.generate_data_key("claim_check")
        corrupted = bytes(b ^ 0x5A for b in key.wrapped)
        with expect_error(ErrorCategory.INVALID):
            await secrets.unwrap_data_key(corrupted, purpose="claim_check")

    # --- 明文不外泄 ---

    async def test_secrets_never_render_in_plain_text(self, secrets: SecretStore, caplog: pytest.LogCaptureFixture) -> None:
        credential = await secrets.issue_credential(PROBE_ROLE, ttl=timedelta(minutes=5))
        key = await secrets.generate_data_key("claim_check")
        static = await secrets.read("model-api-key")
        with caplog.at_level(logging.INFO):
            logging.getLogger("conformance").info("credential=%s key=%r static=%s", credential, key, static)
        rendered = [
            str(static), repr(static), f"{static}", f"{static!r}", f"{static:>20}",
            repr(credential), str(credential), repr(key), str(key), caplog.text,
            json.dumps({"v": str(static)}),
        ]
        plain = _forms(credential.secret.reveal()) + _forms(key.plaintext.reveal()) + _forms(static.reveal())
        for text in rendered:
            for form in plain:
                assert form not in text, "明文出现在了打印结果里"
        assert MASK in str(static)

    async def test_secrets_cannot_be_serialized(self, secrets: SecretStore) -> None:
        static = await secrets.read("model-api-key")
        with pytest.raises(TypeError):
            pickle.dumps(static)
        with pytest.raises(TypeError):
            json.dumps({"v": static})

    async def test_errors_do_not_echo_secret_material(self, secrets: SecretStore) -> None:
        key = await secrets.generate_data_key("claim_check")
        failures: list[ProviderError] = []
        for wrapped, purpose in [(bytes(b ^ 0x5A for b in key.wrapped), "claim_check"), (key.wrapped, "other")]:
            try:
                await secrets.unwrap_data_key(wrapped, purpose=purpose)
            except ProviderError as error:
                failures.append(error)
        assert len(failures) == 2
        plain = _forms(key.plaintext.reveal()) + _forms(TENANT_SECRET_VALUE) + _forms(key.wrapped)
        for error in failures:
            for form in plain:
                assert form not in str(error) and form not in repr(error)
