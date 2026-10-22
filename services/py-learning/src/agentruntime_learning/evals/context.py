"""S-11 的长任务样本与压缩评测（计划 S-11 验收“在 S-08 中加入长任务样本，压缩后的 Probe 通过率与重复操作率不劣于
‘只做 mask’的基线”；设计书附录 G.3、G.6）。

样本放在 <评测集>/context_samples/<id>.json（格式见 context_sample.schema.json），每个工作模式至少一条：
一条长的 tool_loop 轨迹，加上 Task State、Task Plan 与 Goal Node；标注是若干道 Probe 题（问题与答案，答案能在原始轨迹或
Task State 里找到）和已经完成的动作（压缩后再做一遍就算重复操作）。标注要经人确认。样本与 S-08 的用例分开读取，
不影响 S-08 评测集的版本号。

评测：每条样本按两种策略压缩到它的有效长度——mask_only（offload、mask、prune）与 full（整个 Compaction Ladder，含
S-11 的增量摘要与 Compaction Probe）——再让被测模型只凭压缩后的上下文答 Probe 题、给出下一步动作：
- Probe 通过率：答对的题数 / 总题数（判分用 Worker 的 probe.grade）；
- 重复操作率：下一步动作是已完成动作的样本数 / 样本数。
门槛：full 的 Probe 通过率不低于 mask_only，重复操作率不高于 mask_only。留出集只给汇总。
另给两个与被测模型无关的诊断，不进门槛：答案留在压缩后上下文里的比例（retention_rate），走到 summarize 的样本数（summarized）。
真模型有随机性：repeat 次重复跑时指标取中位数、门槛按中位数判，每次的结果都保留（T-02）。

样本必须长到“只做 mask 之后仍超过 summarize_at”，否则 full 不会摘要，两种策略的差别只剩随机波动：加载时就检查（T-01）。

被测模型：lexical 是离线基线（按词面作答，摘要由抽取式的假模型给出），只用来检查评测链路；真模型见
worker_subject.context_subject（经 Model Gateway）。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import statistics
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import enums_pb2, payload_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_worker.activities.types import Tenant
from agentruntime_worker.context.builder import LADDER, BuiltContext, ContextBuilder, ContextConfig, ContextRequest, tenant_context
from agentruntime_worker.context.probe import ProbeFact, grade
from agentruntime_worker.context.summary import SECTIONS
from agentruntime_worker.executor.model_gateway import ModelGateway
from agentruntime_worker.executor.tool_loop import SYSTEM_PROMPT, TOOL_LOOP_TIER

from agentruntime_learning.evals import schemas
from agentruntime_learning.evals.cases import EvalSetError

CONTEXT_DIR = "context_samples"
STRATEGIES = ("mask_only", "full")
STRATEGY_STEPS = {"mask_only": ("offload", "mask", "prune"), "full": LADDER}
EVAL_TENANT = "evals"
ANSWER_SCHEMA: dict[str, Any] = {"type": "object", "required": ["answers"], "properties": {"answers": {"type": "array", "items": {"type": "string"}}}}
ANSWER_INSTRUCTION = "只根据上面的上下文，按顺序回答下面的问题，每题一句；上下文里找不到就回答“不知道”，不要猜。"


@dataclass(frozen=True)
class ContextSample:
    id: str
    split: str
    review: dict[str, Any]
    description: str
    work_mode: str
    effective_tokens: int
    task_state: dict[str, Any]
    task_plan: dict[str, Any]
    node: dict[str, Any]
    history: list[dict[str, Any]]
    probes: list[dict[str, str]]
    """[{"question", "answer"}]"""
    completed: list[dict[str, Any]]
    """已经完成的动作 [{"tool", "args"}]。"""

    @property
    def confirmed(self) -> bool:
        return self.review.get("status") == "confirmed"


def _action(tool: Any, args: Any) -> tuple[str, str]:
    return str(tool), json.dumps(args or {}, ensure_ascii=False, sort_keys=True)


def _sample_problems(data: dict[str, Any]) -> list[str]:
    found = []
    source = json.dumps(data["history"], ensure_ascii=False) + json.dumps(data["task_state"], ensure_ascii=False)
    for probe in data["probes"]:
        if probe["answer"] not in source:
            found.append(f"Probe 题“{probe['question']}”的答案不在原始轨迹或 Task State 里")
    actions = {_action(s["tool"], s["args"]) for s in data["history"]}
    for done in data["completed"]:
        if _action(done["tool"], done["args"]) not in actions:
            found.append(f"已完成的动作 {done['tool']} {done['args']} 不在轨迹里")
    plan_mode = ((data["task_plan"].get("context") or {}).get("work_mode"))
    if plan_mode and plan_mode != data["work_mode"]:
        found.append(f"Task Plan 的 work_mode 是 {plan_mode}，样本标的是 {data['work_mode']}")
    return found


def load_context_samples(root: Path) -> list[ContextSample]:
    """读取并校验 <root>/context_samples；不合格时抛 cases.EvalSetError。没有这个目录时返回空列表。"""
    directory = Path(root) / CONTEXT_DIR
    if not directory.is_dir():
        return []
    validator = schemas.validator("context_sample")
    problems: list[str] = []
    samples: list[ContextSample] = []
    for path in sorted(directory.glob("*.json")):
        rel = f"{CONTEXT_DIR}/{path.name}"
        try:
            data = json.loads(path.read_text("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            problems.append(f"{rel}：读不出来（{exc}）")
            continue
        errors = schemas.problems(validator, data)
        if errors:
            problems += [f"{rel}：{e}" for e in errors]
            continue
        if data["id"] != path.stem:
            problems.append(f"{rel}：id {data['id']} 与文件名不一致")
        found = _sample_problems(data)
        problems += [f"{rel}：{p}" for p in found]
        if found or data["id"] != path.stem:
            continue
        sample = ContextSample(
            id=data["id"],
            split=data["split"],
            review=data["review"],
            description=data["description"],
            work_mode=data["work_mode"],
            effective_tokens=data["effective_tokens"],
            task_state=data["task_state"],
            task_plan=data["task_plan"],
            node=data["node"],
            history=data["history"],
            probes=data["probes"],
            completed=data["completed"],
        )
        masked, threshold = masked_tokens(sample), summarize_threshold(sample)
        if masked <= threshold:
            problems.append(f"{rel}：只做 mask 之后约 {masked} token，不超过 summarize_at（{threshold:.0f}），full 不会摘要；调小 effective_tokens 或加长轨迹")
            continue
        samples.append(sample)
    if problems:
        raise EvalSetError(problems)
    return samples


class ContextSubject(Protocol):
    """压缩评测的被测模型：只凭给定的上下文作答。gateway 用于 full 策略里的增量摘要与 Compaction Probe。"""

    name: str
    gateway: ModelGateway

    async def answer(self, messages: Sequence[mg.ChatMessage], questions: Sequence[str]) -> list[str]: ...

    async def next_action(self, messages: Sequence[mg.ChatMessage], tools: Sequence[str]) -> tuple[str, dict[str, Any]]: ...


def summarize_threshold(sample: ContextSample) -> float:
    """样本触发 summarize 的 token 数：与 Context Builder 同一算法（Task Plan 的 context.summarize_at 覆盖默认值）。"""
    settings = sample.task_plan.get("context") if isinstance(sample.task_plan.get("context"), Mapping) else {}
    return float(settings.get("summarize_at", ContextConfig.summarize_at)) * sample.effective_tokens


def masked_tokens(sample: ContextSample) -> int:
    """只做 mask（mask_only 策略）之后上下文的 token 数。可以在事件循环里调用：在单独的线程里跑 Context Builder。"""

    async def measure() -> int:
        with tempfile.TemporaryDirectory(prefix="context-check-") as scratch:
            return (await build_context(sample, "mask_only", ExtractiveGateway(), Path(scratch))).tokens

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, measure()).result()


async def build_context(sample: ContextSample, strategy: str, gateway: ModelGateway, work_dir: Path) -> BuiltContext:
    """用 Worker 的 Context Builder 按策略压缩样本的轨迹；结果里有压缩后的上下文与用到的阶梯步骤。每次用全新的 ObjectStore，互不影响。"""
    if strategy not in STRATEGY_STEPS:
        raise ValueError(f"未知的压缩策略 {strategy}")
    objects = LocalDiskObjectStoreProvider(Path(work_dir) / uuid.uuid4().hex, signing_key=b"context-evals")
    config = ContextConfig(effective_tokens=sample.effective_tokens, steps=STRATEGY_STEPS[strategy], probe=strategy == "full")
    return await ContextBuilder(gateway, objects, config).build(
        ContextRequest(
            tenant=Tenant(tenant_id=EVAL_TENANT, session_id=str(sample.task_state.get("session_id", ""))),
            task_id=str(sample.task_state.get("task_id", "")),
            round_seq=1,
            node=sample.node,
            history=sample.history,
            task_state=sample.task_state,
            task_plan=sample.task_plan,
            system=[SYSTEM_PROMPT],
        )
    )


async def compact(sample: ContextSample, strategy: str, gateway: ModelGateway, work_dir: Path) -> list[mg.ChatMessage]:
    """build_context 的压缩后上下文。"""
    return (await build_context(sample, strategy, gateway, work_dir)).messages


@dataclass
class ContextReport:
    split: str
    subject: str
    metrics: dict[str, dict[str, float]]
    """策略 → {"probe_pass_rate", "repeat_rate", "retention_rate", "summarized", "samples"}；重复跑时取各次的中位数。
    summarized 是走到 summarize 的样本数。"""
    gate_passed: bool
    """按 metrics（中位数）判。"""
    samples: list[dict[str, Any]] = field(default_factory=list)
    """每次、每条样本的明细（run 从 1 数）；留出集为空。"""
    runs: list[dict[str, dict[str, float]]] = field(default_factory=list)
    """每次的 metrics，保留单次结果。"""
    ranges: dict[str, dict[str, tuple[float, float]]] = field(default_factory=dict)
    """策略 → 指标 → (各次的最小值, 最大值)。"""

    def summary(self) -> str:
        repeat = len(self.runs)
        head = f"S-11 压缩评测：被测 {self.subject}，{self.split}"
        lines = [head + (f"，重复 {repeat} 次（指标取中位数，括号里是各次的范围）" if repeat > 1 else "")]

        def show(strategy: str, name: str, fmt: str = "{:.0%}") -> str:
            value = fmt.format(self.metrics[strategy][name])
            if repeat > 1:
                low, high = self.ranges[strategy][name]
                value += f"（{fmt.format(low)}–{fmt.format(high)}）"
            return value

        for strategy in STRATEGIES:
            m = self.metrics[strategy]
            lines.append(
                f"  {strategy}：Probe 通过率 {show(strategy, 'probe_pass_rate')}，重复操作率 {show(strategy, 'repeat_rate')}，"
                f"答案留在上下文里的比例 {show(strategy, 'retention_rate')}，触发摘要 {show(strategy, 'summarized', '{:g}')} 条（共 {int(m['samples'])} 条）"
            )
        lines.append("  门槛（full 不劣于 mask_only）：" + ("通过" if self.gate_passed else "未通过"))
        for row in self.samples:
            run = f"第 {row['run']} 次 · " if repeat > 1 else ""
            lines.append(
                f"    {run}{row['id']} · {row['strategy']}：Probe {row['passed']}/{len(row['probes'])}，留存 {row['retained']}，"
                f"阶梯 {'/'.join(row['applied']) or '无'}{'，重复操作' if row['repeated'] else ''}"
            )
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "split": self.split,
            "subject": self.subject,
            "repeat": len(self.runs),
            "metrics": self.metrics,
            "ranges": {s: {name: list(bounds) for name, bounds in r.items()} for s, r in self.ranges.items()},
            "runs": self.runs,
            "gate_passed": self.gate_passed,
            "samples": self.samples,
        }


async def run_context_eval(samples: Sequence[ContextSample], subject: ContextSubject, *, split: str, work_dir: Path, repeat: int = 1) -> ContextReport:
    """跑 repeat 次（模型有随机性，单次差一两题不下结论）：metrics 取各次的中位数，门槛按中位数判，每次的结果都保留。"""
    if repeat < 1:
        raise ValueError(f"repeat 至少是 1，得到 {repeat}")
    selected = [s for s in samples if s.split == split]
    runs: list[dict[str, dict[str, float]]] = []
    rows: list[dict[str, Any]] = []
    for run in range(1, repeat + 1):
        metrics, run_rows = await _run_once(selected, subject, split=split, work_dir=Path(work_dir), run=run)
        runs.append(metrics)
        rows += run_rows
    medians = {s: {name: statistics.median(r[s][name] for r in runs) for name in runs[0][s]} for s in STRATEGIES}
    ranges = {s: {name: (min(r[s][name] for r in runs), max(r[s][name] for r in runs)) for name in runs[0][s]} for s in STRATEGIES}
    full, masked = medians["full"], medians["mask_only"]
    gate = full["probe_pass_rate"] >= masked["probe_pass_rate"] and full["repeat_rate"] <= masked["repeat_rate"]
    return ContextReport(split=split, subject=subject.name, metrics=medians, gate_passed=gate, samples=rows, runs=runs, ranges=ranges)


async def _run_once(selected: Sequence[ContextSample], subject: ContextSubject, *, split: str, work_dir: Path, run: int) -> tuple[dict[str, dict[str, float]], list[dict[str, Any]]]:
    metrics: dict[str, dict[str, float]] = {}
    rows: list[dict[str, Any]] = []
    for strategy in STRATEGIES:
        questions = passed = retained = repeats = summarized = 0
        for sample in selected:
            built = await build_context(sample, strategy, subject.gateway, work_dir / strategy / sample.id)
            messages = built.messages
            summarized += int("summarize" in built.applied)
            answers = await subject.answer(messages, [p["question"] for p in sample.probes])
            context_text = "\n".join(_context_lines(messages))
            graded, kept = [], []
            for i, probe in enumerate(sample.probes):
                fact = ProbeFact(category="label", text=probe["answer"], answer=probe["answer"])
                graded.append(grade(fact, answers[i] if i < len(answers) else ""))
                kept.append(grade(fact, context_text))
            tool, args = await subject.next_action(messages, list(sample.node.get("tools") or []))
            repeated = _action(tool, args) in {_action(d["tool"], d["args"]) for d in sample.completed}
            questions += len(graded)
            passed += sum(graded)
            retained += sum(kept)
            repeats += int(repeated)
            if split == "dev":
                rows.append(
                    {
                        "run": run,
                        "id": sample.id,
                        "strategy": strategy,
                        "applied": built.applied,
                        "probes": graded,
                        "passed": sum(graded),
                        "retained": sum(kept),
                        "repeated": repeated,
                        "action": [tool, args],
                    }
                )
        metrics[strategy] = {
            "probe_pass_rate": passed / questions if questions else 0.0,
            "repeat_rate": repeats / len(selected) if selected else 0.0,
            "retention_rate": retained / questions if questions else 0.0,
            "summarized": float(summarized),
            "samples": float(len(selected)),
        }
    return metrics, rows


# --- 离线基线 ---

_TERM = re.compile(r"[A-Za-z0-9_./:-]+|[一-鿿]")


def _terms(text: str) -> set[str]:
    chars = _TERM.findall(text.lower())
    words = {c for c in chars if len(c) > 1}
    cjk = [c for c in chars if len(c) == 1 and "一" <= c <= "鿿"]
    return words | {a + b for a, b in zip(cjk, cjk[1:], strict=False)}


def _context_lines(messages: Sequence[mg.ChatMessage]) -> list[str]:
    lines: list[str] = []
    for m in messages:
        lines += [line for line in m.text.splitlines() if line.strip()]
        lines += [f"{c.name} {json.dumps(json_format.MessageToDict(c.arguments), ensure_ascii=False)}" for c in m.tool_calls]
    return lines


def lexical_answer(messages: Sequence[mg.ChatMessage], question: str) -> str:
    """按词面作答：上下文里与问题重合最多的一行（去掉问题本身的那一行）。"""
    wanted = _terms(question.replace("____", " "))
    best, score = "", 0
    for line in _context_lines(messages):
        overlap = len(wanted & _terms(line))
        if overlap > score:
            best, score = line, overlap
    return best if score else "不知道"


class ExtractiveGateway:
    """离线基线用的假 Model Gateway：增量摘要按抽取（意图取 Goal，产物取路径，未决取报错），Probe 按词面作答。"""

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        messages = list(request.generation.messages)
        if request.call.node_id.endswith("#summary"):
            text = messages[-1].text
            goal = text.splitlines()[0].removeprefix("Goal：") if text else ""
            answer: Any = {s: [] for s in SECTIONS}
            answer["intent"] = [goal] if goal else []
            answer["artifacts"] = sorted(set(re.findall(r"[\w.-]+/[\w./-]+\.\w+", text)))[:10]
            answer["open"] = sorted(set(re.findall(r'"error": "([^"]{1,120})"', text)))[:5]
        else:
            questions = json.loads(messages[-1].text).get("questions", [])
            answer = {"answers": [lexical_answer(messages[:-1], q) for q in questions]}
        value = struct_pb2.Value()
        json_format.ParseDict(answer, value)
        return mg.GenerateResponse(result=value, finish_reason=mg.FINISH_REASON_STOP, model=mg.ModelChoice(tier=request.call.tier, vendor="extractive"))

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        raise AssertionError("压缩评测不做嵌入")


class LexicalSubject:
    """离线基线：按词面在上下文里找答案、把上下文里最后一个动作重做一遍，用于检查评测链路；不代表模型的真实表现。"""

    name = "lexical"

    def __init__(self, gateway: ModelGateway | None = None) -> None:
        self.gateway = gateway or ExtractiveGateway()

    async def answer(self, messages: Sequence[mg.ChatMessage], questions: Sequence[str]) -> list[str]:
        return [lexical_answer(messages, q) for q in questions]

    async def next_action(self, messages: Sequence[mg.ChatMessage], tools: Sequence[str]) -> tuple[str, dict[str, Any]]:
        calls = [c for m in messages for c in m.tool_calls]
        if not calls:
            return (str(tools[0]), {}) if tools else ("", {})
        return calls[-1].name, json_format.MessageToDict(calls[-1].arguments)


# --- 真模型 ---


def _value(data: Any) -> struct_pb2.Value:
    value = struct_pb2.Value()
    json_format.ParseDict(data, value)
    return value


class GatewayContextSubject:
    """经 Model Gateway 的被测模型：答题用结构化输出；下一步动作与 tool_loop 一样带着工具定义问模型。"""

    def __init__(self, gateway: ModelGateway, *, tier: str = TOOL_LOOP_TIER, name: str = "worker-context") -> None:
        self.gateway = gateway
        self.tier = tier
        self.name = name

    def _request(self, node_id: str, params: mg.GenerateParams) -> mg.GenerateRequest:
        return mg.GenerateRequest(
            tenant=tenant_context(Tenant(tenant_id=EVAL_TENANT)),
            call=mg.CallContext(node_id=node_id, role=enums_pb2.ROLE_ACT, tier=self.tier),
            generation=params,
        )

    async def answer(self, messages: Sequence[mg.ChatMessage], questions: Sequence[str]) -> list[str]:
        payload = json.dumps({"instruction": ANSWER_INSTRUCTION, "questions": list(questions)}, ensure_ascii=False)
        params = mg.GenerateParams(
            messages=[*messages, mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=payload)],
            output_schema=payload_pb2.JsonPayload(value=_value(ANSWER_SCHEMA)),
        )
        response = await self.gateway.generate(self._request("context_eval#answer", params))
        answers = json_format.MessageToDict(response.result).get("answers") if response.HasField("result") else None
        return [str(a) for a in answers] if isinstance(answers, list) else []

    async def next_action(self, messages: Sequence[mg.ChatMessage], tools: Sequence[str]) -> tuple[str, dict[str, Any]]:
        params = mg.GenerateParams(
            messages=list(messages),
            tools=[mg.ToolSpec(name=t, params_schema=payload_pb2.JsonPayload(value=_value({"type": "object"}))) for t in tools],
        )
        response = await self.gateway.generate(self._request("context_eval#next", params))
        if response.tool_calls:
            return response.tool_calls[0].name, json_format.MessageToDict(response.tool_calls[0].arguments)
        return "", {}


def load_context_subject(spec: str) -> ContextSubject:
    """lexical，或 <模块>:<工厂函数>（工厂函数不带参数、返回 ContextSubject）。"""
    if spec == "lexical":
        return LexicalSubject()
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError("--subject 要写 lexical 或 <模块>:<工厂函数>")
    try:
        factory = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"找不到被测对象 {spec}：{exc}") from exc
    return factory()
