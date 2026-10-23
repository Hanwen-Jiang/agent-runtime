"""Telemetry 各实现共用的属性规则（语义见 agentruntime_core.providers.telemetry）。

控制台实现与 OpenTelemetry 实现调同一份，Collector 的第二道脱敏（deploy/helm/agent-runtime/files/otel-collector.yaml）
也对照这里的列表（tests/deploy 检查）。Go 侧的同一份规则在 libs/go-core/providers/telemetry。
"""

from __future__ import annotations

from typing import Any

from agentruntime_core.providers.telemetry import REDACTED, Attributes
from agentruntime_core.secrets import Secret

# 键名（不分大小写）含其中任何一个就脱敏。
SENSITIVE_KEY_PARTS = ("password", "secret", "token", "api_key", "apikey", "authorization", "credential", "cookie")
# 由绑定加上的租户与作用域属性；调用方传入的一律忽略。
RESERVED_KEYS = ("tenant_id", "user_id", "workspace_id", "session_id", "scope")
# GenAI 语义约定的 token 计数：键名含 token，但值是整数时不是凭据，不脱敏（T-15）。
GENAI_USAGE_COUNT_KEYS = ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens")


def _clean_value(value: Any) -> Any:
    if isinstance(value, Secret):
        return REDACTED
    if isinstance(value, (list, tuple)):
        return REDACTED if any(isinstance(v, Secret) for v in value) else list(value)
    return value


def is_sensitive(key: str, value: Any) -> bool:
    if key in GENAI_USAGE_COUNT_KEYS and isinstance(value, int) and not isinstance(value, bool):
        return False
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def sanitize(attributes: Attributes | None) -> dict[str, Any]:
    """去掉保留属性，脱敏敏感键与 Secret 值。"""
    clean: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if key in RESERVED_KEYS:
            continue
        clean[key] = REDACTED if is_sensitive(key, value) else _clean_value(value)
    return clean
