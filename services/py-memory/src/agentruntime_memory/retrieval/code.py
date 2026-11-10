"""Preserve parser-produced source ranges instead of sentence-splitting code.

This layer never parses or executes code. The isolated file engine supplies
structural blocks; oversized input uses an explicit, byte-exact line fallback.
"""
from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from typing import Any

from llama_index.core.schema import TextNode

from agentruntime_core.errors import InvalidError

MAX_CODE_BYTES = 2400
CODE_CHUNKER = f"structural-code-utf8bound-v1:{MAX_CODE_BYTES}"


def is_code_block(block: Mapping[str, Any]) -> bool:
    anchor = block.get("anchor")
    return block.get("kind") == "code" or isinstance(anchor, dict) and anchor.get("kind") == "code_range"


def _byte_ranges(raw: bytes) -> Iterator[tuple[int, int]]:
    start = 0
    while start < len(raw):
        end = min(start + MAX_CODE_BYTES, len(raw))
        if end < len(raw):
            # Prefer an entire line. A very long line still has a strict byte
            # bound, and is cut only at a UTF-8 code point boundary.
            newline = raw.rfind(b"\n", start, end)
            if newline >= start:
                end = newline + 1
            else:
                while end > start and raw[end] & 0xC0 == 0x80:
                    end -= 1
        yield start, end
        start = end


def code_nodes(text: str, anchor: dict[str, Any], *, truncated: bool = False) -> list[tuple[TextNode, dict[str, Any]]]:
    """Build nodes with original text and absolute UTF-8, end-exclusive offsets."""
    raw = text.encode("utf-8")
    required = ("start_byte", "end_byte", "start_line", "end_line")
    # Protobuf Struct numbers arrive as floats over KnowledgeService gRPC.
    # Accept exact integral values, but never booleans, fractions or infinities.
    anchor = dict(anchor)
    for key in required:
        number = anchor.get(key)
        if (isinstance(number, bool) or not isinstance(number, (int, float)) or
                not math.isfinite(number) or int(number) != number or abs(number) > 2**53):
            raise InvalidError("retrieval_code_anchor", "Code source offsets must be exact integers")
        anchor[key] = int(number)
    if (anchor.get("kind") != "code_range" or
            any(type(anchor.get(k)) is not int for k in required) or
            anchor["start_byte"] < 0 or anchor["end_byte"] < anchor["start_byte"] or
            anchor["start_line"] < 1 or anchor["end_line"] < anchor["start_line"] or
            not isinstance(anchor.get("language"), str) or not anchor["language"] or len(anchor["language"]) > 64):
        raise InvalidError("retrieval_code_anchor", "Code blocks require valid source byte and line ranges")
    size = anchor["end_byte"] - anchor["start_byte"]
    if size != len(raw) and not (truncated and len(raw) < size):
        raise InvalidError("retrieval_code_anchor", "Code text does not match its source byte range")
    for key in ("symbol", "qualified_symbol"):
        if key in anchor and (not isinstance(anchor[key], str) or len(anchor[key]) > 1024):
            raise InvalidError("retrieval_code_anchor", "Code symbol metadata is invalid")
    out = []
    next_line, next_char = anchor["start_line"], 0
    for start, end in _byte_ranges(raw):
        piece = raw[start:end]
        body = piece.decode("utf-8")
        selected = {
            **anchor,
            "start_byte": anchor["start_byte"] + start,
            "end_byte": anchor["start_byte"] + end,
            "start_line": next_line,
            "end_line": next_line + piece.count(b"\n") - int(piece.endswith(b"\n")),
        }
        selected["end_line"] = max(selected["start_line"], selected["end_line"])
        if len(raw) > MAX_CODE_BYTES:
            selected["boundary_split"] = "line_or_utf8_limit"
            selected["partial_symbol"] = True
        if truncated:
            selected["truncated"] = True
            selected["partial_symbol"] = True
        out.append((TextNode(text=body, start_char_idx=next_char, end_char_idx=next_char + len(body)), selected))
        next_char += len(body)
        next_line += piece.count(b"\n")
    return out
