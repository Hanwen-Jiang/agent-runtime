"""故障计划（计划 S-07）：测试写规则，故障 Worker 在各个注入点按规则出错。

规则放在一个 JSON 文件里，注入点每次检查都重新读，测试改规则不用重启 Worker。每条规则：

    id      规则名，在一份计划里唯一
    point   注入点，见下面的 POINTS
    match   可选：子串列表，注入点的上下文必须全部包含（例如本用例独有的 ref），用来只影响本用例的 Node
    action  见下面的 ACTIONS
    times   最多触发几次；省略表示不限

触发记录追加到 <计划>.fired.jsonl，先记录再执行，所以因 crash 退出、重启后的 Worker 不会再次触发同一条规则。
测试与 Worker 都用这个模块：测试写规则、读触发次数，Worker 检查注入点。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 注入点 → 上下文（match 在上下文里找子串）。
POINTS = {
    # 外部写操作（ledger 工具）落地之前、之后，以及按幂等键查询：上下文是“工具名 参数 JSON”。
    "tool.before_apply": "工具名与参数",
    "tool.after_apply": "工具名与参数",
    "tool.lookup": "工具名与幂等键",
    # 模型供应商的一次调用：上下文是“供应商 Node 输入 JSON”。
    "model.vendor": "供应商与 Node 输入",
    # ObjectStore 的写入：上下文是对象键。
    "objects.put": "对象键",
}
ACTIONS = {
    "crash": "Worker 进程立即退出（不收尾），模拟进程崩溃",
    "transient": "抛 transient 错误（依赖暂时不可用）",
    "lose_response": "外部已生效，但响应超过工具超时才回来，调用方只看到超时",
    "timeout": "模型供应商超过它的超时仍没有响应",
    "hang": "一直挂起，直到进程被杀",
}
# crash 的退出码，测试据此确认 Worker 是按规则退出的。
CRASH_EXIT_CODE = 77
# 故障 Worker 的环境变量：故障计划文件、外部 ledger 目录。
ENV_FAULT_PLAN = "AR_FAULT_PLAN"
ENV_FAULT_LEDGER_DIR = "AR_FAULT_LEDGER_DIR"


@dataclass(frozen=True)
class Rule:
    id: str
    point: str
    action: str
    match: tuple[str, ...] = ()
    times: int | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "point": self.point, "action": self.action}
        if self.match:
            out["match"] = list(self.match)
        if self.times is not None:
            out["times"] = self.times
        return out

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> Rule:
        rule = Rule(
            id=str(raw["id"]),
            point=str(raw["point"]),
            action=str(raw["action"]),
            match=tuple(str(m) for m in raw.get("match", ())),
            times=int(raw["times"]) if raw.get("times") is not None else None,
        )
        if rule.point not in POINTS:
            raise ValueError(f"不认识的注入点 {rule.point}")
        if rule.action not in ACTIONS:
            raise ValueError(f"不认识的故障 {rule.action}")
        return rule


_lock = threading.Lock()


class FaultPlan:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fired_path = path.with_name(path.name + ".fired.jsonl")

    # --- 测试一侧 ---

    def set(self, *rules: Rule) -> None:
        """换成一份新计划，触发次数从零计（触发记录只对当前这份计划有效）。"""
        ids = [r.id for r in rules]
        if len(set(ids)) != len(ids):
            raise ValueError(f"规则 id 重复：{ids}")
        self.fired_path.unlink(missing_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"rules": [r.to_dict() for r in rules]}, ensure_ascii=False), "utf-8")
        os.replace(tmp, self.path)

    def clear(self) -> None:
        self.set()

    def fired(self, rule_id: str) -> int:
        return sum(1 for f in self._fired() if f["rule"] == rule_id)

    # --- Worker 一侧 ---

    def fire(self, point: str, context: str) -> Rule | None:
        """注入点调用：有匹配且还没用完的规则时，记下一次触发并返回它；否则返回 None。"""
        with _lock:
            for rule in self._rules():
                if rule.point != point or not all(m in context for m in rule.match):
                    continue
                if rule.times is not None and self.fired(rule.id) >= rule.times:
                    continue
                self._record(rule, context)
                return rule
        return None

    # --- 内部 ---

    def _rules(self) -> list[Rule]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text("utf-8") or "{}")
        return [Rule.from_dict(r) for r in raw.get("rules", [])]

    def _fired(self) -> list[dict[str, Any]]:
        if not self.fired_path.exists():
            return []
        out = []
        for line in self.fired_path.read_text("utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:  # 进程被杀时可能留下半行
                    continue
        return out

    def _record(self, rule: Rule, context: str) -> None:
        line = json.dumps({"rule": rule.id, "point": rule.point, "action": rule.action, "context": context[:200], "pid": os.getpid()}, ensure_ascii=False)
        with self.fired_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
