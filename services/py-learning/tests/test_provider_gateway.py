"""评测用的临时 Model Gateway（直连 OpenAI 兼容的供应商）：配置读取、请求转换、回答解析、错误分类与重试、计价（T-04）。

不连网络：HTTP 由假的 post 代替，记下发出去的请求。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import payload_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.errors import DeniedError, TransientError
from agentruntime_learning.evals.cases import load_eval_set
from agentruntime_learning.evals.context import GatewayContextSubject
from agentruntime_learning.evals.provider_gateway import DEFAULT_MODELS, DEFAULT_PRICES, Price, ProviderConfig, ProviderGateway, load_config, parse_json
from agentruntime_learning.evals.runner import run_eval
from agentruntime_learning.evals.worker_subject import WorkerPlannerSubject

from support import task_case

KEY = "sk-test-not-a-real-key"


class FakePost:
    """按顺序返回 (状态码, 响应体)；响应体是 dict 时转成 JSON。"""

    def __init__(self, *replies: tuple[int, Any]) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        self.requests.append({"url": url, "headers": headers, "body": json.loads(body)})
        status, reply = self.replies.pop(0)
        return status, json.dumps(reply, ensure_ascii=False).encode() if isinstance(reply, dict) else reply


def completion(content: str | None = "", *, tool_calls: list[dict] | None = None, finish: str = "stop") -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "model": "claude-opus-5-5",
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 40, "prompt_tokens_details": {"cached_tokens": 1000}},
    }


def gateway(post: FakePost) -> tuple[ProviderGateway, list[float]]:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    return ProviderGateway(ProviderConfig(base_url="https://llm.example/v1", api_key=KEY), post=post, sleep=sleep), slept


def request(*messages: mg.ChatMessage, tier: str = "strong@medium", schema: dict | None = None, tools: tuple[str, ...] = ()) -> mg.GenerateRequest:
    params = mg.GenerateParams(messages=list(messages))
    if schema is not None:
        value = struct_pb2.Value()
        json_format.ParseDict(schema, value)
        params.output_schema.CopyFrom(payload_pb2.JsonPayload(value=value))
    for name in tools:
        params.tools.append(mg.ToolSpec(name=name))
    return mg.GenerateRequest(call=mg.CallContext(node_id="n1", tier=tier), generation=params)


def test_config_comes_from_the_file_and_environment_variables_win(tmp_path: Path) -> None:
    path = tmp_path / "provider.env"
    path.write_text(
        f"# 注释\nAR_MODEL_PROVIDER_BASE_URL=https://llm.example/v1/\nAR_MODEL_PROVIDER_API_KEY=\"{KEY}\"\nAR_MODEL_PROVIDER_MODEL_LIGHT=small-model\n"
        "AR_MODEL_PROVIDER_PRICE_LIGHT=0.1,0.4\nAR_MODEL_PROVIDER_PRICE_STRONG=5,25\n",
        "utf-8",
    )
    config = load_config({"AR_MODEL_PROVIDER_FILE": str(path), "AR_MODEL_PROVIDER_MODEL_STRONG": "big-model"})
    assert config.base_url == "https://llm.example/v1"
    assert config.api_key == KEY
    assert config.models == {"strong": "big-model", "mid": DEFAULT_MODELS["mid"], "light": "small-model"}
    assert KEY not in repr(config), "密钥不出现在 repr 里"


def test_a_missing_key_is_reported_without_guessing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="AR_MODEL_PROVIDER_API_KEY"):
        load_config({"AR_MODEL_PROVIDER_FILE": str(tmp_path / "missing.env"), "AR_MODEL_PROVIDER_BASE_URL": "https://llm.example/v1"})


async def test_messages_tools_and_tier_are_translated() -> None:
    post = FakePost((200, completion(None, tool_calls=[{"id": "c9", "type": "function", "function": {"name": "repo_read", "arguments": "{\"path\": \"a.py\"}"}}], finish="tool_calls")))
    gw, _ = gateway(post)
    args = struct_pb2.Struct()
    args.update({"path": "x.py"})
    response = await gw.generate(request(
        mg.ChatMessage(role=mg.CHAT_ROLE_SYSTEM, text="规则", cache_breakpoint=True),
        mg.ChatMessage(role=mg.CHAT_ROLE_USER, text="任务"),
        mg.ChatMessage(role=mg.CHAT_ROLE_ASSISTANT, tool_calls=[mg.ToolCall(id="c1", name="repo_read", arguments=args)]),
        mg.ChatMessage(role=mg.CHAT_ROLE_TOOL, tool_call_id="c1", text="第 0 步的输出：…"),
        tier="light@low", tools=("repo_read",),
    ))
    sent = post.requests[0]
    assert sent["url"] == "https://llm.example/v1/chat/completions"
    assert sent["headers"]["Authorization"] == f"Bearer {KEY}"
    body = sent["body"]
    assert body["model"] == DEFAULT_MODELS["light"]
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "tool"]
    assert json.loads(body["messages"][2]["tool_calls"][0]["function"]["arguments"]) == {"path": "x.py"}
    assert body["messages"][3]["tool_call_id"] == "c1"
    assert body["tools"][0]["function"]["parameters"] == {"type": "object", "properties": {}}
    assert response.finish_reason == mg.FINISH_REASON_TOOL_CALLS
    assert [(c.name, json_format.MessageToDict(c.arguments)) for c in response.tool_calls] == [("repo_read", {"path": "a.py"})]
    assert (response.usage.input_tokens, response.usage.output_tokens, response.usage.cache_read_tokens) == (1200, 40, 1000)
    assert response.model.vendor == "anthropic"
    cost = response.usage.cost_usd_micros
    assert cost > 0
    assert gw.calls == [{"node_id": "n1", "model": "claude-opus-5-5", "input_tokens": 1200, "output_tokens": 40, "cache_read_tokens": 1000, "cost_usd_micros": cost}]


async def test_structured_output_is_asked_for_and_parsed_from_the_text() -> None:
    post = FakePost((200, completion("好的：\n```json\n{\"answers\": [\"INV-0058\", \"不知道\"]}\n```")))
    gw, _ = gateway(post)
    response = await gw.generate(request(mg.ChatMessage(role=mg.CHAT_ROLE_USER, text="问题"), schema={"type": "object", "required": ["answers"]}))
    last = post.requests[0]["body"]["messages"][-1]
    assert last["role"] == "user" and '"required": ["answers"]' in last["content"]
    assert json_format.MessageToDict(response.result) == {"answers": ["INV-0058", "不知道"]}


async def test_an_unparsable_structured_answer_leaves_only_text() -> None:
    gw, _ = gateway(FakePost((200, completion("我不太确定。"))))
    response = await gw.generate(request(mg.ChatMessage(role=mg.CHAT_ROLE_USER, text="问题"), schema={"type": "object"}))
    assert not response.HasField("result")
    assert response.text == "我不太确定。"


@pytest.mark.parametrize(("text", "expected"), [("{\"a\": 1}", {"a": 1}), ("前面 {\"a\": [1, 2]} 后面", {"a": [1, 2]}), ("```\n{\"a\": 1}\n```", {"a": 1}), ("没有 JSON", None)])
def test_parse_json(text: str, expected: Any) -> None:
    assert parse_json(text) == expected


async def test_rate_limits_are_retried_with_backoff() -> None:
    gw, slept = gateway(FakePost((429, b"slow down"), (503, b"busy"), (200, completion("好"))))
    response = await gw.generate(request(mg.ChatMessage(role=mg.CHAT_ROLE_USER, text="hi")))
    assert response.text == "好"
    assert slept == [2.0, 4.0]


async def test_giving_up_after_three_transient_failures() -> None:
    gw, _ = gateway(FakePost((429, b"a"), (429, b"b"), (429, b"c")))
    with pytest.raises(TransientError):
        await gw.generate(request(mg.ChatMessage(role=mg.CHAT_ROLE_USER, text="hi")))


async def test_auth_errors_are_not_retried_and_never_echo_the_key() -> None:
    post = FakePost((401, f"invalid key {KEY}".encode()))
    gw, slept = gateway(post)
    with pytest.raises(DeniedError) as err:
        await gw.generate(request(mg.ChatMessage(role=mg.CHAT_ROLE_USER, text="hi")))
    assert KEY not in str(err.value)
    assert slept == [] and len(post.requests) == 1


async def test_the_context_eval_subject_reads_answers_through_it() -> None:
    gw, _ = gateway(FakePost((200, completion("{\"answers\": [\"6602\"]}"))))
    answers = await GatewayContextSubject(gw).answer([mg.ChatMessage(role=mg.CHAT_ROLE_USER, text="第 1 步：科目 6602")], ["INV-0042 归到了哪个科目？"])
    assert answers == ["6602"]


# --- T-04：按单价算金额 ---
# 单价是美元每百万 token，正好等于微美元每 token；OpenAI 兼容接口的 prompt_tokens 含命中缓存的 cached_tokens，
# completion_tokens 含思考 token。


class EveryTime(FakePost):
    """每次都返回同一个回答。"""

    def __init__(self, reply: tuple[int, Any]) -> None:
        super().__init__()
        self.reply = reply

    async def __call__(self, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        self.replies = [self.reply]
        return await super().__call__(url, headers, body)


def usage_completion(prompt: int, completion_tokens: int, *, cached: int = 0, reasoning: int = 0) -> dict[str, Any]:
    reply = completion("好")
    reply["usage"] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion_tokens,
        "prompt_tokens_details": {"cached_tokens": cached},
        "completion_tokens_details": {"reasoning_tokens": reasoning},
    }
    return reply


def test_every_default_model_has_a_default_price() -> None:
    for model in DEFAULT_MODELS.values():
        price = DEFAULT_PRICES[model]
        assert price.input_per_m > 0 and price.output_per_m > 0 and price.cache_read_per_m > 0, model


async def test_cost_uses_the_price_of_the_model_for_the_tier() -> None:
    """未命中缓存的输入、命中缓存的输入、输出各按各的单价：200×4 + 1000×0.2 + 40×20 = 1800 微美元。"""
    config = ProviderConfig(base_url="https://llm.example/v1", api_key=KEY, prices={"claude-opus-5-5": Price(Decimal(4), Decimal(20), Decimal("0.2"))})
    gw = ProviderGateway(config, post=FakePost((200, usage_completion(1200, 40, cached=1000))))
    response = await gw.generate(request(mg.ChatMessage(role=mg.CHAT_ROLE_USER, text="hi"), tier="strong@medium"))
    assert response.usage.cost_usd_micros == 1800
    assert gw.calls[-1]["cost_usd_micros"] == 1800


async def test_default_prices_apply_without_configuration() -> None:
    gw, _ = gateway(FakePost((200, usage_completion(3000, 500, cached=1000))))
    response = await gw.generate(request(mg.ChatMessage(role=mg.CHAT_ROLE_USER, text="hi"), tier="mid@low"))
    price = DEFAULT_PRICES[DEFAULT_MODELS["mid"]]
    expected = 2000 * price.input_per_m + 1000 * price.cache_read_per_m + 500 * price.output_per_m
    assert response.usage.cost_usd_micros == int(expected.to_integral_value(rounding="ROUND_HALF_UP"))


async def test_reasoning_tokens_are_billed_once_as_output() -> None:
    config = ProviderConfig(base_url="https://llm.example/v1", api_key=KEY, prices={"claude-haiku-5-5": Price(Decimal("0.1"), Decimal("0.5"), Decimal("0.1"))})
    gw = ProviderGateway(config, post=FakePost((200, usage_completion(1234, 100, reasoning=60))))
    response = await gw.generate(request(mg.ChatMessage(role=mg.CHAT_ROLE_USER, text="hi"), tier="light@low"))
    # 1234×0.1 + 100×0.5 = 173.4 → 173；思考 token 已在 completion_tokens 里，不再另算。
    assert response.usage.reasoning_tokens == 60
    assert response.usage.cost_usd_micros == 173


async def test_fractional_micros_round_half_up() -> None:
    config = ProviderConfig(base_url="https://llm.example/v1", api_key=KEY, prices={"claude-haiku-5-5": Price(Decimal("0.1"), Decimal("0.5"), Decimal("0.1"))})
    gw = ProviderGateway(config, post=FakePost((200, usage_completion(5, 0))))
    response = await gw.generate(request(mg.ChatMessage(role=mg.CHAT_ROLE_USER, text="hi"), tier="light@low"))
    assert response.usage.cost_usd_micros == 1, "0.5 微美元进为 1"


def test_prices_are_configured_per_tier_and_bind_to_that_tier_s_model(tmp_path: Path) -> None:
    path = tmp_path / "provider.env"
    path.write_text(
        "AR_MODEL_PROVIDER_BASE_URL=https://llm.example/v1\n"
        f"AR_MODEL_PROVIDER_API_KEY={KEY}\n"
        "AR_MODEL_PROVIDER_MODEL_MID=gpt-5.5-mini\n"
        "AR_MODEL_PROVIDER_PRICE_MID=0.25, 2, 0.025\n"
        "AR_MODEL_PROVIDER_PRICE_LIGHT=0.2,1\n",
        "utf-8",
    )
    config = load_config({"AR_MODEL_PROVIDER_FILE": str(path)})
    assert config.prices["gpt-5.5-mini"] == Price(Decimal("0.25"), Decimal(2), Decimal("0.025"))
    light = config.prices[DEFAULT_MODELS["light"]]
    assert light == Price(Decimal("0.2"), Decimal(1), Decimal("0.2")), "没给缓存读单价时按输入单价算（不假设有折扣）"
    assert config.prices[DEFAULT_MODELS["strong"]] == DEFAULT_PRICES[DEFAULT_MODELS["strong"]]


def test_a_model_without_a_price_is_refused(tmp_path: Path) -> None:
    """不认识的模型又没配单价：读配置时就报错，不悄悄按 0 记成本。"""
    env = {"AR_MODEL_PROVIDER_FILE": str(tmp_path / "missing.env"), "AR_MODEL_PROVIDER_BASE_URL": "https://llm.example/v1", "AR_MODEL_PROVIDER_API_KEY": KEY, "AR_MODEL_PROVIDER_MODEL_STRONG": "big-model"}
    with pytest.raises(ValueError, match="AR_MODEL_PROVIDER_PRICE_STRONG"):
        load_config(env)
    with pytest.raises(ValueError, match="big-model"):
        ProviderConfig(base_url="https://llm.example/v1", api_key=KEY, models={**DEFAULT_MODELS, "strong": "big-model"})


@pytest.mark.parametrize("value", ["abc", "1", "1,2,3,4", "-1,2", "1,,2"])
def test_a_malformed_price_names_the_setting(tmp_path: Path, value: str) -> None:
    env = {"AR_MODEL_PROVIDER_FILE": str(tmp_path / "missing.env"), "AR_MODEL_PROVIDER_BASE_URL": "https://llm.example/v1", "AR_MODEL_PROVIDER_API_KEY": KEY, "AR_MODEL_PROVIDER_PRICE_LIGHT": value}
    with pytest.raises(ValueError, match="AR_MODEL_PROVIDER_PRICE_LIGHT") as err:
        load_config(env)
    assert KEY not in str(err.value)


async def test_planning_cost_share_is_computable_through_the_provider_gateway(make_set) -> None:
    """S-09 的“规划成本占 Task 预计成本”要有金额才算得出来：经临时 Gateway 规划，比例有值且大于 0。"""
    es = load_eval_set(make_set([task_case("t_full", estimated_cost=1.0)]))
    gw = ProviderGateway(ProviderConfig(base_url="https://llm.example/v1", api_key=KEY), post=EveryTime((200, usage_completion(2000, 300))))
    report = await run_eval(es, WorkerPlannerSubject(gw), reports_dir=None, compare=None)
    cost = report.data["metrics"]["cost"]
    assert cost["total_usd"] > 0
    assert cost["planner_cost_ratio"]["cases"] == 1 and cost["planner_cost_ratio"]["mean"] > 0


async def test_small_and_light_name_the_same_band() -> None:
    """Planner 的分诊用 small@low，Context Builder 的摘要用 light@low：都走小模型，不报 unknown_tier。"""
    post = FakePost((200, completion("好")), (200, completion("好")))
    gw, _ = gateway(post)
    for tier in ("small@low", "light@low"):
        await gw.generate(request(mg.ChatMessage(role=mg.CHAT_ROLE_USER, text="hi"), tier=tier))
    assert [r["body"]["model"] for r in post.requests] == [DEFAULT_MODELS["light"]] * 2
