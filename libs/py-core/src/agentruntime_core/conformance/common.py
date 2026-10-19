"""所有一致性套件共用的租户、断言与规则。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Protocol, TypeVar

import pytest

from agentruntime.common.v1 import tenant_pb2

from agentruntime_core.errors import ErrorCategory, ProviderError

TENANT_A_ID = "t-alpha"
TENANT_B_ID = "t-beta"

# 格式不对的 tenant_id：大写、路径穿越、分隔符、空白、超长、首字符非法、换行。
MALFORMED_TENANT_IDS = ("T1", "../t-beta", "t-alpha/x", "t alpha", "a" * 64, "-t1", "t1\n")

P = TypeVar("P")


def tenant(tenant_id: str, **fields: str) -> tenant_pb2.TenantContext:
    return tenant_pb2.TenantContext(tenant_id=tenant_id, **fields)


def tenant_a() -> tenant_pb2.TenantContext:
    return tenant(TENANT_A_ID, user_id="u-alice", workspace_id="w-1")


def tenant_b() -> tenant_pb2.TenantContext:
    return tenant(TENANT_B_ID, user_id="u-bob", workspace_id="w-2")


@contextmanager
def expect_error(category: ErrorCategory, code: str | None = None) -> Iterator[pytest.ExceptionInfo[ProviderError]]:
    """断言抛出指定类别的 ProviderError，并核对 retryable 与类别一致。"""
    with pytest.raises(ProviderError) as info:
        yield info
    error = info.value
    assert error.category is category, f"类别应为 {category.name}，实际为 {error.category.name}（{error}）"
    assert error.retryable is (category is ErrorCategory.TRANSIENT)
    if code is not None:
        assert error.code == code, f"code 应为 {code}，实际为 {error.code}"


class FaultInjectionUnsupported(Exception):
    """harness 不支持注入故障。生产实现的故障由 S-07 的故障注入套件覆盖。"""


class Harness(Protocol[P]):
    """所有 harness 的公共部分。

    每个测试拿到一个干净的 provider：开发实现每次新建；生产实现在测试之间清空租户 A、租户 B 与平台作用域的数据。
    """

    @property
    def provider(self) -> P: ...

    async def advance(self, delta: timedelta) -> None:
        """让时间前进 delta：开发实现拨动 FakeClock，生产实现真实等待。"""
        ...

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        """让客户端下一次调用 operation 时抛出该类别的错误；不支持时抛 FaultInjectionUnsupported。"""
        ...


def inject_or_skip(harness: Harness[Any], operation: str, category: ErrorCategory) -> None:
    try:
        harness.inject_fault(operation, category)
    except FaultInjectionUnsupported:
        pytest.skip("该实现不支持故障注入，由 S-07 的故障注入套件覆盖")


class BindingRules:
    """租户绑定的规则，与接口无关。"""

    def test_bind_without_tenant_is_denied(self, harness: Harness[Any]) -> None:
        with expect_error(ErrorCategory.DENIED, "missing_tenant"):
            harness.provider.bind(None)

    def test_bind_without_tenant_id_is_denied(self, harness: Harness[Any]) -> None:
        with expect_error(ErrorCategory.DENIED, "missing_tenant"):
            harness.provider.bind(tenant_pb2.TenantContext(user_id="u-alice"))

    @pytest.mark.parametrize("bad_id", MALFORMED_TENANT_IDS)
    def test_bind_with_malformed_tenant_id_is_invalid(self, harness: Harness[Any], bad_id: str) -> None:
        with expect_error(ErrorCategory.INVALID, "malformed_tenant"):
            harness.provider.bind(tenant(bad_id))

    def test_bind_rejects_non_tenant_context(self, harness: Harness[Any]) -> None:
        with expect_error(ErrorCategory.INVALID, "malformed_tenant"):
            harness.provider.bind({"tenant_id": TENANT_A_ID})

    @pytest.mark.parametrize("purpose", ["", "   "])
    def test_bind_platform_requires_purpose(self, harness: Harness[Any], purpose: str) -> None:
        with expect_error(ErrorCategory.INVALID, "missing_purpose"):
            harness.provider.bind_platform(purpose)


class IsolationRules:
    """租户隔离：A 写入，B 读不到；平台与租户互不可见；绑定时复制上下文。

    子类实现 write_probe 与 probe_visible：在客户端上写入一个名为 name 的数据，再判断它是否可见。
    """

    async def write_probe(self, client: Any, name: str) -> None:
        raise NotImplementedError

    async def probe_visible(self, client: Any, name: str) -> bool:
        raise NotImplementedError

    async def test_tenant_b_cannot_see_tenant_a_data(self, harness: Harness[Any]) -> None:
        a = harness.provider.bind(tenant_a())
        b = harness.provider.bind(tenant_b())
        await self.write_probe(a, "probe-isolation")
        assert await self.probe_visible(a, "probe-isolation"), "写入方自己应当能读到"
        assert not await self.probe_visible(b, "probe-isolation"), "租户 B 读到了租户 A 的数据"

    async def test_platform_and_tenant_scopes_are_isolated(self, harness: Harness[Any]) -> None:
        a = harness.provider.bind(tenant_a())
        platform = harness.provider.bind_platform("conformance")
        await self.write_probe(platform, "probe-platform")
        await self.write_probe(a, "probe-tenant")
        assert await self.probe_visible(platform, "probe-platform")
        assert not await self.probe_visible(a, "probe-platform"), "租户读到了平台作用域的数据"
        assert not await self.probe_visible(platform, "probe-tenant"), "平台作用域读到了租户的数据"

    async def test_bind_copies_tenant_context(self, harness: Harness[Any]) -> None:
        context = tenant_a()
        client = harness.provider.bind(context)
        context.tenant_id = TENANT_B_ID  # 绑定之后修改原对象，不应改变客户端的租户
        await self.write_probe(client, "probe-copy")
        assert await self.probe_visible(harness.provider.bind(tenant_a()), "probe-copy")
        assert not await self.probe_visible(harness.provider.bind(tenant_b()), "probe-copy")


class ErrorRules:
    """错误分类：transient 可重试，其余不可重试；故障过去后客户端照常可用。

    子类设置 probe_operation：write_probe 会调用到的客户端方法名。
    """

    probe_operation: str = ""

    async def write_probe(self, client: Any, name: str) -> None:
        raise NotImplementedError

    async def test_transient_error_is_retryable(self, harness: Harness[Any]) -> None:
        client = harness.provider.bind(tenant_a())
        inject_or_skip(harness, self.probe_operation, ErrorCategory.TRANSIENT)
        with expect_error(ErrorCategory.TRANSIENT):
            await self.write_probe(client, "probe-fault")
        await self.write_probe(client, "probe-fault")  # 故障过去后重试成功

    @pytest.mark.parametrize(
        "category",
        [ErrorCategory.INVALID, ErrorCategory.DENIED, ErrorCategory.NOT_FOUND, ErrorCategory.CONFLICT],
        ids=lambda c: c.name.lower(),
    )
    async def test_other_errors_are_not_retryable(self, harness: Harness[Any], category: ErrorCategory) -> None:
        client = harness.provider.bind(tenant_a())
        inject_or_skip(harness, self.probe_operation, category)
        with expect_error(category):
            await self.write_probe(client, "probe-fault")


class StandardRules(BindingRules, IsolationRules, ErrorRules):
    """多数接口都适用的三组规则。"""
