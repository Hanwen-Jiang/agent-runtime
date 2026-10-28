"""凭据封装 wsc1 的格式（docs/plan/workspace-layer.md 第 5 节；D-06）：task-service 封（Go 的 libs/go-core/sealing），Worker 解
（Python 的 agentruntime_core.sealing）。这里按向量（sealed_credential_vectors.json）校验 Python；Go 在 go test 里按同一份
向量校验（sealing_test.go）。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from agentruntime_core.errors import DeniedError, InvalidError
from agentruntime_core.sealing import open_sealed, seal

VECTORS = json.loads((Path(__file__).parent / "sealed_credential_vectors.json").read_text("utf-8"))
BY_NAME = {c["name"]: c for c in VECTORS["valid"]}


@pytest.mark.parametrize("case", VECTORS["valid"], ids=[c["name"] for c in VECTORS["valid"]])
def test_valid_vectors_seal_and_open(case: dict) -> None:
    key = case["key"].encode()
    sealed = seal(key, case["tenant_id"], case["operation_id"], case["plaintext"].encode(), nonce=bytes.fromhex(case["nonce_hex"]))
    assert sealed == case["sealed"]
    assert open_sealed(key, case["tenant_id"], case["operation_id"], case["sealed"]).decode() == case["plaintext"]


def _tampered(case: dict) -> tuple[bytes, str, str, str]:
    base = BY_NAME[case["from"]]
    sealed = case.get("sealed", base["sealed"])
    if "flip" in case:
        body = sealed[len("wsc1."):]
        raw = bytearray(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        raw[case["flip"]] ^= 0x01
        sealed = "wsc1." + base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode()
    return case.get("key", base["key"]).encode(), case.get("tenant_id", base["tenant_id"]), case.get("operation_id", base["operation_id"]), sealed


@pytest.mark.parametrize("case", VECTORS["tampered"], ids=[c["name"] for c in VECTORS["tampered"]])
def test_tampered_or_moved_credentials_are_denied_without_saying_why(case: dict) -> None:
    key, tenant, operation, sealed = _tampered(case)
    with pytest.raises(DeniedError) as info:
        open_sealed(key, tenant, operation, sealed)
    assert info.value.code == "invalid_sealed" and BY_NAME[case["from"]]["plaintext"] not in str(info.value)


def test_random_nonces_differ_and_weak_keys_are_refused() -> None:
    key = b"k" * 32
    assert seal(key, "t1", "wso-1", b"x") != seal(key, "t1", "wso-1", b"x")
    with pytest.raises(InvalidError):
        seal(b"short", "t1", "wso-1", b"x")
    with pytest.raises(InvalidError):
        seal(key, "t1", "wso-1", b"x", nonce=b"123")
