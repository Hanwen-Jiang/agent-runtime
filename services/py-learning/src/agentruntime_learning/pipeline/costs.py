"""Task 的模型成本读业务库的 model_usage（R-06-D；代审第 7 节第 1、5 条，docs/plan/learning-pipeline.md 第 8 节）。

Model Gateway 每次 provider attempt 预留时在 model_usage 写一行、结算时填实际金额（libs/go-core/modelbudget，迁移 301、302；
这张表的迁移归 Go 一侧，这里只读）。Learning 读同一事实源：

- 每个 Task 的成本是它全部尝试的实际金额之和，失败的尝试也算（每次成功成本按全部花费算，附录 H.2）；
- 有还没结算的尝试（actual_usd_micros 为空：用量未知，网关留着预留），或账本里没有这个 Task 的记录，算成本未知：不按 0 算，
  门槛交人工判断（R-06-B）；
- 没有 task_id 的调用（例如 Learning 自己的失败归因）不归任何 Task。

租户由 RelationalStore 的绑定决定（SQLite 是租户自己的库文件，Postgres 是行级安全），SQL 里没有租户条件。读不了账本时抛
CostSourceError，调用方让整次运行失败，不退回 trace 上的成本。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from agentruntime_core.errors import ProviderError
from agentruntime_core.tenancy import TenantContext

from agentruntime_learning.pipeline.traces import TaskTrace

CHUNK = 500
MICROS = 1_000_000


class CostSourceError(Exception):
    """读不了业务库的 model_usage。消息里不带连接串与库的位置。"""


@dataclass(frozen=True)
class TaskCost:
    usd: float
    attempts: int
    pending: int

    @property
    def known(self) -> bool:
        return self.attempts > 0 and self.pending == 0


# 成本来源：(租户, task_id 列表) → 每个 task_id 的成本。run_pipeline 只认这个形状，测试可以换成替身。
CostSource = Callable[[str, list[str]], Awaitable[Mapping[str, TaskCost]]]


async def ledger_costs(store: Any, tenant_id: str, task_ids: Iterable[str]) -> dict[str, TaskCost]:
    """store 是 RelationalStoreProvider（Postgres 或开发用的 SQLite）。每个请求的 task_id 都有一项，没有记录的 attempts 为 0。"""
    ids = sorted({t for t in task_ids if t})
    micros: dict[str, int] = dict.fromkeys(ids, 0)
    attempts: dict[str, int] = dict.fromkeys(ids, 0)
    pending: dict[str, int] = dict.fromkeys(ids, 0)
    try:
        async with store.bind(TenantContext(tenant_id=tenant_id)).transaction(read_only=True) as tx:
            for start in range(0, len(ids), CHUNK):
                chunk = ids[start:start + CHUNK]
                marks = ", ".join(f"${i}" for i in range(1, len(chunk) + 1))
                rows = await tx.query(f"SELECT task_id, actual_usd_micros FROM model_usage WHERE task_id IN ({marks})", chunk)
                for row in rows:
                    task = str(row["task_id"])
                    attempts[task] += 1
                    if row["actual_usd_micros"] is None:
                        pending[task] += 1
                    else:
                        micros[task] += int(row["actual_usd_micros"])
    except ProviderError as err:
        raise CostSourceError(f"读不了业务库的 model_usage：{err}") from None
    return {t: TaskCost(usd=micros[t] / MICROS, attempts=attempts[t], pending=pending[t]) for t in ids}


def apply_costs(tasks: Iterable[TaskTrace], costs: Mapping[str, TaskCost]) -> list[TaskTrace]:
    """用账本的成本替换 trace 上的；账本里没有的 Task 成本未知（按 0 填，cost_known 为假）。顺序不变。"""
    out = []
    for task in tasks:
        cost = costs.get(task.task_id)
        out.append(dataclasses.replace(task, cost_usd=cost.usd if cost else 0.0, cost_known=bool(cost and cost.known)))
    return out
