"""E4：每个 Task 的第 1 个 Round 是不是“只有 Atomic 模型节点、没有 tool_loop”的链；这样的 Round 之后多花了几个 Round、多少钱。"""

import json
import sys
from pathlib import Path

from agentruntime_learning.pipeline.traces import read_spans

sys.stdout.reconfigure(encoding="utf-8")
spans = read_spans(Path(sys.argv[1]).read_text("utf-8").splitlines(), "otlp_json")
tasks = {t["task_id"]: t for t in json.loads(Path(sys.argv[2]).read_text("utf-8"))}
roots = {s.span_id: s for s in spans if s.name == "invoke_workflow"}
by_task: dict[str, list] = {}
for s in spans:
    if s.name == "runtime.round" and s.parent_span_id in roots:
        by_task.setdefault(roots[s.parent_span_id].attributes["task_id"], []).append(s)
rows = []
for tid, rs in by_task.items():
    rs.sort(key=lambda s: int(s.attributes.get("round_seq") or 0))
    planned = [r for r in rs if r.attributes.get("triage_level")]

    def no_tools(r):
        nodes = json.loads(r.attributes.get("runtime.stage_shape") or "{}").get("nodes", [])
        tool_steps = [n for n in nodes if n.get("kind") == "atomic" and any(k in n.get("id", "") for k in ("run", "read", "fix", "verify", "test", "edit"))]
        has_loop = any(n.get("kind") == "goal" for n in nodes)
        return bool(tool_steps) and not has_loop

    wasted = [r for r in planned if no_tools(r)]
    root = roots[next(s.parent_span_id for s in rs)]
    rows.append((tasks[tid]["workspace"], tasks[tid].get("status"), len(rs), len(wasted), round(sum(float(r.attributes.get("runtime.cost_usd") or 0) for r in wasted), 3),
                 round(float(root.attributes.get("runtime.cost_usd") or 0), 3)))
for r in sorted(rows):
    print(r)
n_tasks_hit = sum(1 for r in rows if r[3])
print(f"有“无工具的 Atomic 链”Round 的 Task：{n_tasks_hit}/{len(rows)}；这些 Round 共 {sum(r[3] for r in rows)} 个，占全部 Round {sum(r[2] for r in rows)} 个；"
      f"花费 ${sum(r[4] for r in rows):.2f} / 全部 ${sum(r[5] for r in rows):.2f}")
