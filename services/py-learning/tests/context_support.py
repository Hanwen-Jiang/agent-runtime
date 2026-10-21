"""S-11 评测测试共用：在临时目录里写长任务样本，假 Model Gateway（增量摘要与 Probe 按脚本作答），按脚本作答的被测模型。"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from google.protobuf import json_format, struct_pb2

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg

from support import SESSION_ID, TASK_ID

SECTIONS = ("intent", "decisions", "artifacts", "open", "next")


def filler(i: int, chars: int = 600) -> str:
    head = f"<<step {i} body>> "
    return head + ("lorem ipsum dolor sit amet " * (chars // 27 + 1))[: max(chars - len(head), 0)]


def long_history(n: int = 40) -> list[dict[str, Any]]:
    steps = []
    for i in range(n):
        if i == 3:
            steps.append({"tool": "repo_read", "args": {"path": "docker-compose.yml"}, "observation": {"exit_code": 0, "content": "redis 映射到 6379 端口；" + filler(i)}})
        elif i % 3 == 2:
            steps.append({"tool": "run_tests", "args": {"target": "make test"}, "observation": {"exit_code": 1, "failed": ["test_checkout"], "stdout": filler(i)}})
        else:
            steps.append({"tool": "repo_read", "args": {"path": f"src/module_{i}.py"}, "observation": {"exit_code": 0, "content": filler(i)}})
    return steps


def context_sample(sample_id: str = "c_coding", *, split: str = "dev", work_mode: str = "coding", review: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    sample: dict[str, Any] = {
        "kind": "context",
        "id": sample_id,
        "split": split,
        "review": review or {"status": "draft"},
        "description": f"长任务样本 {sample_id}",
        "work_mode": work_mode,
        "effective_tokens": 4_000,
        "task_state": {
            "task_id": TASK_ID,
            "session_id": SESSION_ID,
            "goal": "修复 CI 里失败的 test_checkout",
            "done_when": "make test 全部通过",
            "facts": ["测试依赖本地 Redis 的 6379 端口"],
            "open_questions": ["fixtures/orders.json 是否需要更新"],
        },
        "task_plan": {"triage": {"level": "full", "task_family": "engineering.ci_fix"}, "horizon": "per_stage", "context": {"work_mode": work_mode}},
        "node": {"id": "fix", "kind": "goal", "goal": "修复 test_checkout", "done_when": "make test 通过", "mode": "tool_loop", "tools": ["repo_read", "run_tests", "repo_edit"], "output_schema": {"type": "object"}},
        "history": long_history(),
        "probes": [
            {"question": "docker-compose 里 redis 映射到哪个端口？", "answer": "6379"},
            {"question": "哪个测试失败了？", "answer": "test_checkout"},
            {"question": "还有哪个 fixture 文件待确认？", "answer": "fixtures/orders.json"},
        ],
        "completed": [{"tool": "repo_read", "args": {"path": "docker-compose.yml"}}],
    }
    sample.update(extra)
    return sample


def write_samples(root: Path, samples: Sequence[dict[str, Any]]) -> Path:
    (root / "context_samples").mkdir(parents=True, exist_ok=True)
    for sample in samples:
        (root / "context_samples" / f"{sample['id']}.json").write_text(json.dumps(sample, ensure_ascii=False, indent=2), "utf-8")
    return root


class CompactionGateway:
    """增量摘要与 Compaction Probe 用的假 Model Gateway：摘要返回固定的增量，Probe 按 probe_fn 作答（默认全答“不知道”）。"""

    def __init__(self, *, summary: dict[str, list[str]] | None = None, probe_fn: Callable[[mg.GenerateRequest], dict[str, Any]] | None = None) -> None:
        self.summary = summary or {s: [] for s in SECTIONS} | {"intent": ["修复 test_checkout"]}
        self.probe_fn = probe_fn
        self.requests: list[mg.GenerateRequest] = []

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        self.requests.append(request)
        if request.call.node_id.endswith("#summary"):
            answer: Any = self.summary
        elif request.call.node_id.endswith("#probe"):
            questions = json.loads(request.generation.messages[-1].text)["questions"]
            answer = self.probe_fn(request) if self.probe_fn else {"answers": ["不知道"] * len(questions)}
        else:
            raise AssertionError(f"压缩评测里不该有别的模型调用：{request.call.node_id}")
        value = struct_pb2.Value()
        json_format.ParseDict(answer, value)
        return mg.GenerateResponse(result=value, finish_reason=mg.FINISH_REASON_STOP, model=mg.ModelChoice(tier=request.call.tier, vendor="mock"))

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        raise AssertionError("压缩评测不做嵌入")


def text_of(messages: Sequence[mg.ChatMessage]) -> str:
    return "\n".join(m.text for m in messages)


class ScriptedContextSubject:
    """按脚本作答：answer_fn(上下文文本, 问题) → 回答；action_fn(上下文文本) → (工具, 参数)。记下每次看到的上下文。"""

    def __init__(
        self,
        gateway: Any,
        *,
        answer_fn: Callable[[str, str], str] | None = None,
        action_fn: Callable[[str], tuple[str, dict[str, Any]]] | None = None,
        name: str = "scripted",
    ) -> None:
        self.name = name
        self.gateway = gateway
        self.answer_fn = answer_fn or (lambda context, question: "")
        self.action_fn = action_fn or (lambda context: ("repo_edit", {"path": "src/checkout.py"}))
        self.seen: list[str] = []

    async def answer(self, messages: Sequence[mg.ChatMessage], questions: Sequence[str]) -> list[str]:
        context = text_of(messages)
        self.seen.append(context)
        return [self.answer_fn(context, q) for q in questions]

    async def next_action(self, messages: Sequence[mg.ChatMessage], tools: Sequence[str]) -> tuple[str, dict[str, Any]]:
        return self.action_fn(text_of(messages))
