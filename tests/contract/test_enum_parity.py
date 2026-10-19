"""枚举只在一处定义（计划 S-01 难点“枚举”）。

proto 为准；JSON Schema 的枚举集中在 defs.schema.json 的 $defs，由本文件逐项比对。
映射规则：proto 枚举 TriageLevel 的值 TRIAGE_LEVEL_FULL ↔ $defs.triage_level 的 "full"。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from contract_tests import protos, schemas

COMMON = "agentruntime.common.v1"

# 附录 A 的枚举取值。人审时与附录 A 对照的就是这张表。
APPENDIX_A = {
    "executor": ["model", "program", "human"],
    "effect": ["none", "read", "write_reversible", "write_irreversible"],
    "role": ["fetch", "transform", "plan", "route", "judge", "aggregate", "act", "gate", "deliver"],
    "mode": ["template", "tool_loop", "sub_agent", "sub_task"],
    "quality": ["none", "revise", "ensemble", "search"],
    "reasoning": ["direct", "stepwise", "plan_first", "templated"],
    "verdict": ["done", "continue", "repair", "replan", "rollback", "reroute", "escalate", "abort"],
    "triage_level": ["fast", "light", "full", "replan"],
}

# $defs 名 → proto 枚举全名。
PROTO_ENUMS = {
    "executor": f"{COMMON}.Executor",
    "effect": f"{COMMON}.Effect",
    "role": f"{COMMON}.Role",
    "mode": f"{COMMON}.Mode",
    "quality": f"{COMMON}.Quality",
    "reasoning": f"{COMMON}.Reasoning",
    "verdict": f"{COMMON}.Verdict",
    "triage_level": f"{COMMON}.TriageLevel",
}


def _defs() -> dict[str, Any]:
    return schemas.schema_files()[schemas.DEFS_SCHEMA]["$defs"]


def _schema_enum(name: str) -> list[str]:
    definition = _defs().get(name)
    assert definition is not None, f"defs.schema.json 缺少 $defs.{name}"
    values = definition.get("enum")
    assert isinstance(values, list), f"$defs.{name} 应直接用 enum 列出取值"
    return values


@pytest.mark.parametrize("name", sorted(APPENDIX_A))
def test_appendix_a_values_in_schema(name):
    assert sorted(_schema_enum(name)) == sorted(APPENDIX_A[name])


@pytest.mark.parametrize("name", sorted(APPENDIX_A))
def test_appendix_a_values_in_proto(name):
    desc = protos.enum(PROTO_ENUMS[name])
    assert sorted(protos.enum_values(desc)) == sorted(APPENDIX_A[name])


def test_same_named_enums_agree():
    """proto 与 $defs 中同名的枚举（CamelCase ↔ snake_case）取值必须一致。"""
    defs = _defs()
    compared = set()
    mismatches = []
    for desc in protos.enums():
        key = protos.lower_snake(desc.name)
        if key not in defs or "enum" not in defs[key]:
            continue
        compared.add(key)
        proto_values = sorted(protos.enum_values(desc))
        schema_values = sorted(defs[key]["enum"])
        if proto_values != schema_values:
            mismatches.append(f"{desc.full_name} {proto_values} ≠ $defs.{key} {schema_values}")
    assert not mismatches, "\n".join(mismatches)
    assert set(PROTO_ENUMS) <= compared


def test_id_prefixes_match_schema_patterns():
    """ID 前缀只在 proto 的 IdKind 定义；defs.schema.json 中对应 ID 的 pattern 必须以它开头。"""
    prefixes = protos.option_values(protos.enum(f"{COMMON}.IdKind"), protos.ID_PREFIX)
    defs = _defs()
    mismatches = []
    for value_name, prefix in prefixes.items():
        def_name = value_name.removeprefix("ID_KIND_").lower() + "_id"
        pattern = defs.get(def_name, {}).get("pattern", "")
        if not prefix or not pattern.startswith(f"^{prefix}_"):
            mismatches.append(f"{value_name}（前缀 {prefix!r}）↔ $defs.{def_name}.pattern {pattern!r}")
    assert not mismatches, "\n".join(mismatches)


def _enum_keywords(node: Any, path: str = "") -> Iterator[str]:
    if isinstance(node, dict):
        if isinstance(node.get("enum"), list):
            yield path or "/"
        for key, value in node.items():
            yield from _enum_keywords(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _enum_keywords(value, f"{path}/{index}")


def test_schema_enums_only_in_defs():
    """除 defs.schema.json 外，schema 里不内联 enum，一律 $ref 共享定义。"""
    offenders = [
        f"{name}.schema.json{location}"
        for name, schema in schemas.schema_files().items()
        if name != schemas.DEFS_SCHEMA
        for location in _enum_keywords(schema)
    ]
    assert not offenders, f"内联的 enum：{offenders}"
