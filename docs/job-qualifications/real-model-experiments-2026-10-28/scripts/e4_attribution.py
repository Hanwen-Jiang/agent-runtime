"""E4：(1) 消费组 learning-e4 提交到了哪里、KEDA 会看到的延迟；(2) 对主题里失败（aborted）的 Task 直接跑真模型的失败归因（J.2），
因为它们的任务族是空的、Learning 按任务族过滤时进不了评测组。归因的输入与 Learning 一样只有结局、各 Round 的 verdict 与形状。"""

import asyncio
import json
import sys
from dataclasses import replace

from confluent_kafka import Consumer, TopicPartition

from agentruntime_learning.pipeline.attribution import attribute_failures
from agentruntime_learning.pipeline.budget import LearningBudget
from agentruntime_learning.pipeline.stream import TopicReader
from agentruntime_learning.pipeline.traces import read_spans, task_traces
from agentruntime_worker.executor.grpc_gateway import GrpcModelGateway

sys.stdout.reconfigure(encoding="utf-8")
brokers, gateway_address = sys.argv[1], sys.argv[2]
c = Consumer({"bootstrap.servers": brokers, "group.id": "learning-e4"})
tp = TopicPartition("telemetry.spans", 0)
committed = c.committed([tp], timeout=10)[0].offset
high = c.get_watermark_offsets(tp, timeout=10, cached=False)[1]
c.close()
print(f"learning-e4 提交到 {committed}，末尾 {high}，KEDA 看到的延迟 {high - committed}")

reader = TopicReader(brokers, "telemetry.spans", "e4-inspect")
try:
    snap = reader.snapshot()
finally:
    reader.close()
spans = read_spans(snap.lines(), "otlp_json")
# 任务族为空的 Task 在 task_traces 里被跳过；这里给它们补一个占位的任务族，只为了能交给归因。
patched = [replace(s, attributes={**s.attributes, "runtime.task_family": s.attributes.get("runtime.task_family") or "unclassified.code"})
           if s.name == "invoke_workflow" else s for s in spans]
failed = [t for t in task_traces(patched, "t13").tasks if t.status in ("aborted", "failed")]
print("失败的 Task：", [(t.task_id, t.status, [(r.verdict, r.triage_level) for r in t.rounds]) for t in failed])


async def main() -> None:
    gw = GrpcModelGateway(gateway_address)
    budget = LearningBudget(limit_usd=0.5)
    try:
        out = await attribute_failures(failed, tenant_id="t13", task_family="unclassified.code", gateway=gw, budget=budget)
    finally:
        await gw.aclose()
    for a in out:
        print(json.dumps(a.__dict__, ensure_ascii=False))
    print("预算：", budget.report())


asyncio.run(main())
