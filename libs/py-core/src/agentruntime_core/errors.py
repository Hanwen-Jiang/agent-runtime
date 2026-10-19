"""统一的错误模型（计划 S-02 难点“错误语义统一”）。

Python 与 Go 共用（Go 侧见 libs/go-core/providererr），两边语义一致：

- Provider 实现只抛 ProviderError，不把底层库的异常漏给调用方。
- 类别取自 proto 的 agentruntime.common.v1.ErrorCategory。
- 是否可重试只由类别决定：transient 可重试，其余都不可重试。Node Executor 的失败分类（附录 D.6）依赖这一点。
- 错误信息里不得出现凭据、密钥或租户的原始数据。
"""

from __future__ import annotations

import enum
from datetime import timedelta
from typing import ClassVar

from agentruntime.common.v1 import error_pb2


class ErrorCategory(enum.IntEnum):
    """取值与 proto 的 ErrorCategory 一一对应，由测试比对。"""

    TRANSIENT = error_pb2.ERROR_CATEGORY_TRANSIENT
    INVALID = error_pb2.ERROR_CATEGORY_INVALID
    DENIED = error_pb2.ERROR_CATEGORY_DENIED
    NOT_FOUND = error_pb2.ERROR_CATEGORY_NOT_FOUND
    CONFLICT = error_pb2.ERROR_CATEGORY_CONFLICT


class ProviderError(Exception):
    """所有 Provider 错误的基类。用 ProviderError.of() 按类别构造。"""

    category: ClassVar[ErrorCategory]

    def __init__(self, code: str, message: str = "", *, retry_after: timedelta | None = None) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        # 只对 transient 有意义：建议的最早重试时间，例如限流返回的 Retry-After。
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.category is ErrorCategory.TRANSIENT

    @staticmethod
    def of(
        category: ErrorCategory, code: str, message: str = "", *, retry_after: timedelta | None = None
    ) -> ProviderError:
        return _BY_CATEGORY[category](code, message, retry_after=retry_after)

    def to_proto(self) -> error_pb2.Error:
        return error_pb2.Error(category=int(self.category), code=self.code, message=self.message)

    @staticmethod
    def from_proto(error: error_pb2.Error) -> ProviderError:
        return ProviderError.of(ErrorCategory(error.category), error.code, error.message)


class TransientError(ProviderError):
    """超时、限流、依赖暂时不可用。唯一可重试的类别。"""

    category = ErrorCategory.TRANSIENT


class InvalidError(ProviderError):
    """参数或输出不合法。"""

    category = ErrorCategory.INVALID


class DeniedError(ProviderError):
    """被权限、租户策略或 Hook 拒绝；缺少租户上下文也归此类。"""

    category = ErrorCategory.DENIED


class NotFoundError(ProviderError):
    """不存在，或属于别的租户。跨租户访问一律报 not_found，不泄露对象是否存在。"""

    category = ErrorCategory.NOT_FOUND


class ConflictError(ProviderError):
    """版本冲突、幂等键冲突、对象已存在等。"""

    category = ErrorCategory.CONFLICT


_BY_CATEGORY: dict[ErrorCategory, type[ProviderError]] = {
    ErrorCategory.TRANSIENT: TransientError,
    ErrorCategory.INVALID: InvalidError,
    ErrorCategory.DENIED: DeniedError,
    ErrorCategory.NOT_FOUND: NotFoundError,
    ErrorCategory.CONFLICT: ConflictError,
}
