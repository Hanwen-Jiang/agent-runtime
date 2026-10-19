from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class TenantContext(_message.Message):
    __slots__ = ("tenant_id", "user_id", "workspace_id", "session_id", "trace")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TRACE_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    user_id: str
    workspace_id: str
    session_id: str
    trace: TraceContext
    def __init__(self, tenant_id: _Optional[str] = ..., user_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., session_id: _Optional[str] = ..., trace: _Optional[_Union[TraceContext, _Mapping]] = ...) -> None: ...

class TraceContext(_message.Message):
    __slots__ = ("traceparent", "tracestate")
    TRACEPARENT_FIELD_NUMBER: _ClassVar[int]
    TRACESTATE_FIELD_NUMBER: _ClassVar[int]
    traceparent: str
    tracestate: str
    def __init__(self, traceparent: _Optional[str] = ..., tracestate: _Optional[str] = ...) -> None: ...
