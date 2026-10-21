"""对照 /schemas 与评测用例格式（case.schema.json）校验。"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema_specifications import REGISTRY as SPECIFICATIONS
from referencing import Registry, Resource

SCHEMAS_DIR = Path(__file__).resolve().parents[5] / "schemas"
CASE_SCHEMA = Path(__file__).with_name("case.schema.json")
CONTEXT_SAMPLE_SCHEMA = Path(__file__).with_name("context_sample.schema.json")


@cache
def _schemas() -> dict[str, dict[str, Any]]:
    """名字（stage、task_plan…，以及 case、context_sample）→ schema。"""
    found = {p.name.removesuffix(".schema.json"): json.loads(p.read_text("utf-8")) for p in sorted(SCHEMAS_DIR.glob("*.schema.json"))}
    found["case"] = json.loads(CASE_SCHEMA.read_text("utf-8"))
    found["context_sample"] = json.loads(CONTEXT_SAMPLE_SCHEMA.read_text("utf-8"))
    return found


@cache
def _registry() -> Registry:
    return SPECIFICATIONS.with_resources((s["$id"], Resource.from_contents(s)) for s in _schemas().values())


@cache
def validator(name: str) -> Draft202012Validator:
    """name 是 /schemas 里的 schema 名（stage、task_plan、task_state、verdict…）、case 或 context_sample。"""
    return Draft202012Validator(_schemas()[name], registry=_registry(), format_checker=Draft202012Validator.FORMAT_CHECKER)


@cache
def case_def_validator(definition: str) -> Draft202012Validator:
    """case.schema.json 的某个 $defs（例如 catalog）。"""
    schema = {"$ref": f"{_schemas()['case']['$id']}#/$defs/{definition}"}
    return Draft202012Validator(schema, registry=_registry(), format_checker=Draft202012Validator.FORMAT_CHECKER)


def _path(error: Any) -> str:
    return "/".join(str(p) for p in error.absolute_path) or "（根）"


def problems(v: Draft202012Validator, value: Any) -> list[str]:
    """完整的错误说明，给评测集的作者看（会引用取值）。"""
    errors = sorted(v.iter_errors(value), key=lambda e: [str(p) for p in e.absolute_path])
    return [f"{_path(e)}: {e.message}" for e in errors]


def locations(v: Draft202012Validator, value: Any) -> list[str]:
    """只有位置与出错的关键字、不引用取值的错误说明：被测输出可能抄了用例内容，留出集的报告也要汇总它。"""
    errors = sorted(v.iter_errors(value), key=lambda e: [str(p) for p in e.absolute_path])
    return sorted({f"{_path(e)}（{e.validator}）" for e in errors})
