"""E4：主题里有什么：按 Learning 的读法（快照 → otlp_json → task_traces）列出租户 t13 的 Task：任务族、结局、各 Round 的 verdict 与形状摘要。"""

import json
import sys
from collections import Counter

from agentruntime_learning.pipeline.stream import TopicReader
from agentruntime_learning.pipeline.traces import read_spans, task_traces

sys.stdout.reconfigure(encoding="utf-8")
reader = TopicReader(sys.argv[1], "telemetry.spans", "e4-inspect")
try:
    snap = reader.snapshot()
finally:
    reader.close()
spans = read_spans(snap.lines(), "otlp_json")
print("快照：", snap.sources, "记录", len(snap.records), "span", len(spans), Counter(s.name for s in spans).most_common(8))
ts = task_traces(spans, "t13")
print("t13 的 Task", len(ts.tasks), "跳过", ts.skipped, "重复", ts.duplicates, "别的租户", ts.other_tenant_spans)
for t in ts.tasks:
    rounds = [(r.seq, r.verdict, r.triage_level, len((r.shape or {}).get("nodes", []))) for r in t.rounds]
    print(t.task_id, t.task_family, t.status, f"${t.cost_usd:.3f}", rounds)
print(Counter(t.task_family for t in ts.tasks))
