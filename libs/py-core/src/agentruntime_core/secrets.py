"""Secret：凭据与密钥的值类型（CLAUDE.md：凭据与密钥不得出现在日志、trace 和错误信息里）。

Python 与 Go 共用（Go 侧见 libs/go-core/secret），两边语义一致：打印、格式化、序列化都只得到掩码，
取明文必须显式调用 reveal()。
"""

from __future__ import annotations

import hmac
from typing import Any, NoReturn

MASK = "***"


class Secret:
    __slots__ = ("__value",)

    def __init__(self, value: str | bytes) -> None:
        if not isinstance(value, (str, bytes)):
            raise TypeError("Secret 只接受 str 或 bytes")
        self.__value = value

    def reveal(self) -> str | bytes:
        return self.__value

    def __repr__(self) -> str:
        return f"Secret({MASK!r})"

    def __str__(self) -> str:
        return MASK

    def __format__(self, spec: str) -> str:
        return MASK

    def __bool__(self) -> bool:
        return bool(self.__value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        a, b = self.__value, other.reveal()
        if type(a) is not type(b):
            return False
        return hmac.compare_digest(a, b)  # type: ignore[arg-type]

    __hash__ = None  # type: ignore[assignment]

    def __reduce__(self) -> NoReturn:
        raise TypeError("Secret 不能被序列化")

    def __getstate__(self) -> Any:
        raise TypeError("Secret 不能被序列化")
