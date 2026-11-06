"""Real DeepEval 4.3.1 prompt probe with an in-process deterministic judge.

Run with the pinned framework interpreter. This verifies framework input routing;
it does not call a real model and does not substitute for the live EV-DE run.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path

os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] = "YES"

from deepeval.models import DeepEvalBaseLLM


class OfflineJudge(DeepEvalBaseLLM):
    def __init__(self):
        self.prompts = []
        super().__init__(model="offline-fixture")

    def load_model(self):
        return self

    def get_model_name(self):
        return "offline-fixture"

    def generate(self, prompt, schema=None, **kwargs):
        self.prompts.append(prompt)
        data = {"score": 9, "reason": "probe"}
        return schema.model_validate(data) if schema is not None else json.dumps(data)

    async def a_generate(self, prompt, schema=None, **kwargs):
        return self.generate(prompt, schema, **kwargs)


def main():
    assert importlib.metadata.version("deepeval") == "4.3.1"
    path = Path(__file__).parents[1] / "src/agentruntime_learning/evals/bridges/deepeval_bridge.py"
    spec = importlib.util.spec_from_file_location("evde_bridge_probe", path)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    results = []
    for name, params in (("correctness", ["input", "actual_output", "expected_output"]),
                         ("honesty", ["input", "actual_output", "tool_evidence"])):
        judge = OfflineJudge()
        row = {"id": "probe", "input": "total?", "actual_output": "243", "expected_output": "243",
               "tool_evidence": ["EVIDENCE_ONLY_7281"], "metrics": [{"name": name, "kind": "geval",
               "evaluation_steps": ["Compare the supplied evidence."], "params": params}]}
        result = bridge.score_case(row, judge)
        present = any("EVIDENCE_ONLY_7281" in prompt for prompt in judge.prompts)
        assert result["scores"][name]["score"] == 0.9 and judge.prompts
        assert present == (name == "honesty")
        results.append({"metric": name, "tool_evidence_in_prompt": present, "prompt_count": len(judge.prompts)})
    print(json.dumps({"framework": "deepeval", "version": "4.3.1", "model_calls": 0, "checks": results}))


if __name__ == "__main__":
    main()
