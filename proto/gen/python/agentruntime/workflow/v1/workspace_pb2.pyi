from agentruntime.common.v1 import error_pb2 as _error_pb2
from agentruntime.common.v1 import tenant_pb2 as _tenant_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class WorkspaceOperation(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    WORKSPACE_OPERATION_UNSPECIFIED: _ClassVar[WorkspaceOperation]
    WORKSPACE_OPERATION_CREATE: _ClassVar[WorkspaceOperation]
    WORKSPACE_OPERATION_DIFF: _ClassVar[WorkspaceOperation]
    WORKSPACE_OPERATION_COMMIT: _ClassVar[WorkspaceOperation]
    WORKSPACE_OPERATION_PUSH: _ClassVar[WorkspaceOperation]
    WORKSPACE_OPERATION_ARCHIVE: _ClassVar[WorkspaceOperation]
    WORKSPACE_OPERATION_DELETE: _ClassVar[WorkspaceOperation]
WORKSPACE_OPERATION_UNSPECIFIED: WorkspaceOperation
WORKSPACE_OPERATION_CREATE: WorkspaceOperation
WORKSPACE_OPERATION_DIFF: WorkspaceOperation
WORKSPACE_OPERATION_COMMIT: WorkspaceOperation
WORKSPACE_OPERATION_PUSH: WorkspaceOperation
WORKSPACE_OPERATION_ARCHIVE: WorkspaceOperation
WORKSPACE_OPERATION_DELETE: WorkspaceOperation

class WorkspaceOperationInput(_message.Message):
    __slots__ = ("tenant", "operation_id", "operation", "workspace_id", "args", "sealed_credential", "requested_by")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    OPERATION_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    SEALED_CREDENTIAL_FIELD_NUMBER: _ClassVar[int]
    REQUESTED_BY_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    operation_id: str
    operation: WorkspaceOperation
    workspace_id: str
    args: _struct_pb2.Struct
    sealed_credential: str
    requested_by: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., operation_id: _Optional[str] = ..., operation: _Optional[_Union[WorkspaceOperation, str]] = ..., workspace_id: _Optional[str] = ..., args: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., sealed_credential: _Optional[str] = ..., requested_by: _Optional[str] = ...) -> None: ...

class WorkspaceOperationResult(_message.Message):
    __slots__ = ("result", "error")
    RESULT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    result: _struct_pb2.Struct
    error: _error_pb2.Error
    def __init__(self, result: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., error: _Optional[_Union[_error_pb2.Error, _Mapping]] = ...) -> None: ...
