"""Model Gateway 单供应商直连（docs/plan/model-gateway-direct.md）：Python 客户端 GrpcModelGateway。

假的 ModelGatewayService 在进程内（grpc.aio，见 gateway_fakes）。覆盖：
- 请求与响应原样往返（交付 3；验收“跨语言：租户与计量往返一致”的 Python 一侧）；
- 错误还原：trailer ar-error-bin 优先，其次按 gRPC 状态码；trailer 损坏或类别不认识时退回状态码；
  客户端超时为 transient；错误文本不含消息内容（难点“错误分类一致”“密钥不外泄”）；
- from_env 与 AR_EVAL_GATEWAY_FACTORY 的用法（交付 3）。
"""

from __future__ import annotations

import asyncio
import importlib
import time
from datetime import timedelta

import grpc
import pytest

from agentruntime.common.v1 import error_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.errors import (
    ConflictError,
    DeniedError,
    InvalidError,
    NotFoundError,
    ProviderError,
    TransientError,
)

from agentruntime_worker.executor import grpc_gateway
from agentruntime_worker.executor.grpc_gateway import GrpcModelGateway, MissingGatewayAddressError, from_env
from agentruntime_worker.executor.model_gateway import ModelGateway

from .gateway_fakes import (
    RETRY_AFTER_DETAIL,
    SECRET_MESSAGE,
    TENANT,
    Abort,
    FakeModelGateway,
    embed_request,
    embed_response,
    free_port,
    generate_request,
    generate_response,
    open_client,
    provider_error,
    serve,
)

ENV_ADDRESS = "AR_MODEL_GATEWAY_ADDRESS"
FACTORY_SPEC = "agentruntime_worker.executor.grpc_gateway:from_env"


async def _call(gateway: GrpcModelGateway, method: str) -> object:
    if method == "generate":
        return await gateway.generate(generate_request())
    return await gateway.embed(embed_request())


# --- 往返 ---


async def test_generate_round_trips_request_and_response() -> None:
    """交付 3：请求原样到达（tenant、call、generation 逐一比对），响应字段原样返回。"""
    fake = FakeModelGateway()
    request = generate_request()
    async with serve(fake) as address, open_client(address) as gateway:
        response = await gateway.generate(request)

    assert len(fake.requests) == 1
    received = fake.requests[0]
    assert received.tenant == TENANT, "租户上下文原样转发（tenant_id、user_id、workspace_id、session_id、trace）"
    assert received.call == request.call, "选档依据原样转发（task、node、role、tier、reasoning、pinned、avoid_vendor、成本上限）"
    assert received.generation == request.generation, "消息、工具、output_schema、max_output_tokens、思考预算原样转发"
    assert received == request

    expected = generate_response()
    assert response.text == expected.text
    assert response.result == expected.result
    assert list(response.tool_calls) == list(expected.tool_calls)
    assert response.rationale == expected.rationale
    assert response.finish_reason == expected.finish_reason
    assert response.model == expected.model
    assert response.usage == expected.usage
    assert response == expected


async def test_embed_round_trips_request_and_response() -> None:
    fake = FakeModelGateway()
    request = embed_request()
    async with serve(fake) as address, open_client(address) as gateway:
        response = await gateway.embed(request)

    assert fake.requests == [request]
    assert fake.requests[0].tenant == TENANT
    assert fake.requests[0].call == request.call
    assert list(fake.requests[0].texts) == list(request.texts)
    assert fake.requests[0].embedding_version == request.embedding_version

    expected = embed_response()
    assert [list(e.values) for e in response.embeddings] == [list(e.values) for e in expected.embeddings]
    assert response.embedding_version == expected.embedding_version
    assert response.model == expected.model
    assert response.usage == expected.usage


def test_grpc_gateway_satisfies_the_model_gateway_protocol() -> None:
    """交付 3：GrpcModelGateway 实现 ModelGateway Protocol（Node Executor、Planner、Reviewer 只认这个接口）。"""
    gateway: ModelGateway = GrpcModelGateway("127.0.0.1:1")
    assert asyncio.iscoroutinefunction(gateway.generate)
    assert asyncio.iscoroutinefunction(gateway.embed)
    assert asyncio.iscoroutinefunction(GrpcModelGateway.aclose)


async def test_a_client_serves_many_calls_and_closes_idempotently() -> None:
    """同一个客户端连续调用（Worker 里长期复用）；aclose 可以重复调用，没调用过也能关。"""
    fake = FakeModelGateway()
    async with serve(fake) as address:
        gateway = GrpcModelGateway(address)
        for _ in range(3):
            await gateway.generate(generate_request())
        await gateway.embed(embed_request())
        await gateway.aclose()
        await gateway.aclose()
        await GrpcModelGateway(address).aclose()
    assert len(fake.requests) == 4


# --- 错误还原：trailer ar-error-bin ---

# 约定 3：类别 → Connect 错误码（gRPC 状态码）→ Python 的错误类。
TRAILER_CASES = [
    (error_pb2.ERROR_CATEGORY_TRANSIENT, grpc.StatusCode.UNAVAILABLE, TransientError, "upstream_unavailable"),
    (error_pb2.ERROR_CATEGORY_INVALID, grpc.StatusCode.INVALID_ARGUMENT, InvalidError, "missing_tenant"),
    (error_pb2.ERROR_CATEGORY_DENIED, grpc.StatusCode.PERMISSION_DENIED, DeniedError, "model_not_allowed"),
    (error_pb2.ERROR_CATEGORY_NOT_FOUND, grpc.StatusCode.NOT_FOUND, NotFoundError, "model_not_found"),
    (error_pb2.ERROR_CATEGORY_CONFLICT, grpc.StatusCode.ABORTED, ConflictError, "conflict"),
]


@pytest.mark.parametrize("method", ["generate", "embed"])
@pytest.mark.parametrize(("category", "status", "expected", "code"), TRAILER_CASES, ids=[c[3] for c in TRAILER_CASES])
async def test_error_trailer_restores_the_category(method: str, category: int, status: grpc.StatusCode, expected: type[ProviderError], code: str) -> None:
    """难点“错误分类一致”：按 ar-error-bin 还原类别、code 与 message；只有 transient 可重试。"""
    fake = FakeModelGateway(abort=Abort(status, "网关拒绝", provider_error(category, code, "网关给出的说明")))
    async with serve(fake) as address, open_client(address) as gateway:
        with pytest.raises(ProviderError) as info:
            await _call(gateway, method)
    err = info.value
    assert type(err) is expected
    assert err.code == code
    assert err.message == "网关给出的说明"
    assert err.retryable is (expected is TransientError)


async def test_retry_after_is_restored_from_the_error_details() -> None:
    """约定 3：transient 且有 RetryAfter 时 details.retry_after_ms 还原为 retry_after。"""
    error = provider_error(error_pb2.ERROR_CATEGORY_TRANSIENT, "rate_limit", "上游限流", {RETRY_AFTER_DETAIL: 1500})
    fake = FakeModelGateway(abort=Abort(grpc.StatusCode.UNAVAILABLE, "限流", error))
    async with serve(fake) as address, open_client(address) as gateway:
        with pytest.raises(TransientError) as info:
            await gateway.generate(generate_request())
    assert info.value.retry_after == timedelta(milliseconds=1500)
    assert info.value.code == "rate_limit"


async def test_transient_without_retry_after_has_none() -> None:
    error = provider_error(error_pb2.ERROR_CATEGORY_TRANSIENT, "upstream_5xx", "上游 503")
    fake = FakeModelGateway(abort=Abort(grpc.StatusCode.UNAVAILABLE, "", error))
    async with serve(fake) as address, open_client(address) as gateway:
        with pytest.raises(TransientError) as info:
            await gateway.generate(generate_request())
    assert info.value.retry_after is None


@pytest.mark.parametrize(
    ("status", "category", "expected"),
    [
        (grpc.StatusCode.UNKNOWN, error_pb2.ERROR_CATEGORY_NOT_FOUND, NotFoundError),
        (grpc.StatusCode.UNAVAILABLE, error_pb2.ERROR_CATEGORY_DENIED, DeniedError),
        (grpc.StatusCode.INTERNAL, error_pb2.ERROR_CATEGORY_INVALID, InvalidError),
    ],
)
async def test_error_trailer_wins_over_the_status_code(status: grpc.StatusCode, category: int, expected: type[ProviderError]) -> None:
    """有 ar-error-bin 时以它为准，状态码只是没有 trailer 时的退路。"""
    fake = FakeModelGateway(abort=Abort(status, "", provider_error(category, "from_trailer", "以 trailer 为准")))
    async with serve(fake) as address, open_client(address) as gateway:
        with pytest.raises(ProviderError) as info:
            await gateway.generate(generate_request())
    assert type(info.value) is expected
    assert info.value.code == "from_trailer"


# 损坏或意外的 trailer：解析失败、类别为 UNSPECIFIED（0）或不认识的值。ProviderError.from_proto 遇到这些会抛
# ValueError 或 KeyError，客户端必须先挡住，退回按状态码映射（与没有 trailer 时相同），仍抛 ProviderError。
GARBAGE = b"\xff\xff\xff\xff\x0f not a proto"
CORRUPT_TRAILER_CASES = {
    "garbage-invalid": (grpc.StatusCode.INVALID_ARGUMENT, GARBAGE, InvalidError),
    "garbage-unavailable": (grpc.StatusCode.UNAVAILABLE, GARBAGE, TransientError),
    "empty-denied": (grpc.StatusCode.PERMISSION_DENIED, b"", DeniedError),
    "unspecified-denied": (
        grpc.StatusCode.PERMISSION_DENIED,
        provider_error(error_pb2.ERROR_CATEGORY_UNSPECIFIED, "no_category", "类别没填").SerializeToString(),
        DeniedError,
    ),
    "unknown-not-found": (grpc.StatusCode.NOT_FOUND, provider_error(99, "future_category", "以后才有的类别").SerializeToString(), NotFoundError),
    "unknown-internal": (grpc.StatusCode.INTERNAL, provider_error(99, "future_category", "以后才有的类别").SerializeToString(), TransientError),
}


@pytest.mark.parametrize("method", ["generate", "embed"])
@pytest.mark.parametrize("case", list(CORRUPT_TRAILER_CASES))
async def test_corrupt_error_trailer_falls_back_to_the_status_code(method: str, case: str) -> None:
    """约定 5、难点“错误分类一致”：ar-error-bin 解析不了或类别不认识时，按 gRPC 状态码映射；抛的仍是 ProviderError，
    code 不为空，不把解析异常（DecodeError、ValueError、KeyError）漏给调用方。"""
    status, raw, expected = CORRUPT_TRAILER_CASES[case]
    fake = FakeModelGateway(abort=Abort(status, "trailer 坏了", raw_trailer=raw))
    async with serve(fake) as address, open_client(address) as gateway:
        with pytest.raises(ProviderError) as info:
            await _call(gateway, method)
    err = info.value
    assert type(err) is expected, f"{case}：应按状态码 {status.name} 还原为 {expected.__name__}，实际 {type(err).__name__}"
    assert err.code, "code 不能为空，Node Executor 的失败分类要用它"
    assert err.retryable is (expected is TransientError)


@pytest.mark.parametrize("retry_after_ms", ["soon", -5, {"ms": 1500}], ids=["string", "negative", "object"])
async def test_malformed_retry_after_is_ignored(retry_after_ms: object) -> None:
    """约定 3：details.retry_after_ms 不是非负数时不还原 retry_after（为 None），类别、code 照常还原，不抛别的异常。"""
    error = provider_error(error_pb2.ERROR_CATEGORY_TRANSIENT, "rate_limit", "上游限流", {RETRY_AFTER_DETAIL: retry_after_ms})
    fake = FakeModelGateway(abort=Abort(grpc.StatusCode.UNAVAILABLE, "限流", error))
    async with serve(fake) as address, open_client(address) as gateway:
        with pytest.raises(ProviderError) as info:
            await gateway.generate(generate_request())
    assert type(info.value) is TransientError
    assert info.value.code == "rate_limit"
    assert info.value.retry_after is None


# --- 错误还原：没有 trailer 时按 gRPC 状态码 ---

STATUS_CASES = [
    (grpc.StatusCode.UNAVAILABLE, TransientError),
    (grpc.StatusCode.DEADLINE_EXCEEDED, TransientError),
    (grpc.StatusCode.RESOURCE_EXHAUSTED, TransientError),
    (grpc.StatusCode.INVALID_ARGUMENT, InvalidError),
    (grpc.StatusCode.PERMISSION_DENIED, DeniedError),
    (grpc.StatusCode.UNAUTHENTICATED, DeniedError),
    (grpc.StatusCode.NOT_FOUND, NotFoundError),
    # 其余 → transient。
    (grpc.StatusCode.CANCELLED, TransientError),
    (grpc.StatusCode.UNKNOWN, TransientError),
    (grpc.StatusCode.ALREADY_EXISTS, TransientError),
    (grpc.StatusCode.FAILED_PRECONDITION, TransientError),
    (grpc.StatusCode.ABORTED, TransientError),
    (grpc.StatusCode.OUT_OF_RANGE, TransientError),
    (grpc.StatusCode.UNIMPLEMENTED, TransientError),
    (grpc.StatusCode.INTERNAL, TransientError),
    (grpc.StatusCode.DATA_LOSS, TransientError),
]


@pytest.mark.parametrize("method", ["generate", "embed"])
@pytest.mark.parametrize(("status", "expected"), STATUS_CASES, ids=[c[0].name for c in STATUS_CASES])
async def test_status_code_maps_to_a_category_without_trailer(method: str, status: grpc.StatusCode, expected: type[ProviderError]) -> None:
    """约定 5：没有 ar-error-bin 时按状态码映射；抛的是 ProviderError，不把 grpc 的异常漏给调用方。"""
    fake = FakeModelGateway(abort=Abort(status, "没有 trailer 的错误"))
    async with serve(fake) as address, open_client(address) as gateway:
        with pytest.raises(ProviderError) as info:
            await _call(gateway, method)
    assert type(info.value) is expected
    assert info.value.code, "code 不能为空，Node Executor 的失败分类要用它"
    assert not isinstance(info.value, grpc.RpcError)


async def test_unreachable_gateway_is_transient() -> None:
    """网关没起来（连不上）是 UNAVAILABLE → transient，Node Executor 会重试。"""
    async with open_client(f"127.0.0.1:{free_port()}", timeout_s=5) as gateway:
        with pytest.raises(TransientError):
            await gateway.generate(generate_request())


async def test_client_timeout_is_transient() -> None:
    """每次调用的 deadline 是 timeout_s；超时 → TransientError，而且不会等到服务端返回。"""
    fake = FakeModelGateway(delay_s=5)
    async with serve(fake) as address, open_client(address, timeout_s=0.3) as gateway:
        started = time.monotonic()
        with pytest.raises(TransientError):
            await gateway.generate(generate_request())
        elapsed = time.monotonic() - started
    assert elapsed < 3, f"deadline 0.3 秒，实际等了 {elapsed:.1f} 秒"


ERROR_SCENARIOS = {
    "trailer": Abort(grpc.StatusCode.INVALID_ARGUMENT, "上游 400", provider_error(error_pb2.ERROR_CATEGORY_INVALID, "bad_request", "上游拒绝了请求")),
    "status_only": Abort(grpc.StatusCode.INTERNAL, "内部错误"),
    "rate_limited": Abort(
        grpc.StatusCode.UNAVAILABLE, "限流", provider_error(error_pb2.ERROR_CATEGORY_TRANSIENT, "rate_limit", "上游限流", {RETRY_AFTER_DETAIL: 200})
    ),
}


@pytest.mark.parametrize("scenario", [*ERROR_SCENARIOS, "timeout"])
async def test_errors_do_not_echo_the_request_messages(scenario: str) -> None:
    """难点“密钥不外泄”的 Python 一侧：错误信息（str、repr、code、message）里不出现请求的消息内容。"""
    fake = FakeModelGateway(abort=ERROR_SCENARIOS.get(scenario), delay_s=5 if scenario == "timeout" else 0)
    async with serve(fake) as address, open_client(address, timeout_s=0.3 if scenario == "timeout" else 30) as gateway:
        with pytest.raises(ProviderError) as info:
            await gateway.generate(generate_request(SECRET_MESSAGE))
    err = info.value
    for text in (str(err), repr(err), err.code, err.message):
        assert SECRET_MESSAGE not in text
        assert "C-1024" not in text


# --- from_env 与评测的工厂 ---


async def test_from_env_reads_the_address() -> None:
    fake = FakeModelGateway()
    async with serve(fake) as address:
        gateway = from_env({ENV_ADDRESS: address})
        try:
            assert isinstance(gateway, GrpcModelGateway)
            await gateway.generate(generate_request())
        finally:
            await gateway.aclose()
    assert len(fake.requests) == 1


@pytest.mark.parametrize("env", [{}, {ENV_ADDRESS: ""}, {ENV_ADDRESS: "   "}], ids=["missing", "empty", "blank"])
def test_from_env_without_an_address_is_invalid(env: dict[str, str]) -> None:
    """约定 5：缺 AR_MODEL_GATEWAY_ADDRESS 抛 InvalidError，不悄悄换成桩模型。

    同时是 ValueError：评测命令行（py-learning 的 evals.__main__）的 load_subject 只接 ValueError，按参数错误以退出码 2
    报出；作为 AR_EVAL_GATEWAY_FACTORY 时缺地址应是一行提示，不是调用栈。
    """
    with pytest.raises(InvalidError) as info:
        from_env(env)
    assert isinstance(info.value, MissingGatewayAddressError)
    assert isinstance(info.value, ValueError), "评测命令行只接 ValueError"
    assert not info.value.retryable
    assert ENV_ADDRESS in str(info.value)


def test_from_env_defaults_to_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_ADDRESS, raising=False)
    with pytest.raises(InvalidError):
        from_env()
    monkeypatch.setenv(ENV_ADDRESS, "127.0.0.1:8091")
    assert isinstance(from_env(), GrpcModelGateway)


def test_from_env_works_as_the_eval_gateway_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """交付 3：AR_EVAL_GATEWAY_FACTORY=agentruntime_worker.executor.grpc_gateway:from_env。

    照评测命令行的用法（py-learning 的 evals.worker_subject._gateway）：按 <模块>:<函数> 加载，不带参数调用，
    而且是在事件循环之外构造（命令行先构造被测对象，再 asyncio.run），之后在 asyncio.run 里调用。
    """
    port = free_port()
    monkeypatch.setenv(ENV_ADDRESS, f"127.0.0.1:{port}")
    module_name, _, attr = FACTORY_SPEC.partition(":")
    factory = getattr(importlib.import_module(module_name), attr)
    assert factory is grpc_gateway.from_env
    gateway = factory()
    assert isinstance(gateway, GrpcModelGateway)

    fake = FakeModelGateway()

    async def scenario() -> mg.GenerateResponse:
        async with serve(fake, port):
            try:
                return await gateway.generate(generate_request())
            finally:
                await gateway.aclose()

    response = asyncio.run(scenario())
    assert response == generate_response()
    assert len(fake.requests) == 1
