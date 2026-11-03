"""能力矩阵的各套题（CellEval）与加载规则（docs/plan/capability-matrix-evals.md 第 3 节）。

计划里一套题写成 `<模块>` 或 `<模块>:<工厂函数>`：

- 短名（不含点，例如 `s09`、`bfcl`）指 `agentruntime_learning.capability.evals.<短名>`；本包里没有这个模块时
  再当顶层模块找（测试里的假评测）；
- 带点的是完整的模块路径（测试或仓库外的评测用）；
- 工厂函数默认是 `make`，签名 `make(**options) -> CellEval`，options 是计划里这一项的 `options`。

本包里的组件级评测：

| 写法 | 角色 | 题 |
| --- | --- | --- |
| `s09` | plan | S-09 的 Task 用例里标为 full、replan 的（这些才调用规划模型） |
| `s09:route` | route | S-09 里由分诊模型定级的用例（上一轮的 Verdict 已定级的 replan 不算），只看 Triage 对不对 |
| `s10` | judge | S-10 的 Reviewer 判定点：Reviewer 专用样本与 Task 用例里每个 Round 的判定点 |
| `s11` | transform | S-11 的长任务样本，按 full 策略压缩（增量摘要是 transform 调用），看 Probe 与重复操作 |

公开数据集的适配器（BFCL v4、τ²-bench、RewardBench 2、JudgeBench、LongBench v2、SimpleQA、FRAMES）按同样的规则放在这里。
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Any

from agentruntime_learning.capability.base import ROLES, CellEval

PACKAGE = __name__
DEFAULT_FACTORY = "make"
_REQUIRED = ("name", "role", "version", "items", "run_item")


def resolve(spec: str) -> tuple[str, str]:
    """`<模块>[:<工厂函数>]` → （完整模块名, 工厂函数名）。"""
    module, sep, factory = str(spec).strip().partition(":")
    if not module or (sep and not factory):
        raise ValueError(f"评测要写成 <模块> 或 <模块>:<工厂函数>，得到 {spec!r}")
    if "." not in module:
        module = f"{PACKAGE}.{module}"
    return module, factory or DEFAULT_FACTORY


def load_eval(spec: str, options: Mapping[str, Any] | None = None) -> CellEval:
    """按计划里的写法造一套题；找不到、造出来的不像 CellEval 时报 ValueError。"""
    module_name, factory_name = resolve(spec)
    try:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            short = module_name.removeprefix(f"{PACKAGE}.")
            if exc.name != module_name or short == module_name:
                raise
            module = importlib.import_module(short)
        factory = getattr(module, factory_name)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"找不到评测 {spec!r}（{module_name}:{factory_name}）：{exc}") from exc
    try:
        cell_eval = factory(**dict(options or {}))
    except TypeError as exc:
        raise ValueError(f"评测 {spec!r} 不接受这些参数 {sorted(options or {})}：{exc}") from exc
    missing = [a for a in _REQUIRED if not hasattr(cell_eval, a)]
    if missing:
        raise ValueError(f"{spec} 造出来的不是 CellEval：缺 {'、'.join(missing)}")
    if cell_eval.role not in ROLES:
        raise ValueError(f"{spec} 的 role {cell_eval.role!r} 不是附录 A 的角色")
    return cell_eval


__all__ = ["DEFAULT_FACTORY", "PACKAGE", "load_eval", "resolve"]
