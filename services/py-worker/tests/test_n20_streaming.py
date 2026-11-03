"""N-19/N-20：deliver 节点经 Model Gateway 的 StreamGenerate 生成，记首个 token 的用时（契约清单 streaming_prerequisite、latency_segments）。

片段只用来量首个 token；推送 token_delta、thinking_delta 的通道待定（docs/plan/n19-n20-implementation-2026-11-03.md），这里不推。
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any

import pytest

from agentruntime.common.v1 import metering_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.errors import TransientError
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.executor.grpc_gateway import GrpcModelGateway
from agentruntime_worker.executor.model_gateway import generate_streaming

from .executor_harness import ExecutorFixture, make_fixture, model
from .test_node_executor import run_model

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _Stream:
    def __init__(self, chunks: list[mg.StreamGenerateResponse]) -> None:
        self.chunks = chunks

    def __aiter__(self) -> "_Stream":
        return self

    async def __anext__(self) -> mg.StreamGenerateResponse:
        if not self.chunks:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self.chunks.pop(0)


class _Stub:
    def __init__(self, chunks: list[mg.StreamGenerateResponse]) -> None:
        self.chunks = chunks
        self.requests: list[mg.StreamGenerateRequest] = []

    def StreamGenerate(self, request: mg.StreamGenerateRequest, timeout: float) -> _Stream:  # noqa: N802 — gRPC 的方法名
        self.requests.append(request)
        return _Stream(list(self.chunks))


def _gateway(chunks: list[mg.StreamGenerateResponse]) -> tuple[GrpcModelGateway, _Stub]:
    gw = GrpcModelGateway("127.0.0.1:1")
    stub = _Stub(chunks)
    gw._stub, gw._loop = stub, asyncio.get_running_loop()  # noqa: SLF001 — 不起服务，直接换掉 stub
    return gw, stub


def _final(text: str) -> mg.StreamGenerateResponse:
    usage = metering_pb2.Usage(input_tokens=10, output_tokens=3, cost_usd_micros=42)
    return mg.StreamGenerateResponse(final=mg.GenerateResponse(text=text, finish_reason=mg.FINISH_REASON_STOP), usage=usage)


def _request() -> mg.GenerateRequest:
    return mg.GenerateRequest(call=mg.CallContext(task_id="tsk_x", node_id="answer"), generation=mg.GenerateParams(messages=[mg.ChatMessage(text="hi")]))


async def test_the_grpc_client_streams_deltas_and_returns_the_final_chunk() -> None:
    gw, stub = _gateway([mg.StreamGenerateResponse(thinking_delta="想"), mg.StreamGenerateResponse(text_delta="你"), mg.StreamGenerateResponse(text_delta="好"), _final("你好")])
    seen: list[tuple[str, str]] = []
    response = await gw.stream_generate(_request(), lambda kind, text: seen.append((kind, text)))
    assert seen == [("thinking", "想"), ("text", "你"), ("text", "好")]
    assert response.text == "你好" and response.usage.cost_usd_micros == 42, "usage 取最后一个分片上的"
    assert stub.requests[0].call.node_id == "answer" and stub.requests[0].generation.messages[0].text == "hi"


async def test_a_stream_without_a_final_chunk_is_transient() -> None:
    gw, _ = _gateway([mg.StreamGenerateResponse(text_delta="半")])
    with pytest.raises(TransientError):
        await gw.stream_generate(_request())


async def test_generate_streaming_measures_the_first_token_or_reports_none() -> None:
    gw, _ = _gateway([mg.StreamGenerateResponse(text_delta="a"), _final("a")])
    response, first = await generate_streaming(gw, _request())
    assert response.text == "a" and isinstance(first, int) and first >= 0

    class Plain:
        async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
            return mg.GenerateResponse(text="b")

    response, first = await generate_streaming(Plain(), _request())  # type: ignore[arg-type]
    assert response.text == "b" and first is None, "没有流式通道时不伪造"


@pytest.fixture
def fx(tmp_path: Path) -> ExecutorFixture:
    return make_fixture(tmp_path)


def _streaming(fx: ExecutorFixture) -> list[str]:
    calls: list[str] = []

    async def stream_generate(request: mg.GenerateRequest, on_delta: Any = None) -> mg.GenerateResponse:
        calls.append(request.call.node_id)
        if on_delta is not None:
            on_delta("text", "部分")
        return await fx.models.generate(request)

    fx.models.stream_generate = stream_generate  # type: ignore[attr-defined]
    fx.executor.deps = dataclasses.replace(fx.executor.deps, stream_deliver=True)
    return calls


async def test_deliver_nodes_stream_and_record_the_first_token(fx: ExecutorFixture) -> None:
    calls = _streaming(fx)
    fx.models.reply({"summary": "ok"})
    out = await run_model(fx, model("answer", role="deliver"))
    assert calls == ["answer"] and isinstance(out["meta"].get("first_token_ms"), int)
    assert out["result"] == {"summary": "ok"}, "结果以最后一个分片为准"


async def test_other_nodes_and_the_default_do_not_stream(fx: ExecutorFixture) -> None:
    calls = _streaming(fx)
    fx.models.reply({"summary": "ok"})
    out = await run_model(fx, model("summarize", role="transform"))
    assert calls == [] and "first_token_ms" not in out["meta"]
    fx.executor.deps = dataclasses.replace(fx.executor.deps, stream_deliver=False)
    fx.models.reply({"summary": "ok"})
    out = await run_model(fx, model("answer", role="deliver"))
    assert calls == [] and "first_token_ms" not in out["meta"], "默认关"


def test_the_flag_comes_from_the_environment() -> None:
    assert WorkerConfig.from_env({"AR_STREAM_DELIVER": "1"}).stream_deliver
    assert not WorkerConfig.from_env({}).stream_deliver
