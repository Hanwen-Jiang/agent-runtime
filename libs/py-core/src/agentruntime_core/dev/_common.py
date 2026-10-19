"""开发实现共用的小工具。"""

from __future__ import annotations

import re
from datetime import timedelta

from agentruntime_core.errors import InvalidError
from agentruntime_core.tenancy import Scope

# 平台作用域的标识。租户 ID 不能以下划线开头，不会与它冲突。
PLATFORM_ID = "_platform"

_NAME = re.compile(r"[^\s\x00-\x1f\x7f]{1,512}")


def scope_id(scope: Scope) -> str:
    """作用域的稳定标识：租户 ID，平台作用域为 _platform。"""
    return PLATFORM_ID if scope.is_platform else scope.tenant_id


def check_name(value: str, what: str) -> str:
    """键、锁名、流名：1 到 512 个字符，不含空白与控制字符。"""
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise InvalidError("invalid_argument", f"{what} 必须是 1 到 512 个不含空白与控制字符的字符")
    return value


def check_positive(value: timedelta, what: str) -> timedelta:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise InvalidError("invalid_argument", f"{what} 必须大于 0")
    return value
