"""能力矩阵评测的共同接口（docs/plan/capability-matrix-evals.md，2026-11-03 人审通过）。

矩阵调度（capability.matrix）与各评测适配器（capability.evals.*）都按这里的接口写，互不依赖对方的实现：

- 一个**格子**（Cell）是 task_family × role × tier；一个 **CellEval** 是能给某个角色出分的一套题（S-09、BFCL v4、RewardBench 2……）。
- 调度器对每个候选模型：用 `PinningGateway` 把被测角色钉在候选模型上（其他角色照常选档），对每道题跑 r 次 `run_item`，
  收集 `ItemResult`，再做 bootstrap、收缩与区间判断（第 4 节），写成 Profile 的 `capability_scores`。
- 题目来自公开数据集或其改编（第 3 节）；需要裁判的题经 Model Gateway 调裁判模型（钉 Opus 5.5），裁判的输入、分数与理由
  放进 `ItemResult.record`，由调度器落盘（第 5 节）。

被测角色的调用只要有一次没落在钉的模型上（钉的模型出错、网关切到同档别的模型），这道题这一次记为 contaminated，不计分。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentruntime.common.v1 import enums_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_worker.executor.model_gateway import ModelGateway

ROLES = ("plan", "route", "judge", "act", "transform", "deliver", "fetch", "aggregate", "gate")


def role_enum(role: str) -> int:
    """附录 A 的 role 名 → proto 枚举值。"""
    if role not in ROLES:
        raise ValueError(f"role 只能是 {'、'.join(ROLES)}，得到 {role!r}")
    return enums_pb2.Role.Value(f"ROLE_{role.upper()}")


def role_name(value: int) -> str:
    name = enums_pb2.Role.Name(value) if value in enums_pb2.Role.values() else "ROLE_UNSPECIFIED"
    return name.removeprefix("ROLE_").lower()


@dataclass(frozen=True)
class Cell:
    """路由按 (task_family, role, tier) 精确匹配能力分（router.go）。"""

    task_family: str
    role: str
    tier: str

    def __post_init__(self) -> None:
        role_enum(self.role)


@dataclass(frozen=True)
class Item:
    """一道题。payload 只给适配器自己看；id 在一个 CellEval 里唯一且稳定（同一个 seed 选出同样的题）。"""

    id: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class ItemResult:
    """一道题跑一次的结果。score 在 0～1（通过记 1）；contaminated 的不计分。record 是要落盘的细节（不含密钥）。"""

    item_id: str
    score: float
    cost_usd: float = 0.0
    contaminated: bool = False
    error: str | None = None
    record: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"score 要在 0～1 之间，得到 {self.score}")


class CellEval(Protocol):
    """给一个角色出分的一套题。

    items(n, seed) 要按 seed 确定、并且前缀稳定：items(n + 20, seed) 的前 n 道就是 items(n, seed)（矩阵的淘汰赛与追加样本
    都靠它保证各候选做的是同一批题）。题不够 n 道时给全部。

    可选的属性（矩阵调度用 getattr 读，没有也行）：
    - est_tokens：(输入, 输出)，被测角色每道题大约的 token 数，估算花费按候选的单价算；
    - est_other_usd：每道题其他角色（照常选档）大约的花费；
    - uses_judge：True 时调度把裁判调用经 evals.judging.RecordingJudgeGateway（钉 Opus 5.5、逐条落盘）；被测角色是 judge 的格子不能用。
    """

    name: str  # 例如 "bfcl-v4"、"s09"；写进 capability_scores 的 source
    role: str  # 附录 A 的 role 名
    version: str  # 数据集版本或提交，写进 source

    def items(self, n: int, seed: int) -> Sequence[Item]: ...

    async def run_item(self, item: Item, gateway: ModelGateway, *, candidate: str) -> ItemResult: ...


class PinningGateway:
    """包一层 Model Gateway：被测角色的调用填 pinned_model_profile = 候选模型，其他角色不动；并记下每次调用实际用的 Profile，
    被测角色只要有一次没落在候选模型上（或调用出错），`contaminated` 就为 True。每道题每次重复用一个新的实例。

    tier（形如 strong@medium）给了时，被测角色的调用一律改成这个 Tier：网关只在请求的档里找钉的模型（router.go），
    候选与评测默认的档不同类时（例如压缩默认 small@low，候选是 mid 档的模型）不改就钉不上。矩阵调度给的是
    `<候选的 class>@<格子的 tier>`。pinned_calls、missed_calls 是被测角色的调用次数与其中没落在候选上（或出错）的次数，
    评测命令的 --pin 按它们报告。"""

    def __init__(self, inner: ModelGateway, *, role: str, candidate: str, tier: str | None = None) -> None:
        self.inner = inner
        self.role = role_enum(role)
        self.candidate = candidate
        self.tier = tier or None
        self.calls: Counter[tuple[str, str]] = Counter()
        self.cost_usd = 0.0
        self.contaminated = False
        self.pinned_calls = 0
        self.missed_calls = 0

    def _pin(self, request: Any) -> Any:
        if request.call.role == self.role:
            request = type(request).FromString(request.SerializeToString())
            request.call.pinned_model_profile = self.candidate
            if self.tier:
                request.call.tier = self.tier
        return request

    async def _call(self, method: str, request: Any) -> Any:
        pinned = request.call.role == self.role
        if pinned:
            self.pinned_calls += 1
        try:
            response = await getattr(self.inner, method)(self._pin(request))
        except Exception:
            if pinned:
                self.contaminated = True
                self.missed_calls += 1
            raise
        profile = response.model.model_profile_id if response.HasField("model") else ""
        self.calls[(role_name(request.call.role), profile or "unknown")] += 1
        if pinned and profile != self.candidate:
            self.contaminated = True
            self.missed_calls += 1
        if response.HasField("usage"):
            self.cost_usd += response.usage.cost_usd_micros / 1_000_000
        return response

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        return await self._call("generate", request)

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        return await self._call("embed", request)


__all__ = ["ROLES", "Cell", "CellEval", "Item", "ItemResult", "PinningGateway", "role_enum", "role_name"]
