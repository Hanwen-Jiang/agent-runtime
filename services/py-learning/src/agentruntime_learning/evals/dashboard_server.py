"""评测看板（Eval Dashboard）的静态服务（docs/plan/eval-observability-and-runtime-fixes.md 第 11.12 节）。只用标准库。

跑在已有的 learning 镜像里（不为一个静态页面加新依赖）：页面是 --dir 下的 index.html（ConfigMap 挂进来，由发布步骤写），
scripts/eval_dashboard.py --public 生成。

- 只答 /、/index.html 与 /healthz，只收 GET、HEAD；目录里的别的文件一概不给，请求的路径不拼进文件名。
- 每次请求现读文件：ConfigMap 更新后不用重启。还没发布时 / 回 404，/healthz 仍是 200。
- 响应带安全头（页面只有内联的脚本与样式，别的一概不许），错误信息不回显请求内容，不露 Python 版本，不记访问日志（入口有）。
"""

from __future__ import annotations

import argparse
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

PAGE_NAME = "index.html"
PAGE_PATHS = frozenset({"/", "/index.html"})
HEALTH_PATH = "/healthz"
CONTENT_SECURITY_POLICY = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
# Cloudflare 可以短暂缓存；发布后几分钟内更新。
PAGE_CACHE_CONTROL = "public, max-age=300"
REQUEST_TIMEOUT_S = 30


def make_server(root: Path, host: str, port: int) -> ThreadingHTTPServer:
    """绑定地址、还没开始服务的 HTTP 服务；port 为 0 时随机分配。"""
    page = Path(root) / PAGE_NAME

    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT_S

        def version_string(self) -> str:
            return "eval-dashboard"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — 基类的参数名
            return

        def _reply(self, status: int, body: bytes, content_type: str, **headers: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            for name, value in headers.items():
                self.send_header(name.replace("_", "-"), value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _text(self, status: int, **headers: str) -> None:
            self._reply(status, (HTTPStatus(status).phrase.lower() + "\n").encode(), "text/plain; charset=utf-8", **headers)

        def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
            # 基类的错误页会回显请求里的内容（方法、版本），这里只说状态。
            self._text(code)

        def _get(self) -> None:
            path = urlsplit(self.path).path
            if path == HEALTH_PATH:
                self._reply(HTTPStatus.OK, b"ok", "text/plain; charset=utf-8", Cache_Control="no-store")
                return
            if path not in PAGE_PATHS:
                self._text(HTTPStatus.NOT_FOUND)
                return
            try:
                body = page.read_bytes()
            except OSError:
                self._text(HTTPStatus.NOT_FOUND)
                return
            self._reply(HTTPStatus.OK, body, "text/html; charset=utf-8", Cache_Control=PAGE_CACHE_CONTROL)

        def _not_allowed(self) -> None:
            self._text(HTTPStatus.METHOD_NOT_ALLOWED, Allow="GET, HEAD")

        do_GET = do_HEAD = _get  # noqa: N815 — 基类按方法名分派
        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed  # noqa: N815

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agentruntime_learning.evals.dashboard_server", description="评测看板的静态服务")
    parser.add_argument("--dir", type=Path, required=True, help="放 index.html 的目录")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 — 在 Pod 里监听，入站由 NetworkPolicy 限定
    args = parser.parse_args(argv)
    with make_server(args.dir, args.host, args.port) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
