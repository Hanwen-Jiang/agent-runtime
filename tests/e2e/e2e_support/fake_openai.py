"""模拟的 OpenAI 兼容服务（http.server，只用标准库）：Model Gateway 端到端测试的上游供应商。

- POST /v1/chat/completions：返回固定回答、思考内容（reasoning_content）与 usage；请求带 response_format 时，
  回答是 structured 的 JSON 文本；stream=true 时按 SSE 分片返回，最后一个分片带 usage（stream_options.include_usage）。
- POST /v1/embeddings：每段文本返回固定向量与 usage。
- 记录每个请求的路径、Authorization 里的密钥与 JSON 正文，测试据此断言模型、消息与密钥。
- fail 不为 None 时所有请求都按它失败；错误正文故意回显请求里的密钥，检验网关不把它带进错误、日志与计量
  （docs/plan/model-gateway-direct.md 难点“密钥不外泄”：即使上游把它回显在错误正文里）。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# 固定回答与思考内容：测试断言交付文本来自这里，思考内容只进 rationale。
ANSWER = "模拟上游的回答：退款率上升主要因为物流延误。"
REASONING = "模拟上游的思考：先看退款原因分布。"
STRUCTURED = {"answer": "物流延误", "confidence": 0.75}
# 聊天的 usage：prompt 1200（其中缓存命中 200），completion 300。
CHAT_USAGE = {
    "prompt_tokens": 1200,
    "completion_tokens": 300,
    "total_tokens": 1500,
    "prompt_tokens_details": {"cached_tokens": 200},
    "completion_tokens_details": {"reasoning_tokens": 0},
}
EMBED_USAGE = {"prompt_tokens": 5000, "total_tokens": 5000}
EMBED_VECTOR = [0.5, -0.25, 0.125, 1.0]


@dataclass(frozen=True)
class UpstreamRequest:
    path: str
    # Authorization: Bearer <key> 里的 key；没有时为空。
    api_key: str
    body: dict[str, Any]

    @property
    def model(self) -> str:
        return str(self.body.get("model", ""))

    def message_texts(self) -> list[str]:
        """messages 里各条消息的文本（content 是字符串或分段列表）。"""
        out: list[str] = []
        for m in self.body.get("messages", []):
            content = m.get("content")
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                out.extend(str(part.get("text", "")) for part in content if isinstance(part, dict))
        return out


@dataclass(frozen=True)
class Failure:
    """让上游按这个状态码失败；headers 例如 Retry-After；code 是 OpenAI 错误正文里的 error.code。"""

    status: int
    code: str
    headers: dict[str, str] = field(default_factory=dict)


# 常见的上游失败（难点“错误分类一致”）。
RATE_LIMITED = Failure(429, "rate_limit_exceeded", {"Retry-After": "1"})
SERVER_ERROR = Failure(500, "server_error")
BAD_REQUEST = Failure(400, "invalid_request_error")
UNAUTHORIZED = Failure(401, "invalid_api_key")
FORBIDDEN = Failure(403, "permission_denied")
MODEL_NOT_FOUND = Failure(404, "model_not_found")


class FakeOpenAI:
    def __init__(self) -> None:
        self.fail: Failure | None = None
        self._requests: list[UpstreamRequest] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # --- 生命周期 ---

    def start(self) -> None:
        owner = self

        class Handler(_Handler):
            fake = owner

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-openai", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def base_url(self) -> str:
        """形如 http://127.0.0.1:<port>/v1，即网关的 AR_MODEL_PROVIDER_BASE_URL。"""
        assert self._server is not None, "先 start()"
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    # --- 记录 ---

    def record(self, req: UpstreamRequest) -> None:
        with self._lock:
            self._requests.append(req)

    @property
    def requests(self) -> list[UpstreamRequest]:
        with self._lock:
            return list(self._requests)

    def mark(self) -> int:
        """当前已收到的请求数；配合 since() 只看某个用例里的请求。"""
        with self._lock:
            return len(self._requests)

    def since(self, mark: int, path_suffix: str = "") -> list[UpstreamRequest]:
        return [r for r in self.requests[mark:] if r.path.endswith(path_suffix)]


class _Handler(BaseHTTPRequestHandler):
    fake: FakeOpenAI
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — 覆盖基类签名
        pass

    def do_GET(self) -> None:  # noqa: N802 — http.server 的约定
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": []})
            return
        self._json(404, {"error": {"message": "not found", "type": "invalid_request_error", "code": "not_found"}})

    def do_POST(self) -> None:  # noqa: N802 — http.server 的约定
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {"_raw": raw.decode("utf-8", errors="replace")}
        auth = self.headers.get("Authorization", "")
        key = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        path = self.path.split("?", 1)[0]
        self.fake.record(UpstreamRequest(path=path, api_key=key, body=body))

        failure = self.fake.fail
        if failure is not None:
            # 故意把密钥回显在错误正文里（真实供应商在 401 时会这样做）。
            payload = {"error": {"message": f"模拟上游失败，收到的密钥是 {key}", "type": failure.code, "code": failure.code, "param": None}}
            self._json(failure.status, payload, failure.headers)
            return
        if path.endswith("/chat/completions"):
            if body.get("stream"):
                self._stream_chat(body)
            else:
                self._json(200, _chat_completion(body))
            return
        if path.endswith("/embeddings"):
            inputs = body.get("input")
            texts = inputs if isinstance(inputs, list) else [inputs]
            self._json(
                200,
                {
                    "object": "list",
                    "data": [{"object": "embedding", "index": i, "embedding": list(EMBED_VECTOR)} for i in range(len(texts))],
                    "model": body.get("model", ""),
                    "usage": dict(EMBED_USAGE),
                },
            )
            return
        self._json(404, {"error": {"message": f"未知路径 {path}", "type": "invalid_request_error", "code": "not_found"}})

    # --- 应答 ---

    def _json(self, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, val in (headers or {}).items():
            self.send_header(name, val)
        self.end_headers()
        self.wfile.write(data)

    def _stream_chat(self, body: dict[str, Any]) -> None:
        content = _content(body)
        half = len(content) // 2
        base = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": int(time.time()), "model": body.get("model", "")}
        chunks: list[dict[str, Any]] = [
            {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": REASONING}, "finish_reason": None}]},
            {**base, "choices": [{"index": 0, "delta": {"content": content[:half]}, "finish_reason": None}]},
            {**base, "choices": [{"index": 0, "delta": {"content": content[half:]}, "finish_reason": None}]},
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        if (body.get("stream_options") or {}).get("include_usage"):
            chunks.append({**base, "choices": [], "usage": dict(CHAT_USAGE)})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


def _content(body: dict[str, Any]) -> str:
    """有 response_format（json_schema 或 json_object）时回 STRUCTURED 的 JSON 文本，否则回 ANSWER。"""
    if body.get("response_format"):
        return json.dumps(STRUCTURED, ensure_ascii=False)
    return ANSWER


def _chat_completion(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", ""),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": _content(body), "reasoning_content": REASONING},
                "finish_reason": "stop",
            }
        ],
        "usage": dict(CHAT_USAGE),
    }
