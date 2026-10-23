"""Learning Pipeline 自己的预算（设计书 3.5.1 关键规则“后台任务单独记预算”；T-16）。

一次运行一个预算，只算 Learning 自己的模型调用（按 Model Gateway 返回的 cost_usd_micros）；用完（剩余 ≤ 0）就不再调用模型。
不计入任何 Task 的成本：调用不带 task_id。
"""

from __future__ import annotations

from typing import Any


class LearningBudget:
    def __init__(self, *, limit_usd: float) -> None:
        if limit_usd < 0:
            raise ValueError("预算不能为负")
        self.limit_micros = round(limit_usd * 1_000_000)
        self.spent_micros = 0
        self.calls = 0

    @property
    def remaining_micros(self) -> int:
        return self.limit_micros - self.spent_micros

    @property
    def exhausted(self) -> bool:
        return self.remaining_micros <= 0

    @property
    def spent_usd(self) -> float:
        return self.spent_micros / 1_000_000

    def charge(self, cost_usd_micros: int) -> None:
        self.calls += 1
        self.spent_micros += max(int(cost_usd_micros), 0)

    def report(self) -> dict[str, Any]:
        return {"limit_usd": self.limit_micros / 1_000_000, "spent_usd": self.spent_usd, "calls": self.calls, "exhausted": self.exhausted}
