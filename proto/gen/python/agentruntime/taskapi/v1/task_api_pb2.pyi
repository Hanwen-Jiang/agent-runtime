import datetime

from agentruntime.common.v1 import enums_pb2 as _enums_pb2
from agentruntime.common.v1 import metering_pb2 as _metering_pb2
from agentruntime.common.v1 import payload_pb2 as _payload_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class TaskEventType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TASK_EVENT_TYPE_UNSPECIFIED: _ClassVar[TaskEventType]
    TASK_EVENT_TYPE_TASK_ACCEPTED: _ClassVar[TaskEventType]
    TASK_EVENT_TYPE_ROUND_STARTED: _ClassVar[TaskEventType]
    TASK_EVENT_TYPE_STAGE_STARTED: _ClassVar[TaskEventType]
    TASK_EVENT_TYPE_NODE_STARTED: _ClassVar[TaskEventType]
    TASK_EVENT_TYPE_NODE_FINISHED: _ClassVar[TaskEventType]
    TASK_EVENT_TYPE_THINKING_DELTA: _ClassVar[TaskEventType]
    TASK_EVENT_TYPE_TOOL_STATUS: _ClassVar[TaskEventType]
    TASK_EVENT_TYPE_TOKEN_DELTA: _ClassVar[TaskEventType]
    TASK_EVENT_TYPE_HUMAN_INPUT_REQUIRED: _ClassVar[TaskEventType]
    TASK_EVENT_TYPE_VERDICT: _ClassVar[TaskEventType]
    TASK_EVENT_TYPE_TASK_FINISHED: _ClassVar[TaskEventType]
    TASK_EVENT_TYPE_HEARTBEAT: _ClassVar[TaskEventType]
TASK_EVENT_TYPE_UNSPECIFIED: TaskEventType
TASK_EVENT_TYPE_TASK_ACCEPTED: TaskEventType
TASK_EVENT_TYPE_ROUND_STARTED: TaskEventType
TASK_EVENT_TYPE_STAGE_STARTED: TaskEventType
TASK_EVENT_TYPE_NODE_STARTED: TaskEventType
TASK_EVENT_TYPE_NODE_FINISHED: TaskEventType
TASK_EVENT_TYPE_THINKING_DELTA: TaskEventType
TASK_EVENT_TYPE_TOOL_STATUS: TaskEventType
TASK_EVENT_TYPE_TOKEN_DELTA: TaskEventType
TASK_EVENT_TYPE_HUMAN_INPUT_REQUIRED: TaskEventType
TASK_EVENT_TYPE_VERDICT: TaskEventType
TASK_EVENT_TYPE_TASK_FINISHED: TaskEventType
TASK_EVENT_TYPE_HEARTBEAT: TaskEventType

class SubmitTaskRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id", "session_id", "client_request_id", "message", "inputs", "attachments", "session_access", "priority", "effect_ceiling", "network_request")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    CLIENT_REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    INPUTS_FIELD_NUMBER: _ClassVar[int]
    ATTACHMENTS_FIELD_NUMBER: _ClassVar[int]
    SESSION_ACCESS_FIELD_NUMBER: _ClassVar[int]
    PRIORITY_FIELD_NUMBER: _ClassVar[int]
    EFFECT_CEILING_FIELD_NUMBER: _ClassVar[int]
    NETWORK_REQUEST_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    session_id: str
    client_request_id: str
    message: str
    inputs: _struct_pb2.Struct
    attachments: _containers.RepeatedScalarFieldContainer[str]
    session_access: _enums_pb2.SessionAccess
    priority: _enums_pb2.Priority
    effect_ceiling: _enums_pb2.Effect
    network_request: NetworkRequest
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., session_id: _Optional[str] = ..., client_request_id: _Optional[str] = ..., message: _Optional[str] = ..., inputs: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., attachments: _Optional[_Iterable[str]] = ..., session_access: _Optional[_Union[_enums_pb2.SessionAccess, str]] = ..., priority: _Optional[_Union[_enums_pb2.Priority, str]] = ..., effect_ceiling: _Optional[_Union[_enums_pb2.Effect, str]] = ..., network_request: _Optional[_Union[NetworkRequest, _Mapping]] = ...) -> None: ...

class NetworkRequest(_message.Message):
    __slots__ = ("hosts", "ports", "minutes", "reason")
    HOSTS_FIELD_NUMBER: _ClassVar[int]
    PORTS_FIELD_NUMBER: _ClassVar[int]
    MINUTES_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    hosts: _containers.RepeatedScalarFieldContainer[str]
    ports: _containers.RepeatedScalarFieldContainer[int]
    minutes: int
    reason: str
    def __init__(self, hosts: _Optional[_Iterable[str]] = ..., ports: _Optional[_Iterable[int]] = ..., minutes: _Optional[int] = ..., reason: _Optional[str] = ...) -> None: ...

class SubmitTaskResponse(_message.Message):
    __slots__ = ("task_id", "session_id", "events_url", "deduplicated")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    EVENTS_URL_FIELD_NUMBER: _ClassVar[int]
    DEDUPLICATED_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    session_id: str
    events_url: str
    deduplicated: bool
    def __init__(self, task_id: _Optional[str] = ..., session_id: _Optional[str] = ..., events_url: _Optional[str] = ..., deduplicated: _Optional[bool] = ...) -> None: ...

class CancelTaskRequest(_message.Message):
    __slots__ = ("tenant_id", "task_id", "reason")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    task_id: str
    reason: str
    def __init__(self, tenant_id: _Optional[str] = ..., task_id: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class CancelTaskResponse(_message.Message):
    __slots__ = ("status",)
    STATUS_FIELD_NUMBER: _ClassVar[int]
    status: _enums_pb2.TaskStatus
    def __init__(self, status: _Optional[_Union[_enums_pb2.TaskStatus, str]] = ...) -> None: ...

class SignalTaskRequest(_message.Message):
    __slots__ = ("tenant_id", "task_id", "human_reply")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    HUMAN_REPLY_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    task_id: str
    human_reply: HumanReply
    def __init__(self, tenant_id: _Optional[str] = ..., task_id: _Optional[str] = ..., human_reply: _Optional[_Union[HumanReply, _Mapping]] = ...) -> None: ...

class SignalTaskResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class HumanReply(_message.Message):
    __slots__ = ("question_id", "option", "guidance")
    QUESTION_ID_FIELD_NUMBER: _ClassVar[int]
    OPTION_FIELD_NUMBER: _ClassVar[int]
    GUIDANCE_FIELD_NUMBER: _ClassVar[int]
    question_id: str
    option: str
    guidance: str
    def __init__(self, question_id: _Optional[str] = ..., option: _Optional[str] = ..., guidance: _Optional[str] = ...) -> None: ...

class QueryTaskRequest(_message.Message):
    __slots__ = ("tenant_id", "task_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    task_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., task_id: _Optional[str] = ...) -> None: ...

class QueryTaskResponse(_message.Message):
    __slots__ = ("task_id", "session_id", "status", "round_seq", "task_state", "last_event_id", "usage")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ROUND_SEQ_FIELD_NUMBER: _ClassVar[int]
    TASK_STATE_FIELD_NUMBER: _ClassVar[int]
    LAST_EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    session_id: str
    status: _enums_pb2.TaskStatus
    round_seq: int
    task_state: _payload_pb2.JsonPayload
    last_event_id: int
    usage: _metering_pb2.Usage
    def __init__(self, task_id: _Optional[str] = ..., session_id: _Optional[str] = ..., status: _Optional[_Union[_enums_pb2.TaskStatus, str]] = ..., round_seq: _Optional[int] = ..., task_state: _Optional[_Union[_payload_pb2.JsonPayload, _Mapping]] = ..., last_event_id: _Optional[int] = ..., usage: _Optional[_Union[_metering_pb2.Usage, _Mapping]] = ...) -> None: ...

class TaskEvent(_message.Message):
    __slots__ = ("event_id", "task_id", "session_id", "type", "emitted_at", "payload")
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    EMITTED_AT_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    event_id: int
    task_id: str
    session_id: str
    type: TaskEventType
    emitted_at: _timestamp_pb2.Timestamp
    payload: _payload_pb2.JsonPayload
    def __init__(self, event_id: _Optional[int] = ..., task_id: _Optional[str] = ..., session_id: _Optional[str] = ..., type: _Optional[_Union[TaskEventType, str]] = ..., emitted_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., payload: _Optional[_Union[_payload_pb2.JsonPayload, _Mapping]] = ...) -> None: ...
