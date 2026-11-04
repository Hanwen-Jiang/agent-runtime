"""DELTA（docs/plan/all-plans-dag-2026-11-03.md 7.1）：deliver 节点流式生成的片段，经 task-service 的内部端口推成 token_delta、thinking_delta。

1. 片段按时间合并成少数几次推送；同一种的 offset 首尾相接，拼起来就是完整的文字；每条不超过入口的上限（8 KiB、一批 200 条）。
2. 令牌取 SecretStore 平台作用域的 delta/<调用方>；被拒（401）时重读一次再推。
3. 推送是尽力而为：入口不在、报错，节点照常完成，不抛错；令牌不进日志。
4. 没有输出 schema 的 deliver 节点默认流式（AR_STREAM_DELIVER=0 关掉）；有 schema 的、别的角色的不流式、不推。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.executor.deltas import MAX_BATCH, MAX_TEXT_BYTES, PATH, DeltaPusher, secret_token

from .executor_harness import ExecutorFixture, make_fixture, model
from .test_node_executor import run_model

pytestmark = pytest.mark.asyncio(loop_scope="session")

TASK = "tsk_01J" + "0" * 22 + "T"


class _Inlet:
    """假的 task-service 内部端口：记下每次推送；accept 里的令牌才收。"""

    def __init__(self, accept: set[str]) -> None:
        self.accept = accept
        self.posts: list[dict[str, Any]] = []
        self.auth: list[str] = []
        inlet = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — http.server 的方法名
                body = self.rfile.read(int(self.headers["Content-Length"]))
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                inlet.auth.append(token)
                if self.path != PATH or token not in inlet.accept:
                    self.send_response(401)
                    self.end_headers()
                    return
                batch = json.loads(body)
                inlet.posts.append(batch)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"accepted": len(batch["deltas"]), "dropped": 0}).encode())

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def deltas(self, kind: str | None = None) -> list[dict[str, Any]]:
        out = [d for p in self.posts for d in p["deltas"]]
        return [d for d in out if kind is None or d["type"] == kind]


@pytest.fixture
def inlet() -> Iterator[_Inlet]:
    i = _Inlet({"tok-1"})
    yield i
    i.server.shutdown()


def _tokens(*values: str | None) -> Any:
    seq = list(values)

    async def read() -> str | None:
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return read


def _joined(deltas: list[dict[str, Any]]) -> str:
    text = ""
    for d in sorted(deltas, key=lambda d: d["offset"]):
        assert d["offset"] == len(text), f"offset 首尾相接：{d}"
        text += d["text"]
    return text


async def test_pieces_are_merged_into_few_posts_with_contiguous_offsets(inlet: _Inlet) -> None:
    sink = DeltaPusher(inlet.url, _tokens("tok-1"), interval_s=0.05).node("t1", TASK, "answer")
    words = [f"字{i}" for i in range(200)]
    for i, w in enumerate(words):
        sink("text", w)
        if i % 50 == 0:
            sink("thinking", "想")
            await asyncio.sleep(0.02)
    await sink.aclose()
    assert 1 <= len(inlet.posts) <= 12, f"合并成少数几次推送：{len(inlet.posts)}"
    assert _joined(inlet.deltas("token_delta")) == "".join(words)
    assert _joined(inlet.deltas("thinking_delta")) == "想" * 4
    assert all(d["tenant_id"] == "t1" and d["task_id"] == TASK and d["node_id"] == "answer" for d in inlet.deltas())
    assert set(inlet.auth) == {"tok-1"}


async def test_long_text_is_split_to_the_endpoint_limits(inlet: _Inlet) -> None:
    sink = DeltaPusher(inlet.url, _tokens("tok-1"), interval_s=0.05).node("t1", TASK, "answer")
    text = "é" * 30_000 + "好" * 300_000
    sink("text", text)
    await sink.aclose()
    assert _joined(inlet.deltas("token_delta")) == text
    assert all(len(d["text"].encode()) <= MAX_TEXT_BYTES for d in inlet.deltas())
    assert all(len(p["deltas"]) <= MAX_BATCH for p in inlet.posts)


async def test_a_rejected_token_is_read_again_once(inlet: _Inlet) -> None:
    sink = DeltaPusher(inlet.url, _tokens("stale", "tok-1"), interval_s=0.05).node("t1", TASK, "answer")
    sink("text", "你好")
    await sink.aclose()
    assert inlet.auth[:2] == ["stale", "tok-1"] and _joined(inlet.deltas()) == "你好"


async def test_failures_never_reach_the_node(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    dead = _Inlet(set())
    dead.server.shutdown()
    dead.server.server_close()
    for url, token in ((dead.url, "secret-delta-token"), ("http://127.0.0.1:9", "secret-delta-token")):
        sink = DeltaPusher(url, _tokens(token), interval_s=0.01, timeout_s=0.5).node("t1", TASK, "answer")
        started = time.monotonic()
        for _ in range(50):
            sink("text", "x")
            await asyncio.sleep(0.002)
        await sink.aclose()
        assert time.monotonic() - started < 5, "推不出去也不拖住节点"
    no_token = DeltaPusher("http://127.0.0.1:9", _tokens(None), interval_s=0.01).node("t1", TASK, "answer")
    no_token("text", "x")
    await no_token.aclose()
    assert "secret-delta-token" not in caplog.text


async def test_the_token_comes_from_the_platform_scope(tmp_path: Path) -> None:
    file = tmp_path / "secrets.json"
    read = secret_token(file, "worker")
    assert await read() is None, "没有时为 None"
    store = LocalFileSecretStoreProvider(file)
    store.seed_secret("", "delta/worker", "tok-from-file")
    assert await secret_token(file, "worker")() == "tok-from-file"
    assert await secret_token(None, "worker")() is None


def test_the_internal_url_and_the_default_come_from_the_environment() -> None:
    cfg = WorkerConfig.from_env({})
    assert cfg.stream_deliver and cfg.task_service_internal_url == "" and cfg.delta_caller == "worker", "默认流式；没给地址时不推"
    cfg = WorkerConfig.from_env({"AR_TASK_SERVICE_INTERNAL_URL": "http://ts-internal:8093", "AR_DELTA_CALLER": "worker-gpu", "AR_STREAM_DELIVER": "0"})
    assert cfg.task_service_internal_url == "http://ts-internal:8093" and cfg.delta_caller == "worker-gpu" and not cfg.stream_deliver
    for bad in ({"AR_TASK_SERVICE_INTERNAL_URL": "ts-internal:8093"}, {"AR_DELTA_CALLER": "Worker/1"}, {"AR_STREAM_DELIVER": "yes"}):
        with pytest.raises(ValueError):
            WorkerConfig.from_env(bad)


# --- Node Executor ---


class _Recorder:
    def __init__(self) -> None:
        self.nodes: list[tuple[str, str, str]] = []
        self.pieces: list[tuple[str, str]] = []
        self.closed = 0

    def node(self, tenant_id: str, task_id: str, node_id: str) -> "_Recorder":
        self.nodes.append((tenant_id, task_id, node_id))
        return self

    def __call__(self, kind: str, text: str) -> None:
        self.pieces.append((kind, text))

    async def aclose(self) -> None:
        self.closed += 1


@pytest.fixture
def fx(tmp_path: Path) -> ExecutorFixture:
    return make_fixture(tmp_path)


def _streaming(fx: ExecutorFixture) -> tuple[list[str], _Recorder]:
    calls: list[str] = []

    async def stream_generate(request: mg.GenerateRequest, on_delta: Any = None) -> mg.GenerateResponse:
        calls.append(request.call.node_id)
        if on_delta is not None:
            on_delta("thinking", "想")
            on_delta("text", "部分")
        return await fx.models.generate(request)

    fx.models.stream_generate = stream_generate  # type: ignore[attr-defined]
    recorder = _Recorder()
    fx.executor.deps = dataclasses.replace(fx.executor.deps, deltas=recorder)
    return calls, recorder


async def test_a_deliver_node_without_a_schema_streams_and_pushes_by_default(fx: ExecutorFixture) -> None:
    calls, recorder = _streaming(fx)
    assert fx.executor.deps.stream_deliver, "默认流式"
    fx.models.reply({"summary": "ok"})
    out = await run_model(fx, model("answer", role="deliver"))
    assert calls == ["answer"] and out["result"] == {"summary": "ok"}
    assert recorder.nodes and recorder.nodes[0][2] == "answer" and recorder.pieces == [("thinking", "想"), ("text", "部分")]
    assert recorder.closed == 1, "生成完就把剩下的推完"


async def test_nodes_with_a_schema_or_another_role_or_the_flag_off_do_not_push(fx: ExecutorFixture) -> None:
    calls, recorder = _streaming(fx)
    schema = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}
    for node in (model("report", role="deliver", output_schema=schema), model("summarize", role="transform")):
        fx.models.reply({"summary": "ok"})
        await run_model(fx, node)
    fx.executor.deps = dataclasses.replace(fx.executor.deps, stream_deliver=False)
    fx.models.reply({"summary": "ok"})
    await run_model(fx, model("answer", role="deliver"))
    assert calls == [] and recorder.nodes == [] and recorder.pieces == []
