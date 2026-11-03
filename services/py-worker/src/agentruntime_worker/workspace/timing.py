"""PERF-00：Workspace 操作与 shell_run 的分段耗时，以及进出 Worker 的字节数（WK-LITE-03；
docs/plan/workspace-read-latency-and-disk-2026-11-03.md 第 2、7 节）。

一次操作开一个 Meter（contextvar）；调用点用 phase() 计时、add_bytes() 计数，没开 Meter 时都是空操作，行为不变。
阶段可以嵌套，记的是独占时间：借环境（acquire）里的恢复只算在 restore 上，归还（release）里的存档只算在 archive 上；
总时长减去各阶段是 other（业务库、策略查询等）。字节按（通道，方向）记：通道是 sandbox（与环境之间：命令的 stdin、
stdout、stderr，读写文件，导出导入存档）与 object_store（存档的块与清单）；方向 in 是进 Worker，out 是出 Worker。
数的是载荷本身，不含 HTTP、JSON 与 base64 的开销。只记阶段名与数字，不记路径与内容。
"""

from __future__ import annotations

import contextlib
import contextvars
import time
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

PHASES = ("queue", "acquire", "restore", "command", "archive", "release")
CHANNELS = ("sandbox", "object_store")
DIRECTIONS = ("in", "out")

_current: contextvars.ContextVar[Meter | None] = contextvars.ContextVar("workspace_meter", default=None)


class Meter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.started = clock()
        self.ms: dict[str, float] = {}
        self.bytes: dict[tuple[str, str], int] = {}
        self.labels: dict[str, str] = {}  # 借到环境时记上：workspace.mode、sandbox.backend
        self._stack: list[list[float]] = []  # 每层：[开始时刻, 子阶段用掉的秒数]

    def add_ms(self, phase: str, ms: float) -> None:
        """不在本进程里量出来的阶段（排队）：直接记上。"""
        self.ms[phase] = self.ms.get(phase, 0.0) + max(ms, 0.0)

    def total_ms(self) -> float:
        return (self._clock() - self.started) * 1000

    def attributes(self) -> dict[str, Any]:
        """span 的属性：workspace.ms.<阶段>（含 other 与 total，毫秒取整）、worker.bytes.<通道>_<方向>。"""
        total = self.total_ms()
        out: dict[str, Any] = {f"workspace.ms.{k}": round(v) for k, v in sorted(self.ms.items())}
        measured = sum(v for k, v in self.ms.items() if k != "queue")
        out["workspace.ms.other"] = round(max(total - measured, 0.0))
        out["workspace.ms.total"] = round(total)
        return {**out, **self.byte_attributes()}

    def byte_attributes(self) -> dict[str, int]:
        return {f"worker.bytes.{channel}_{direction}": n for (channel, direction), n in sorted(self.bytes.items())}


def current() -> Meter | None:
    return _current.get()


@contextlib.contextmanager
def metering(meter: Meter | None = None) -> Iterator[Meter]:
    """在这一段里开一个 Meter；嵌套时（Workspace 操作里又有存档）外层的那个照用。"""
    outer = _current.get()
    if outer is not None:
        yield outer
        return
    meter = meter or Meter()
    token = _current.set(meter)
    try:
        yield meter
    finally:
        _current.reset(token)


@contextlib.asynccontextmanager
async def metered(span_cm: Any) -> AsyncIterator[tuple[Any, Meter]]:
    """开一个 Meter 再进 span（shell_run 用）：得到（span，Meter）。"""
    with metering() as meter:
        async with span_cm as span:
            yield span, meter


@contextlib.contextmanager
def phase(name: str) -> Iterator[None]:
    meter = _current.get()
    if meter is None:
        yield
        return
    frame = [meter._clock(), 0.0]  # noqa: SLF001
    meter._stack.append(frame)  # noqa: SLF001
    try:
        yield
    finally:
        meter._stack.pop()  # noqa: SLF001
        elapsed = meter._clock() - frame[0]  # noqa: SLF001
        meter.ms[name] = meter.ms.get(name, 0.0) + max(elapsed - frame[1], 0.0) * 1000
        if meter._stack:  # noqa: SLF001
            meter._stack[-1][1] += elapsed  # noqa: SLF001


def add_bytes(channel: str, direction: str, n: int) -> None:
    meter = _current.get()
    if meter is None or n <= 0:
        return
    key = (channel, direction)
    meter.bytes[key] = meter.bytes.get(key, 0) + int(n)


def exec_bytes(stdin: bytes | None, result: Any) -> None:
    """一次 exec：stdin 出 Worker，stdout、stderr 进 Worker。"""
    add_bytes("sandbox", "out", len(stdin or b""))
    add_bytes("sandbox", "in", len(getattr(result, "stdout", b"") or b"") + len(getattr(result, "stderr", b"") or b""))


async def record_durations(telemetry: Any, attributes: dict[str, Any], labels: dict[str, Any]) -> None:
    """span 属性里的每个 workspace.ms.<阶段>（含 other、total）记一条直方图 runtime.workspace_op.duration_ms。
    telemetry 只绑租户（不带工作区、会话），指标的标签才不随工作区膨胀。"""
    for key, value in attributes.items():
        if key.startswith("workspace.ms."):
            await telemetry.observe("runtime.workspace_op.duration_ms", float(value), attributes={**labels, "workspace.phase": key.removeprefix("workspace.ms.")})


async def record_bytes(telemetry: Any, meter: Meter, *, activity: str, labels: dict[str, Any] | None = None) -> None:
    """每个（通道，方向）记一条计数器 runtime.worker.bytes；同样只绑租户。"""
    for (channel, direction), n in sorted(meter.bytes.items()):
        await telemetry.count("runtime.worker.bytes", n, attributes={
            "worker.activity": activity, "worker.channel": channel, "worker.direction": direction, **(labels or {}),
        })


__all__ = [
    "CHANNELS", "DIRECTIONS", "PHASES", "Meter", "add_bytes", "current", "exec_bytes", "metered", "metering", "phase", "record_bytes", "record_durations",
]
