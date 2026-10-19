from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ErrorCategory(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ERROR_CATEGORY_UNSPECIFIED: _ClassVar[ErrorCategory]
    ERROR_CATEGORY_TRANSIENT: _ClassVar[ErrorCategory]
    ERROR_CATEGORY_INVALID: _ClassVar[ErrorCategory]
    ERROR_CATEGORY_DENIED: _ClassVar[ErrorCategory]
    ERROR_CATEGORY_NOT_FOUND: _ClassVar[ErrorCategory]
    ERROR_CATEGORY_CONFLICT: _ClassVar[ErrorCategory]
ERROR_CATEGORY_UNSPECIFIED: ErrorCategory
ERROR_CATEGORY_TRANSIENT: ErrorCategory
ERROR_CATEGORY_INVALID: ErrorCategory
ERROR_CATEGORY_DENIED: ErrorCategory
ERROR_CATEGORY_NOT_FOUND: ErrorCategory
ERROR_CATEGORY_CONFLICT: ErrorCategory

class Error(_message.Message):
    __slots__ = ("category", "code", "message", "details")
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    DETAILS_FIELD_NUMBER: _ClassVar[int]
    category: ErrorCategory
    code: str
    message: str
    details: _struct_pb2.Struct
    def __init__(self, category: _Optional[_Union[ErrorCategory, str]] = ..., code: _Optional[str] = ..., message: _Optional[str] = ..., details: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...
