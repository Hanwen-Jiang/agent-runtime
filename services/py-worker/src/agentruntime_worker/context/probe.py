"""Compaction Probe（附录 G.6；计划 S-11）。

summarize 或 reset 之后，从 Task State 与 Task Plan 抽 3 到 5 个关键事实，各出一道填空题，让模型只凭压缩后的上下文作答，
由代码判分；答错的事实补回原文片段（history 里提到它的那一步的观察摘录；找不到时把事实本身补进 status）。

- 抽事实（代码完成）：验收条件、约束、已验证的事实、产物、里程碑、待查问题；按工作模式的 probe_priority 轮流取，
  让不同类别都抽到。
- 出题（代码完成）：从事实里挑一个关键片段挖空，优先级是路径或 URI、数字、ID、引号里的词、英文词；约束挖它的取值。
- 判分（代码完成）：归一化（NFKC、大小写、空白）后看回答里有没有答案；数字要作为完整的数出现，6380、63790 都不算 6379。

事实的原文与 status 区、brief 区里的写法一致（state_lines），所以上下文保住了事实，按词面就答得出。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agentruntime_worker.context.modes import ModePolicy

MIN_FACTS = 3
MAX_FACTS = 5
BLANK = "____"
CONSTRAINT_LABELS = {"effect_ceiling": "effect 上限", "budget_usd": "预算上限（美元）", "deadline_min": "截止（分钟）"}

# 边界只看 ASCII 的词字符：Python 的 \w 包括汉字，“6379端口”里的 6379 也要认得出。
_PATH = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[!-~]+|(?<![0-9A-Za-z_./-])[0-9A-Za-z_.-]*[A-Za-z_][0-9A-Za-z_.-]*/[0-9A-Za-z_./-]+")
_NUMBER = re.compile(r"(?<![0-9A-Za-z_.])\d+(?:\.\d+)?(?![0-9A-Za-z_])")
_ID = re.compile(r"(?<![0-9A-Za-z_])[a-z]{2,4}_[0-9A-Za-z]{6,}(?![0-9A-Za-z_])")
_QUOTED = re.compile(r"[“「『\"']([^”」』\"']{2,40})[”」』\"']")
_WORD = re.compile(r"(?<![0-9A-Za-z_.])[A-Za-z_][A-Za-z0-9_.-]{2,}(?![0-9A-Za-z_])")


@dataclass(frozen=True)
class ProbeFact:
    category: str
    """done_when、constraint、fact、artifact、milestone、open_question。"""
    text: str
    """事实原文（与 status、brief 里的写法一致）。"""
    answer: str
    """挖空的片段。"""

    def question(self) -> str:
        """填空题：事实原文里的 answer 换成 BLANK（只换第一处）。"""
        return self.text.replace(self.answer, BLANK, 1)


@dataclass(frozen=True)
class ProbeResult:
    fact: ProbeFact
    given: str
    passed: bool


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value) != ""


def state_lines(task_state: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """Task State 的精确状态，逐行：（类别、原文、挖空的答案或空串）。status 区按这个写，Probe 也从这里抽。"""
    lines: list[tuple[str, str, str]] = []
    constraints = task_state.get("constraints") if isinstance(task_state.get("constraints"), Mapping) else {}
    for key, value in constraints.items():
        if _scalar(value):
            lines.append(("constraint", f"{CONSTRAINT_LABELS.get(key, '约束 ' + key)}：{value}", str(value)))
    for category, field, prefix in (
        ("fact", "facts", "已验证："),
        ("artifact", "artifacts", "产物："),
        ("milestone", "milestones", "里程碑："),
        ("open_question", "open_questions", "待查："),
        ("assumption", "assumptions", "待回查的假设："),
    ):
        for item in task_state.get(field) or []:
            if isinstance(item, str) and item:
                lines.append((category, prefix + item, salient(item)))
    # 用户在运行中发来的纠偏：压缩之后也要在。不当事实抽查（没有挖空的答案）。
    for item in task_state.get("guidance") or []:
        if isinstance(item, Mapping) and item.get("state") == "applied":
            lines.append(("guidance", f"用户的纠偏：{item.get('text', '')}", ""))
    return lines


def done_when_line(task_state: Mapping[str, Any]) -> str:
    done_when = task_state.get("done_when")
    return f"验收条件：{done_when}" if isinstance(done_when, str) and done_when else ""


def salient(text: str) -> str:
    """事实里最值得挖空的片段；没有就返回空串。"""
    for pattern in (_PATH, _NUMBER, _ID):
        match = pattern.search(text)
        if match:
            return match.group(0).rstrip("。，；,.;)")
    match = _QUOTED.search(text)
    if match:
        return match.group(1)
    match = _WORD.search(text)
    return match.group(0) if match else ""


def select_facts(task_state: Mapping[str, Any], task_plan: Mapping[str, Any] | None, policy: ModePolicy, *, limit: int = MAX_FACTS) -> list[ProbeFact]:
    """抽 MIN_FACTS 到 limit 个能挖空的关键事实；不足 MIN_FACTS 时有几个给几个。按 probe_priority 轮流从各类别取。"""
    by_category: dict[str, list[ProbeFact]] = {}
    done_when = done_when_line(task_state)
    candidates = [("done_when", done_when, salient(task_state.get("done_when") or ""))] if done_when else []
    candidates += [c for c in state_lines(task_state) if c[0] != "assumption"]
    budget = (task_plan or {}).get("budget") if isinstance((task_plan or {}).get("budget"), Mapping) else {}
    candidates += [("constraint", f"计划预算 {k}：{v}", str(v)) for k, v in (budget or {}).items() if _scalar(v)]
    for category, text, answer in candidates:
        if answer and answer in text:
            by_category.setdefault(category, []).append(ProbeFact(category=category, text=text, answer=answer))
    order = [c for c in policy.probe_priority if c in by_category] + [c for c in by_category if c not in policy.probe_priority]
    picked: list[ProbeFact] = []
    while len(picked) < limit and any(by_category.get(c) for c in order):
        for category in order:
            if by_category.get(category) and len(picked) < limit:
                picked.append(by_category[category].pop(0))
    return picked


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip().lower()


def grade(fact: ProbeFact, given: str) -> bool:
    answer, given_n = _norm(fact.answer), _norm(given)
    if not answer:
        return False
    if re.fullmatch(r"\d+(?:\.\d+)?", answer):
        return answer in _NUMBER.findall(given_n)
    return answer in given_n
