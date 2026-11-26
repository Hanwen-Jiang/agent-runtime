"""Pre-execution JSON Schema validator and field-level correction formatter for tool calls."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _is_int(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    # Model Gateway protobuf Struct encodes all JSON numbers as floats.
    return isinstance(value, float) and value.is_integer()


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return _is_int(value)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "null":
        return value is None
    return True


def _validate_value(path: str, value: Any, schema: Mapping[str, Any], errors: list[str]) -> None:
    expected_type = schema.get("type")
    if isinstance(expected_type, str):
        if not _matches_type(value, expected_type):
            errors.append(f"字段 {path} 应为 {expected_type}，实际收到 {type(value).__name__}")
            return
    elif isinstance(expected_type, Sequence) and not isinstance(expected_type, (str, bytes)):
        if not any(_matches_type(value, str(t)) for t in expected_type):
            allowed = "/".join(str(t) for t in expected_type)
            errors.append(f"字段 {path} 应为 {allowed}，实际收到 {type(value).__name__}")
            return

    enum_vals = schema.get("enum")
    if isinstance(enum_vals, Sequence) and not isinstance(enum_vals, (str, bytes)) and value not in enum_vals:
        allowed = ", ".join(repr(x) for x in enum_vals[:10])
        errors.append(f"字段 {path} 必须是枚举值 [{allowed}] 之一")
        return

    if isinstance(value, str):
        min_len = schema.get("minLength")
        if isinstance(min_len, (int, float)) and len(value) < int(min_len):
            errors.append(f"字段 {path} 长度不能少于 {int(min_len)}")

    if isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for idx, item in enumerate(value[:16]):
                _validate_value(f"{path}[{idx}]", item, item_schema, errors)

    if isinstance(value, Mapping):
        props = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
        required = schema.get("required") if isinstance(schema.get("required"), Sequence) else ()
        for req in required:
            req_name = str(req)
            if req_name not in value or value[req_name] is None or (
                isinstance(value[req_name], str) and not value[req_name]
                and isinstance(props.get(req_name), Mapping)
                and int(props[req_name].get("minLength") or 0) > 0
            ):
                errors.append(f"缺少必填字段 {path}.{req_name}")
        if schema.get("additionalProperties") is False:
            unknown = sorted(str(k) for k in value if k not in props)
            if unknown:
                errors.append(f"包含未声明的字段 {', '.join(f'{path}.{u}' for u in unknown)}")
        for k, v in value.items():
            prop_schema = props.get(k)
            if isinstance(prop_schema, Mapping):
                _validate_value(f"{path}.{k}", v, prop_schema, errors)


def validate_tool_arguments(tool_name: str, args: Mapping[str, Any] | None, schema: Mapping[str, Any] | None) -> list[str]:
    """Validate structured tool call arguments against the tool JSON Schema."""
    if not isinstance(schema, Mapping) or not schema:
        return []
    if args is None or not isinstance(args, Mapping):
        return [f"工具 {tool_name} 的参数必须是 JSON 对象"]
    errors: list[str] = []
    props = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
    required = schema.get("required") if isinstance(schema.get("required"), Sequence) else ()
    for req in required:
        req_name = str(req)
        if req_name not in args or args[req_name] is None:
            hint = ""
            if isinstance(props.get(req_name), Mapping) and props[req_name].get("type"):
                hint = f"（期望类型：{props[req_name]['type']}）"
            errors.append(f"缺少必填字段 {req_name}{hint}")
    if schema.get("additionalProperties") is False:
        unknown = sorted(str(k) for k in args if k not in props)
        if unknown:
            errors.append(f"包含未知字段：{', '.join(unknown)}")
    for k, v in args.items():
        prop_schema = props.get(k)
        if isinstance(prop_schema, Mapping):
            _validate_value(str(k), v, prop_schema, errors)
    return errors


def format_schema_correction(tool_name: str, errors: Sequence[str]) -> str:
    """Build a single concise field-level correction prompt for the model."""
    joined = "；".join(errors[:6])
    return f"【上一次的回答不能用】工具 {tool_name} 的参数未通过 Schema 校验：{joined}。请按工具定义修正参数并重新调用。"
