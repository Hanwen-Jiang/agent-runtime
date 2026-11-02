"""S-11 的长任务样本当能力矩阵的题（docs/plan/capability-matrix-evals.md 第 3 节，role transform：压缩、摘要）。

一条样本一道题：按 full 策略（整个 Compaction Ladder，含增量摘要与 Compaction Probe）用 Worker 的 Context Builder 压缩；
增量摘要是 transform 调用，被钉在候选上。答 Probe 题与给下一步动作的是 act 调用、Compaction Probe 是 judge 调用，
都照常选档（mask_only 不调 transform，不跑）。复用 evals.context 的压缩、判分与被测模型。

题的分数：Probe 题答对的数加上“没有重做已完成的动作”这一项，除以（Probe 题数 + 1）。一条样本出错（例如模型给的 JSON
解析不了）记 0 分并带上错误，同 S-11 评测。没走到 summarize 的（transform 没被调用）在 record 里记 summarized = false。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from agentruntime_worker.executor.model_gateway import ModelGateway
from agentruntime_worker.context.probe import ProbeFact, grade

from agentruntime_learning.capability.base import Item, ItemResult
from agentruntime_learning.capability.evals.s09 import pick
from agentruntime_learning.evals.cases import DEFAULT_EVAL_SET
from agentruntime_learning.evals.context import CONTEXT_DIR, ContextSample, GatewayContextSubject, _action, build_context, load_context_samples

SPLITS = ("all", "dev", "holdout")
STRATEGY = "full"


def _version(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((root / CONTEXT_DIR).glob("*.json")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:12]


class S11Compress:
    name = "s11"
    role = "transform"
    # 增量摘要：长轨迹约 8k 输入、600 输出（可能不止一次）；答题与下一步动作照常选档，另计。
    est_tokens = (8000, 600)
    est_other_usd = 0.004

    def __init__(self, eval_set: str | Path | None = None, split: str = "all") -> None:
        if split not in SPLITS:
            raise ValueError(f"split 只能是 {'、'.join(SPLITS)}，得到 {split!r}")
        root = Path(eval_set) if eval_set else DEFAULT_EVAL_SET
        samples = load_context_samples(root)
        self.samples: dict[str, ContextSample] = {s.id: s for s in samples if split == "all" or s.split == split}
        self.version = _version(root)

    def items(self, n: int, seed: int) -> list[Item]:
        return [Item(id=i, payload={"split": self.samples[i].split}) for i in pick(list(self.samples), n, seed)]

    async def run_item(self, item: Item, gateway: ModelGateway, *, candidate: str) -> ItemResult:
        sample = self.samples[item.id]
        subject = GatewayContextSubject(gateway)
        record: dict[str, Any] = {"split": sample.split, "probes": len(sample.probes)}
        try:
            with tempfile.TemporaryDirectory(prefix="capability-s11-") as scratch:
                built = await build_context(sample, STRATEGY, gateway, Path(scratch))
            answers = await subject.answer(built.messages, [p["question"] for p in sample.probes])
            graded = [
                grade(ProbeFact(category="label", text=p["answer"], answer=p["answer"]), answers[i] if i < len(answers) else "")
                for i, p in enumerate(sample.probes)
            ]
            tool, args = await subject.next_action(built.messages, list(sample.node.get("tools") or []))
            repeated = _action(tool, args) in {_action(d["tool"], d["args"]) for d in sample.completed}
        except Exception as exc:  # noqa: BLE001 — 同 S-11 评测：一条样本出错只记这一条
            record["error"] = f"{type(exc).__name__}: {exc}"[:300]
            return ItemResult(item.id, score=0.0, error=record["error"], record=record)
        record.update(
            {
                "applied": list(built.applied),
                "summarized": "summarize" in built.applied,
                "passed": sum(graded),
                "repeated": repeated,
            }
        )
        if sample.split == "dev":  # 留出集不记模型给的动作（可能抄了样本内容）
            record["action"] = [tool, json.loads(json.dumps(args, ensure_ascii=False))]
        score = (sum(graded) + (0 if repeated else 1)) / (len(sample.probes) + 1)
        return ItemResult(item.id, score=score, record=record)


def make(eval_set: str | None = None, split: str = "all") -> S11Compress:
    return S11Compress(eval_set, split)
