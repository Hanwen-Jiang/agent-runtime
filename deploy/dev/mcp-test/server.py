"""测试用的远程 MCP Server（N-15 的线上验收，docs/plan/cloud-deploy.md）：Streamable HTTP，只用标准库。

- 鉴权：请求头 X-Api-Key 要等于环境变量 MCP_TEST_KEY，否则 401。
- 工具：echo（只读）、list_notes（只读）、add_note（不可逆写：记一条笔记，进程内）。每次 tools/call 打一行日志
  （工具名、幂等键），验收时据此看调用到没到、拒绝后有没有执行。
"""

import json
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = os.environ.get("MCP_TEST_KEY", "")
NOTES: list[dict] = []
SESSIONS: set[str] = set()
TOOLS = [
    {"name": "echo", "description": "Echo the given text back.", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "list_notes", "description": "List the notes recorded so far.", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "add_note", "description": "Record a note (cannot be undone).", "annotations": {"readOnlyHint": False, "destructiveHint": True},
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
]


def log(**kw):
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **kw}, ensure_ascii=False), flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        return KEY and secrets.compare_digest(self.headers.get("X-Api-Key", ""), KEY)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"ok": True})
        self._send(405)

    def do_DELETE(self):
        SESSIONS.discard(self.headers.get("Mcp-Session-Id", ""))
        self._send(200 if self._authorized() else 401)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if not self._authorized():
            log(event="unauthorized")
            return self._send(401, {"error": "unauthorized"})
        try:
            msg = json.loads(raw)
        except ValueError:
            return self._send(400, {"error": "bad json"})
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if mid is None:
            return self._send(202)
        if method == "initialize":
            sid = secrets.token_hex(16)
            SESSIONS.add(sid)
            return self._send(200, {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": params.get("protocolVersion") or "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "agentruntime-mcp-test", "version": "1"}}}, {"Mcp-Session-Id": sid})
        sid = self.headers.get("Mcp-Session-Id", "")
        if sid and sid not in SESSIONS:
            return self._send(404, {"error": "session expired"})
        if method == "tools/list":
            return self._send(200, {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            key = (params.get("_meta") or {}).get("agentruntime/idempotency_key", "")
            log(event="tools/call", tool=name, idempotency_key=key, args=args)
            if name == "echo":
                result = {"content": [{"type": "text", "text": str(args.get("text", ""))}]}
            elif name == "list_notes":
                result = {"content": [{"type": "text", "text": json.dumps(NOTES)}], "structuredContent": {"notes": NOTES}}
            elif name == "add_note":
                if not any(n["key"] == key for n in NOTES if key):
                    NOTES.append({"text": str(args.get("text", "")), "key": key})
                result = {"content": [{"type": "text", "text": f"recorded ({len(NOTES)} notes)"}]}
            else:
                return self._send(200, {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}})
            return self._send(200, {"jsonrpc": "2.0", "id": mid, "result": result})
        self._send(200, {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})


if __name__ == "__main__":
    if not KEY:
        sys.exit("MCP_TEST_KEY is required")
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
