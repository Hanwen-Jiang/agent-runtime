"""评测用的临时 Model Gateway：直连一个 OpenAI 兼容的模型供应商。Model Gateway 的单供应商直连（实施计划第 7 节）接好之前，
给真模型评测用：

    AR_EVAL_GATEWAY_FACTORY=agentruntime_learning.evals.provider_gateway:make

配置从文件读（默认 ~/.agentruntime/model-provider.env，AR_MODEL_PROVIDER_FILE 可换），同名环境变量优先：
- AR_MODEL_PROVIDER_BASE_URL、AR_MODEL_PROVIDER_API_KEY：必填；
- AR_MODEL_PROVIDER_MODEL_STRONG、_MID、_LIGHT：各档用的模型，默认 claude-opus-5-5、claude-sonnet-5-5、claude-haiku-5-5。
文件放在仓库外面：密钥不进仓库，也不出现在错误信息里。

只做评测用得到的部分：
- Tier 只看档位（strong@medium 取 strong），推理强度与 thinking 预算不传；
- 结构化输出：把 output_schema 写进最后一条提示，要求只输出 JSON，再从回答里解析；解析不出来就只给 text，
  由调用方按“不合格”处理（与真 Gateway 结构化输出失败时一样）；
- 工具定义与工具调用按 OpenAI 的 function calling 转换；
- cache_breakpoint 不传：各家 OpenAI 兼容接口对缓存参数的支持不一；
- 计量只记 token（含命中缓存的 cached_tokens），不算金额；
- 限流、5xx、超时与连不上按 transient 重试 2 次，仍失败抛 TransientError；其余 HTTP 错误按状态码抛对应类别的
  ProviderError。
不做嵌入。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import metering_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.errors import ErrorCategory, ProviderError

ENV_FILE = "AR_MODEL_PROVIDER_FILE"
ENV_PREFIX = "AR_MODEL_PROVIDER_"
DEFAULT_FILE = Path.home() / ".agentruntime" / "model-provider.env"
DEFAULT_MODELS = {"strong": "claude-opus-5-5", "mid": "claude-sonnet-5-5", "light": "claude-haiku-5-5"}
ATTEMPTS = 3
BACKOFF_S = 2.0
TIMEOUT_S = 180.0
JSON_INSTRUCTION = "只输出一个符合下面 JSON Schema 的 JSON 对象，不要输出其他文字，也不要用代码块包起来：\n"
VENDORS = {"claude": "anthropic", "gpt": "openai", "gemini": "google", "deepseek": "deepseek", "qwen": "alibaba"}
FINISH = {"stop": mg.FINISH_REASON_STOP, "length": mg.FINISH_REASON_MAX_TOKENS, "tool_calls": mg.FINISH_REASON_TOOL_CALLS, "content_filter": mg.FINISH_REASON_CONTENT_FILTER}

Post = Callable[[str, Mapping[str, str], bytes], Awaitable[tuple[int, bytes]]]
"""(url, headers, body) → (HTTP 状态码, 响应体)；连不上或超时抛 OSError。"""


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    api_key: str = field(repr=False)
    models: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MODELS))


def _read_env_file(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_config(env: Mapping[str, str] | None = None) -> ProviderConfig:
    env = os.environ if env is None else env
    path = Path(env.get(ENV_FILE) or DEFAULT_FILE)
    values = _read_env_file(path) if path.is_file() else {}
    values.update({k: v for k, v in env.items() if k.startswith(ENV_PREFIX)})
    base_url, api_key = values.get(f"{ENV_PREFIX}BASE_URL", ""), values.get(f"{ENV_PREFIX}API_KEY", "")
    if not base_url or not api_key:
        raise ValueError(f"缺少 {ENV_PREFIX}BASE_URL 或 {ENV_PREFIX}API_KEY：写进 {path}，或设同名环境变量")
    models = {tier: values.get(f"{ENV_PREFIX}MODEL_{tier.upper()}") or model for tier, model in DEFAULT_MODELS.items()}
    return ProviderConfig(base_url=base_url.rstrip("/"), api_key=api_key, models=models)


def _urllib_post(timeout_s: float) -> Post:
    def send(url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, bytes]:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    async def post(url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, bytes]:
        return await asyncio.to_thread(send, url, headers, body)

    return post


def _category(status: int) -> ErrorCategory:
    if status in (408, 409, 425, 429) or status >= 500:
        return ErrorCategory.TRANSIENT
    if status in (401, 403):
        return ErrorCategory.DENIED
    if status == 404:
        return ErrorCategory.NOT_FOUND
    return ErrorCategory.INVALID


def _value(data: Any) -> struct_pb2.Value:
    value = struct_pb2.Value()
    json_format.ParseDict(data, value)
    return value


def _schema(payload: Any) -> dict[str, Any]:
    schema = json_format.MessageToDict(payload.value) if payload.HasField("value") else {}
    schema = schema if isinstance(schema, dict) else {}
    if schema.get("type", "object") == "object":
        schema = {"type": "object", "properties": {}, **schema}
    return schema


def _messages(messages: Sequence[mg.ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == mg.CHAT_ROLE_SYSTEM:
            out.append({"role": "system", "content": m.text})
        elif m.role == mg.CHAT_ROLE_USER:
            out.append({"role": "user", "content": m.text})
        elif m.role == mg.CHAT_ROLE_ASSISTANT:
            item: dict[str, Any] = {"role": "assistant", "content": m.text or None}
            if m.tool_calls:
                item["tool_calls"] = [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(json_format.MessageToDict(c.arguments), ensure_ascii=False)}}
                    for c in m.tool_calls
                ]
            out.append(item)
        elif m.role == mg.CHAT_ROLE_TOOL:
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.text})
        else:
            raise ProviderError.of(ErrorCategory.INVALID, "bad_message", f"不认识的消息角色 {m.role}")
    return out


def parse_json(text: str) -> Any:
    """从回答里取出 JSON：容忍代码块与前后多余的文字；取不出来返回 None。"""
    body = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    for candidate in (body, body[body.find("{") : body.rfind("}") + 1] if "{" in body else ""):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class ProviderGateway:
    """ModelGateway 的实现：每次 generate 是一次 /chat/completions。calls 记下每次调用的 node、模型与 token，便于看花费。"""

    def __init__(self, config: ProviderConfig, *, post: Post | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.config = config
        self.post = post or _urllib_post(TIMEOUT_S)
        self.sleep = sleep
        self.host = urllib.parse.urlsplit(config.base_url).hostname or "provider"
        self.calls: list[dict[str, Any]] = []

    def model_for(self, tier: str) -> str:
        band = tier.partition("@")[0] or "strong"
        if band not in self.config.models:
            raise ProviderError.of(ErrorCategory.INVALID, "unknown_tier", f"不认识的 Tier {tier}")
        return self.config.models[band]

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        gen = request.generation
        model = self.model_for(request.call.tier)
        messages = _messages(gen.messages)
        structured = gen.HasField("output_schema")
        if structured:
            messages.append({"role": "user", "content": JSON_INSTRUCTION + json.dumps(_schema(gen.output_schema), ensure_ascii=False)})
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if gen.tools:
            payload["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description or t.name, "parameters": _schema(t.params_schema)}}
                for t in gen.tools
            ]
        if gen.max_output_tokens:
            payload["max_tokens"] = gen.max_output_tokens
        data = await self._send(payload)
        return self._response(request, model, data, structured)

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        raise ProviderError.of(ErrorCategory.INVALID, "unsupported", "临时的评测 Gateway 不做嵌入")

    async def _send(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.config.base_url + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json", "User-Agent": "agentruntime-evals"}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        error: ProviderError | None = None
        for attempt in range(ATTEMPTS):
            if attempt:
                await self.sleep(BACKOFF_S * 2 ** (attempt - 1))
            try:
                status, raw = await self.post(url, headers, body)
            except OSError as exc:
                error = ProviderError.of(ErrorCategory.TRANSIENT, "unreachable", f"连不上 {self.host}：{type(exc).__name__}")
                continue
            if status == 200:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    raise ProviderError.of(ErrorCategory.INVALID, "bad_response", f"{self.host} 返回的不是 JSON") from None
                if not isinstance(data, dict) or not data.get("choices"):
                    raise ProviderError.of(ErrorCategory.INVALID, "bad_response", f"{self.host} 的回答里没有 choices")
                return data
            error = ProviderError.of(_category(status), f"http_{status}", raw[:300].decode("utf-8", "replace").replace(self.config.api_key, "***"))
            if not error.retryable:
                raise error
        assert error is not None
        raise error

    def _response(self, request: mg.GenerateRequest, model: str, data: dict[str, Any], structured: bool) -> mg.GenerateResponse:
        choice = data["choices"][0]
        message = choice.get("message") or {}
        content = message.get("content")
        text = content if isinstance(content, str) else "".join(p.get("text", "") for p in content or [] if isinstance(p, dict))
        usage = data.get("usage") or {}
        served = str(data.get("model") or model)
        response = mg.GenerateResponse(
            finish_reason=FINISH.get(choice.get("finish_reason") or "", mg.FINISH_REASON_UNSPECIFIED),
            model=mg.ModelChoice(model_profile_id=f"{self.host}/{served}", tier=request.call.tier, vendor=VENDORS.get(served.split("-")[0].lower(), "unknown")),
            usage=metering_pb2.Usage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
                cache_read_tokens=int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
                reasoning_tokens=int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
            ),
            rationale=str(message.get("reasoning_content") or ""),
        )
        result = parse_json(text) if structured and text else None
        if isinstance(result, dict):
            response.result.CopyFrom(_value(result))
        else:
            response.text = text
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            arguments = struct_pb2.Struct()
            json_format.ParseDict(_arguments(fn.get("arguments")), arguments)
            response.tool_calls.append(mg.ToolCall(id=str(call.get("id") or ""), name=str(fn.get("name") or ""), arguments=arguments))
        self.calls.append({"node_id": request.call.node_id, "model": served, "input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens, "cache_read_tokens": response.usage.cache_read_tokens})
        return response


def make() -> ProviderGateway:
    """AR_EVAL_GATEWAY_FACTORY 用的工厂：按 load_config() 读配置。"""
    return ProviderGateway(load_config())
