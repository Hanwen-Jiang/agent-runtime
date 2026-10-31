"""docs/plan/running-task-interaction.md 第 2 节验收：取消一次 exec 时 AgentClient 断开连接。

K8s 后端与 Cloudflare 后端都经 AgentClient。agent 看到连接断开才杀进程组；现在是 asyncio.to_thread 里的阻塞请求，协程取消了线程
还在等，连接不断，命令一直跑。这里不用真的 agent，直接看连接；每个后端的命令真的停了在一致性测试里
（conformance/sandbox.py 的 test_cancelling_exec_stops_the_command_and_its_children：本地、假桥上真的 agent，kind 要 AR_TEST_KIND，
真桥要 AR_LIVE_CLOUDFLARE=1）。agent 一侧的测试在 services/go-sandbox-agent。
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentruntime_core.adapters.sandbox_agent import AgentClient, AgentEndpoint


class _FakeAgent:
    """收到 /v1/exec 后不回答，一直看连接有没有断。"""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.disconnected = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.started.set()
                self.connection.settimeout(0.1)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    try:
                        if self.connection.recv(1) == b"":
                            outer.disconnected.set()
                            return
                    except TimeoutError:
                        continue
                    except OSError:
                        outer.disconnected.set()
                        return

            def log_message(self, *_: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


async def test_cancelling_exec_closes_the_connection_to_the_agent() -> None:
    agent = _FakeAgent()
    try:
        client = AgentClient()
        endpoint = AgentEndpoint(base_url=agent.url, token="t" * 32)
        call = asyncio.create_task(client.exec(endpoint, ["sleep", "600"], timeout=timedelta(seconds=600)))
        assert await asyncio.to_thread(agent.started.wait, 10)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(call, 5)
        assert await asyncio.to_thread(agent.disconnected.wait, 5), "连接没断：agent 不知道调用方走了，命令会一直跑"
    finally:
        agent.close()
