from agentruntime.common.v1 import options_pb2 as _options_pb2
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from typing import ClassVar as _ClassVar

DESCRIPTOR: _descriptor.FileDescriptor

class IdKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ID_KIND_UNSPECIFIED: _ClassVar[IdKind]
    ID_KIND_SESSION: _ClassVar[IdKind]
    ID_KIND_TASK: _ClassVar[IdKind]
    ID_KIND_MEMORY: _ClassVar[IdKind]
    ID_KIND_TRACE: _ClassVar[IdKind]
ID_KIND_UNSPECIFIED: IdKind
ID_KIND_SESSION: IdKind
ID_KIND_TASK: IdKind
ID_KIND_MEMORY: IdKind
ID_KIND_TRACE: IdKind
