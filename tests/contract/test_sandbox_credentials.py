"""沙箱环境凭据的格式（docs/plan/sandbox-platform.md 第 3 节；D-15、D-16）：Python 的 agentruntime_core.envcred 按向量
（sandbox_credential_vectors.json）校验；Go 的 libs/go-core/envcred 在 go test 里按同一份向量校验（envcred_test.go）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentruntime_core.envcred import Claims, issue, verify
from agentruntime_core.errors import DeniedError, InvalidError

VECTORS = json.loads((Path(__file__).parent / "sandbox_credential_vectors.json").read_text("utf-8"))
KEY = bytes.fromhex(VECTORS["key_hex"])
NOW = datetime.fromtimestamp(VECTORS["now_unix"], UTC)


@pytest.mark.parametrize("case", VECTORS["valid"], ids=[c["name"] for c in VECTORS["valid"]])
def test_valid_vectors_verify_to_their_claims(case: dict) -> None:
    claims = verify(KEY, case["token"], purpose=case["purpose"], now=NOW)
    assert claims.payload() == case["claims"]


@pytest.mark.parametrize("case", VECTORS["invalid"], ids=[c["name"] for c in VECTORS["invalid"]])
def test_invalid_vectors_are_denied_without_saying_why(case: dict) -> None:
    with pytest.raises(DeniedError) as info:
        verify(KEY, case["token"], purpose=case["purpose"], now=NOW)
    assert info.value.code == "invalid_credential" and case["token"] not in str(info.value)


def test_another_key_cannot_verify() -> None:
    with pytest.raises(DeniedError):
        verify(b"x" * 32, VECTORS["valid"][0]["token"], purpose="mirror", now=NOW)


def test_issue_refuses_weak_keys_and_bad_claims() -> None:
    claims = Claims(env_ref="sbe-0123456789abcdef", tenant_id="t-a", expires_at=VECTORS["now_unix"] + 60, purpose="mirror")
    with pytest.raises(InvalidError):
        issue(b"short", claims)
    for bad in (Claims(env_ref="x", tenant_id="t-a", expires_at=1, purpose="mirror"), Claims(env_ref=claims.env_ref, tenant_id="T A", expires_at=1, purpose="mirror"),
                Claims(env_ref=claims.env_ref, tenant_id="t-a", expires_at=1, purpose="anything")):
        with pytest.raises(InvalidError):
            issue(KEY, bad)
