"""token 估算：没有供应商的分词器时按字符估算（ASCII 约 4 个字符一个 token，其余字符各算一个）。"""

from __future__ import annotations

import math


def estimate(text: str) -> int:
    ascii_chars = sum(1 for c in text if c.isascii())
    return math.ceil(ascii_chars / 4) + (len(text) - ascii_chars)


def truncate(text: str, tokens: int) -> str:
    """截到不超过 tokens 的前缀，截断时以“…”结尾。"""
    if estimate(text) <= tokens:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate(text[:mid]) + 1 <= tokens:
            low = mid
        else:
            high = mid - 1
    return text[:low] + "…"
