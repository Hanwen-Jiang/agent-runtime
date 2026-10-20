"""幂等键与缓存键（计划 S-05 难点“幂等键”“缓存”）。纯函数：Stage Runner 在工作流里生成幂等键，
Node Executor 在 activity 里重新算一遍核对，两边必须得到同样的值。

- 幂等键 = idk_ + sha256(规范化 JSON{tenant_id, task_id, node_id, inputs})。不含 attempt 编号，activity 重试与重放时不变。
- 缓存键 = node-cache: + 模板（含版本） + : + sha256(规范化输入)（附录 D.1）。缓存按租户绑定，键里不含租户。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

IDEMPOTENCY_PREFIX = "idk_"
CACHE_PREFIX = "node-cache:"


def canonical_json(value: Any) -> str:
    """规范化 JSON：键排序、无多余空白、保留非 ASCII 字符。键的顺序不同、内容相同的输入得到同样的文本。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def input_hash(value: Any) -> str:
    """规范化 JSON 的 sha256（十六进制）。"""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def idempotency_key(tenant_id: str, task_id: str, node_id: str, inputs: Mapping[str, Any]) -> str:
    return IDEMPOTENCY_PREFIX + input_hash({"tenant_id": tenant_id, "task_id": task_id, "node_id": node_id, "inputs": inputs})


def cache_key(template: str, inputs: Mapping[str, Any]) -> str:
    return f"{CACHE_PREFIX}{template}:{input_hash(inputs)}"
