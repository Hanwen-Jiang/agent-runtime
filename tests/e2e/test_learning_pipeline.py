"""T-16 的演示：Learning Pipeline 对真的 Registry（task-service 的内部端口）跑两次（细化见 docs/plan/learning-pipeline.md）。

- 一个任务族的候选成功率不如现行做法：发布被拒，Registry 里没有它的 Recipe；
- 另一个任务族的候选过门槛：Recipe 经 Publish、Promote 成为该租户的默认版本；别的租户看不到它。

trace 按 T-15 的 span 约定直接写成控制台格式的文件（与 Worker 的收尾写回打出来的一样），命令行跑 `python -m agentruntime_learning.pipeline run`。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentruntime_worker.activities.types import Tenant
from agentruntime_worker.registry import GrpcRegistry

from e2e_support.stack import REPO_ROOT, Stack
from test_registry import registry  # noqa: F401 — 复用 Registry 的 fixture（种子、发布令牌、以 Registry 重启 task-service）

pytestmark = pytest.mark.e2e

PASSING = "finance.invoice_coding"
FAILING = "support.ticket_triage"
# 只用 Registry 的 fixture 已经种下的模板（sql_query@v1、answer@v1）：端到端环境在整个会话里共用，
# 这里另种模板会进入别的用例里新 Task 的固定版本。退役的版本仍可按版本号读到，所以 answer 被晋升到 v2 之后也不影响。
FETCH = {"id": "fetch", "kind": "atomic", "template": "sql_query@v1"}
CODE = {"id": "code", "kind": "atomic", "template": "answer@v1", "after": ["fetch"], "tier": "mid@low"}
CHECK = {"id": "check", "kind": "goal", "goal": "核对", "done_when": "总额一致", "mode": "tool_loop", "output_schema": {"type": "object"}}
ROUTE_A = [{"goal": "取数并归类", "exit_when": "都有科目", "nodes": [FETCH, CODE]}, {"goal": "核对", "exit_when": "一致", "nodes": [CHECK]}]
ROUTE_B = [{"goal": "一把做完", "exit_when": "交付", "nodes": [{**CHECK, "id": "do_all", "goal": "全部做完"}]}]


def _id(seed_text: str) -> str:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    digest = hashlib.sha256(seed_text.encode()).hexdigest()
    return "tsk_0" + "".join(alphabet[int(c, 16) % 32] for c in digest[:25])


def spans(tenant: str, family: str, n: int, route: list[dict[str, Any]], status: str, cost: float = 0.1) -> list[str]:
    tid = _id(f"{tenant}/{family}/{n}/{status}/{len(route)}")
    trace = hashlib.sha256(tid.encode()).hexdigest()[:32]
    root = hashlib.sha256(("root" + tid).encode()).hexdigest()[:16]
    scope = {"tenant_id": tenant, "user_id": "u1"}
    lines = [
        {"kind": "span", "name": "invoke_workflow", "trace_id": trace, "span_id": root, "parent_span_id": "", "status": "ok", "events": [],
         "attributes": {**scope, "task_id": tid, "gen_ai.operation.name": "invoke_workflow", "recipe": "", "runtime.task_family": family,
                        "runtime.task_status": status, "runtime.cost_usd": cost, "runtime.rounds": len(route), "runtime.synthetic": True}}
    ]
    for seq, shape in enumerate(route, start=1):
        last = seq == len(route)
        verdict = ("done" if status == "done" else "abort") if last else "continue"
        lines.append(
            {"kind": "span", "name": "runtime.round", "trace_id": trace, "span_id": hashlib.sha256(f"{seq}{tid}".encode()).hexdigest()[:16], "parent_span_id": root,
             "status": "ok", "events": [], "attributes": {**scope, "task_id": tid, "round_seq": seq, "verdict": verdict, "triage_level": "full",
                                                          "runtime.cost_usd": round(cost / len(route), 6), "runtime.synthetic": True,
                                                          "runtime.stage_shape": json.dumps(shape, ensure_ascii=False, sort_keys=True)}}
        )
    return [json.dumps(line, ensure_ascii=False) for line in lines]


def population(tenant: str, family: str, *, a_success: int, a_fail: int, b_success: int, b_fail: int) -> list[str]:
    out: list[str] = []
    for i in range(a_success):
        out += spans(tenant, family, i, ROUTE_A, "done")
    for i in range(a_fail):
        out += spans(tenant, family, i, ROUTE_A, "aborted")
    for i in range(b_success):
        out += spans(tenant, family, i, ROUTE_B, "done")
    for i in range(b_fail):
        out += spans(tenant, family, i, ROUTE_B, "aborted")
    return out


def run_pipeline(stack: Stack, address: str, traces: Path, family: str) -> dict[str, Any]:
    reports = stack.root / "learning-reports"
    proc = subprocess.run(
        [sys.executable, "-m", "agentruntime_learning.pipeline", "run", "--tenant", "t1", "--family", family, "--traces", str(traces),
         "--format", "console", "--report-dir", str(reports), "--budget-usd", "0"],
        env={**os.environ, "AR_REGISTRY_ADDRESS": address, "AR_SECRETS_FILE": str(stack.secrets_file)},
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, f"pipeline 失败：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


async def learned(address: str, tenant: str, family: str) -> list[Any]:
    client = GrpcRegistry(address)
    try:
        return [r for r in await client.list_recipes(Tenant(tenant_id=tenant, user_id="u1"), family) if r.name.startswith("learned_")]
    finally:
        await client.aclose()


def test_rejected_then_published(stack: Stack, registry: str) -> None:  # noqa: F811
    traces = stack.root / "spans.jsonl"
    lines = [
        *population("t1", PASSING, a_success=115, a_fail=5, b_success=85, b_fail=35),
        *population("t1", FAILING, a_success=100, a_fail=40, b_success=90, b_fail=2),
        *population("t2", PASSING, a_success=10, a_fail=90, b_success=0, b_fail=0),
    ]
    traces.write_text("\n".join(lines), "utf-8")

    rejected = run_pipeline(stack, registry, traces, FAILING)
    assert rejected["status"] == "rejected", rejected
    assert asyncio.run(learned(registry, "t1", FAILING)) == [], "未过门槛，Registry 里没有这个任务族学来的 Recipe"

    published = run_pipeline(stack, registry, traces, PASSING)
    assert published["status"] == "published", published
    [recipe] = asyncio.run(learned(registry, "t1", PASSING))
    assert f"{recipe.name}@{recipe.version}" == published["recipe"]
    assert asyncio.run(learned(registry, "t2", PASSING)) == [], "t1 学来的 Recipe 在 t1 的作用域，t2 看不到"
