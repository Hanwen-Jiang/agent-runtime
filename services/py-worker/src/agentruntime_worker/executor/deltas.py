"""逐字增量的推送（DELTA，docs/plan/all-plans-dag-2026-11-03.md 7.1）。

deliver 节点流式生成（StreamGenerate）的片段经 task-service 的内部端口（AR_TASK_SERVICE_INTERNAL_URL，路径 PATH）推成
token_delta、thinking_delta，task-service 放进 Valkey 里这个 Task 的增量流（保留 10 分钟），SSE 推给在线的连接。

增量是可丢的：不进 Temporal 历史与 trace，也不经工作流；推送在 activity 里尽力而为，入口不在、报错、超时都只记一条日志，
节点照常完成，结果以最后一个分片为准。片段按时间（interval_s）合并，同一时刻只有一个请求在路上，后来的片段并进下一批。
offset 是这一种增量在这次生成里已经推过的字符数（Python 的 str 长度）；activity 重试时从 0 重新开始，客户端见到更小的 offset 就从那里重写。

令牌取 SecretStore 平台作用域的 delta/<调用方>（task-service 启动时补齐）；被拒（401）时重读一次再推。令牌不进日志与错误。
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

PATH = "/internal/v1/deltas"
SECRET_PURPOSE = "delta"
SECRET_PREFIX = "delta/"
# 与 task-service 的 internal/deltas 一致：一批最多 200 条，每条的文字最多 8 KiB。
MAX_BATCH = 200
MAX_TEXT_BYTES = 8 << 10
KINDS = {"text": "token_delta", "thinking": "thinking_delta"}
# 一个节点连续推不出去这么多次就不再推了（入口多半不在），省得每批都等超时。
MAX_FAILURES = 3

TokenSource = Callable[[], Awaitable[str | None]]


class NodeSink(Protocol):
    def __call__(self, kind: str, text: str) -> None: ...

    async def aclose(self) -> None: ...


class DeltaSink(Protocol):
    """Node Executor 用的接口：DeltaPusher 是生产实现。"""

    def node(self, tenant_id: str, task_id: str, node_id: str) -> NodeSink: ...


def secret_token(secrets_file: Path | None, caller: str) -> TokenSource:
    """从开发 SecretStore（AR_SECRETS_FILE）的平台作用域读 delta/<调用方>；没有时为 None。每次调用都重读文件。"""

    async def read() -> str | None:
        if secrets_file is None:
            return None
        from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
        from agentruntime_core.errors import NotFoundError

        try:
            value = (await LocalFileSecretStoreProvider(secrets_file).bind_platform(SECRET_PURPOSE).read(SECRET_PREFIX + caller)).reveal()
        except NotFoundError:
            return None
        return value.decode() if isinstance(value, bytes) else str(value)

    return read


class _Rejected(Exception):
    pass


class DeltaPusher:
    """Worker 进程里一个：按节点给出 NodeDeltas（Node Executor 交给 generate_streaming 的 on_delta）。"""

    def __init__(self, base_url: str, token: TokenSource, *, interval_s: float = 0.1, timeout_s: float = 2.0) -> None:
        self._url = base_url.rstrip("/") + PATH
        self._read_token = token
        self._token: str | None = None
        self.interval_s = interval_s
        self.timeout_s = timeout_s

    def node(self, tenant_id: str, task_id: str, node_id: str) -> NodeDeltas:
        return NodeDeltas(self, {"tenant_id": tenant_id, "task_id": task_id, "node_id": node_id})

    async def _token_value(self, fresh: bool) -> str | None:
        if fresh or self._token is None:
            self._token = await self._read_token()
        return self._token

    async def post(self, deltas: list[dict[str, object]]) -> None:
        """推一批；被拒时重读令牌再推一次。失败抛错，由调用方记日志。"""
        body = json.dumps({"deltas": deltas}, ensure_ascii=False).encode()
        for fresh in (False, True):
            token = await self._token_value(fresh)
            if not token:
                raise _Rejected("没有令牌")
            try:
                await asyncio.to_thread(self._send, body, token)
                return
            except _Rejected:
                continue
        raise _Rejected("令牌被拒")

    def _send(self, body: bytes, token: str) -> None:
        req = urllib.request.Request(self._url, data=body, method="POST", headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise _Rejected("令牌被拒") from None
            raise RuntimeError(f"增量入口返回 {exc.code}") from None


class NodeDeltas:
    """一个节点一次生成的增量：调用它交片段（不阻塞），aclose 推完剩下的。"""

    def __init__(self, pusher: DeltaPusher, ids: dict[str, str]) -> None:
        self._pusher = pusher
        self._ids = ids
        self._pending: list[tuple[str, str]] = []
        self._offsets = dict.fromkeys(KINDS, 0)
        self._wake = asyncio.Event()
        self._closed = False
        self._failures = 0
        self._task: asyncio.Task[None] | None = None

    def __call__(self, kind: str, text: str) -> None:
        if kind not in KINDS or not text or self._closed or self._failures >= MAX_FAILURES:
            return
        self._pending.append((kind, text))
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def aclose(self) -> None:
        self._closed = True
        self._wake.set()
        if self._task is None:
            return
        try:
            await self._task
        except asyncio.CancelledError:
            self._task.cancel()
            raise

    async def _run(self) -> None:
        while True:
            if not self._closed:
                try:
                    await asyncio.wait_for(self._wake.wait(), self._pusher.interval_s)
                except TimeoutError:
                    pass
            batch = self._take()
            for start in range(0, len(batch), MAX_BATCH):
                if self._failures >= MAX_FAILURES:
                    break
                try:
                    await self._pusher.post(batch[start : start + MAX_BATCH])
                    self._failures = 0
                except Exception as exc:  # noqa: BLE001 — 尽力而为：任何失败都不传给节点
                    self._failures += 1
                    logger.warning("逐字增量推不出去（%s，节点 %s）：%s", type(exc).__name__, self._ids["node_id"], exc)
            if (self._closed and not self._pending) or self._failures >= MAX_FAILURES:
                return

    def _take(self) -> list[dict[str, object]]:
        """取出攒下的片段：相邻同种的并成一条，按 MAX_TEXT_BYTES 切开，记上 offset。"""
        merged: list[tuple[str, str]] = []
        for kind, text in self._pending:
            if merged and merged[-1][0] == kind:
                merged[-1] = (kind, merged[-1][1] + text)
            else:
                merged.append((kind, text))
        self._pending = []
        out: list[dict[str, object]] = []
        for kind, text in merged:
            for piece in _split(text):
                out.append({**self._ids, "type": KINDS[kind], "offset": self._offsets[kind], "text": piece})
                self._offsets[kind] += len(piece)
        return out


def _split(text: str) -> list[str]:
    """按 UTF-8 字节切成不超过 MAX_TEXT_BYTES 的段，不切开字符。"""
    if len(text.encode()) <= MAX_TEXT_BYTES:
        return [text]
    out: list[str] = []
    start = size = 0
    for i, ch in enumerate(text):
        n = len(ch.encode())
        if size + n > MAX_TEXT_BYTES:
            out.append(text[start:i])
            start, size = i, 0
        size += n
    out.append(text[start:])
    return out
