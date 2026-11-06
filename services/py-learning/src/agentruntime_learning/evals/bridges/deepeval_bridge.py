"""DeepEval 的桥（docs/plan/feedback-2026-11-01.md N-12）：在装了 deepeval 的单独 Python 里跑，只用标准库与 deepeval。

    <python> -I deepeval_bridge.py --cases cases.jsonl --out scores.jsonl

不 import 本仓库的任何代码（-I 也不读 PYTHONPATH 与当前目录），不进本仓库的依赖（同 SWE-bench 的官方判分工具）。

输入每行一条：{"id", "input", "actual_output", "expected_output"?, "context"?: [..], "tool_evidence"?: [..], "metrics": [{"name", "kind", ...}]}
- kind = geval：G-Eval，按 criteria（或 evaluation_steps）打 0～1 分；params 是参与评判的字段（input、actual_output、expected_output、context、tool_evidence）。
- tool_evidence 映射为 DeepEval context；原有 context 仍为 retrieval_context，两者分开。
- kind = answer_relevancy：回答与问题的相关性。
- kind = faithfulness：回答相对 context（retrieval_context）是否忠实。
输出每行一条：{"id", "scores": {name: {"score", "success", "reason"}}, "error"?}；某个指标出错只记在那一项。

判分的模型经 OpenAI 兼容的接口（环境变量 AR_JUDGE_BASE_URL、AR_JUDGE_API_KEY、AR_JUDGE_MODEL），密钥只在请求头里，不进输出与错误说明。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric, GEval
from deepeval.models import DeepEvalBaseLLM
from deepeval.test_case import LLMTestCase

try:  # deepeval 3.x 改过名字
    from deepeval.test_case import SingleTurnParams as Params
except ImportError:  # pragma: no cover
    from deepeval.test_case import LLMTestCaseParams as Params  # type: ignore[no-redef]

PARAMS = {"input": Params.INPUT, "actual_output": Params.ACTUAL_OUTPUT, "expected_output": Params.EXPECTED_OUTPUT,
          "context": Params.RETRIEVAL_CONTEXT, "tool_evidence": Params.CONTEXT}
TIMEOUT_S = 120


class JudgeError(RuntimeError):
    pass


def chat(prompt: str, *, json_mode: bool) -> str:
    base = os.environ["AR_JUDGE_BASE_URL"].rstrip("/")
    body: dict[str, Any] = {"model": os.environ["AR_JUDGE_MODEL"], "messages": [{"role": "user", "content": prompt}], "temperature": 0}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        base + "/chat/completions", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer " + os.environ["AR_JUDGE_API_KEY"], "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 — 地址来自调用方的配置
            data = json.loads(resp.read())
    except urllib.error.HTTPError as err:
        raise JudgeError(f"judge HTTP {err.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise JudgeError(f"judge unreachable ({type(err).__name__})") from None
    try:
        return str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        raise JudgeError("judge answer malformed") from None


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()


class Judge(DeepEvalBaseLLM):
    """deepeval 的自定义模型：经 OpenAI 兼容的接口；要结构化输出时开 JSON 模式并按 schema 校验。"""

    def __init__(self) -> None:
        super().__init__(model=os.environ.get("AR_JUDGE_MODEL", "judge"))

    def load_model(self) -> Any:
        return self

    def generate(self, prompt: str, schema: Any = None) -> Any:
        text = chat(prompt, json_mode=schema is not None)
        if schema is None:
            return text
        return schema.model_validate_json(_strip_fence(text))

    async def a_generate(self, prompt: str, schema: Any = None) -> Any:
        return self.generate(prompt, schema)

    def get_model_name(self) -> str:
        return os.environ.get("AR_JUDGE_MODEL", "judge")


def build_metric(spec: dict[str, Any], judge: Judge) -> Any:
    kind = spec.get("kind")
    threshold = float(spec.get("threshold", 0.5))
    if kind == "geval":
        params = [PARAMS[p] for p in spec.get("params") or ["input", "actual_output"]]
        steps = spec.get("evaluation_steps") or None
        return GEval(name=spec["name"], criteria=None if steps else spec["criteria"], evaluation_steps=steps, evaluation_params=params, model=judge,
                     threshold=threshold, async_mode=False)
    if kind == "answer_relevancy":
        return AnswerRelevancyMetric(model=judge, threshold=threshold, async_mode=False)
    if kind == "faithfulness":
        return FaithfulnessMetric(model=judge, threshold=threshold, async_mode=False)
    raise ValueError(f"unknown metric kind {kind!r}")


def score_case(case: dict[str, Any], judge: Judge) -> dict[str, Any]:
    scores: dict[str, Any] = {}
    for spec in case.get("metrics") or []:
        tc = LLMTestCase(
            input=case["input"], actual_output=case.get("actual_output") or "", expected_output=case.get("expected_output"),
            retrieval_context=case.get("context") or None,
            context=(case.get("tool_evidence") or None) if "tool_evidence" in spec.get("params", []) else None,
        )
        try:
            metric = build_metric(spec, judge)
            metric.measure(tc)
            scores[spec["name"]] = {"score": float(metric.score), "success": bool(metric.success), "reason": str(metric.reason or "")[:1000]}
        except Exception as exc:  # noqa: BLE001 — 一个指标出错只记这一项（不带异常文本：可能有请求内容）
            scores[spec["name"]] = {"score": None, "success": False, "error": type(exc).__name__ + (f": {exc}" if isinstance(exc, (JudgeError, ValueError)) else "")}
    return {"id": case["id"], "scores": scores}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DeepEval bridge")
    parser.add_argument("--cases", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    for name in ("AR_JUDGE_BASE_URL", "AR_JUDGE_API_KEY", "AR_JUDGE_MODEL"):
        if not os.environ.get(name):
            print(f"missing {name}", file=sys.stderr)
            return 2
    os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
    judge = Judge()
    with open(args.cases, encoding="utf-8") as src, open(args.out, "w", encoding="utf-8") as out:
        for line in src:
            if line.strip():
                out.write(json.dumps(score_case(json.loads(line), judge), ensure_ascii=False) + "\n")
                out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
