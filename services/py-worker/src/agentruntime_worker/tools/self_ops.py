"""Tier-2 platform self-ops tools: todo_write, file_inspect, and memory_search (MAT-TOOL-L2A)."""

from __future__ import annotations

import re
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

from agentruntime_core.enums import Effect
from agentruntime_core.errors import InvalidError, NotFoundError
from agentruntime_core.providers.tool_driver import ToolInvocation, ToolSpec
from agentruntime_core.tenancy import TenantContext

_LANG_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".go": "go",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".rs": "rust",
    ".java": "java",
    ".md": "markdown",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".sql": "sql",
    ".sh": "shell",
    ".css": "css",
    ".html": "html",
}

_SYMBOL_RE = re.compile(
    r"^\s*(?:async\s+def|def|class|func|type|interface|struct|export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const))\s+([A-Za-z_][A-Za-z0-9_]*)"
)

_TODO_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})


class TodoStore:
    """Thread-safe task-scoped checklist storage isolated by (tenant_id, workspace_id, task_id)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[tuple[str, str, str], list[dict[str, str]]] = {}

    def tool(self) -> tuple[ToolSpec, Any]:
        return (
            ToolSpec(
                name="todo_write",
                version="v1",
                effect=Effect.WRITE_REVERSIBLE,
                description="更新当前任务的结构化步骤清单（按租户/工作区/任务隔离，不污染 Git 工作区）",
                input_schema={
                    "type": "object",
                    "required": ["todos"],
                    "properties": {
                        "todos": {
                            "type": "array",
                            "maxItems": 30,
                            "items": {
                                "type": "object",
                                "required": ["id", "content", "status"],
                                "properties": {
                                    "id": {"type": "string", "minLength": 1, "maxLength": 64},
                                    "content": {"type": "string", "minLength": 1, "maxLength": 400},
                                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
                                },
                                "additionalProperties": False,
                            },
                        },
                        "replace": {"type": "boolean", "description": "是否全量覆盖当前任务清单（默认 true；false 则按 id 合并）"},
                    },
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                permissions=frozenset(),
                timeout=timedelta(seconds=15),
            ),
            self.write,
        )

    async def write(self, invocation: ToolInvocation) -> dict[str, Any]:
        raw_todos = invocation.arguments.get("todos")
        if not isinstance(raw_todos, list):
            raise InvalidError("invalid_todos", "todos 必须是数组")
        replace = bool(invocation.arguments.get("replace", True))
        normalized: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for item in raw_todos:
            if not isinstance(item, dict):
                raise InvalidError("invalid_todos", "每条 todo 必须是对象")
            tid = str(item.get("id") or "").strip()
            content = str(item.get("content") or "").strip()
            status = str(item.get("status") or "").strip()
            if not tid or not content or status not in _TODO_STATUSES:
                raise InvalidError("invalid_todos", f"无效的 todo 条目：{item}")
            if tid in seen_ids:
                raise InvalidError("duplicate_todo_id", f"重复的 todo id：{tid}")
            seen_ids.add(tid)
            normalized.append({"id": tid, "content": content, "status": status})

        key = (invocation.tenant_id, invocation.workspace_id, invocation.task_id)
        with self._lock:
            if replace:
                stored = normalized
            else:
                existing = {row["id"]: row for row in self._items.get(key, [])}
                order = [row["id"] for row in self._items.get(key, [])]
                for row in normalized:
                    if row["id"] not in existing:
                        order.append(row["id"])
                    existing[row["id"]] = row
                stored = [existing[i] for i in order]
            self._items[key] = stored

        counts = {s: sum(1 for row in stored if row["status"] == s) for s in _TODO_STATUSES}
        return {"items": list(stored), "total": len(stored), "counts": counts}


class FileInspector:
    """Inspect file metadata, line count, language, header preview, and top-level symbols without blind full reads."""

    def __init__(self, workspaces: Any) -> None:
        self.w = workspaces

    def tool(self) -> tuple[ToolSpec, Any]:
        return (
            ToolSpec(
                name="file_inspect",
                version="v1",
                effect=Effect.READ,
                description="探测工作区文件元信息（字节数、行数、语言、前几行预览与顶层符号清单），减少整文件盲读",
                input_schema={
                    "type": "object",
                    "required": ["path"],
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                        "preview_lines": {"type": "integer", "minimum": 1, "maximum": 80},
                    },
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                permissions=frozenset(),
                timeout=timedelta(seconds=30),
            ),
            self.inspect,
        )

    async def inspect(self, invocation: ToolInvocation) -> dict[str, Any]:
        rel = str(invocation.arguments["path"])
        preview_lines = max(1, min(int(invocation.arguments.get("preview_lines", 25)), 80))

        if await self.w._archived(invocation):
            env_rel = self.w._env_path(rel)

            async def _fetch(box: Any, env_id: str) -> bytes:
                try:
                    return await box.read_file(env_id, env_rel)
                except NotFoundError as err:
                    if err.code == "unknown_env":
                        raise
                    raise NotFoundError("file_not_found", f"没有 {rel}") from None

            raw = await self.w._in_env(invocation, writable=False, run=_fetch)
        else:
            root = await self.w._mounted(invocation)
            target = self.w._resolve(root, rel)
            if target == root or not target.is_file():
                raise NotFoundError("file_not_found", f"没有 {rel}")
            raw = target.read_bytes()

        size_bytes = len(raw)
        is_binary = b"\x00" in raw[:8192]
        language = _LANG_BY_EXT.get(Path(rel).suffix.lower(), "text")
        if is_binary:
            return {
                "path": rel,
                "size_bytes": size_bytes,
                "language": "binary",
                "is_binary": True,
                "line_count": 0,
                "preview": [],
                "symbols": [],
            }

        lines = raw.decode("utf-8", errors="replace").splitlines()
        symbols: list[dict[str, Any]] = []
        for idx, line in enumerate(lines, start=1):
            match = _SYMBOL_RE.match(line)
            if match:
                symbols.append({"line": idx, "name": match.group(1)})
                if len(symbols) >= 40:
                    break

        preview = [f"{idx:4d}: {line}" for idx, line in enumerate(lines[:preview_lines], start=1)]
        return {
            "path": rel,
            "size_bytes": size_bytes,
            "language": language,
            "is_binary": False,
            "line_count": len(lines),
            "preview": preview,
            "symbols": symbols,
        }


def register_memory_search(provider: Any, config: Any) -> bool:
    """Register memory_search tool when Worker has both memory_address and dev_signing_key configured."""
    from google.protobuf import json_format

    from agentruntime.memory.v1 import memory_pb2
    from agentruntime_worker.memory import WorkerMemoryClient, tenant_context

    address = getattr(config, "memory_address", "")
    signing_key = getattr(config, "dev_signing_key", "")
    if not address or not signing_key:
        return False

    client = WorkerMemoryClient(address, signing_key)
    spec = ToolSpec(
        name="memory_search",
        version="v1",
        effect=Effect.READ,
        description="按语义检索当前租户沉淀的历史经验与已验证结论（只读，跨任务复用）",
        input_schema={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
                "token_budget": {"type": "integer", "minimum": 100, "maximum": 4000},
            },
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        permissions=frozenset(),
        timeout=timedelta(seconds=15),
    )

    async def _search(invocation: ToolInvocation) -> dict[str, Any]:
        query = str(invocation.arguments["query"]).strip()
        if not query:
            raise InvalidError("invalid_query", "query 不能为空")
        top_k = max(1, min(int(invocation.arguments.get("top_k", 5)), 10))
        token_budget = max(100, min(int(invocation.arguments.get("token_budget", 1200)), 4000))
        tenant = TenantContext(
            tenant_id=invocation.tenant_id,
            workspace_id=invocation.workspace_id,
            session_id=invocation.session_id,
            user_id=invocation.user_id,
        )
        req = memory_pb2.SearchRequest(
            tenant=tenant_context(tenant),
            trigger=memory_pb2.SEARCH_TRIGGER_NODE_START,
            query=query,
            token_budget=token_budget,
            top_k=top_k,
        )
        resp = await client.search(req)
        hits: list[dict[str, Any]] = []
        for item in resp.items:
            raw = json_format.MessageToDict(item.memory_item.value) if item.memory_item.HasField("value") else {}
            content = raw.get("content") or {}
            hits.append({
                "id": str(raw.get("id") or ""),
                "kind": str(raw.get("kind") or ""),
                "scope": str(raw.get("scope") or ""),
                "title": str(content.get("title") or ""),
                "guidance": str(content.get("guidance") or ""),
                "trust": float(raw.get("trust") or 0.0),
                "score": float(item.score),
            })
        return {"query": query, "hits": hits, "total": len(hits)}

    provider.register(spec, _search)
    return True
