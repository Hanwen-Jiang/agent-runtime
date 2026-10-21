"""评测用的临时 Model Gateway（直连 OpenAI 兼容的供应商）：配置读取、请求转换、回答解析、错误分类与重试。

不连网络：HTTP 由假的 post 代替，记下发出去的请求。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import payload_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.errors import DeniedError, TransientError
from agentruntime_learning.evals.context import GatewayContextSubject
from agentruntime_learning.evals.provider_gateway import DEFAULT_MODELS, ProviderConfig, ProviderGateway, load_config, parse_json

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
    path.write_text(f"# 注释\nAR_MODEL_PROVIDER_BASE_URL=https://llm.example/v1/\nAR_MODEL_PROVIDER_API_KEY=\"{KEY}\"\nAR_MODEL_PROVIDER_MODEL_LIGHT=small-model\n", "utf-8")
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
    assert gw.calls == [{"node_id": "n1", "model": "claude-opus-5-5", "input_tokens": 1200, "output_tokens": 40, "cache_read_tokens": 1000}]


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
