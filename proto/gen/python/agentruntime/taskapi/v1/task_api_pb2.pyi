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

class TaskArtifactState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TASK_ARTIFACT_STATE_UNSPECIFIED: _ClassVar[TaskArtifactState]
    TASK_ARTIFACT_STATE_READY: _ClassVar[TaskArtifactState]
    TASK_ARTIFACT_STATE_EXPIRED: _ClassVar[TaskArtifactState]

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
    TASK_EVENT_TYPE_DECISION_RETRY: _ClassVar[TaskEventType]
    TASK_EVENT_TYPE_GUIDANCE_UPDATED: _ClassVar[TaskEventType]
    TASK_EVENT_TYPE_TASK_MESSAGE_EDITED: _ClassVar[TaskEventType]
    TASK_EVENT_TYPE_CONTEXT_USAGE: _ClassVar[TaskEventType]
    TASK_EVENT_TYPE_PLAN_UPDATED: _ClassVar[TaskEventType]
TASK_ARTIFACT_STATE_UNSPECIFIED: TaskArtifactState
TASK_ARTIFACT_STATE_READY: TaskArtifactState
TASK_ARTIFACT_STATE_EXPIRED: TaskArtifactState
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
TASK_EVENT_TYPE_DECISION_RETRY: TaskEventType
TASK_EVENT_TYPE_GUIDANCE_UPDATED: TaskEventType
TASK_EVENT_TYPE_TASK_MESSAGE_EDITED: TaskEventType
TASK_EVENT_TYPE_CONTEXT_USAGE: TaskEventType
TASK_EVENT_TYPE_PLAN_UPDATED: TaskEventType

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
    __slots__ = ("tenant_id", "task_id", "human_reply", "submit_guidance", "edit_guidance", "retract_guidance", "edit_queued_message")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    HUMAN_REPLY_FIELD_NUMBER: _ClassVar[int]
    SUBMIT_GUIDANCE_FIELD_NUMBER: _ClassVar[int]
    EDIT_GUIDANCE_FIELD_NUMBER: _ClassVar[int]
    RETRACT_GUIDANCE_FIELD_NUMBER: _ClassVar[int]
    EDIT_QUEUED_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    task_id: str
    human_reply: HumanReply
    submit_guidance: SubmitGuidance
    edit_guidance: EditGuidance
    retract_guidance: RetractGuidance
    edit_queued_message: EditQueuedMessage
    def __init__(self, tenant_id: _Optional[str] = ..., task_id: _Optional[str] = ..., human_reply: _Optional[_Union[HumanReply, _Mapping]] = ..., submit_guidance: _Optional[_Union[SubmitGuidance, _Mapping]] = ..., edit_guidance: _Optional[_Union[EditGuidance, _Mapping]] = ..., retract_guidance: _Optional[_Union[RetractGuidance, _Mapping]] = ..., edit_queued_message: _Optional[_Union[EditQueuedMessage, _Mapping]] = ...) -> None: ...

class SignalTaskResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class SubmitGuidance(_message.Message):
    __slots__ = ("guidance_id", "text", "mode")
    GUIDANCE_ID_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    guidance_id: str
    text: str
    mode: _enums_pb2.GuidanceMode
    def __init__(self, guidance_id: _Optional[str] = ..., text: _Optional[str] = ..., mode: _Optional[_Union[_enums_pb2.GuidanceMode, str]] = ...) -> None: ...

class EditGuidance(_message.Message):
    __slots__ = ("guidance_id", "text")
    GUIDANCE_ID_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    guidance_id: str
    text: str
    def __init__(self, guidance_id: _Optional[str] = ..., text: _Optional[str] = ...) -> None: ...

class RetractGuidance(_message.Message):
    __slots__ = ("guidance_id",)
    GUIDANCE_ID_FIELD_NUMBER: _ClassVar[int]
    guidance_id: str
    def __init__(self, guidance_id: _Optional[str] = ...) -> None: ...

class EditQueuedMessage(_message.Message):
    __slots__ = ("message",)
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    message: str
    def __init__(self, message: _Optional[str] = ...) -> None: ...

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

class ListTaskArtifactsRequest(_message.Message):
    __slots__ = ("tenant_id", "task_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    task_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., task_id: _Optional[str] = ...) -> None: ...

class TaskArtifact(_message.Message):
    __slots__ = ("artifact_id", "name", "path", "media_type", "size_bytes", "sha256", "created_at", "expires_at", "state", "download_path")
    ARTIFACT_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    MEDIA_TYPE_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    DOWNLOAD_PATH_FIELD_NUMBER: _ClassVar[int]
    artifact_id: str
    name: str
    path: str
    media_type: str
    size_bytes: int
    sha256: str
    created_at: _timestamp_pb2.Timestamp
    expires_at: _timestamp_pb2.Timestamp
    state: TaskArtifactState
    download_path: str
    def __init__(self, artifact_id: _Optional[str] = ..., name: _Optional[str] = ..., path: _Optional[str] = ..., media_type: _Optional[str] = ..., size_bytes: _Optional[int] = ..., sha256: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., expires_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., state: _Optional[_Union[TaskArtifactState, str]] = ..., download_path: _Optional[str] = ...) -> None: ...

class ListTaskArtifactsResponse(_message.Message):
    __slots__ = ("artifacts",)
    ARTIFACTS_FIELD_NUMBER: _ClassVar[int]
    artifacts: _containers.RepeatedCompositeFieldContainer[TaskArtifact]
    def __init__(self, artifacts: _Optional[_Iterable[_Union[TaskArtifact, _Mapping]]] = ...) -> None: ...

class ListSessionEventsRequest(_message.Message):
    __slots__ = ("tenant_id", "session_id", "cursors", "page_size")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    CURSORS_FIELD_NUMBER: _ClassVar[int]
    PAGE_SIZE_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    session_id: str
    cursors: _containers.RepeatedCompositeFieldContainer[TaskEventCursor]
    page_size: int
    def __init__(self, tenant_id: _Optional[str] = ..., session_id: _Optional[str] = ..., cursors: _Optional[_Iterable[_Union[TaskEventCursor, _Mapping]]] = ..., page_size: _Optional[int] = ...) -> None: ...

class TaskEventCursor(_message.Message):
    __slots__ = ("task_id", "after_event_id")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    AFTER_EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    after_event_id: int
    def __init__(self, task_id: _Optional[str] = ..., after_event_id: _Optional[int] = ...) -> None: ...

class ListSessionEventsResponse(_message.Message):
    __slots__ = ("tasks",)
    TASKS_FIELD_NUMBER: _ClassVar[int]
    tasks: _containers.RepeatedCompositeFieldContainer[TaskEvents]
    def __init__(self, tasks: _Optional[_Iterable[_Union[TaskEvents, _Mapping]]] = ...) -> None: ...

class TaskEvents(_message.Message):
    __slots__ = ("task_id", "found", "events", "next_after_event_id", "more", "events_unavailable", "finished")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    FOUND_FIELD_NUMBER: _ClassVar[int]
    EVENTS_FIELD_NUMBER: _ClassVar[int]
    NEXT_AFTER_EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    MORE_FIELD_NUMBER: _ClassVar[int]
    EVENTS_UNAVAILABLE_FIELD_NUMBER: _ClassVar[int]
    FINISHED_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    found: bool
    events: _containers.RepeatedCompositeFieldContainer[TaskEvent]
    next_after_event_id: int
    more: bool
    events_unavailable: bool
    finished: bool
    def __init__(self, task_id: _Optional[str] = ..., found: _Optional[bool] = ..., events: _Optional[_Iterable[_Union[TaskEvent, _Mapping]]] = ..., next_after_event_id: _Optional[int] = ..., more: _Optional[bool] = ..., events_unavailable: _Optional[bool] = ..., finished: _Optional[bool] = ...) -> None: ...

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
