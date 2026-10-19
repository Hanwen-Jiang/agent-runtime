"""Temporal payload 的 Python 侧编解码，供跨语言往返测试使用。

Go 侧的对应实现是 tests/contract/go/cmd/payloadconv。
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from pathlib import Path

import temporalio.converter
from google.protobuf import json_format
from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message
from temporalio.api.common.v1 import Payload

from contract_tests import protos

ENCODING_JSON_PROTOBUF = b"json/protobuf"

_INT64_TYPES = {
    FieldDescriptor.TYPE_INT64,
    FieldDescriptor.TYPE_UINT64,
    FieldDescriptor.TYPE_SINT64,
    FieldDescriptor.TYPE_FIXED64,
    FieldDescriptor.TYPE_SFIXED64,
}
# 超过 2^53 的整数用 double 表示会丢精度，用来检查 int64 是否按字符串编码。
_DOUBLE_EXACT_LIMIT = 2**53


def data_converter() -> temporalio.converter.DataConverter:
    """Runtime 的 DataConverter。

    S-01 用 SDK 默认值：proto 消息优先编码为 json/protobuf。
    S-03 接入 py-worker 自己的 DataConverter、S-06 接入 Claim-Check 编解码后，改为调用服务里的同一个工厂函数。
    """
    return temporalio.converter.DataConverter.default


@dataclass(frozen=True)
class Fixture:
    type: str
    message: Message


def load_fixture(path: Path) -> Fixture:
    """fixture 文件格式：{"type": "<消息全名>", "message": <protojson>}。"""
    doc = json.loads(path.read_text("utf-8"))
    cls = protos.message_class(doc["type"])
    return Fixture(doc["type"], json_format.ParseDict(doc["message"], cls()))


def encode(message: Message) -> Payload:
    return data_converter().payload_converter.to_payloads([message])[0]


def decode(payload: Payload, type_name: str) -> Message:
    cls = protos.message_class(type_name)
    return data_converter().payload_converter.from_payloads([payload], [cls])[0]


def assert_json_protobuf(payload: Payload, type_name: str) -> None:
    encoding = payload.metadata.get("encoding")
    assert encoding == ENCODING_JSON_PROTOBUF, f"encoding = {encoding!r}，应为 json/protobuf"
    message_type = payload.metadata.get("messageType")
    assert message_type == type_name.encode(), f"messageType = {message_type!r}，应为 {type_name}"


def read_payload(path: Path) -> Payload:
    return json_format.Parse(path.read_text("utf-8"), Payload())


def write_payload(payload: Payload, path: Path) -> None:
    path.write_text(json_format.MessageToJson(payload), "utf-8")


def diff(expected: Message, actual: Message) -> str:
    def lines(msg: Message) -> list[str]:
        return json_format.MessageToJson(msg, sort_keys=True).splitlines(keepends=True)

    return "".join(difflib.unified_diff(lines(expected), lines(actual), "fixture", "decoded"))


def coverage(message: Message) -> set[str]:
    """消息里实际出现的“容易在跨语言 JSON 编码中出错”的值类型。

    只统计 Runtime 自己的字段；google.protobuf.Struct 内部的 map 与 repeated 单独记为 struct_*。
    """
    seen: set[str] = set()

    def visit(field: FieldDescriptor, value: object, owned: bool) -> None:
        if field.type == FieldDescriptor.TYPE_MESSAGE:
            name = field.message_type.full_name
            if name == "google.protobuf.Struct":
                seen.add("struct")
            elif name == "google.protobuf.ListValue":
                seen.add("struct_list")
            elif name == "google.protobuf.Timestamp":
                seen.add("timestamp")
            elif name == "google.protobuf.Duration":
                seen.add("duration")
            walk(value)  # type: ignore[arg-type]
        elif field.type == FieldDescriptor.TYPE_ENUM:
            if field.enum_type.full_name == "google.protobuf.NullValue":
                seen.add("struct_null")
            elif owned:
                seen.add("enum")
        elif field.type in _INT64_TYPES:
            if abs(value) > _DOUBLE_EXACT_LIMIT:  # type: ignore[arg-type]
                seen.add("int64_beyond_double")
        elif field.type == FieldDescriptor.TYPE_STRING:
            if any(ord(ch) > 127 for ch in value):  # type: ignore[union-attr]
                seen.add("non_ascii_string")
        elif field.type == FieldDescriptor.TYPE_BYTES:
            seen.add("bytes")

    def walk(msg: Message) -> None:
        owned = msg.DESCRIPTOR.full_name.startswith(protos.ROOT_PACKAGE + ".")
        for field, value in msg.ListFields():
            if protos.is_map(field):
                if owned:
                    seen.add("map")
                value_field = field.message_type.fields_by_name["value"]
                for item in value.values():
                    visit(value_field, item, owned)
            elif protos.is_repeated(field):
                if owned:
                    seen.add("repeated")
                for item in value:
                    visit(field, item, owned)
            else:
                visit(field, value, owned)

    walk(message)
    return seen
