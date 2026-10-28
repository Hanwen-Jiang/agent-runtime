"""E4：从 Collector 的文件导出读出几个 Task 的各 Round 的 Stage 形状（Node 的 id、kind、mode、template、tools）。"""

import json
import sys
from pathlib import Path

from agentruntime_learning.pipeline.traces import read_spans

sys.stdout.reconfigure(encoding="utf-8")
spans = read_spans(Path(sys.argv[1]).read_text("utf-8").splitlines(), "otlp_json")
want = set(sys.argv[2:])
roots = {s.span_id: s.attributes.get("task_id") for s in spans if s.name == "invoke_workflow" and s.attributes.get("task_id") in want}
for s in sorted((s for s in spans if s.name == "runtime.round" and s.parent_span_id in roots), key=lambda s: (roots[s.parent_span_id], int(s.attributes.get("round_seq") or 0))):
    shape = json.loads(s.attributes.get("runtime.stage_shape") or "{}")
    nodes = [(n.get("id"), n.get("kind"), n.get("mode") or n.get("template"), n.get("role")) for n in shape.get("nodes", [])]
    print(roots[s.parent_span_id][-6:], "round", s.attributes.get("round_seq"), s.attributes.get("verdict"), s.attributes.get("triage_level") or "-", "|", shape.get("goal", "")[:40], "|", nodes)
