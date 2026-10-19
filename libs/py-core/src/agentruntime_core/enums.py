"""Python 代码里用的附录 A 枚举。取值来自 proto（agentruntime.common.v1），由测试比对，不另行定义。"""

from __future__ import annotations

import enum


class Effect(enum.StrEnum):
    """副作用等级（附录 D.4）。"""

    NONE = "none"
    READ = "read"
    WRITE_REVERSIBLE = "write_reversible"
    WRITE_IRREVERSIBLE = "write_irreversible"

    @property
    def is_write(self) -> bool:
        return self in (Effect.WRITE_REVERSIBLE, Effect.WRITE_IRREVERSIBLE)
