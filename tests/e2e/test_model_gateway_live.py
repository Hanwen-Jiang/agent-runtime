"""Model Gateway 单供应商直连（docs/plan/model-gateway-direct.md）验收“真实端点”：只在 AR_LIVE_MODEL_PROVIDER=1 时运行。

用仓库里的开发 Profile（deploy/dev/model-profiles.json）与真实的 OpenAI 兼容端点起网关，经 Python 的 GrpcModelGateway：
small、mid、strong 各完成一次生成，再完成一次结构化输出与一次嵌入；断言有内容、usage 与 cost_usd_micros > 0。

端点与密钥由人从本地密钥文件 source 进环境变量：AR_MODEL_PROVIDER_BASE_URL、AR_MODEL_PROVIDER_API_KEY。
密钥只经 model-gateway dev-secret 的标准输入写进临时的开发 SecretStore 文件，不写进仓库、不进命令行参数、
不传给网关进程的环境变量；断言失败时也不打印它。
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import enums_pb2, payload_pb2, tenant_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_worker.executor.grpc_gateway import GrpcModelGateway

from e2e_support.gateway import DEFAULT_KEY_NAME, DEV_PROFILES_FILE, Gateway, build_gateway, dev_secret

ENV_LIVE = "AR_LIVE_MODEL_PROVIDER"
ENV_BASE_URL = "AR_MODEL_PROVIDER_BASE_URL"
ENV_API_KEY = "AR_MODEL_PROVIDER_API_KEY"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get(ENV_LIVE) != "1",
        reason=f"真实端点的测试只在 {ENV_LIVE}=1 时运行（另需 {ENV_BASE_URL} 与 {ENV_API_KEY}，由人从本地密钥文件 source）",
    ),
]

TENANT = tenant_pb2.TenantContext(tenant_id="t-live", user_id="u-live", session_id="ses-live")
# 嵌入的单价很低：文本太短时成本四舍五入为 0 micros，所以给足长度（约几百 token）。
EMBED_TEXTS = [
    "上季度退款率上升，主要原因是华东仓的物流延误与包装破损。" * 8,
    "客服工单里与退款相关的投诉集中在发货后的第三到第五天。" * 8,
]
# 生成也给一段像样的系统提示，保证小模型的成本不会四舍五入为 0。
SYSTEM_PROMPT = "你是一个简洁的助手。只用中文回答，不超过二十个字。" + "回答要直接，不要解释推理过程。" * 4


def _profiles() -> list[dict[str, Any]]:
    if not DEV_PROFILES_FILE.exists():
        pytest.fail(f"找不到开发用的 Model Profile 文件 {DEV_PROFILES_FILE}", pytrace=False)
    return list(json.loads(DEV_PROFILES_FILE.read_text("utf-8"))["profiles"])


def tier_of(cls: str) -> tuple[str, set[str]]:
    """开发 Profile 里某个模型档的 Tier（<class>@<第一个 tier 名>）与该档的 Profile id。"""
    profiles = [p for p in _profiles() if p.get("class") == cls]
    if not profiles:
        pytest.fail(f"{DEV_PROFILES_FILE.name} 里没有 class={cls} 的 Profile", pytrace=False)
    return f"{cls}@{profiles[0]['tiers'][0]['name']}", {p["id"] for p in profiles}


@pytest.fixture(scope="module")
def live_key() -> str:
    key = os.environ.get(ENV_API_KEY, "")
    if not key or not os.environ.get(ENV_BASE_URL):
        pytest.fail(f"{ENV_LIVE}=1 时必须设 {ENV_BASE_URL} 与 {ENV_API_KEY}", pytrace=False)
    return key


@pytest.fixture(scope="module")
def live_gateway(tmp_path_factory: pytest.TempPathFactory, live_key: str) -> Iterator[Gateway]:
    root = tmp_path_factory.mktemp("model-gateway-live")
    binary = build_gateway(root)
    secrets = root / "secrets.json"
    result = dev_secret(binary, secrets, DEFAULT_KEY_NAME, live_key)
    if result.returncode != 0:
        pytest.fail(f"model-gateway dev-secret 失败（退出码 {result.returncode}）：\n{result.stderr.replace(live_key, '***')}", pytrace=False)
    gw = Gateway(
        binary=binary,
        root=root / "serve",
        profiles_file=DEV_PROFILES_FILE,
        secrets_file=secrets,
        provider="openai_compat",
        base_url=os.environ[ENV_BASE_URL],
        secrets=[live_key],
    )
    gw.start()
    try:
        yield gw
    finally:
        gw.stop()
        secrets.unlink(missing_ok=True)


@asynccontextmanager
async def client(gateway: Gateway) -> AsyncIterator[GrpcModelGateway]:
    gw = GrpcModelGateway(gateway.address, timeout_s=180)
    try:
        yield gw
    finally:
        await gw.aclose()


def request(tier: str, text: str, *, node_id: str, schema: dict[str, Any] | None = None) -> mg.GenerateRequest:
    params = mg.GenerateParams(
        messages=[mg.ChatMessage(role=mg.CHAT_ROLE_SYSTEM, text=SYSTEM_PROMPT), mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=text)],
        max_output_tokens=2048,
    )
    if schema is not None:
        value = struct_pb2.Value()
        json_format.ParseDict(schema, value)
        params.output_schema.CopyFrom(payload_pb2.JsonPayload(value=value))
    return mg.GenerateRequest(
        tenant=TENANT,
        call=mg.CallContext(task_id="tsk_live", node_id=node_id, role=enums_pb2.ROLE_TRANSFORM, tier=tier),
        generation=params,
    )


def assert_usage(usage: Any) -> None:
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0
    assert usage.cost_usd_micros > 0


def assert_key_not_leaked(gateway: Gateway) -> None:
    """不用 assert key not in output：失败时 pytest 会把密钥打印出来。"""
    output = gateway.output()
    if any(key and key in output for key in gateway.secrets):
        pytest.fail("网关的输出里出现了密钥（已隐去）", pytrace=False)


@pytest.mark.parametrize("cls", ["small", "mid", "strong"])
async def test_generate_on_each_model_class(live_gateway: Gateway, cls: str) -> None:
    tier, ids = tier_of(cls)
    async with client(live_gateway) as gw:
        resp = await gw.generate(request(tier, "天空通常是什么颜色？", node_id=f"live_{cls}"))
    assert resp.text.strip(), "应有回答内容"
    assert resp.model.tier == tier
    assert resp.model.model_profile_id in ids
    assert_usage(resp.usage)
    assert live_gateway.metering_for(tenant_id="t-live", task_id="tsk_live", node_id=f"live_{cls}")
    assert_key_not_leaked(live_gateway)


async def test_structured_output(live_gateway: Gateway) -> None:
    tier, ids = tier_of("mid")
    schema = {"type": "object", "properties": {"color": {"type": "string"}}, "required": ["color"], "additionalProperties": False}
    async with client(live_gateway) as gw:
        resp = await gw.generate(request(tier, "天空通常是什么颜色？按要求的 JSON 输出。", node_id="live_structured", schema=schema))
    assert resp.HasField("result"), "结构化输出放在 result"
    result = json_format.MessageToDict(resp.result)
    assert isinstance(result, dict) and isinstance(result.get("color"), str) and result["color"].strip()
    assert resp.model.model_profile_id in ids
    assert_usage(resp.usage)
    assert_key_not_leaked(live_gateway)


async def test_embed(live_gateway: Gateway) -> None:
    _, ids = tier_of("embed")
    async with client(live_gateway) as gw:
        resp = await gw.embed(mg.EmbedRequest(tenant=TENANT, call=mg.CallContext(task_id="tsk_live", node_id="live_embed"), texts=EMBED_TEXTS))
    assert len(resp.embeddings) == len(EMBED_TEXTS)
    assert all(len(e.values) > 0 for e in resp.embeddings)
    assert resp.embedding_version in ids, "embedding_version 等于嵌入 Profile 的 id"
    assert resp.usage.input_tokens > 0
    assert resp.usage.cost_usd_micros > 0
    assert_key_not_leaked(live_gateway)
