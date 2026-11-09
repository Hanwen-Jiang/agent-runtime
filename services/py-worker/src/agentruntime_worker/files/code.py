"""Syntax-aware source representations; never execute code or resolve imports.

Tree-sitter is optional at runtime: unavailable grammars, invalid source and
oversized input explicitly fall back to the existing bounded text reader.
Only immutable ranges are cached, not native parser trees or authorization.
"""
from __future__ import annotations

import bisect
import hashlib
import importlib
import importlib.metadata
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from .models import Context, FileEngineError, MAX_BLOCKS, MAX_TEXT_CHARS, positive

SOURCE_LANGUAGES = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript", ".tsx": "tsx",
    ".c": "c", ".h": "c", ".go": "go", ".rs": "rust",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".java": "java", ".cs": "csharp", ".rb": "ruby", ".php": "php",
}
GRAMMARS = {
    "python": ("tree_sitter_python", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "c": ("tree_sitter_c", "language"),
    "go": ("tree_sitter_go", "language"),
    "rust": ("tree_sitter_rust", "language"),
}
CHUNK_BYTES = 2400
MAX_PARSE_BYTES = 4 << 20
MAX_NODES = 100_000
MAX_SYMBOLS = 5000
MAX_SYMBOL_BYTES = 512
MAX_CACHE_ENTRIES = 32
MAX_CACHE_BYTES = 8 << 20


@dataclass(frozen=True)
class Symbol:
    start: int
    end: int
    name: str
    qualified: str
    kind: str
    parent: int


@dataclass(frozen=True)
class Segment:
    start: int
    end: int
    symbol: int = -1


@dataclass(frozen=True)
class Parsed:
    segments: tuple[Segment, ...]
    symbols: tuple[Symbol, ...]
    versions: tuple[str, ...]


_cache: OrderedDict[tuple, tuple[Parsed, int]] = OrderedDict()
_cache_lock = threading.Lock()
_cache_bytes = 0


def _versions(language: str) -> tuple[str, ...]:
    module, _ = GRAMMARS[language]
    return ("tree-sitter-code-v1", importlib.metadata.version("tree-sitter"),
            module, importlib.metadata.version(module.replace("_", "-")))


def _name(node, raw: bytes) -> str:
    name = node.child_by_field_name("name")
    if name is None and node.type == "impl_item":
        name = node.child_by_field_name("type")
    if name is None and node.type == "function_definition":  # C declarators can be nested.
        name = node.child_by_field_name("declarator")
        while name is not None and name.type not in ("identifier", "field_identifier"):
            name = name.child_by_field_name("declarator")
    if name is None:
        return ""
    if name.end_byte - name.start_byte > MAX_SYMBOL_BYTES:
        raise FileEngineError("code_symbol_limit", "symbol name exceeds syntax metadata limit; using text ranges")
    return raw[name.start_byte:name.end_byte].decode("utf-8")


def _definition(node, language: str, raw: bytes):
    """Return a named definition and optionally its decorated child to skip."""
    types = {
        "function_definition": "function", "class_definition": "class",
        "function_declaration": "function", "generator_function_declaration": "function",
        "class_declaration": "class", "method_definition": "method",
        "interface_declaration": "interface", "type_alias_declaration": "type",
        "enum_declaration": "enum", "method_declaration": "method",
        "type_spec": "type", "impl_item": "impl",
        "function_item": "function", "struct_item": "struct", "enum_item": "enum",
        "trait_item": "trait",
    }
    if language == "python" and node.type == "decorated_definition":
        definition = node.child_by_field_name("definition")
        if definition is not None:
            return _name(definition, raw), types.get(definition.type, "function"), definition.id
    if node.type == "variable_declarator":
        value = node.child_by_field_name("value")
        if value is not None and value.type in ("arrow_function", "function_expression", "generator_function"):
            return _name(node, raw), "function", None
    if node.type in types:
        name = _name(node, raw)
        # JS/TS named function expressions are intentionally not declarations.
        if name:
            return name, types[node.type], None
    if language == "c" and node.type in ("struct_specifier", "enum_specifier", "union_specifier"):
        if node.child_by_field_name("body") is not None and (name := _name(node, raw)):
            return name, node.type.removesuffix("_specifier"), None
    return "", "", None


def _receiver_name(node, raw: bytes) -> str:
    """Go receivers live outside the declared function name."""
    receiver = node.child_by_field_name("receiver")
    if receiver is None:
        return ""
    for parameter in receiver.named_children:
        value = parameter.child_by_field_name("type")
        if value is None:
            continue
        pending = [value]
        while pending:
            part = pending.pop()
            if part.type in ("type_identifier", "qualified_type"):
                if part.end_byte - part.start_byte > MAX_SYMBOL_BYTES:
                    raise FileEngineError("code_symbol_limit", "receiver name exceeds syntax metadata limit; using text ranges")
                return raw[part.start_byte:part.end_byte].decode("utf-8")
            pending.extend(reversed(part.named_children))
    return ""


def _bounded(raw: bytes, start: int, end: int, maximum: int):
    """Prefer line endings, then split an oversized line on UTF-8 boundaries."""
    while start < end:
        stop = min(start + maximum, end)
        if stop < end:
            newline = raw.rfind(b"\n", start, stop)
            if newline >= start:
                stop = newline + 1
            else:
                while stop > start and raw[stop] & 0xC0 == 0x80:
                    stop -= 1
        yield start, stop
        start = stop


def _parse(raw: bytes, language: str, versions: tuple[str, ...]) -> Parsed:
    from tree_sitter import Language, Parser
    module, entry = GRAMMARS[language]
    grammar = Language(getattr(importlib.import_module(module), entry)())
    tree = Parser(grammar).parse(raw)
    if tree.root_node.has_error:
        raise FileEngineError("code_syntax_error", "source contains syntax errors; using text ranges")
    symbols, native, children = [], [], {-1: []}
    stack = [(tree.root_node, -1, None)]
    visited = 0
    while stack:
        node, parent, skip = stack.pop()
        visited += 1
        if visited > MAX_NODES or len(symbols) >= MAX_SYMBOLS:
            raise FileEngineError("code_parse_limit", "source exceeds syntax traversal limits; using text ranges")
        name, kind, decorated = _definition(node, language, raw) if node.id != skip else ("", "", None)
        if name:
            qualified = symbols[parent].qualified + "." + name if parent >= 0 else name
            if language == "go" and node.type == "method_declaration" and (receiver := _receiver_name(node, raw)):
                qualified = receiver + "." + name
            if len(qualified.encode("utf-8")) > MAX_SYMBOL_BYTES:
                raise FileEngineError("code_symbol_limit", "qualified symbol exceeds syntax metadata limit; using text ranges")
            index = len(symbols)
            symbols.append(Symbol(node.start_byte, node.end_byte, name, qualified, kind, parent))
            native.append(node)
            children.setdefault(parent, []).append(index)
            children[index] = []
            parent = index
        for child in reversed(node.named_children):
            stack.append((child, parent, decorated))

    segments = []

    def plain(start: int, end: int, owner: int) -> None:
        segments.extend(Segment(a, b, owner) for a, b in _bounded(raw, start, end, CHUNK_BYTES))

    def syntax(start: int, end: int, owner: int, node) -> None:
        # Iterative splitting avoids recursion on adversarially nested source.
        pending = [(start, end, node)]
        while pending:
            a, b, current = pending.pop()
            if b - a <= CHUNK_BYTES:
                if b > a:
                    segments.append(Segment(a, b, owner))
                continue
            parts = []
            cursor = a
            for child in current.named_children if current is not None else ():
                if child.start_byte < a or child.end_byte > b or child.end_byte <= child.start_byte:
                    continue
                if child.start_byte > cursor:
                    parts.append((cursor, child.start_byte, None))
                parts.append((child.start_byte, child.end_byte, child))
                cursor = child.end_byte
            if not parts or len(parts) == 1 and parts[0][:2] == (a, b) and parts[0][2] is current:
                plain(a, b, owner)
                continue
            if cursor < b:
                parts.append((cursor, b, None))
            pending.extend(reversed(parts))

    # Partition around nested definitions; parent headers, comments, imports and
    # top-level expressions remain present as disjoint source ranges.
    pending = [(0, len(raw), -1)]
    while pending:
        start, end, owner = pending.pop()
        nested = children.get(owner, [])
        if not nested:
            syntax(start, end, owner, native[owner] if owner >= 0 else tree.root_node)
            continue
        parts = []
        cursor = start
        for index in nested:
            symbol = symbols[index]
            if cursor < symbol.start:
                parts.append((cursor, symbol.start, owner, False))
            parts.append((symbol.start, symbol.end, index, True))
            cursor = symbol.end
        if cursor < end:
            parts.append((cursor, end, owner, False))
        for a, b, index, recurse in reversed(parts):
            if recurse:
                pending.append((a, b, index))
            else:
                plain(a, b, index)
    combined = []
    for segment in sorted(segments, key=lambda value: value.start):
        if (combined and combined[-1].end == segment.start and combined[-1].symbol == segment.symbol
                and segment.end - combined[-1].start <= CHUNK_BYTES):
            previous = combined[-1]
            combined[-1] = Segment(previous.start, segment.end, segment.symbol)
        else:
            combined.append(segment)
    return Parsed(tuple(combined), tuple(symbols), versions)


def _parsed(raw: bytes, language: str) -> tuple[Parsed, bool]:
    global _cache_bytes
    versions = _versions(language)
    key = (hashlib.sha256(raw).hexdigest(), language, versions)
    with _cache_lock:
        if key in _cache:
            item, _ = _cache[key]
            _cache.move_to_end(key)
            return item, True
    parsed = _parse(raw, language, versions)
    size = len(parsed.segments) * 96 + sum(96 + len(s.name.encode()) + len(s.qualified.encode()) for s in parsed.symbols)
    if size <= MAX_CACHE_BYTES:
        with _cache_lock:
            previous = _cache.pop(key, None)
            if previous:
                _cache_bytes -= previous[1]
            while _cache and (len(_cache) >= MAX_CACHE_ENTRIES or _cache_bytes + size > MAX_CACHE_BYTES):
                _, (_, old_size) = _cache.popitem(last=False)
                _cache_bytes -= old_size
            _cache[key] = parsed, size
            _cache_bytes += size
    return parsed, False


def code_read(ctx: Context, fallback: Callable[[Context], dict]) -> dict:
    language = SOURCE_LANGUAGES[ctx.source.suffix.lower()]
    requested = ctx.selector.get("symbol")
    if requested is not None and (not isinstance(requested, str) or not requested.strip() or len(requested) > 512):
        raise FileEngineError("invalid_selector", "symbol must be a nonempty name of at most 512 characters")

    def text_fallback(code: str, message: str) -> dict:
        if requested is not None:
            raise FileEngineError("code_symbol_unavailable", "symbol selection requires a supported, valid UTF-8 syntax tree", "unsupported")
        response = fallback(ctx)
        response["warnings"].append({"code": code, "message": message})
        response["coverage"].update({"parser": "text", "language": language, "syntax_fallback": True})
        return response

    raw = ctx.source.read_bytes()
    if language not in GRAMMARS:
        return text_fallback("code_language_unsupported", "no grammar is configured; using text ranges")
    if len(raw) > MAX_PARSE_BYTES:
        return text_fallback("code_parse_limit", "source exceeds 4 MiB syntax limit; using text ranges")
    if ctx.options.get("encoding", "utf-8-sig") not in ("utf-8", "utf-8-sig"):
        return text_fallback("code_encoding_fallback", "syntax parsing requires UTF-8; using requested text encoding")
    try:
        raw.decode("utf-8")
        if b"\x00" in raw:
            return text_fallback("code_binary_fallback", "source contains NUL; using text validation")
        parsed, _ = _parsed(raw, language)
    except UnicodeError:
        return text_fallback("code_encoding_fallback", "source is not UTF-8; specify its text encoding")
    except (ImportError, importlib.metadata.PackageNotFoundError):
        return text_fallback("code_grammar_unavailable", "syntax parser or grammar is not installed; using text ranges")
    except FileEngineError as error:
        return text_fallback(error.code, error.message)
    except (ValueError, TypeError, RuntimeError):
        return text_fallback("code_parser_unavailable", "syntax parser could not read this source; using text ranges")

    line_starts = [0] + [i + 1 for i, byte in enumerate(raw) if byte == 10]
    total_lines = len(line_starts) - (bool(raw) and raw.endswith(b"\n"))
    if not raw:
        total_lines = 0

    def anchor(start: int, end: int, symbol: Symbol | None = None) -> dict:
        value = {"kind": "code_range", "language": language, "start_byte": start, "end_byte": end,
                 "offset_unit": "utf8_byte", "start_line": bisect.bisect_right(line_starts, start),
                 "end_line": bisect.bisect_right(line_starts, max(start, end - 1))}
        if symbol:
            value.update({"symbol": symbol.name, "qualified_symbol": symbol.qualified, "symbol_kind": symbol.kind})
        return value

    selected = [(0, len(raw))]
    if requested is not None:
        matched = [s for s in parsed.symbols if s.name == requested or s.qualified == requested]
        if not matched:
            raise FileEngineError("code_symbol_not_found", "the requested symbol does not exist in this source")
        selected = []
        for symbol in sorted(matched, key=lambda s: (s.start, -s.end)):
            if not selected or symbol.start >= selected[-1][1]:
                selected.append((symbol.start, symbol.end))
    explicit_lines = "start_line" in ctx.selector or "max_lines" in ctx.selector
    if explicit_lines:
        start = positive(ctx.selector.get("start_line"), 1, max(total_lines, 1), "start_line")
        count = positive(ctx.selector.get("max_lines"), 200, 2000, "max_lines")
        a = line_starts[start - 1]
        b = line_starts[min(start - 1 + count, len(line_starts) - 1)] if start - 1 + count < len(line_starts) else len(raw)
        selected = [(max(a, x), min(b, y)) for x, y in selected if min(b, y) > max(a, x)]

    blocks = []
    remaining = MAX_TEXT_CHARS
    output_limited = False
    for segment in parsed.segments:
        for left, right in selected:
            start, end = max(segment.start, left), min(segment.end, right)
            if end <= start:
                continue
            if len(blocks) >= MAX_BLOCKS or remaining <= 0:
                output_limited = True
                continue
            text = raw[start:end].decode("utf-8")
            if len(text) > remaining:
                text = text[:remaining]
                end = start + len(text.encode("utf-8"))
                output_limited = True
            remaining -= len(text)
            symbol = parsed.symbols[segment.symbol] if segment.symbol >= 0 else None
            where = anchor(start, end, symbol)
            where["partial_symbol"] = bool(symbol and (start > symbol.start or end < symbol.end))
            blocks.append(ctx.block("code", text, where, derivation="tree_sitter"))
    outline = [{**anchor(s.start, s.end, s), "kind": s.kind} for s in parsed.symbols
               if any(s.start < b and s.end > a for a, b in selected)]
    warnings = []
    if output_limited:
        warnings.append({"code": "output_limit", "message": "code output limited to 500 blocks / 40000 characters"})
    if len(outline) > MAX_BLOCKS:
        warnings.append({"code": "outline_limit", "message": "symbol outline limited to 500 entries"})
        output_limited = True
    partial = selected != [(0, len(raw))] or output_limited
    result = ctx.response(status="partial" if partial else "ready", blocks=blocks, outline=outline[:MAX_BLOCKS], warnings=warnings,
                        coverage={"parser": "tree-sitter", "language": language, "syntax_fallback": False,
                                  "parser_versions": list(parsed.versions),
                                  "total_lines": total_lines, "source_bytes": len(raw),
                                  "selected_bytes": sum(b - a for a, b in selected), "symbols": len(parsed.symbols),
                                  "returned_bytes": sum(len(b["text"].encode("utf-8")) for b in blocks),
                                  "selected_symbol": requested, "chunk_bytes": CHUNK_BYTES})
    result["coverage"]["output_truncated"] |= output_limited
    return result
