"""把只用一次的凭据封起来，跨服务交接时不以明文进事件历史（docs/plan/cloud-deploy.md D-06）。

task-service 的 WorkspaceService 封（Go 的 libs/go-core/sealing），Worker 的工作区操作在这里解；两边对着同一组向量测试
（tests/contract/sealed_credential_vectors.json）：

    wsc1.<base64url(nonce(12) || AES-256-GCM 密文)>
    密钥 = SHA-256(SecretStore 平台作用域的 workspace/credential-key)，关联数据 = "wsc1|" + tenant_id + "|" + operation_id

解不开一律是同一个 denied（invalid_sealed），不说明原因。AES-GCM 用 cryptography（py-core 的 temporal 附加依赖里已有）。
"""

from __future__ import annotations

import base64
import hashlib
import os

from agentruntime_core.errors import DeniedError, InvalidError

PREFIX = "wsc1."
SECRET_NAME = "workspace/credential-key"


def _aead(key: bytes):  # noqa: ANN202 - cryptography 是可选依赖，类型在里面
    if len(key) < 32:
        raise InvalidError("weak_sealing_key", "封凭据的密钥至少 32 字节")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(hashlib.sha256(key).digest())


def _associated(tenant_id: str, operation_id: str) -> bytes:
    return f"wsc1|{tenant_id}|{operation_id}".encode()


def seal(key: bytes, tenant_id: str, operation_id: str, plaintext: bytes, *, nonce: bytes | None = None) -> str:
    nonce = nonce if nonce is not None else os.urandom(12)
    if len(nonce) != 12:
        raise InvalidError("invalid_nonce", "nonce 要 12 字节")
    out = nonce + _aead(key).encrypt(nonce, plaintext, _associated(tenant_id, operation_id))
    return PREFIX + base64.urlsafe_b64encode(out).rstrip(b"=").decode()


def open_sealed(key: bytes, tenant_id: str, operation_id: str, sealed: str) -> bytes:
    bad = DeniedError("invalid_sealed", "封起的凭据无效")
    if not isinstance(sealed, str) or not sealed.startswith(PREFIX):
        raise bad
    body = sealed[len(PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except ValueError:
        raise bad from None
    if len(raw) < 12 + 16:
        raise bad
    from cryptography.exceptions import InvalidTag

    try:
        return _aead(key).decrypt(raw[:12], raw[12:], _associated(tenant_id, operation_id))
    except InvalidTag:
        raise bad from None


__all__ = ["PREFIX", "SECRET_NAME", "open_sealed", "seal"]
