"""评测边跑边发的指标（docs/plan/eval-observability-and-runtime-fixes.md 第 11.16 节）。评测看板的 /api/live.json
（agentruntime_learning.evals.dashboard_server）从 VictoriaMetrics 读它们，跑到一半也看得到进度。

一次运行用 eval.run_id 标识；同一次运行可以分几个进程跑完（SWE-bench 的续跑），每个进程一个 eval.process：

- 开始：runtime.eval.cases_planned（计数，值是这次运行一共要跑的用例数；每个进程都发同样的数，看板取最大）；
  续跑时另发 runtime.eval.cases_carried（之前的进程已经跑完的用例数）与 runtime.eval.cost_carried_usd（它们花的钱，直方图），
  看板按“已带过来的 + 这个进程跑完的”取各进程的最大，之前的进程没发指标也算得对。
- 每条用例结束：runtime.eval.cases（计数，eval.outcome：passed、failed、error；要事后判分的 Benchmark 交了答案是 submitted）
  与 runtime.eval.case_cost_usd（直方图，美元）。
- 结束：runtime.eval.finished（计数）；分数是 runtime.eval.score（直方图，eval.metric，同第 9.2 节）。

共同的属性：eval.suite（task-loop、context、swebench……）、eval.run_id、eval.process、eval.subject、eval.split，知道时加
eval.model（实际服务的 Profile 或 Benchmark 的模型标签）。只有名字与数字：用例原文、用例 id 都不发。
留出集（split 为 holdout）只发开始与结束的汇总，不发逐条的。

没有 Telemetry（没设 OTEL_EXPORTER_OTLP_ENDPOINT）时什么都不发。发指标出错只记一句日志，不让评测失败。
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import threading
from collections.abc import Coroutine, Mapping
from datetime import UTC, datetime
from typing import Any

from agentruntime_core.providers.telemetry import Telemetry

logger = logging.getLogger(__name__)

OUTCOMES = ("passed", "failed", "error", "submitted")
HOLDOUT = "holdout"

PLANNED = "runtime.eval.cases_planned"
CARRIED = "runtime.eval.cases_carried"
COST_CARRIED = "runtime.eval.cost_carried_usd"
CASES = "runtime.eval.cases"
CASE_COST = "runtime.eval.case_cost_usd"
FINISHED = "runtime.eval.finished"
SCORE = "runtime.eval.score"


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-.")[:60] or "run"


def new_run_id(subject: str, now: datetime | None = None) -> str:
    """一次运行的 id：时间、被测对象与几位随机数（同一秒开两次也不撞）。"""
    return f"{(now or datetime.now(UTC)):%Y%m%dT%H%M%SZ}-{_slug(subject)}-{secrets.token_hex(2)}"


class LiveRun:
    """一次评测运行在一个进程里的实时指标。"""

    def __init__(self, telemetry: Telemetry | None, *, suite: str, subject: str, split: str, run_id: str | None = None, model: str = "") -> None:
        self.telemetry = telemetry
        self.suite = suite
        self.subject = subject
        self.split = split
        self.run_id = run_id or new_run_id(subject)
        self.process = secrets.token_hex(4)
        self.model = model

    @property
    def per_case(self) -> bool:
        return self.telemetry is not None and self.split != HOLDOUT

    def attributes(self, **extra: Any) -> dict[str, Any]:
        attrs: dict[str, Any] = {"eval.suite": self.suite, "eval.run_id": self.run_id, "eval.process": self.process, "eval.subject": self.subject, "eval.split": self.split}
        if self.model:
            attrs["eval.model"] = self.model
        return {**attrs, **extra}

    async def _send(self, kind: str, name: str, value: float, attributes: Mapping[str, Any]) -> None:
        if self.telemetry is None:
            return
        try:
            if kind == "count":
                await self.telemetry.count(name, int(value), attributes=dict(attributes))
            else:
                await self.telemetry.observe(name, float(value), attributes=dict(attributes))
        except Exception as exc:  # noqa: BLE001 — 指标发不出去不影响评测
            logger.warning("评测指标 %s 没发出去（%s）", name, type(exc).__name__)

    async def planned(self, total: int, *, carried: int = 0, carried_cost_usd: float = 0.0) -> None:
        await self._send("count", PLANNED, max(0, total), self.attributes())
        if carried:
            await self._send("count", CARRIED, carried, self.attributes())
        if carried_cost_usd:
            await self._send("observe", COST_CARRIED, carried_cost_usd, self.attributes())

    async def case(self, outcome: str, cost_usd: float | None = None) -> None:
        if outcome not in OUTCOMES:
            raise ValueError(f"用例的结果只能是 {'、'.join(OUTCOMES)}，得到 {outcome!r}")
        if not self.per_case:
            return
        await self._send("count", CASES, 1, self.attributes(**{"eval.outcome": outcome}))
        if cost_usd is not None:
            await self._send("observe", CASE_COST, max(0.0, float(cost_usd)), self.attributes())

    async def score(self, metric: str, value: float, **extra: Any) -> None:
        await self._send("observe", SCORE, value, self.attributes(**{"eval.metric": metric}, **extra))

    async def finished(self, *, model: str = "") -> None:
        if model:
            self.model = model
        await self._send("count", FINISHED, 1, self.attributes())


class BlockingLive:
    """给同步的代码用（SWE-bench 的适配器在线程池里跑）：在自己的事件循环线程里发，调用方不用管事件循环。"""

    def __init__(self, live: LiveRun, *, timeout_s: float = 10.0) -> None:
        self.live = live
        self.timeout_s = timeout_s
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="eval-live", daemon=True)
        self._thread.start()

    def _call(self, coro: Coroutine[Any, Any, Any]) -> None:
        try:
            asyncio.run_coroutine_threadsafe(coro, self._loop).result(self.timeout_s)
        except Exception as exc:  # noqa: BLE001 — 同 LiveRun._send
            logger.warning("评测指标没发出去（%s）", type(exc).__name__)

    def planned(self, total: int, *, carried: int = 0, carried_cost_usd: float = 0.0) -> None:
        self._call(self.live.planned(total, carried=carried, carried_cost_usd=carried_cost_usd))

    def case(self, outcome: str, cost_usd: float | None = None) -> None:
        if outcome not in OUTCOMES:
            raise ValueError(f"用例的结果只能是 {'、'.join(OUTCOMES)}，得到 {outcome!r}")
        self._call(self.live.case(outcome, cost_usd))

    def score(self, metric: str, value: float) -> None:
        self._call(self.live.score(metric, value))

    def finished(self, *, model: str = "") -> None:
        self._call(self.live.finished(model=model))

    def flush(self) -> None:
        if self.live.telemetry is not None:
            self._call(self.live.telemetry.flush())

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(self.timeout_s)
        if not self._loop.is_running():
            self._loop.close()


__all__ = ["CASES", "CASE_COST", "FINISHED", "OUTCOMES", "PLANNED", "SCORE", "BlockingLive", "LiveRun", "new_run_id"]
