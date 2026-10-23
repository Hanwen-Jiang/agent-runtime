"""测试用的假 MCP Server：stdio 上逐行收发 JSON-RPC 2.0（initialize、tools/list、tools/call）。

环境变量 FAKE_MCP_LOG 给出时，每次 tools/call 往这个文件追加一行工具名，测试据此数调用次数（进程重启也不丢）。
工具：echo（结构化输出）、shout（只有文本）、fail（isError）、slow（睡 seconds 秒）、crash（进程直接退出）、
write_note（写操作）、secret_tool（Server 提供、但登记文件里不登记）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

TOOLS: list[dict[str, Any]] = [
    {
        "name": "echo",
        "description": "回显 text",
        "inputSchema": {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}},
        "outputSchema": {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}},
    },
    {"name": "shout", "description": "把 text 变成大写", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
    {"name": "fail", "description": "总是失败", "inputSchema": {"type": "object"}},
    {"name": "slow", "description": "睡一会儿", "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}}},
    {"name": "crash", "description": "让 Server 崩溃", "inputSchema": {"type": "object"}},
    {"name": "write_note", "description": "记一条笔记", "inputSchema": {"type": "object", "required": ["note"], "properties": {"note": {"type": "string"}}}},
    {"name": "secret_tool", "description": "没登记的工具", "inputSchema": {"type": "object"}},
]


def log_call(name: str) -> None:
    path = os.environ.get("FAKE_MCP_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(name + "\n")


def text(value: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": value}]


def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    log_call(name)
    if name == "echo":
        return {"content": text(args["text"]), "structuredContent": {"text": args["text"]}}
    if name == "shout":
        return {"content": text(str(args.get("text", "")).upper())}
    if name == "fail":
        return {"content": text("出错了"), "isError": True}
    if name == "slow":
        time.sleep(float(args.get("seconds", 1)))
        return {"content": text("睡醒了")}
    if name == "crash":
        sys.stdout.flush()
        os._exit(1)
    if name == "write_note":
        return {"content": text("记下了"), "structuredContent": {"saved": True}}
    return {"content": text("不认识的工具"), "isError": True}


def respond(message_id: Any, result: dict[str, Any] | None = None, error: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        method = message.get("method")
        if "id" not in message:  # 通知，例如 notifications/initialized
            continue
        if method == "initialize":
            respond(message["id"], {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "0.1"}})
        elif method == "tools/list":
            respond(message["id"], {"tools": TOOLS})
        elif method == "tools/call":
            params = message.get("params") or {}
            respond(message["id"], call(str(params.get("name")), dict(params.get("arguments") or {})))
        else:
            respond(message["id"], error={"code": -32601, "message": f"不认识的方法 {method}"})


if __name__ == "__main__":
    main()
