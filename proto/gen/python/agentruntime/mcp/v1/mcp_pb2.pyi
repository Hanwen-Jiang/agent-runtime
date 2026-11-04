import datetime

from agentruntime.common.v1 import enums_pb2 as _enums_pb2
from agentruntime.common.v1 import payload_pb2 as _payload_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class McpConnectionState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MCP_CONNECTION_STATE_UNSPECIFIED: _ClassVar[McpConnectionState]
    MCP_CONNECTION_STATE_READY: _ClassVar[McpConnectionState]
    MCP_CONNECTION_STATE_ERROR: _ClassVar[McpConnectionState]
MCP_CONNECTION_STATE_UNSPECIFIED: McpConnectionState
MCP_CONNECTION_STATE_READY: McpConnectionState
MCP_CONNECTION_STATE_ERROR: McpConnectionState

class McpAuth(_message.Message):
    __slots__ = ("secret_name", "header")
    SECRET_NAME_FIELD_NUMBER: _ClassVar[int]
    HEADER_FIELD_NUMBER: _ClassVar[int]
    secret_name: str
    header: str
    def __init__(self, secret_name: _Optional[str] = ..., header: _Optional[str] = ...) -> None: ...

class McpToolAnnotations(_message.Message):
    __slots__ = ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint")
    READ_ONLY_HINT_FIELD_NUMBER: _ClassVar[int]
    DESTRUCTIVE_HINT_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENT_HINT_FIELD_NUMBER: _ClassVar[int]
    OPEN_WORLD_HINT_FIELD_NUMBER: _ClassVar[int]
    read_only_hint: bool
    destructive_hint: bool
    idempotent_hint: bool
    open_world_hint: bool
    def __init__(self, read_only_hint: _Optional[bool] = ..., destructive_hint: _Optional[bool] = ..., idempotent_hint: _Optional[bool] = ..., open_world_hint: _Optional[bool] = ...) -> None: ...

class McpTool(_message.Message):
    __slots__ = ("tool", "name", "description", "input_schema", "schema_digest", "annotations", "enabled", "effect", "timeout_s", "unavailable_reason", "confirmed_at")
    TOOL_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    INPUT_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_DIGEST_FIELD_NUMBER: _ClassVar[int]
    ANNOTATIONS_FIELD_NUMBER: _ClassVar[int]
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    EFFECT_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_S_FIELD_NUMBER: _ClassVar[int]
    UNAVAILABLE_REASON_FIELD_NUMBER: _ClassVar[int]
    CONFIRMED_AT_FIELD_NUMBER: _ClassVar[int]
    tool: str
    name: str
    description: str
    input_schema: _payload_pb2.JsonPayload
    schema_digest: str
    annotations: McpToolAnnotations
    enabled: bool
    effect: _enums_pb2.Effect
    timeout_s: int
    unavailable_reason: str
    confirmed_at: _timestamp_pb2.Timestamp
    def __init__(self, tool: _Optional[str] = ..., name: _Optional[str] = ..., description: _Optional[str] = ..., input_schema: _Optional[_Union[_payload_pb2.JsonPayload, _Mapping]] = ..., schema_digest: _Optional[str] = ..., annotations: _Optional[_Union[McpToolAnnotations, _Mapping]] = ..., enabled: _Optional[bool] = ..., effect: _Optional[_Union[_enums_pb2.Effect, str]] = ..., timeout_s: _Optional[int] = ..., unavailable_reason: _Optional[str] = ..., confirmed_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class McpConnection(_message.Message):
    __slots__ = ("connection_id", "name", "url", "auth", "state", "error_code", "server_name", "server_version", "tools", "created_at", "refreshed_at")
    CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    AUTH_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    SERVER_NAME_FIELD_NUMBER: _ClassVar[int]
    SERVER_VERSION_FIELD_NUMBER: _ClassVar[int]
    TOOLS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    REFRESHED_AT_FIELD_NUMBER: _ClassVar[int]
    connection_id: str
    name: str
    url: str
    auth: McpAuth
    state: McpConnectionState
    error_code: str
    server_name: str
    server_version: str
    tools: _containers.RepeatedCompositeFieldContainer[McpTool]
    created_at: _timestamp_pb2.Timestamp
    refreshed_at: _timestamp_pb2.Timestamp
    def __init__(self, connection_id: _Optional[str] = ..., name: _Optional[str] = ..., url: _Optional[str] = ..., auth: _Optional[_Union[McpAuth, _Mapping]] = ..., state: _Optional[_Union[McpConnectionState, str]] = ..., error_code: _Optional[str] = ..., server_name: _Optional[str] = ..., server_version: _Optional[str] = ..., tools: _Optional[_Iterable[_Union[McpTool, _Mapping]]] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., refreshed_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class CreateMcpConnectionRequest(_message.Message):
    __slots__ = ("tenant_id", "name", "url", "auth")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    AUTH_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    name: str
    url: str
    auth: McpAuth
    def __init__(self, tenant_id: _Optional[str] = ..., name: _Optional[str] = ..., url: _Optional[str] = ..., auth: _Optional[_Union[McpAuth, _Mapping]] = ...) -> None: ...

class CreateMcpConnectionResponse(_message.Message):
    __slots__ = ("connection", "created")
    CONNECTION_FIELD_NUMBER: _ClassVar[int]
    CREATED_FIELD_NUMBER: _ClassVar[int]
    connection: McpConnection
    created: bool
    def __init__(self, connection: _Optional[_Union[McpConnection, _Mapping]] = ..., created: _Optional[bool] = ...) -> None: ...

class ListMcpConnectionsRequest(_message.Message):
    __slots__ = ("tenant_id",)
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    def __init__(self, tenant_id: _Optional[str] = ...) -> None: ...

class ListMcpConnectionsResponse(_message.Message):
    __slots__ = ("connections",)
    CONNECTIONS_FIELD_NUMBER: _ClassVar[int]
    connections: _containers.RepeatedCompositeFieldContainer[McpConnection]
    def __init__(self, connections: _Optional[_Iterable[_Union[McpConnection, _Mapping]]] = ...) -> None: ...

class GetMcpConnectionRequest(_message.Message):
    __slots__ = ("tenant_id", "connection_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    connection_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., connection_id: _Optional[str] = ...) -> None: ...

class GetMcpConnectionResponse(_message.Message):
    __slots__ = ("connection",)
    CONNECTION_FIELD_NUMBER: _ClassVar[int]
    connection: McpConnection
    def __init__(self, connection: _Optional[_Union[McpConnection, _Mapping]] = ...) -> None: ...

class RefreshMcpToolsRequest(_message.Message):
    __slots__ = ("tenant_id", "connection_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    connection_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., connection_id: _Optional[str] = ...) -> None: ...

class RefreshMcpToolsResponse(_message.Message):
    __slots__ = ("connection", "added", "disabled", "removed")
    CONNECTION_FIELD_NUMBER: _ClassVar[int]
    ADDED_FIELD_NUMBER: _ClassVar[int]
    DISABLED_FIELD_NUMBER: _ClassVar[int]
    REMOVED_FIELD_NUMBER: _ClassVar[int]
    connection: McpConnection
    added: _containers.RepeatedScalarFieldContainer[str]
    disabled: _containers.RepeatedScalarFieldContainer[str]
    removed: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, connection: _Optional[_Union[McpConnection, _Mapping]] = ..., added: _Optional[_Iterable[str]] = ..., disabled: _Optional[_Iterable[str]] = ..., removed: _Optional[_Iterable[str]] = ...) -> None: ...

class McpToolSetting(_message.Message):
    __slots__ = ("tool", "enabled", "effect", "timeout_s", "schema_digest")
    TOOL_FIELD_NUMBER: _ClassVar[int]
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    EFFECT_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_S_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_DIGEST_FIELD_NUMBER: _ClassVar[int]
    tool: str
    enabled: bool
    effect: _enums_pb2.Effect
    timeout_s: int
    schema_digest: str
    def __init__(self, tool: _Optional[str] = ..., enabled: _Optional[bool] = ..., effect: _Optional[_Union[_enums_pb2.Effect, str]] = ..., timeout_s: _Optional[int] = ..., schema_digest: _Optional[str] = ...) -> None: ...

class ConfigureMcpToolsRequest(_message.Message):
    __slots__ = ("tenant_id", "connection_id", "tools")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    TOOLS_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    connection_id: str
    tools: _containers.RepeatedCompositeFieldContainer[McpToolSetting]
    def __init__(self, tenant_id: _Optional[str] = ..., connection_id: _Optional[str] = ..., tools: _Optional[_Iterable[_Union[McpToolSetting, _Mapping]]] = ...) -> None: ...

class ConfigureMcpToolsResponse(_message.Message):
    __slots__ = ("connection",)
    CONNECTION_FIELD_NUMBER: _ClassVar[int]
    connection: McpConnection
    def __init__(self, connection: _Optional[_Union[McpConnection, _Mapping]] = ...) -> None: ...

class DeleteMcpConnectionRequest(_message.Message):
    __slots__ = ("tenant_id", "connection_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    connection_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., connection_id: _Optional[str] = ...) -> None: ...

class DeleteMcpConnectionResponse(_message.Message):
    __slots__ = ("deleted",)
    DELETED_FIELD_NUMBER: _ClassVar[int]
    deleted: bool
    def __init__(self, deleted: _Optional[bool] = ...) -> None: ...
