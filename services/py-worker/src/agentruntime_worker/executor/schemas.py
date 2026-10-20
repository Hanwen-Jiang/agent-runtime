"""output_schema 的解析与校验（设计书 3.2.4 第 3 条）。

Node 的 output_schema 是已登记的 schema 名（schema_ref，例如 region_refund_rate_v1）或内联的 JSON Schema。
登记的 schema 由 Registry 提供；开发与测试用 DictSchemaResolver。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from jsonschema import Draft202012Validator


class OutputSchemaResolver(Protocol):
    def resolve(self, ref: str) -> Mapping[str, Any] | None:
        """返回登记的 schema；没有登记时返回 None。"""
        ...


class DictSchemaResolver:
    def __init__(self, schemas: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self._schemas = dict(schemas or {})

    def resolve(self, ref: str) -> Mapping[str, Any] | None:
        return self._schemas.get(ref)


class UnknownSchema(LookupError):
    """output_schema 引用了没有登记的 schema。"""


def schema_for(node: Mapping[str, Any], resolver: OutputSchemaResolver) -> Mapping[str, Any] | None:
    """Node 的 output_schema：没有写时为 None；登记名查不到时抛 UnknownSchema。"""
    spec = node.get("output_schema")
    if spec is None:
        return None
    if isinstance(spec, Mapping):
        return spec
    resolved = resolver.resolve(str(spec))
    if resolved is None:
        raise UnknownSchema(f"output_schema {spec} 没有登记")
    return resolved


def validate(schema: Mapping[str, Any], value: Any) -> list[str]:
    """按 JSON Schema 2020-12 校验，返回错误说明（为空表示合法），按出错位置排序。"""
    validator = Draft202012Validator(dict(schema), format_checker=Draft202012Validator.FORMAT_CHECKER)
    errors = sorted(validator.iter_errors(value), key=lambda e: [str(p) for p in e.absolute_path])
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '(根)'}：{e.message}" for e in errors]
