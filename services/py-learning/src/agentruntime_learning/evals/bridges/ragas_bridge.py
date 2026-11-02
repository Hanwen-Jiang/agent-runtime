"""Ragas 的桥（docs/plan/feedback-2026-11-01.md N-13）：在装了 ragas 的单独 Python 里跑，只用标准库、ragas 与 langchain-openai。

    <python> -I ragas_bridge.py --cases cases.jsonl --out scores.jsonl [--offline]

不 import 本仓库的任何代码，不进本仓库的依赖（同 deepeval_bridge）。

输入每行一条：{"id", "user_input", "retrieved_contexts": [..], "reference_contexts"?: [..], "reference"?, "response"?}
输出每行一条：{"id", "scores": {指标: 分数或 null}, "errors"?: {指标: 类型}}。

--offline：只算不要模型的指标（检索的上下文与参考上下文按字符串相似度比：non_llm_context_precision、non_llm_context_recall）。
不加时另算要模型的：context_precision（带参考答案）、context_recall；有 response 时再加 faithfulness 与 answer_relevancy（要嵌入模型）。
判分与嵌入的模型经 OpenAI 兼容的接口：AR_JUDGE_BASE_URL、AR_JUDGE_API_KEY、AR_JUDGE_MODEL、AR_JUDGE_EMBEDDING_MODEL。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from ragas.dataset_schema import SingleTurnSample
from ragas.metrics import NonLLMContextPrecisionWithReference, NonLLMContextRecall

TIMEOUT_S = 180


def offline_metrics() -> dict[str, Any]:
    return {"non_llm_context_precision": NonLLMContextPrecisionWithReference(), "non_llm_context_recall": NonLLMContextRecall()}


def judged_metrics() -> tuple[dict[str, Any], dict[str, Any]]:
    """要模型的指标：（只要检索与参考的，还要 response 的）。"""
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import Faithfulness, LLMContextPrecisionWithReference, LLMContextRecall, ResponseRelevancy

    base, key = os.environ["AR_JUDGE_BASE_URL"], os.environ["AR_JUDGE_API_KEY"]
    llm = LangchainLLMWrapper(ChatOpenAI(model=os.environ["AR_JUDGE_MODEL"], base_url=base, api_key=key, temperature=0, timeout=TIMEOUT_S))
    retrieval = {"context_precision": LLMContextPrecisionWithReference(llm=llm), "context_recall": LLMContextRecall(llm=llm)}
    answer: dict[str, Any] = {"faithfulness": Faithfulness(llm=llm)}
    embedding_model = os.environ.get("AR_JUDGE_EMBEDDING_MODEL", "")
    if embedding_model:
        emb = LangchainEmbeddingsWrapper(OpenAIEmbeddings(model=embedding_model, base_url=base, api_key=key))
        answer["answer_relevancy"] = ResponseRelevancy(llm=llm, embeddings=emb)
    return retrieval, answer


async def score_case(case: dict[str, Any], retrieval: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    sample = SingleTurnSample(
        user_input=case["user_input"], retrieved_contexts=list(case.get("retrieved_contexts") or []),
        reference_contexts=list(case.get("reference_contexts") or []) or None, reference=case.get("reference"), response=case.get("response"),
    )
    metrics = dict(retrieval)
    if case.get("response"):
        metrics.update(answer)
    scores: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, metric in metrics.items():
        try:
            value = await metric.single_turn_ascore(sample)
            scores[name] = None if value is None or value != value else float(value)
        except Exception as exc:  # noqa: BLE001 — 一个指标出错只记这一项的类型（异常文本可能有请求内容）
            scores[name] = None
            errors[name] = type(exc).__name__
    out: dict[str, Any] = {"id": case["id"], "scores": scores}
    if errors:
        out["errors"] = errors
    return out


async def run(cases_path: str, out_path: str, offline: bool) -> None:
    retrieval = offline_metrics()
    answer: dict[str, Any] = {}
    if not offline:
        judged, answer = judged_metrics()
        retrieval.update(judged)
    with open(cases_path, encoding="utf-8") as src, open(out_path, "w", encoding="utf-8") as out:
        for line in src:
            if line.strip():
                out.write(json.dumps(await score_case(json.loads(line), retrieval, answer), ensure_ascii=False) + "\n")
                out.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ragas bridge")
    parser.add_argument("--cases", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    if not args.offline:
        for name in ("AR_JUDGE_BASE_URL", "AR_JUDGE_API_KEY", "AR_JUDGE_MODEL"):
            if not os.environ.get(name):
                print(f"missing {name}", file=sys.stderr)
                return 2
    os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")
    asyncio.run(run(args.cases, args.out, args.offline))
    return 0


if __name__ == "__main__":
    sys.exit(main())
