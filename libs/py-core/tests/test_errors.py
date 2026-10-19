"""错误模型：类别与 proto 一致；只有 transient 可重试；能与 proto 互转。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from agentruntime.common.v1 import error_pb2

from agentruntime_core.errors import (
    ConflictError,
    DeniedError,
    ErrorCategory,
    InvalidError,
    NotFoundError,
    ProviderError,
    TransientError,
)


def test_categories_mirror_proto():
    proto = {
        name.removeprefix("ERROR_CATEGORY_"): number
        for name, number in error_pb2.ErrorCategory.items()
        if number != 0
    }
    assert {c.name: c.value for c in ErrorCategory} == proto


@pytest.mark.parametrize(
    ("category", "cls"),
    [
        (ErrorCategory.TRANSIENT, TransientError),
        (ErrorCategory.INVALID, InvalidError),
        (ErrorCategory.DENIED, DeniedError),
        (ErrorCategory.NOT_FOUND, NotFoundError),
        (ErrorCategory.CONFLICT, ConflictError),
    ],
)
def test_of_builds_matching_subclass(category, cls):
    error = ProviderError.of(category, "some_code", "说明")
    assert type(error) is cls
    assert error.category is category
    assert error.code == "some_code" and error.message == "说明"


@pytest.mark.parametrize("category", list(ErrorCategory))
def test_only_transient_is_retryable(category):
    assert ProviderError.of(category, "x").retryable is (category is ErrorCategory.TRANSIENT)


def test_proto_roundtrip():
    error = ConflictError("unique_violation", "name 已存在")
    proto = error.to_proto()
    assert proto.category == error_pb2.ERROR_CATEGORY_CONFLICT
    back = ProviderError.from_proto(proto)
    assert isinstance(back, ConflictError) and back.code == "unique_violation" and back.message == "name 已存在"


def test_retry_after_is_kept():
    error = TransientError("rate_limit", retry_after=timedelta(seconds=3))
    assert error.retry_after == timedelta(seconds=3)


def test_provider_error_is_abstract_without_category():
    with pytest.raises(AttributeError):
        ProviderError("x").retryable  # noqa: B018
