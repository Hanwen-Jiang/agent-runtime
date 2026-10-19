"""加载 /schemas 与样例，构造 JSON Schema 2020-12 校验器。"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from functools import cache
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from jsonschema_specifications import REGISTRY as SPECIFICATIONS
from referencing import Registry, Resource

from contract_tests.paths import EXAMPLES_DIR, SCHEMAS_DIR

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
# 共享的枚举与格式定义；其余 schema 只能 $ref 它，不能内联 enum。
DEFS_SCHEMA = "defs"


@cache
def schema_files() -> dict[str, dict[str, Any]]:
    """schema 名 → 内容。文件名形如 <名>.schema.json。"""
    return {
        path.name.removesuffix(".schema.json"): json.loads(path.read_text("utf-8"))
        for path in sorted(SCHEMAS_DIR.glob("*.schema.json"))
    }


@cache
def registry() -> Registry:
    resources = [(schema["$id"], Resource.from_contents(schema)) for schema in schema_files().values()]
    return SPECIFICATIONS.with_resources(resources)


def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(
        schema_files()[name],
        registry=registry(),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


@cache
def example_files() -> dict[str, dict[str, Any]]:
    """schema 名 → 样例文件内容。文件名形如 <名>.examples.json。"""
    return {
        path.name.removesuffix(".examples.json"): json.loads(path.read_text("utf-8"))
        for path in sorted(EXAMPLES_DIR.glob("*.examples.json"))
    }


def flatten(errors: Iterable[ValidationError]) -> Iterator[ValidationError]:
    """展开 anyOf、oneOf 等关键字下的子错误。"""
    for error in errors:
        yield error
        yield from flatten(error.context or ())


def pointer(error: ValidationError) -> str:
    """出错位置的 JSON Pointer；根为空串。"""
    parts = (str(part).replace("~", "~0").replace("/", "~1") for part in error.absolute_path)
    return "".join(f"/{part}" for part in parts)
