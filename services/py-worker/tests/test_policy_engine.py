"""T-09 验收：Policy Engine 本身（设计书 3.2.6、附录 F；细化见 docs/plan/policy-engine.md 第 2、3 节）。

不经 Temporal，直接调 PolicyEngine.run_hooks。覆盖：
- Hook 目录：默认 Hook 与策略 Hook 都在；每个结果合 hook_result.schema.json。
- fail-closed：名字不认识、挂错挂载点、Hook 抛异常，都按 deny；遇到第一个 deny 就停。
- 每个 Hook 的规则：budget_check、idempotency_check、single_writer_lock、trace_result、checkpoint、audit_log、pii_redaction、acceptance_recheck。
- R-01“Hook 里没有复杂推理”：结果确定；一千次执行在秒级以内；policy 包不 import 模型、gRPC 与网络库。
- 审计：deny 与 audit_log 的记录不含输入、结果的原文。
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import io
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.errors import TransientError

from agentruntime_worker.activities.types import HookCall, Tenant
from agentruntime_worker.executor import keys
from agentruntime_worker.policy import HOOKS, HookContext, HookSpec, PolicyEngine
from agentruntime_worker.workflows import stage_plan as sp

from .support import assert_valid

TENANT = Tenant(tenant_id="t1", user_id="u1")
TASK_ID = "tsk_policy"
# 有辨识度的“敏感”内容：断言它不出现在审计与 deny 的记录里。
SECRET = "机密-4f2a：客户 C-77 的银行账号 6222020200112233"
POLICY_DIR = Path(__file__).resolve().parents[1] / "src" / "agentruntime_worker" / "policy"


def make_engine(**kwargs: Any) -> tuple[PolicyEngine, ConsoleTelemetryProvider]:
    telemetry = ConsoleTelemetryProvider(stream=io.StringIO())
    return PolicyEngine(telemetry=telemetry, **kwargs), telemetry


def node(
    node_id: str = "q",
    *,
    effect: str = "read",
    role: str = "fetch",
    executor: str = "program",
    template: str = "sql_query@v1",
    inputs: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": node_id,
        "kind": "atomic",
        "template": template,
        "executor": executor,
        "effect": effect,
        "role": role,
        "inputs": inputs if inputs is not None else {"sql": "SELECT 1"},
        **extra,
    }
    if executor == "model":
        out.setdefault("output", "generate")
    return out


def before_node(
    n: dict[str, Any], *, task_usd: float | None = 2.0, spent_usd: float = 0.0, serialized: bool = True, key: str | None = None
) -> dict[str, Any]:
    """Stage Runner 给 before_node Hook 的 subject（细化第 2 节第 4 条）。"""
    subject: dict[str, Any] = {"node": n, "budget": {"task_usd": task_usd, "spent_usd": spent_usd}, "write_serialized": serialized}
    if n["effect"] in sp.WRITE_EFFECTS:
        subject["idempotency_key"] = key if key is not None else keys.idempotency_key(TENANT.tenant_id, TASK_ID, n["id"], n["inputs"])
    return subject


def call(mount: str, hooks: list[str], subject: dict[str, Any]) -> HookCall:
    return HookCall(tenant=TENANT, task_id=TASK_ID, round_seq=1, mount=mount, hooks=list(hooks), subject=subject)


async def run(engine: PolicyEngine, mount: str, hooks: list[str], subject: dict[str, Any]) -> list[dict[str, Any]]:
    results = await engine.run_hooks(call(mount, hooks, subject))
    for r in results:
        assert_valid("hook_result", r)
    return results


def decisions(results: list[dict[str, Any]]) -> list[str]:
    return [r["decision"] for r in results]


def exported_text(telemetry: ConsoleTelemetryProvider) -> str:
    records = [*telemetry.exported_spans, *telemetry.exported_logs, *telemetry.exported_metrics]
    return json.dumps([dataclasses.asdict(r) for r in records], ensure_ascii=False, default=str)


def spans(telemetry: ConsoleTelemetryProvider, name: str) -> list[Any]:
    return [s for s in telemetry.exported_spans if s.name == name]


class DownTelemetry:
    """Collector 不可用时的 Telemetry：记录照收，flush 抛 TransientError（OpenTelemetry 实现导出超时的样子）。"""

    def __init__(self) -> None:
        self.flushes = 0

    def bind(self, tenant: Any) -> DownTelemetry:
        return self

    @contextlib.asynccontextmanager
    async def start_span(self, name: str, attributes: dict[str, Any] | None = None) -> AsyncIterator[Any]:
        class _Span:
            def add_event(self, *_: Any, **__: Any) -> None:
                pass

        yield _Span()

    async def flush(self) -> None:
        self.flushes += 1
        raise TransientError("export_timeout", "导出到 Collector 超时")


# --- 目录与结果格式 ---

DEFAULT_HOOKS = {"budget_check", "idempotency_check", "single_writer_lock", "trace_result", "checkpoint", "audit_log"}
POLICY_HOOKS = {"pii_redaction", "acceptance_recheck"}


def test_the_catalog_has_the_default_and_policy_hooks() -> None:
    assert set(HOOKS) >= DEFAULT_HOOKS | POLICY_HOOKS
    for effect in ("none", "read", "write_reversible", "write_irreversible"):
        auto = sp.default_node_hooks(node(effect=effect, role="act" if effect in sp.WRITE_EFFECTS else "fetch"), "write_irreversible")
        for mount, names in auto.items():
            for name in names:
                assert name in HOOKS, f"按 effect 自动插入的 {name} 必须有实现"
                assert mount in HOOKS[name].mounts, f"{name} 要能挂在 {mount}"


async def test_every_hook_result_matches_the_schema() -> None:
    engine, _ = make_engine()
    write = node("w", effect="write_reversible", role="act", template="ledger_write@v1", inputs={"amount": 3})
    await run(engine, "before_node", ["budget_check", "idempotency_check", "single_writer_lock"], before_node(write))
    await run(engine, "after_node", ["trace_result", "checkpoint", "pii_redaction", "audit_log"], {"node": write, "result": {"ok": True, "mail": "a@b.cn"}})
    await run(engine, "before_stage", ["budget_check"], {"stage": {"seq": 1, "goal": "取数", "exit_when": "取到"}, "budget": {"task_usd": 2.0, "spent_usd": 0.0}})
    await run(engine, "before_finish", ["acceptance_recheck"], {"verdict": {"verdict": "done"}, "deliver_node": "answer", "deliver_status": "succeeded", "deliverable": {"text": "答案"}})


# --- fail-closed ---


async def test_an_unknown_hook_is_denied_and_stops_the_chain() -> None:
    engine, _ = make_engine()
    results = await run(engine, "after_node", ["no_such_hook", "audit_log"], {"node": node(), "result": {}})
    assert decisions(results) == ["deny"], "遇到第一个 deny 就停，后面的 Hook 不执行"
    assert results[0]["hook"] == "no_such_hook" and "no_such_hook" in results[0]["reason"]


async def test_a_hook_at_the_wrong_mount_is_denied() -> None:
    engine, _ = make_engine()
    results = await run(engine, "before_node", ["acceptance_recheck"], before_node(node()))
    assert decisions(results) == ["deny"]


async def test_a_failing_hook_is_denied_without_leaking_the_error_text() -> None:
    def boom(ctx: HookContext) -> dict[str, Any]:
        raise ValueError(SECRET)

    engine, telemetry = make_engine(hooks={**HOOKS, "boom": HookSpec(mounts=frozenset({"after_node"}), run=boom)})
    results = await run(engine, "after_node", ["boom", "audit_log"], {"node": node(), "result": {}})
    assert decisions(results) == ["deny"]
    assert "ValueError" in results[0]["reason"] and SECRET not in results[0]["reason"]
    assert SECRET not in exported_text(telemetry)


async def test_results_are_deterministic() -> None:
    engine, _ = make_engine()
    write = node("w", effect="write_reversible", role="act", inputs={"amount": 3})
    first = await run(engine, "before_node", ["budget_check", "idempotency_check", "single_writer_lock"], before_node(write))
    second = await run(engine, "before_node", ["budget_check", "idempotency_check", "single_writer_lock"], before_node(write))
    assert first == second


async def test_hooks_are_cheap() -> None:
    engine, _ = make_engine()
    read = node()
    started = time.perf_counter()
    for _ in range(1000):
        await engine.run_hooks(call("before_node", ["budget_check", "idempotency_check"], before_node(read)))
        await engine.run_hooks(call("after_node", ["trace_result", "audit_log"], {"node": read, "result": {"rows": [1, 2, 3]}}))
    assert time.perf_counter() - started < 5, "Hook 必须便宜（设计书 3.2.6 关键规则）"


def test_the_policy_package_does_not_call_models_or_the_network() -> None:
    banned = ("grpc", "httpx", "requests", "urllib", "socket", "aiohttp", "agentruntime_worker.executor.model_gateway",
              "agentruntime_worker.executor.grpc_gateway", "agentruntime_worker.registry", "agentruntime.modelgateway")
    files = sorted(POLICY_DIR.rglob("*.py"))
    assert files, "Policy Engine 在 agentruntime_worker/policy 包里"
    offenders = []
    for path in files:
        for item in ast.walk(ast.parse(path.read_text("utf-8"))):
            modules = [a.name for a in item.names] if isinstance(item, ast.Import) else [item.module or ""] if isinstance(item, ast.ImportFrom) else []
            offenders += [f"{path.name}: {m}" for m in modules if any(m == b or m.startswith(b + ".") for b in banned)]
    assert not offenders, f"Hook 里不调模型、不做网络 I/O：{offenders}"


# --- budget_check ---


@pytest.mark.parametrize(
    ("task_usd", "spent_usd", "node_budget", "expected"),
    [
        (None, 5.0, None, "allow"),  # Task 没有预算
        (2.0, 0.5, None, "allow"),
        (2.0, 2.0, None, "deny"),  # 用完了
        (2.0, 1.5, {"max_cost_usd": 0.8}, "deny"),  # 本 Node 的预算超过剩余
        (2.0, 1.5, {"max_cost_usd": 0.4}, "allow"),
    ],
)
async def test_budget_check_on_nodes(task_usd: float | None, spent_usd: float, node_budget: dict[str, Any] | None, expected: str) -> None:
    engine, _ = make_engine()
    n = node(budget=node_budget) if node_budget else node()
    results = await run(engine, "before_node", ["budget_check"], before_node(n, task_usd=task_usd, spent_usd=spent_usd))
    assert decisions(results) == [expected]


async def test_budget_check_counts_a_goal_nodes_budget() -> None:
    engine, _ = make_engine()
    goal = {"id": "g", "kind": "goal", "goal": "修复", "done_when": "通过", "output_schema": "r_v1", "budget": {"usd": 1.0}, "inputs": {}}
    results = await run(engine, "before_node", ["budget_check"], {"node": goal, "budget": {"task_usd": 2.0, "spent_usd": 1.5}, "write_serialized": True})
    assert decisions(results) == ["deny"]


async def test_budget_check_on_stages() -> None:
    engine, _ = make_engine()
    stage = {"seq": 2, "goal": "成稿", "exit_when": "成稿", "budget": {"usd": 0.6}}
    tight = await run(engine, "before_stage", ["budget_check"], {"stage": stage, "budget": {"task_usd": 2.0, "spent_usd": 1.6}})
    roomy = await run(engine, "before_stage", ["budget_check"], {"stage": stage, "budget": {"task_usd": 2.0, "spent_usd": 0.2}})
    assert decisions(tight) == ["deny"] and decisions(roomy) == ["allow"]


# --- 写操作 ---


async def test_idempotency_check() -> None:
    engine, _ = make_engine()
    write = node("w", effect="write_irreversible", role="act", template="send_email@v1", inputs={"to": "ops@example.com"})
    good = await run(engine, "before_node", ["idempotency_check"], before_node(write))
    wrong = await run(engine, "before_node", ["idempotency_check"], before_node(write, key="idk_" + "0" * 64))
    missing_subject = before_node(write)
    del missing_subject["idempotency_key"]
    missing = await run(engine, "before_node", ["idempotency_check"], missing_subject)
    read = await run(engine, "before_node", ["idempotency_check"], before_node(node()))
    assert decisions(good) == ["allow"]
    assert decisions(wrong) == ["deny"], "幂等键必须按 tenant_id、task_id、node_id 与输入算出（附录 D.4）"
    assert decisions(missing) == ["deny"]
    assert decisions(read) == ["allow"], "只读的 Node 不要幂等键"


async def test_single_writer_lock() -> None:
    engine, _ = make_engine()
    write = node("w", effect="write_reversible", role="act", template="ledger_write@v1")
    held = await run(engine, "before_node", ["single_writer_lock"], before_node(write, serialized=True))
    not_held = await run(engine, "before_node", ["single_writer_lock"], before_node(write, serialized=False))
    assert decisions(held) == ["allow"]
    assert decisions(not_held) == ["deny"], "同一时刻只允许一个节点写同一份共享产物（附录 D.4）"


async def test_checkpoint_records_the_write() -> None:
    engine, telemetry = make_engine()
    write = node("w", effect="write_reversible", role="act", template="ledger_write@v1")
    results = await run(engine, "after_node", ["checkpoint"], {"node": write, "result": {"row": 7}})
    assert decisions(results) == ["allow"]
    (record,) = spans(telemetry, "policy.checkpoint")
    assert record.attributes["node_id"] == "w" and record.attributes["result_hash"] == keys.input_hash({"row": 7})


# --- 记录类 Hook ---


async def test_trace_result_writes_the_result_and_truncates_large_ones() -> None:
    engine, telemetry = make_engine()
    await run(engine, "after_node", ["trace_result"], {"node": node(), "result": {"rows": [1, 2, 3]}})
    await run(engine, "after_node", ["trace_result"], {"node": node("big"), "result": {"blob": "x" * 50_000}})
    small, big = spans(telemetry, "node.result")
    assert keys.canonical_json({"rows": [1, 2, 3]}) in "".join(small.events), "结果按规范化 JSON 写进 trace"
    assert sum(len(e) for e in big.events) < 9 * 1024, "超过 8 KiB 的结果截断"


async def test_trace_result_counts_bytes_not_characters() -> None:
    """R-01 另一家复审第 9 条：8 KiB 按 UTF-8 字节算；截断落在字符边界上，不留半个字。"""
    engine, telemetry = make_engine()
    await run(engine, "after_node", ["trace_result"], {"node": node("cn"), "result": {"text": "长" * 5000}})
    (record,) = spans(telemetry, "node.result")
    text = "".join(record.events)
    assert len(text.encode("utf-8")) < 9 * 1024, "中文三字节一个，按字符截会超过 8 KiB"
    assert "�" not in text


async def test_a_later_hook_sees_an_earlier_modification() -> None:
    """R-01 另一家复审第 4 条：脱敏先作用，后面的 trace_result 记的是脱敏之后的结果，原始的个人信息不进 trace。"""
    engine, telemetry = make_engine()
    subject = {"node": node("q"), "result": {"note": "请联系 zhang.san@example.com"}}
    results = await run(engine, "after_node", ["pii_redaction", "trace_result", "audit_log"], subject)
    assert decisions(results) == ["modify", "allow", "allow"]
    assert "zhang.san@example.com" not in exported_text(telemetry)
    (record,) = spans(telemetry, "node.result")
    assert "[REDACTED]" in "".join(record.events)
    assert subject["result"]["note"] == "请联系 zhang.san@example.com", "改写只作用在引擎自己的副本上，HookCall 不变"


@pytest.mark.parametrize(
    "outcome",
    [
        {"decision": "maybe"},
        {"decision": "deny"},
        {"decision": "modify"},
        {"decision": "modify", "modification": {"target": "output", "patch": [{"op": "replace", "path": "/nope/deeper", "value": 1}]}},
        {"decision": "inject"},
    ],
    ids=["unknown-decision", "deny-without-reason", "modify-without-patch", "patch-that-does-not-apply", "inject-without-injection"],
)
async def test_a_malformed_hook_result_is_denied(outcome: dict[str, Any]) -> None:
    """R-01 另一家复审第 5 条：Hook 的结果不合 hook_result.schema.json 时按 deny（fail-closed），不当作放行。"""
    odd = HookSpec(frozenset({"after_node"}), lambda ctx: dict(outcome))
    engine, _ = make_engine(hooks={**HOOKS, "odd": odd})
    results = await run(engine, "after_node", ["odd", "audit_log"], {"node": node("q"), "result": {"rows": 1}})
    assert decisions(results) == ["deny"], "遇到不合格的结果就停"
    assert "odd" in results[0]["reason"]


async def test_audit_log_records_hashes_not_contents() -> None:
    engine, telemetry = make_engine()
    n = node("pay", effect="write_irreversible", role="act", template="card_charge@v1", inputs={"card": SECRET})
    results = await run(engine, "after_node", ["audit_log"], {"node": n, "result": {"receipt": SECRET}})
    assert decisions(results) == ["allow"]
    (record,) = spans(telemetry, "policy.audit")
    attrs = record.attributes
    assert (attrs["node_id"], attrs["template"], attrs["effect"], attrs["role"]) == ("pay", "card_charge@v1", "write_irreversible", "act")
    assert attrs["input_hash"] == keys.input_hash(n["inputs"]) and attrs["result_hash"] == keys.input_hash({"receipt": SECRET})
    assert attrs["status"] == "succeeded", "没写 status 的是成功执行后的审计"
    assert SECRET not in exported_text(telemetry)


async def test_audit_log_records_how_a_failed_node_ended() -> None:
    """R-01 评审第 4 节第 3 条：失败、结果未知、审批被拒的 Node 也审计，记下结局与失败类别（没有结果）。"""
    engine, telemetry = make_engine()
    n = node("pay", effect="write_irreversible", role="act", template="card_charge@v1", inputs={"card": SECRET})
    results = await run(engine, "after_node", ["audit_log"], {"node": n, "result": None, "status": "unknown", "failure_class": "", "error": f"超时：{SECRET}"})
    assert decisions(results) == ["allow"]
    (record,) = spans(telemetry, "policy.audit")
    assert (record.attributes["status"], record.attributes["node_id"]) == ("unknown", "pay")
    assert SECRET not in exported_text(telemetry), "错误说明可能带数据，不进审计"


async def test_a_telemetry_outage_does_not_fail_the_hooks() -> None:
    """R-01 评审第 4 节第 1 条：Collector 不可用时 Hook 照常给出结果（审计记录写不出去另记日志），Task 不会卡在 after_node 上。"""
    telemetry = DownTelemetry()
    engine = PolicyEngine(telemetry=telemetry)  # type: ignore[arg-type]
    n = node("q")
    assert decisions(await run(engine, "after_node", ["trace_result", "audit_log"], {"node": n, "result": {"rows": 1}})) == ["allow", "allow"]
    assert decisions(await run(engine, "before_node", ["budget_check"], before_node(n, spent_usd=5.0))) == ["deny"]
    assert telemetry.flushes == 2, "每次都试过写记录"


async def test_every_deny_is_audited_without_contents() -> None:
    engine, telemetry = make_engine()
    n = node("pay", effect="write_irreversible", role="act", template="card_charge@v1", inputs={"card": SECRET}, budget={"max_cost_usd": 9.0})
    await run(engine, "before_node", ["budget_check"], before_node(n, task_usd=1.0))
    (record,) = spans(telemetry, "policy.decision")
    assert (record.attributes["hook"], record.attributes["decision"], record.attributes["node_id"]) == ("budget_check", "deny", "pay")
    assert SECRET not in exported_text(telemetry)


# --- 策略声明的 Hook ---


async def test_pii_redaction_masks_personal_data_in_the_result() -> None:
    engine, _ = make_engine()
    result = {"contact": "张三 zhang.san@example.com，电话 13812345678", "ids": ["身份证 110101199003077777"], "count": 3}
    (modified,) = await run(engine, "after_node", ["pii_redaction"], {"node": node(), "result": result})
    assert modified["decision"] == "modify" and modified["modification"]["target"] == "output"
    redacted = sp.apply_patch(result, modified["modification"]["patch"])
    text = json.dumps(redacted, ensure_ascii=False)
    for raw in ("zhang.san@example.com", "13812345678", "110101199003077777"):
        assert raw not in text
    assert "[REDACTED]" in text and redacted["count"] == 3
    (clean,) = await run(engine, "after_node", ["pii_redaction"], {"node": node(), "result": {"rows": 3}})
    assert clean["decision"] == "allow"


SCHEMA = {"type": "object", "required": ["text"], "properties": {"text": {"type": "string", "minLength": 1}}}


@pytest.mark.parametrize(
    ("status", "deliverable", "expected"),
    [
        ("succeeded", {"text": "退款率上升是因为物流延误"}, "allow"),
        ("succeeded", None, "deny"),
        ("succeeded", {}, "deny"),
        ("succeeded", {"text": ""}, "deny"),  # 不合交付节点的 output_schema
        ("failed", {"text": "半成品"}, "deny"),
    ],
)
async def test_acceptance_recheck(status: str, deliverable: Any, expected: str) -> None:
    engine, _ = make_engine()
    subject = {"verdict": {"verdict": "done", "reason": "完成"}, "deliver_node": "answer", "deliver_status": status, "deliverable": deliverable, "output_schema": SCHEMA}
    results = await run(engine, "before_finish", ["acceptance_recheck"], subject)
    assert decisions(results) == [expected]
