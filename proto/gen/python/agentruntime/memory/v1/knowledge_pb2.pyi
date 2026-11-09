from agentruntime.common.v1 import payload_pb2 as _payload_pb2
from agentruntime.common.v1 import tenant_pb2 as _tenant_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class KnowledgeServiceExecuteRequest(_message.Message):
    __slots__ = ("tenant", "operation", "payload")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    OPERATION_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    operation: str
    payload: _payload_pb2.JsonPayload
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., operation: _Optional[str] = ..., payload: _Optional[_Union[_payload_pb2.JsonPayload, _Mapping]] = ...) -> None: ...

class KnowledgeServiceExecuteResponse(_message.Message):
    __slots__ = ("result",)
    RESULT_FIELD_NUMBER: _ClassVar[int]
    result: _payload_pb2.JsonPayload
    def __init__(self, result: _Optional[_Union[_payload_pb2.JsonPayload, _Mapping]] = ...) -> None: ...
