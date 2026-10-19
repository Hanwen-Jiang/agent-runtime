"""开发用的数据密钥包装格式。Go 侧见 libs/go-core/dev/filesecrets/keywrap.go，两边格式相同，
开发环境下 Python 包装的数据密钥 Go 能解开，反之亦然（S-06 的跨语言 Claim-Check 依赖这一点）。

只用标准库：HMAC-SHA256 作伪随机函数生成密钥流（先加密后认证）。生产环境用 Vault/OpenBao 的 transit 或云 KMS。

格式：
    "ARK1" | u8 作用域长度 | 作用域 | u8 用途长度 | 用途 | 16 字节 nonce | u16 密文长度（大端）| 密文 | 32 字节 tag
    enc_key = HMAC(kek, "agentruntime/dev-keywrap/enc")，mac_key = HMAC(kek, "agentruntime/dev-keywrap/mac")
    密钥流第 i 块 = HMAC(enc_key, nonce | u32(i) 大端)；tag = HMAC(mac_key, tag 之前的全部字节)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct

from agentruntime_core.errors import DeniedError, InvalidError

MAGIC = b"ARK1"


def _derive(kek: bytes, label: bytes) -> bytes:
    return hmac.new(kek, b"agentruntime/dev-keywrap/" + label, hashlib.sha256).digest()


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    blocks = bytearray()
    counter = 0
    while len(blocks) < length:
        blocks += hmac.new(enc_key, nonce + struct.pack(">I", counter), hashlib.sha256).digest()
        counter += 1
    return bytes(blocks[:length])


def wrap(kek: bytes, scope: str, purpose: str, plaintext: bytes) -> bytes:
    scope_bytes, purpose_bytes = scope.encode(), purpose.encode()
    nonce = os.urandom(16)
    ciphertext = bytes(a ^ b for a, b in zip(plaintext, _keystream(_derive(kek, b"enc"), nonce, len(plaintext)), strict=True))
    body = (
        MAGIC
        + bytes([len(scope_bytes)]) + scope_bytes
        + bytes([len(purpose_bytes)]) + purpose_bytes
        + nonce
        + struct.pack(">H", len(ciphertext)) + ciphertext
    )
    return body + hmac.new(_derive(kek, b"mac"), body, hashlib.sha256).digest()


def header(wrapped: bytes) -> tuple[str, str]:
    """解析出作用域与用途；格式不对抛 InvalidError。"""
    try:
        if wrapped[:4] != MAGIC:
            raise ValueError
        pos = 4
        scope_len = wrapped[pos]
        scope = wrapped[pos + 1 : pos + 1 + scope_len].decode()
        pos += 1 + scope_len
        purpose_len = wrapped[pos]
        purpose = wrapped[pos + 1 : pos + 1 + purpose_len].decode()
        pos += 1 + purpose_len + 16
        (ct_len,) = struct.unpack(">H", wrapped[pos : pos + 2])
        if len(wrapped) != pos + 2 + ct_len + 32 or len(scope.encode()) != scope_len:
            raise ValueError
    except (ValueError, IndexError, UnicodeDecodeError, struct.error) as error:
        raise InvalidError("invalid_wrapped_key", "包装后的数据密钥格式不对") from error
    return scope, purpose


def unwrap(kek: bytes, scope: str, purpose: str, wrapped: bytes) -> bytes:
    owner, wrapped_purpose = header(wrapped)
    if owner != scope:
        raise DeniedError("cross_scope_key", "数据密钥属于别的作用域")
    if wrapped_purpose != purpose:
        raise DeniedError("purpose_mismatch", "数据密钥的用途不符")
    body, tag = wrapped[:-32], wrapped[-32:]
    if not hmac.compare_digest(tag, hmac.new(_derive(kek, b"mac"), body, hashlib.sha256).digest()):
        raise InvalidError("invalid_wrapped_key", "包装后的数据密钥校验失败")
    nonce_start = 4 + 1 + len(owner.encode()) + 1 + len(purpose.encode())
    nonce = body[nonce_start : nonce_start + 16]
    ciphertext = body[nonce_start + 18 :]
    return bytes(a ^ b for a, b in zip(ciphertext, _keystream(_derive(kek, b"enc"), nonce, len(ciphertext)), strict=True))
