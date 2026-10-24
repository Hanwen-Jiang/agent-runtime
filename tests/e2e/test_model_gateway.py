"""Model Gateway 单供应商直连（docs/plan/model-gateway-direct.md）验收“跨语言”：

Python 的 GrpcModelGateway → 现场编译的 model-gateway（Go）→ 本地模拟的 OpenAI 兼容服务（e2e_support.fake_openai）。
- 生成与嵌入往返：租户与计量一致，cost_usd_micros 按测试 Profile 的单价精确到 micros；
- 上游收到的密钥是 dev-secret 写进开发 SecretStore 的假密钥（BYOK 租户用自己的）；
- 计量记录与网关输出不含密钥与消息内容；错误类别在 Python 侧还原正确；
- 一个 Task 的 model 节点经这条链路交付（整套：Temporal、task-service、Worker）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import enums_pb2, payload_pb2, tenant_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError, ProviderError, TransientError
from agentruntime_worker.executor.grpc_gateway import GrpcModelGateway

from e2e_support import fake_openai
from e2e_support.api import TaskApi, finished_payload, stream_until_finished
from e2e_support.fake_openai import FakeOpenAI, Failure
from e2e_support.gateway import (
    DEFAULT_KEY_NAME,
    METERING_FIELDS,
    Gateway,
    build_gateway,
    dev_secret,
    platform_secret,
    seed_tenant_secret,
)
from e2e_support.stack import Stack

pytestmark = pytest.mark.e2e

# 有辨识度的假密钥：断言它只出现在发给上游的 Authorization 头里。
FAKE_KEY = "sk-test-DO-NOT-LEAK-0123"
BYOK_KEY = "sk-test-BYOK-DO-NOT-LEAK-4567"
BYOK_TENANT = "t-byok"
ALLOW_TENANT = "t-allow"
TENANT = "t-e2e"
# 合乎格式的 Session id（ses_ 加 26 位 ULID）：网关按 tenancy 的规则校验租户上下文，格式不对是 invalid。
SESSION_ID = "ses_01J" + "0" * 22 + "E"
# 有辨识度的消息内容：断言它不进计量记录与错误信息。
SECRET_MESSAGE = "机密消息-e2e-5d1c：客户 C-2048 的退款明细"

PRICE_AT = "2026-10-21"


def _profile(pid: str, vendor: str, model: str, cls: str, tiers: list[dict[str, Any]], caps: list[str], cost: dict[str, float], ctx: int, out: int) -> dict[str, Any]:
    return {
        "id": pid,
        "vendor": vendor,
        "model": model,
        "class": cls,
        "tiers": tiers,
        "capabilities": caps,
        "cost": cost,
        "constraints": {"context_window": ctx, "max_output": out},
        "freshness": {"price_at": PRICE_AT},
    }


GEN_CAPS = ["generate", "structured", "tools", "stream"]
SMALL = _profile("fake/small@2026-10", "openai", "fake-small", "small", [{"name": "low", "effort": "", "thinking_budget": 0}], GEN_CAPS, {"in_per_m": 0.5, "out_per_m": 1.5, "cache_read_per_m": 0.25}, 128000, 16384)
MID = _profile("fake/mid@2026-10", "openai", "fake-mid", "mid", [{"name": "low", "effort": "", "thinking_budget": 0}], GEN_CAPS, {"in_per_m": 2.0, "out_per_m": 8.0, "cache_read_per_m": 0.5}, 128000, 16384)
STRONG = _profile(
    "fake/strong@2026-10",
    "anthropic",
    "fake-strong",
    "strong",
    [{"name": "medium", "effort": "medium", "thinking_budget": 8192}],
    GEN_CAPS,
    {"in_per_m": 3.0, "out_per_m": 15.0, "cache_read_per_m": 0.3},
    200000,
    32000,
)
EMBED = _profile("fake/embed@2026-10", "openai", "fake-embed", "embed", [{"name": "default"}], ["embed"], {"in_per_m": 0.02, "out_per_m": 0.0, "cache_read_per_m": 0.0}, 8192, 0)
TEST_PROFILES = {"version": 1, "profiles": [SMALL, MID, STRONG, EMBED]}


def expected_cost(profile: dict[str, Any], usage: dict[str, Any]) -> int:
    """约定 2：round((input - cache_read) * in + cache_read * cache_read_per_m + output * out)。"""
    cost = profile["cost"]
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
    inp = int(usage.get("prompt_tokens", 0))
    out = int(usage.get("completion_tokens", 0))
    return round((inp - cached) * cost["in_per_m"] + cached * cost.get("cache_read_per_m", 0) + out * cost["out_per_m"])


# 测试 Profile 与模拟上游的 usage 下的成本（手算一遍，防止测试与实现犯同一个错）。
assert expected_cost(MID, fake_openai.CHAT_USAGE) == 4500  # 1000×2.0 + 200×0.5 + 300×8.0
assert expected_cost(SMALL, fake_openai.CHAT_USAGE) == 1000  # 1000×0.5 + 200×0.25 + 300×1.5
assert expected_cost(STRONG, fake_openai.CHAT_USAGE) == 7560  # 1000×3.0 + 200×0.3 + 300×15.0
assert expected_cost(EMBED, fake_openai.EMBED_USAGE) == 100  # 5000×0.02


def tenant(tenant_id: str = TENANT) -> tenant_pb2.TenantContext:
    return tenant_pb2.TenantContext(tenant_id=tenant_id, user_id="u-e2e", session_id=SESSION_ID)


def generate_request(
    *,
    tenant_ctx: tenant_pb2.TenantContext | None = None,
    task_id: str = "tsk_gw_generate",
    node_id: str = "answer",
    role: int = enums_pb2.ROLE_DELIVER,
    tier: str = "",
    text: str = SECRET_MESSAGE,
    schema: dict[str, Any] | None = None,
) -> mg.GenerateRequest:
    params = mg.GenerateParams(
        messages=[mg.ChatMessage(role=mg.CHAT_ROLE_SYSTEM, text="你是测试助手。"), mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=text)],
        max_output_tokens=256,
    )
    if schema is not None:
        value = struct_pb2.Value()
        json_format.ParseDict(schema, value)
        params.output_schema.CopyFrom(payload_pb2.JsonPayload(value=value))
    return mg.GenerateRequest(
        tenant=tenant() if tenant_ctx is None else tenant_ctx,
        call=mg.CallContext(task_id=task_id, node_id=node_id, role=role, tier=tier),
        generation=params,
    )


@asynccontextmanager
async def client(gateway: Gateway, timeout_s: float = 60) -> AsyncIterator[GrpcModelGateway]:
    gw = GrpcModelGateway(gateway.address, timeout_s=timeout_s)
    try:
        yield gw
    finally:
        await gw.aclose()


def assert_no_secret(text: str, secret: str, where: str) -> None:
    """不用 assert x not in y：失败时 pytest 会把整段文本连同密钥打印出来。"""
    if secret in text:
        pytest.fail(f"{where} 里出现了不该出现的内容（已隐去）", pytrace=False)


# --- fixture ---


@dataclass
class GatewayFiles:
    binary: Path
    root: Path
    profiles: Path
    secrets: Path
    policies: Path
    dev_secret_stdout: str
    dev_secret_stderr: str


@pytest.fixture(scope="module")
def upstream() -> Iterator[FakeOpenAI]:
    fake = FakeOpenAI()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture(scope="module")
def gateway_files(tmp_path_factory: pytest.TempPathFactory) -> GatewayFiles:
    root = tmp_path_factory.mktemp("model-gateway")
    binary = build_gateway(root)
    profiles = root / "model-profiles.json"
    profiles.write_text(json.dumps(TEST_PROFILES, ensure_ascii=False), "utf-8")
    secrets = root / "secrets.json"
    result = dev_secret(binary, secrets, DEFAULT_KEY_NAME, FAKE_KEY)
    if result.returncode != 0:
        pytest.fail(f"model-gateway dev-secret 失败（退出码 {result.returncode}）：\n{result.stderr.replace(FAKE_KEY, '***')}", pytrace=False)
    seed_tenant_secret(secrets, BYOK_TENANT, DEFAULT_KEY_NAME, BYOK_KEY)
    # 与 task-service 同一种格式的租户策略文件：effect_ceiling 是 task-service 的，model_allowlist 是网关的。
    policies = root / "tenant_policies.json"
    policies.write_text(
        json.dumps({"tenants": {TENANT: {"effect_ceiling": "read"}, ALLOW_TENANT: {"effect_ceiling": "read", "model_allowlist": [SMALL["id"]]}}}),
        "utf-8",
    )
    return GatewayFiles(binary, root, profiles, secrets, policies, result.stdout, result.stderr)


@pytest.fixture(scope="module")
def gateway(gateway_files: GatewayFiles, upstream: FakeOpenAI) -> Iterator[Gateway]:
    gw = Gateway(
        binary=gateway_files.binary,
        root=gateway_files.root / "serve",
        profiles_file=gateway_files.profiles,
        secrets_file=gateway_files.secrets,
        provider="openai_compat",
        base_url=upstream.base_url,
        tenant_policies_file=gateway_files.policies,
        secrets=[FAKE_KEY, BYOK_KEY],
    )
    gw.start()
    yield gw
    gw.stop()


@pytest.fixture(autouse=True)
def _upstream_healthy(request: pytest.FixtureRequest) -> Iterator[None]:
    """每个用例前后都让上游恢复正常。用例结束时打印网关输出的末尾（密钥已隐去）：pytest 只在用例失败时展示它。"""
    if "upstream" in request.fixturenames:
        request.getfixturevalue("upstream").fail = None
    yield
    if "upstream" in request.fixturenames:
        request.getfixturevalue("upstream").fail = None
    if "gateway" in request.fixturenames:
        try:
            gw = request.getfixturevalue("gateway")
        except BaseException:  # noqa: BLE001 — 网关没起来时 setup 已经报过错
            return
        print(gw.diagnostics())


# --- dev-secret ---


def test_dev_secret_reads_the_key_from_stdin_into_the_platform_scope(gateway_files: GatewayFiles) -> None:
    """交付 2：dev-secret 从标准输入读密钥（末尾换行去掉），写进开发 SecretStore 的平台作用域；密钥不经命令行参数，也不回显。"""
    assert platform_secret(gateway_files.secrets, DEFAULT_KEY_NAME) == FAKE_KEY
    assert_no_secret(gateway_files.dev_secret_stdout + gateway_files.dev_secret_stderr, FAKE_KEY, "dev-secret 的输出")


# --- 生成与嵌入往返 ---


async def test_generate_round_trips_tenant_and_usage(gateway: Gateway, upstream: FakeOpenAI) -> None:
    """验收“跨语言”：生成往返，租户与计量一致，成本按 Profile 单价精确到 micros；上游收到的是假密钥。"""
    mark = upstream.mark()
    async with client(gateway) as gw:
        resp = await gw.generate(generate_request(task_id="tsk_gw_roundtrip", node_id="answer"))

    assert resp.text == fake_openai.ANSWER
    assert resp.rationale == fake_openai.REASONING, "reasoning_content 映射为 rationale（只进 trace）"
    assert fake_openai.REASONING not in resp.text, "思考内容不混进 text"
    assert not resp.HasField("result"), "没有 output_schema 时只有 text"
    assert resp.finish_reason == mg.FINISH_REASON_STOP
    assert (resp.model.model_profile_id, resp.model.tier, resp.model.vendor) == (MID["id"], "mid@low", "openai"), "deliver 的默认 Tier 是 mid@low"
    assert resp.usage.input_tokens == 1200
    assert resp.usage.output_tokens == 300
    assert resp.usage.cache_read_tokens == 200
    assert resp.usage.cost_usd_micros == 4500

    calls = upstream.since(mark, "/chat/completions")
    assert len(calls) == 1
    assert calls[0].model == MID["model"], "按 Profile 的供应商模型名调用"
    assert calls[0].api_key == FAKE_KEY, "密钥来自开发 SecretStore 的平台作用域"
    assert SECRET_MESSAGE in calls[0].message_texts()
    assert "reasoning_effort" not in calls[0].body, "Tier 的思考预算为 0 时不发送 reasoning_effort"

    records = gateway.metering_for(tenant_id=TENANT, task_id="tsk_gw_roundtrip", node_id="answer")
    assert len(records) == 1, f"每次调用一条计量记录：{gateway.metering()}"
    record = records[0]
    assert set(METERING_FIELDS) <= set(record), f"计量记录缺字段：{sorted(set(METERING_FIELDS) - set(record))}"
    assert record["role"] == "deliver"
    assert (record["model_profile_id"], record["tier"], record["vendor"]) == (MID["id"], "mid@low", "openai")
    assert (record["input_tokens"], record["output_tokens"], record["cost_usd_micros"]) == (1200, 300, 4500)


@pytest.mark.parametrize(
    ("role", "tier", "profile", "expected_tier"),
    [
        (enums_pb2.ROLE_PLAN, "", STRONG, "strong@medium"),
        (enums_pb2.ROLE_JUDGE, "", STRONG, "strong@medium"),
        (enums_pb2.ROLE_ROUTE, "", SMALL, "small@low"),
        (enums_pb2.ROLE_TRANSFORM, "", MID, "mid@low"),
        (enums_pb2.ROLE_DELIVER, "small@low", SMALL, "small@low"),
        (enums_pb2.ROLE_ROUTE, "strong@medium", STRONG, "strong@medium"),
    ],
    ids=["plan-default", "judge-default", "route-default", "transform-default", "explicit-small", "explicit-strong"],
)
async def test_tier_selection_round_trips(gateway: Gateway, upstream: FakeOpenAI, role: int, tier: str, profile: dict[str, Any], expected_tier: str) -> None:
    """选档的跨语言可见部分：tier 为空按 role 的默认 Tier（附录 H.3），显式 tier 优先；成本按所选 Profile 的单价。"""
    mark = upstream.mark()
    async with client(gateway) as gw:
        resp = await gw.generate(generate_request(task_id=f"tsk_gw_tier_{expected_tier}", role=role, tier=tier))
    assert resp.model.model_profile_id == profile["id"]
    assert resp.model.tier == expected_tier
    assert resp.model.vendor == profile["vendor"]
    assert resp.usage.cost_usd_micros == expected_cost(profile, fake_openai.CHAT_USAGE)
    calls = upstream.since(mark, "/chat/completions")
    assert [c.model for c in calls] == [profile["model"]]
    if profile is STRONG:
        assert calls[0].body.get("reasoning_effort") == "medium", "strong@medium 的思考预算 8192 → reasoning_effort=medium"


async def test_structured_output_round_trips(gateway: Gateway, upstream: FakeOpenAI) -> None:
    """难点“结构化输出”：靠上游 json_schema 严格模式；结果放在 result，不放 text。"""
    schema = {"type": "object", "properties": {"answer": {"type": "string"}, "confidence": {"type": "number"}}, "required": ["answer", "confidence"], "additionalProperties": False}
    mark = upstream.mark()
    async with client(gateway) as gw:
        resp = await gw.generate(generate_request(task_id="tsk_gw_structured", schema=schema))
    assert resp.HasField("result")
    assert json_format.MessageToDict(resp.result) == fake_openai.STRUCTURED
    assert resp.rationale == fake_openai.REASONING
    calls = upstream.since(mark, "/chat/completions")
    assert len(calls) == 1
    fmt = calls[0].body.get("response_format") or {}
    assert fmt.get("type") == "json_schema"
    assert (fmt.get("json_schema") or {}).get("strict") is True
    assert (fmt.get("json_schema") or {}).get("schema") == schema


async def test_embed_round_trips_tenant_and_usage(gateway: Gateway, upstream: FakeOpenAI) -> None:
    """验收“跨语言”：嵌入往返；embedding_version 等于 Profile id；成本按嵌入 Profile 的单价。"""
    texts = ["第一段文本", "第二段文本"]
    mark = upstream.mark()
    async with client(gateway) as gw:
        resp = await gw.embed(mg.EmbedRequest(tenant=tenant(), call=mg.CallContext(task_id="tsk_gw_embed", node_id="embed"), texts=texts))
    assert len(resp.embeddings) == len(texts)
    assert all(list(e.values) == fake_openai.EMBED_VECTOR for e in resp.embeddings)
    assert resp.embedding_version == EMBED["id"]
    assert (resp.model.model_profile_id, resp.model.tier, resp.model.vendor) == (EMBED["id"], "embed@default", "openai")
    assert resp.usage.input_tokens == 5000
    assert resp.usage.cost_usd_micros == 100

    calls = upstream.since(mark, "/embeddings")
    assert len(calls) == 1
    assert calls[0].model == EMBED["model"]
    assert calls[0].body.get("input") == texts
    assert calls[0].api_key == FAKE_KEY

    records = gateway.metering_for(tenant_id=TENANT, task_id="tsk_gw_embed", node_id="embed")
    assert len(records) == 1
    assert (records[0]["model_profile_id"], records[0]["input_tokens"], records[0]["cost_usd_micros"]) == (EMBED["id"], 5000, 100)


async def test_byok_tenant_uses_its_own_key(gateway: Gateway, upstream: FakeOpenAI) -> None:
    """交付 1：Bind(tenant) 先读租户作用域的同名密钥（BYOK），计量记在该租户名下。"""
    mark = upstream.mark()
    async with client(gateway) as gw:
        await gw.generate(generate_request(tenant_ctx=tenant(BYOK_TENANT), task_id="tsk_gw_byok"))
        await gw.generate(generate_request(task_id="tsk_gw_platform"))
    calls = upstream.since(mark, "/chat/completions")
    assert [c.api_key for c in calls] == [BYOK_KEY, FAKE_KEY], "BYOK 租户用自己的密钥，其余租户用平台密钥"
    assert len(gateway.metering_for(tenant_id=BYOK_TENANT, task_id="tsk_gw_byok", node_id="answer")) == 1


@pytest.mark.parametrize(
    "tenant_ctx",
    [None, tenant_pb2.TenantContext(), tenant_pb2.TenantContext(user_id="u-e2e", session_id=SESSION_ID)],
    ids=["no-tenant-field", "empty-tenant", "blank-tenant-id"],
)
async def test_missing_tenant_is_rejected_without_calling_upstream(gateway: Gateway, upstream: FakeOpenAI, tenant_ctx: tenant_pb2.TenantContext | None) -> None:
    """难点“租户上下文”：缺少租户 → denied（code missing_tenant，与 S-02 的 tenancy 一致，R-02 评审第 4 节第 6 条），不调用供应商。"""
    request = generate_request(task_id="tsk_gw_no_tenant")
    embed = mg.EmbedRequest(call=mg.CallContext(task_id="tsk_gw_no_tenant", node_id="embed"), texts=["x"])
    request.ClearField("tenant")
    if tenant_ctx is not None:
        # 字段在、内容为空（或只有 tenant_id 为空）。
        for msg in (request, embed):
            msg.tenant.SetInParent()
            msg.tenant.MergeFrom(tenant_ctx)
    mark = upstream.mark()
    async with client(gateway) as gw:
        with pytest.raises(DeniedError) as info:
            await gw.generate(request)
        with pytest.raises(DeniedError) as embed_info:
            await gw.embed(embed)
    assert info.value.code == "missing_tenant"
    assert embed_info.value.code == "missing_tenant"
    assert upstream.since(mark) == [], "缺租户时不能调用供应商"


async def test_model_allowlist_from_the_shared_policy_file(gateway: Gateway, upstream: FakeOpenAI) -> None:
    """交付 2：租户模型白名单读 AR_DEV_TENANT_POLICIES_FILE（与 task-service 同一个文件）；白名单之外没有可用模型时报不可重试的错误，不调用供应商。"""
    mark = upstream.mark()
    async with client(gateway) as gw:
        ok = await gw.generate(generate_request(tenant_ctx=tenant(ALLOW_TENANT), task_id="tsk_gw_allow", role=enums_pb2.ROLE_ROUTE))
        assert ok.model.model_profile_id == SMALL["id"]
        with pytest.raises(ProviderError) as info:
            await gw.generate(generate_request(tenant_ctx=tenant(ALLOW_TENANT), task_id="tsk_gw_allow_mid", tier="mid@low"))
    assert not info.value.retryable, f"白名单之外没有可用模型不是暂时的：{type(info.value).__name__} {info.value.code}"
    assert [c.model for c in upstream.since(mark, "/chat/completions")] == [SMALL["model"]]


# --- 错误类别 ---

ERROR_CASES = [
    (fake_openai.RATE_LIMITED, TransientError),
    (fake_openai.SERVER_ERROR, TransientError),
    (fake_openai.BAD_REQUEST, InvalidError),
    (fake_openai.UNAUTHORIZED, DeniedError),
    (fake_openai.FORBIDDEN, DeniedError),
    (fake_openai.MODEL_NOT_FOUND, NotFoundError),
]


@pytest.mark.parametrize(("failure", "expected"), ERROR_CASES, ids=[f"{c[0].status}-{c[0].code}" for c in ERROR_CASES])
async def test_upstream_errors_map_to_python_categories(gateway: Gateway, upstream: FakeOpenAI, failure: Failure, expected: type[ProviderError]) -> None:
    """难点“错误分类一致”：429、5xx 为 transient（重试后仍失败才返回），400 invalid，401、403 denied，模型不存在 not_found；
    非 transient 不重试。上游把密钥回显在错误正文里，Python 侧的错误与网关输出里也不能出现密钥与消息内容。"""
    upstream.fail = failure
    mark = upstream.mark()
    async with client(gateway, timeout_s=90) as gw:
        with pytest.raises(ProviderError) as info:
            await gw.generate(generate_request(task_id=f"tsk_gw_err_{failure.status}"))
    upstream.fail = None
    err = info.value
    assert type(err) is expected, f"上游 {failure.status} 应还原为 {expected.__name__}，实际 {type(err).__name__}（{err.code}）"
    assert err.retryable is (expected is TransientError)
    attempts = len(upstream.since(mark, "/chat/completions"))
    if expected is TransientError:
        assert attempts >= 2, "transient 失败要带抖动重试"
    else:
        assert attempts == 1, "非 transient 不重试"
    for text in (str(err), repr(err), err.code, err.message):
        assert_no_secret(text, FAKE_KEY, "Python 侧的错误信息")
        assert_no_secret(text, SECRET_MESSAGE, "Python 侧的错误信息")
    assert_no_secret(gateway.output(), FAKE_KEY, "网关的标准输出与标准错误")


async def test_metering_and_logs_never_contain_the_key_or_the_messages(gateway: Gateway) -> None:
    """难点“密钥不外泄”、交付 2“计量”：计量记录不含消息内容与密钥；网关的全部输出不含密钥。"""
    async with client(gateway) as gw:
        await gw.generate(generate_request(task_id="tsk_gw_leak", text=SECRET_MESSAGE))
        await gw.generate(generate_request(tenant_ctx=tenant(BYOK_TENANT), task_id="tsk_gw_leak_byok", text=SECRET_MESSAGE))
    assert gateway.metering_for(tenant_id=TENANT, task_id="tsk_gw_leak", node_id="answer")
    assert_no_secret(gateway.stdout(), SECRET_MESSAGE, "计量记录")
    assert_no_secret(gateway.stdout(), "C-2048", "计量记录")
    assert_no_secret(gateway.stdout(), fake_openai.ANSWER, "计量记录")
    for secret in (FAKE_KEY, BYOK_KEY):
        assert_no_secret(gateway.output(), secret, "网关的标准输出与标准错误")


# --- 一个 Task 的 model 节点经这条链路交付 ---

ENV_WORKER_GATEWAY = "AR_MODEL_GATEWAY_ADDRESS"


@pytest.fixture
def gateway_worker(stack: Stack, gateway: Gateway, monkeypatch: pytest.MonkeyPatch) -> Iterator[Stack]:
    """标准 Worker 换成设了 AR_MODEL_GATEWAY_ADDRESS 的（Worker 继承本进程的环境变量）；用例结束换回桩模型。"""
    monkeypatch.setenv(ENV_WORKER_GATEWAY, gateway.address)
    stack.restart_worker()
    try:
        yield stack
    finally:
        monkeypatch.delenv(ENV_WORKER_GATEWAY, raising=False)
        stack.restart_worker()


def test_task_model_node_is_delivered_through_the_gateway(gateway_worker: Stack, api: TaskApi, gateway: Gateway, upstream: FakeOpenAI) -> None:
    """验收“跨语言”：一个 Task 的 model 节点经 Worker → Model Gateway → 模拟上游交付；计量记在 Task 的租户名下。"""
    message = "经网关回答：e2e-gw-7731 上季度退款率为什么上升？"
    mark = upstream.mark()
    sub = api.submit(message)
    events = stream_until_finished(sub["eventsUrl"], api.token or "")
    result = finished_payload(events)
    assert result["status"] == "TASK_STATUS_DONE", result
    assert result["deliver_node_id"] == "answer"
    assert result["deliverable"]["value"]["text"] == fake_openai.ANSWER, "交付文本来自模拟上游，不是桩模型"

    calls = upstream.since(mark, "/chat/completions")
    assert calls, "model 节点没有经网关调用上游"
    assert any("e2e-gw-7731" in t for c in calls for t in c.message_texts()), "Task 的消息经 Node Executor 到达上游"
    assert all(c.api_key == FAKE_KEY for c in calls)

    records = gateway.metering_for(tenant_id="t1", task_id=sub["taskId"], node_id="answer")
    assert len(records) == 1, f"计量记在 Task 的租户（t1）名下：{gateway.metering()}"
    assert records[0]["cost_usd_micros"] == expected_cost(MID, fake_openai.CHAT_USAGE)
    gateway_worker.wait_finalized(sub["taskId"])
