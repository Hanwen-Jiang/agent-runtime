"""业务库里时间的写法（与 Sandbox Service 的 ledger.TimeLayout 相同）：ISO 8601 UTC、微秒、Z 结尾，可以按字典序比较。"""

from __future__ import annotations

from datetime import UTC, datetime


def iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = ["iso", "parse_iso", "utc_now"]
