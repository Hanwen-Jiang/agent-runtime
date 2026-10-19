"""S-01 验收：所有正例通过 schema 校验，反例全部失败，且失败在预期的位置。

样例在 schemas/examples/<schema>.examples.json，格式见 schemas/examples/README.md。
"""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from contract_tests import schemas

# 计划 S-01 交付第 3 条列出的九个 schema。
REQUIRED_SCHEMAS = (
    "atomic_node",
    "goal_node",
    "stage",
    "task_state",
    "task_plan",
    "node_output",
    "verdict",
    "hook_result",
    "memory_item",
)

# 计划 S-01 交付第 4 条：附录 B、C、D、I 的 YAML 示例都要转成正例。
APPENDIX_EXAMPLES = {
    "task_state": ["appendix_b1_task_state"],
    "stage": ["appendix_b2_stage"],
    "goal_node": ["appendix_b2_read_tickets"],
    "atomic_node": ["appendix_b2_analyze", "appendix_d1_q_region"],
    "task_plan": ["appendix_c3_task_plan"],
    "node_output": ["appendix_d2_node_output"],
    "memory_item": ["appendix_i3_memory_item"],
}


def _cases(kind: str) -> list:
    return [
        pytest.param(schema_name, example, id=f"{schema_name}:{example['name']}")
        for schema_name, doc in sorted(schemas.example_files().items())
        for example in doc.get(kind, [])
    ]


def _describe(errors) -> str:
    return "\n".join(f"  {schemas.pointer(e) or '<根>'} [{e.validator}] {e.message}" for e in errors)


# --- schema 文件本身 ---


def test_required_schemas_exist():
    missing = [name for name in REQUIRED_SCHEMAS if name not in schemas.schema_files()]
    assert not missing, f"缺少 schema：{missing}"


def test_defs_schema_exists():
    assert schemas.DEFS_SCHEMA in schemas.schema_files(), "共享枚举与格式应集中在 defs.schema.json"


@pytest.mark.parametrize("name", sorted(schemas.schema_files()))
def test_schema_is_valid_2020_12(name):
    schema = schemas.schema_files()[name]
    assert schema.get("$schema") == schemas.DRAFT_2020_12
    assert schema.get("$id"), "每个 schema 都要有 $id，供 $ref 解析"
    Draft202012Validator.check_schema(schema)


def test_schema_ids_are_unique():
    ids = [schema["$id"] for schema in schemas.schema_files().values()]
    assert len(ids) == len(set(ids)), ids


# --- 样例文件的完整性 ---


def test_every_required_schema_has_valid_and_invalid_examples():
    problems = []
    for name in REQUIRED_SCHEMAS:
        doc = schemas.example_files().get(name)
        if doc is None:
            problems.append(f"{name}：缺少 {name}.examples.json")
            continue
        if doc.get("schema") != name:
            problems.append(f"{name}：文件内 schema 字段为 {doc.get('schema')!r}")
        if not doc.get("valid"):
            problems.append(f"{name}：没有正例")
        if not doc.get("invalid"):
            problems.append(f"{name}：没有反例")
    assert not problems, "\n".join(problems)


def test_example_files_match_existing_schemas():
    orphans = sorted(set(schemas.example_files()) - set(schemas.schema_files()))
    assert not orphans, f"样例文件没有对应的 schema：{orphans}"


def test_example_names_are_unique():
    for schema_name, doc in schemas.example_files().items():
        names = [e["name"] for kind in ("valid", "invalid") for e in doc.get(kind, [])]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        assert not duplicates, f"{schema_name}：样例名重复 {duplicates}"


def test_appendix_examples_are_present():
    missing = []
    for schema_name, names in APPENDIX_EXAMPLES.items():
        present = {e["name"] for e in schemas.example_files().get(schema_name, {}).get("valid", [])}
        missing += [f"{schema_name}/{n}" for n in names if n not in present]
    assert not missing, f"附录 YAML 示例未转成正例：{missing}"


def test_valid_examples_cite_a_source():
    missing = [
        f"{schema_name}/{e['name']}"
        for schema_name, doc in schemas.example_files().items()
        for e in doc.get("valid", [])
        if not e.get("source")
    ]
    assert not missing, f"正例缺少 source：{missing}"


def test_invalid_examples_declare_expectations():
    problems = []
    for schema_name, doc in schemas.example_files().items():
        for e in doc.get("invalid", []):
            label = f"{schema_name}/{e['name']}"
            if not e.get("description"):
                problems.append(f"{label}：缺少 description")
            violates = e.get("violates")
            if not isinstance(violates, list) or not violates:
                problems.append(f"{label}：violates 必须是非空列表")
                continue
            for expected in violates:
                path = expected.get("path")
                if not isinstance(path, str) or (path and not path.startswith("/")):
                    problems.append(f"{label}：path 必须是 JSON Pointer，得到 {path!r}")
                if set(expected) - {"path", "keyword"}:
                    problems.append(f"{label}：violates 只能有 path 与 keyword")
    assert not problems, "\n".join(problems)


# --- 校验 ---


@pytest.mark.parametrize(("schema_name", "example"), _cases("valid"))
def test_valid_example_passes(schema_name, example):
    errors = sorted(
        schemas.validator(schema_name).iter_errors(example["instance"]),
        key=lambda e: [str(p) for p in e.absolute_path],
    )
    assert not errors, f"正例未通过校验：\n{_describe(errors)}"


@pytest.mark.parametrize(("schema_name", "example"), _cases("invalid"))
def test_invalid_example_fails_for_the_stated_reason(schema_name, example):
    errors = list(schemas.flatten(schemas.validator(schema_name).iter_errors(example["instance"])))
    assert errors, f"反例通过了校验：{example['description']}"
    for expected in example["violates"]:
        keyword = expected.get("keyword")
        matched = [
            e
            for e in errors
            if schemas.pointer(e) == expected["path"] and keyword in (None, e.validator)
        ]
        assert matched, (
            f"反例没有在预期位置失败：{example['description']}\n"
            f"预期：{json.dumps(expected, ensure_ascii=False)}\n"
            f"实际错误：\n{_describe(errors)}"
        )
