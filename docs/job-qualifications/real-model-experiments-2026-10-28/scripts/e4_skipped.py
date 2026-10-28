"""E4：task_traces 跳过的 Task 缺的是什么。"""

import json
import sys

from agentruntime_learning.pipeline.stream import TopicReader
from agentruntime_learning.pipeline.traces import read_spans

sys.stdout.reconfigure(encoding="utf-8")
reader = TopicReader(sys.argv[1], "telemetry.spans", "e4-inspect")
try:
    snap = reader.snapshot()
finally:
    reader.close()
spans = read_spans(snap.lines(), "otlp_json")
roots = [s for s in spans if s.name == "invoke_workflow"]
print("invoke_workflow spans:", len(roots))
for s in roots:
    a = s.attributes
    print(a.get("task_id"), "family=", a.get("runtime.task_family"), "status=", a.get("runtime.task_status"), "tenant=", a.get("tenant_id"),
          "keys=", sorted(k for k in a if k.startswith("runtime.")))
round_parents = {s.parent_span_id for s in spans if s.name == "runtime.round"}
root_ids = {s.span_id for s in roots}
print("Round 的父 span 不是任何 invoke_workflow 的：", len(round_parents - root_ids), "个父 span")
tasks = json.loads(open(sys.argv[2], encoding="utf-8").read())
seen = {s.attributes.get("task_id") for s in roots}
for t in tasks:
    print(t["workspace"], t["task_id"], t.get("status"), "有 invoke_workflow" if t["task_id"] in seen else "没有 invoke_workflow")
