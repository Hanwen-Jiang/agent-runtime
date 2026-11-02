import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class EnvironmentState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ENVIRONMENT_STATE_UNSPECIFIED: _ClassVar[EnvironmentState]
    ENVIRONMENT_STATE_CREATING: _ClassVar[EnvironmentState]
    ENVIRONMENT_STATE_READY: _ClassVar[EnvironmentState]
    ENVIRONMENT_STATE_DESTROYING: _ClassVar[EnvironmentState]
    ENVIRONMENT_STATE_DESTROYED: _ClassVar[EnvironmentState]
    ENVIRONMENT_STATE_FAILED: _ClassVar[EnvironmentState]
    ENVIRONMENT_STATE_LOST: _ClassVar[EnvironmentState]

class SetupState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SETUP_STATE_UNSPECIFIED: _ClassVar[SetupState]
    SETUP_STATE_NONE: _ClassVar[SetupState]
    SETUP_STATE_RUNNING: _ClassVar[SetupState]
    SETUP_STATE_SUCCEEDED: _ClassVar[SetupState]
    SETUP_STATE_CACHED: _ClassVar[SetupState]
    SETUP_STATE_FAILED: _ClassVar[SetupState]

class StartState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    START_STATE_UNSPECIFIED: _ClassVar[StartState]
    START_STATE_NONE: _ClassVar[StartState]
    START_STATE_SUCCEEDED: _ClassVar[StartState]
    START_STATE_FAILED: _ClassVar[StartState]
ENVIRONMENT_STATE_UNSPECIFIED: EnvironmentState
ENVIRONMENT_STATE_CREATING: EnvironmentState
ENVIRONMENT_STATE_READY: EnvironmentState
ENVIRONMENT_STATE_DESTROYING: EnvironmentState
ENVIRONMENT_STATE_DESTROYED: EnvironmentState
ENVIRONMENT_STATE_FAILED: EnvironmentState
ENVIRONMENT_STATE_LOST: EnvironmentState
SETUP_STATE_UNSPECIFIED: SetupState
SETUP_STATE_NONE: SetupState
SETUP_STATE_RUNNING: SetupState
SETUP_STATE_SUCCEEDED: SetupState
SETUP_STATE_CACHED: SetupState
SETUP_STATE_FAILED: SetupState
START_STATE_UNSPECIFIED: StartState
START_STATE_NONE: StartState
START_STATE_SUCCEEDED: StartState
START_STATE_FAILED: StartState

class EnvRecord(_message.Message):
    __slots__ = ("env_ref", "tenant_id", "workspace_id", "session_id", "backend", "backend_env_id", "kind", "isolation", "state", "created_at", "ready_at", "last_used_at", "expires_at", "max_until", "destroyed_at", "health_confirmed_at", "reused_at", "ready", "reused", "task_id", "writable", "runtime", "image", "environment_id", "setup_state", "setup_key", "setup_finished_at", "setup_error", "start_state")
    ENV_REF_FIELD_NUMBER: _ClassVar[int]
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    BACKEND_FIELD_NUMBER: _ClassVar[int]
    BACKEND_ENV_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    ISOLATION_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    READY_AT_FIELD_NUMBER: _ClassVar[int]
    LAST_USED_AT_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    MAX_UNTIL_FIELD_NUMBER: _ClassVar[int]
    DESTROYED_AT_FIELD_NUMBER: _ClassVar[int]
    HEALTH_CONFIRMED_AT_FIELD_NUMBER: _ClassVar[int]
    REUSED_AT_FIELD_NUMBER: _ClassVar[int]
    READY_FIELD_NUMBER: _ClassVar[int]
    REUSED_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    WRITABLE_FIELD_NUMBER: _ClassVar[int]
    RUNTIME_FIELD_NUMBER: _ClassVar[int]
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    ENVIRONMENT_ID_FIELD_NUMBER: _ClassVar[int]
    SETUP_STATE_FIELD_NUMBER: _ClassVar[int]
    SETUP_KEY_FIELD_NUMBER: _ClassVar[int]
    SETUP_FINISHED_AT_FIELD_NUMBER: _ClassVar[int]
    SETUP_ERROR_FIELD_NUMBER: _ClassVar[int]
    START_STATE_FIELD_NUMBER: _ClassVar[int]
    env_ref: str
    tenant_id: str
    workspace_id: str
    session_id: str
    backend: str
    backend_env_id: str
    kind: str
    isolation: str
    state: EnvironmentState
    created_at: _timestamp_pb2.Timestamp
    ready_at: _timestamp_pb2.Timestamp
    last_used_at: _timestamp_pb2.Timestamp
    expires_at: _timestamp_pb2.Timestamp
    max_until: _timestamp_pb2.Timestamp
    destroyed_at: _timestamp_pb2.Timestamp
    health_confirmed_at: _timestamp_pb2.Timestamp
    reused_at: _timestamp_pb2.Timestamp
    ready: bool
    reused: bool
    task_id: str
    writable: bool
    runtime: str
    image: str
    environment_id: str
    setup_state: SetupState
    setup_key: str
    setup_finished_at: _timestamp_pb2.Timestamp
    setup_error: str
    start_state: StartState
    def __init__(self, env_ref: _Optional[str] = ..., tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., session_id: _Optional[str] = ..., backend: _Optional[str] = ..., backend_env_id: _Optional[str] = ..., kind: _Optional[str] = ..., isolation: _Optional[str] = ..., state: _Optional[_Union[EnvironmentState, str]] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., ready_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., last_used_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., expires_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., max_until: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., destroyed_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., health_confirmed_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., reused_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., ready: _Optional[bool] = ..., reused: _Optional[bool] = ..., task_id: _Optional[str] = ..., writable: _Optional[bool] = ..., runtime: _Optional[str] = ..., image: _Optional[str] = ..., environment_id: _Optional[str] = ..., setup_state: _Optional[_Union[SetupState, str]] = ..., setup_key: _Optional[str] = ..., setup_finished_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., setup_error: _Optional[str] = ..., start_state: _Optional[_Union[StartState, str]] = ...) -> None: ...

class GetEnvironmentRequest(_message.Message):
    __slots__ = ("tenant_id", "env_ref")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    ENV_REF_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    env_ref: str
    def __init__(self, tenant_id: _Optional[str] = ..., env_ref: _Optional[str] = ...) -> None: ...

class GetEnvironmentResponse(_message.Message):
    __slots__ = ("environment",)
    ENVIRONMENT_FIELD_NUMBER: _ClassVar[int]
    environment: EnvRecord
    def __init__(self, environment: _Optional[_Union[EnvRecord, _Mapping]] = ...) -> None: ...

class ListEnvironmentsRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id", "session_id", "limit")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    session_id: str
    limit: int
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., session_id: _Optional[str] = ..., limit: _Optional[int] = ...) -> None: ...

class ListEnvironmentsResponse(_message.Message):
    __slots__ = ("environments", "truncated")
    ENVIRONMENTS_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    environments: _containers.RepeatedCompositeFieldContainer[EnvRecord]
    truncated: bool
    def __init__(self, environments: _Optional[_Iterable[_Union[EnvRecord, _Mapping]]] = ..., truncated: _Optional[bool] = ...) -> None: ...
