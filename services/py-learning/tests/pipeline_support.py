"""Learning Pipeline 测试的支架（T-16）：按 T-15 的 span 约定造 trace、记录调用的 Registry 与 Model Gateway。

trace 的形状与 Worker 收尾写回打的 span 一致：Task 级 `invoke_workflow`，每个 Round 一个 `runtime.round` 子 span，
Round 的 `runtime.stage_shape` 是 Stage 形状的 JSON（agentruntime_worker.workflows.verdicts.stage_summary 的 shape）。
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import metering_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime.registry.v1 import registry_pb2 as rg
from agentruntime_core.errors import InvalidError, NotFoundError

FAMILY = "finance.invoice_coding"
OTHER_FAMILY = "support.ticket_triage"

# 一种做法：取数 → 归类 → 过账，最后一个 Goal Node 核对。
FETCH = {"id": "fetch", "kind": "atomic", "template": "invoice_fetch@v1"}
CODE = {"id": "code", "kind": "atomic", "template": "invoice_code@v3", "after": ["fetch"], "tier": "mid@low"}
POST = {"id": "post", "kind": "atomic", "template": "ledger_post@v2"}
CHECK = {"id": "check", "kind": "goal", "goal": "核对过账结果", "done_when": "总额一致", "mode": "tool_loop", "output_schema": {"type": "object"}, "after": ["post"]}

SHAPE_A1 = {"goal": "取数并归类", "exit_when": "每张发票都有科目", "nodes": [FETCH, CODE]}
SHAPE_A2 = {"goal": "过账并核对", "exit_when": "过账成功且总额一致", "nodes": [POST, CHECK]}
# 另一种做法：一个 tool_loop 包到底。
SHAPE_B1 = {
    "goal": "完成归类",
    "exit_when": "交付归类结果",
    "nodes": [{"id": "do_all", "kind": "goal", "goal": "归类全部发票", "done_when": "全部归类", "mode": "tool_loop", "output_schema": {"type": "object"}}],
}

ROUTE_A = (SHAPE_A1, SHAPE_A2)
ROUTE_B = (SHAPE_B1,)

TEMPLATE_EFFECTS = {"invoice_fetch@v1": "read", "invoice_code@v3": "none", "ledger_post@v2": "write_reversible"}


def task_id(n: int, salt: str = "") -> str:
    digest = hashlib.sha256(f"{salt}{n}".encode()).hexdigest().upper()
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return "tsk_0" + "".join(alphabet[int(c, 16) % 32] for c in digest[:25])


@dataclass
class TaskSpec:
    """一个 Task 的 trace。rounds：每项 (verdict, Stage 形状, 成本, 是否经 Planner)。"""

    task_id: str
    tenant: str = "t1"
    family: str = FAMILY
    status: str = "done"
    recipe: str = ""
    rounds: list[tuple[str, dict[str, Any], float, bool]] = field(default_factory=list)
    session: str = "ses_01JA8Q2N4H6K8M0P2R4T6V8X0Z"


def route_task(n: int, route: tuple[dict[str, Any], ...], *, status: str = "done", cost: float = 0.1, tenant: str = "t1",
               family: str = FAMILY, recipe: str = "", stop_after: int | None = None, rerun: bool = False, salt: str = "") -> TaskSpec:
    """按 route 走完（或走到第 stop_after 个 Round 停下）的 Task；每个 Round 的成本是 cost / Round 数。rerun 在第一个 Round 后插一个原地重跑。"""
    shapes = list(route[:stop_after] if stop_after else route)
    per = round(cost / max(len(shapes) + (1 if rerun else 0), 1), 6)
    rounds: list[tuple[str, dict[str, Any], float, bool]] = []
    for i, shape in enumerate(shapes):
        last = i == len(shapes) - 1
        verdict = ("done" if status == "done" else "abort") if last else "continue"
        if rerun and i == 0:
            rounds.append(("repair", shape, per, True))
            rounds.append(("continue" if not last else verdict, shape, per, False))
            continue
        rounds.append((verdict, shape, per, True))
    return TaskSpec(task_id=task_id(n, salt + tenant + family), tenant=tenant, family=family, status=status, recipe=recipe, rounds=rounds)


def _hex(seed: str, n: int) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[: n * 2]


def spans_of(spec: TaskSpec) -> list[dict[str, Any]]:
    """控制台 Telemetry 的记录形状：{name, trace_id, span_id, parent_span_id, attributes, status, events}。"""
    trace = _hex("trace" + spec.task_id, 16)
    root = _hex("root" + spec.task_id, 8)
    scope = {"tenant_id": spec.tenant, "user_id": "u1", "session_id": spec.session}
    total = round(sum(r[2] for r in spec.rounds), 6)
    out = [
        {
            "name": "invoke_workflow",
            "trace_id": trace,
            "span_id": root,
            "parent_span_id": "",
            "status": "ok",
            "events": [],
            "attributes": {
                **scope,
                "task_id": spec.task_id,
                "gen_ai.operation.name": "invoke_workflow",
                "temporal.workflow_id": spec.task_id,
                "temporal.run_id": "run-" + spec.task_id[-6:],
                "recipe": spec.recipe,
                "runtime.task_family": spec.family,
                "runtime.task_status": spec.status,
                "runtime.cost_usd": total,
                "runtime.rounds": len(spec.rounds),
                "runtime.synthetic": True,
            },
        }
    ]
    for seq, (verdict, shape, cost, planned) in enumerate(spec.rounds, start=1):
        attrs: dict[str, Any] = {
            **scope,
            "task_id": spec.task_id,
            "round_seq": seq,
            "verdict": verdict,
            "runtime.cost_usd": cost,
            "runtime.stage_shape": json.dumps(shape, ensure_ascii=False, sort_keys=True),
            "runtime.synthetic": True,
        }
        if planned:
            attrs["triage_level"] = "full"
        out.append({"name": "runtime.round", "trace_id": trace, "span_id": _hex(f"r{seq}" + spec.task_id, 8), "parent_span_id": root, "status": "ok", "events": [], "attributes": attrs})
    return out


def console_lines(specs: list[TaskSpec], *, noise: bool = True) -> list[str]:
    """控制台格式：每行一个 {"kind": ..., **记录}；夹几条无关的记录（node 的 span、指标、日志）。"""
    lines = []
    for spec in specs:
        for span in spans_of(spec):
            lines.append(json.dumps({"kind": "span", **span}, ensure_ascii=False))
        if noise:
            lines.append(json.dumps({"kind": "span", "name": "chat", "trace_id": "0" * 32, "span_id": "1" * 16, "parent_span_id": "", "status": "ok", "events": [],
                                     "attributes": {"tenant_id": spec.tenant, "gen_ai.operation.name": "chat"}}))
            # 控制台实现里指标记录的 kind 被记录自己的 kind（counter、histogram）覆盖，不是 "metric"。
            lines.append(json.dumps({"kind": "histogram", "name": "task.cost_usd", "value": 0.1, "attributes": {"tenant_id": spec.tenant}}))
            lines.append(json.dumps({"kind": "log", "level": "info", "message": "x", "attributes": {"tenant_id": spec.tenant}}))
    return lines


def _any_value(v: Any) -> dict[str, Any]:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}  # OTLP JSON 的整数是字符串
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def otlp_lines(specs: list[TaskSpec], *, per_line: int = 3) -> list[str]:
    """OTLP JSON：每行一个 ExportTraceServiceRequest，每行装 per_line 个 Task 的 span（同一 Task 的 span 可能跨行）。"""
    spans = [s for spec in specs for s in spans_of(spec)]
    lines = []
    chunk = max(per_line * 3, 1)
    for i in range(0, len(spans), chunk):
        batch = []
        for s in spans[i : i + chunk]:
            batch.append(
                {
                    "traceId": s["trace_id"],
                    "spanId": s["span_id"],
                    "parentSpanId": s["parent_span_id"],
                    "name": s["name"],
                    "kind": 1,
                    "startTimeUnixNano": "1761300000000000000",
                    "endTimeUnixNano": "1761300001000000000",
                    "attributes": [{"key": k, "value": _any_value(v)} for k, v in s["attributes"].items()],
                    "status": {},
                }
            )
        lines.append(json.dumps({"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "worker"}}]},
                                                    "scopeSpans": [{"scope": {"name": "agentruntime"}, "spans": batch}]}]}))
    return lines


def population(*, a_success: int, a_fail: int, a_partial_fail: int = 0, b_success: int = 0, b_fail: int = 0,
               a_cost: float = 0.1, b_cost: float = 0.1, salt: str = "", tenant: str = "t1", family: str = FAMILY) -> list[TaskSpec]:
    """一个任务族的一批 Task：走 A 路线成功、失败（走完失败）、中途失败；走 B 路线成功、失败。"""
    specs: list[TaskSpec] = []
    n = 0

    def add(route: tuple[dict[str, Any], ...], status: str, cost: float, stop_after: int | None = None) -> None:
        nonlocal n
        n += 1
        specs.append(route_task(n, route, status=status, cost=cost, stop_after=stop_after, salt=salt, tenant=tenant, family=family))

    for _ in range(a_success):
        add(ROUTE_A, "done", a_cost)
    for _ in range(a_fail):
        add(ROUTE_A, "aborted", a_cost)
    for _ in range(a_partial_fail):
        add(ROUTE_A, "aborted", a_cost / 2, stop_after=1)
    for _ in range(b_success):
        add(ROUTE_B, "done", b_cost)
    for _ in range(b_fail):
        add(ROUTE_B, "aborted", b_cost)
    return specs


# --- Registry 的替身 ---


@dataclass
class PublishedRecipe:
    tenant: str | None
    name: str
    version: str
    document: dict[str, Any]
    state: str = "shadow"
    evidence: dict[str, Any] | None = None


class FakeRegistry:
    """记录写调用；读方法返回登记的模板与默认 Recipe。Promote 像真 Registry 一样拒绝 passed 不为 true 的证据。"""

    def __init__(self, *, templates: dict[str, str] | None = None) -> None:
        self.templates = dict(TEMPLATE_EFFECTS if templates is None else templates)
        self.recipes: list[PublishedRecipe] = []
        self.calls: list[tuple[str, Any]] = []
        self.reject_promote = False

    @property
    def writes(self) -> list[tuple[str, Any]]:
        return [c for c in self.calls if c[0] in ("publish", "promote")]

    async def get_node_template(self, tenant: Any, name: str, version: str = "") -> rg.NodeTemplate:
        self.calls.append(("get_node_template", (tenant.tenant_id, name, version)))
        effect = self.templates.get(f"{name}@{version}")
        if effect is None:
            raise NotFoundError("not_found", f"模板 {name}@{version} 不存在")
        return rg.NodeTemplate(name=name, version=version, effect=_effect_enum(effect))

    async def list_recipes(self, tenant: Any, task_family: str = "") -> list[rg.Recipe]:
        self.calls.append(("list_recipes", (tenant.tenant_id, task_family)))
        return [
            rg.Recipe(name=r.name, version=r.version, task_family=r.document["task_family"], state=rg.RELEASE_STATE_DEFAULT)
            for r in self.recipes
            if r.state == "default" and r.tenant in (None, tenant.tenant_id) and (not task_family or r.document["task_family"] == task_family)
        ]

    async def publish(self, tenant: Any, kind: int, name: str, document: dict[str, Any], *, change_note: str = "") -> rg.PublishResponse:
        self.calls.append(("publish", {"tenant": tenant.tenant_id if tenant else None, "kind": kind, "name": name, "document": copy.deepcopy(document), "change_note": change_note}))
        version = f"v{sum(1 for r in self.recipes if r.name == name) + 1}"
        self.recipes.append(PublishedRecipe(tenant=tenant.tenant_id if tenant else None, name=name, version=version, document=copy.deepcopy(document)))
        return rg.PublishResponse(version=version, state=rg.RELEASE_STATE_SHADOW)

    async def promote(self, tenant: Any, kind: int, name: str, version: str, evaluation: dict[str, Any]) -> rg.PromoteResponse:
        self.calls.append(("promote", {"tenant": tenant.tenant_id if tenant else None, "kind": kind, "name": name, "version": version, "evaluation": copy.deepcopy(evaluation)}))
        if self.reject_promote or evaluation.get("passed") is not True:
            raise InvalidError("evaluation_not_passed", "评测证据没有通过")
        previous = ""
        for r in self.recipes:
            if r.name == name and r.state == "default":
                r.state, previous = "retired", r.version
        target = next(r for r in self.recipes if r.name == name and r.version == version)
        target.state, target.evidence = "default", copy.deepcopy(evaluation)
        return rg.PromoteResponse(previous_default_version=previous)

    def promote_default(self, tenant: str | None, name: str, document: dict[str, Any]) -> str:
        """测试准备：直接放一个默认版本的 Recipe。"""
        version = f"v{sum(1 for r in self.recipes if r.name == name) + 1}"
        self.recipes.append(PublishedRecipe(tenant=tenant, name=name, version=version, document=document, state="default"))
        return f"{name}@{version}"


def _effect_enum(effect: str) -> int:
    from agentruntime.common.v1 import enums_pb2

    return {
        "none": enums_pb2.EFFECT_NONE,
        "read": enums_pb2.EFFECT_READ,
        "write_reversible": enums_pb2.EFFECT_WRITE_REVERSIBLE,
        "write_irreversible": enums_pb2.EFFECT_WRITE_IRREVERSIBLE,
    }[effect]


# --- Model Gateway 的替身 ---


class ScriptedGateway:
    """依次返回脚本里的归因结果（dict 或原样的 struct），每次调用花 cost_micros；记下每个请求。"""

    def __init__(self, results: list[Any], *, cost_micros: int = 1000) -> None:
        self.results = list(results)
        self.cost_micros = cost_micros
        self.requests: list[mg.GenerateRequest] = []

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        self.requests.append(request)
        result = self.results.pop(0) if self.results else {"category": "specification", "reason": "目标不清"}
        value = struct_pb2.Value()
        json_format.ParseDict(result, value)
        return mg.GenerateResponse(
            result=value,
            finish_reason=mg.FINISH_REASON_STOP,
            model=mg.ModelChoice(model_profile_id="test/small-a@1", tier="small@low", vendor="openai"),
            usage=metering_pb2.Usage(input_tokens=500, output_tokens=50, cost_usd_micros=self.cost_micros),
        )

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        raise AssertionError("Learning 的归因不用 embed")
