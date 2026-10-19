"""加载 buf 生成的 Python 代码，按描述符遍历契约。

不依赖具体的 .proto 文件名：遍历 agentruntime 包下所有 *_pb2 模块，导入后从描述符池里取。
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterator
from functools import cache
from pathlib import Path

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf.descriptor import (
    Descriptor,
    EnumDescriptor,
    FieldDescriptor,
    FileDescriptor,
    MethodDescriptor,
    ServiceDescriptor,
)
from google.protobuf.message import Message

ROOT_PACKAGE = "agentruntime"


@cache
def generated_modules(suffix: str = "_pb2") -> tuple[str, ...]:
    """agentruntime 包下以 suffix 结尾的生成模块名，例如 agentruntime.common.v1.tenant_pb2。"""
    package = importlib.import_module(ROOT_PACKAGE)
    names: set[str] = set()
    for base in package.__path__:
        base_path = Path(base)
        for path in base_path.rglob(f"*{suffix}.py"):
            relative = path.relative_to(base_path.parent).with_suffix("")
            names.add(".".join(relative.parts))
    return tuple(sorted(names))


@cache
def files() -> tuple[FileDescriptor, ...]:
    loaded = [importlib.import_module(name).DESCRIPTOR for name in generated_modules("_pb2")]
    return tuple(sorted(loaded, key=lambda f: f.name))


def _pool() -> descriptor_pool.DescriptorPool:
    files()
    return descriptor_pool.Default()


def message(full_name: str) -> Descriptor:
    return _pool().FindMessageTypeByName(full_name)


def enum(full_name: str) -> EnumDescriptor:
    return _pool().FindEnumTypeByName(full_name)


def service(full_name: str) -> ServiceDescriptor:
    return _pool().FindServiceByName(full_name)


def message_class(full_name: str) -> type[Message]:
    return message_factory.GetMessageClass(message(full_name))


def messages() -> Iterator[Descriptor]:
    """所有消息，含嵌套消息，不含 map 的 entry。"""

    def walk(desc: Descriptor) -> Iterator[Descriptor]:
        yield desc
        for nested in desc.nested_types:
            if not nested.GetOptions().map_entry:
                yield from walk(nested)

    for file in files():
        for desc in file.message_types_by_name.values():
            yield from walk(desc)


def enums() -> Iterator[EnumDescriptor]:
    for file in files():
        yield from file.enum_types_by_name.values()
    for desc in messages():
        yield from desc.enum_types


def services() -> Iterator[ServiceDescriptor]:
    for file in files():
        yield from file.services_by_name.values()


def is_repeated(field: FieldDescriptor) -> bool:
    flag = getattr(field, "is_repeated", None)
    if isinstance(flag, bool):
        return flag
    return field.label == FieldDescriptor.LABEL_REPEATED


def is_map(field: FieldDescriptor) -> bool:
    return field.type == FieldDescriptor.TYPE_MESSAGE and field.message_type.GetOptions().map_entry


def message_type_name(field: FieldDescriptor) -> str | None:
    if field.type != FieldDescriptor.TYPE_MESSAGE:
        return None
    return field.message_type.full_name


def server_streaming(method: MethodDescriptor) -> bool:
    file_proto = descriptor_pb2.FileDescriptorProto()
    method.containing_service.file.CopyToProto(file_proto)
    for svc in file_proto.service:
        if svc.name == method.containing_service.name:
            for m in svc.method:
                if m.name == method.name:
                    return m.server_streaming
    raise LookupError(method.full_name)


ID_PREFIX = "agentruntime.common.v1.id_prefix"
WIRE_NAME = "agentruntime.common.v1.wire_name"


def option_values(desc: EnumDescriptor, extension: str) -> dict[str, str]:
    """枚举值名 → 自定义选项的值（未设置时为空串）；不含 0 值。"""
    ext = _pool().FindExtensionByName(extension)
    return {
        value.name: value.GetOptions().Extensions[ext]
        for value in desc.values
        if value.number != 0
    }


def upper_snake(name: str) -> str:
    """TriageLevel → TRIAGE_LEVEL，与 buf lint 的枚举值前缀规则一致。"""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).upper()


def lower_snake(name: str) -> str:
    return upper_snake(name).lower()


def enum_values(desc: EnumDescriptor) -> list[str]:
    """EFFECT_WRITE_REVERSIBLE → write_reversible；去掉 0 值 *_UNSPECIFIED。"""
    prefix = upper_snake(desc.name) + "_"
    values = []
    for value in desc.values:
        if value.number == 0:
            continue
        if not value.name.startswith(prefix):
            raise ValueError(f"{desc.full_name}.{value.name} 缺少前缀 {prefix}")
        values.append(value.name[len(prefix) :].lower())
    return values
