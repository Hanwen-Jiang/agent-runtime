import datetime

from agentruntime.common.v1 import enums_pb2 as _enums_pb2
from agentruntime.common.v1 import error_pb2 as _error_pb2
from agentruntime.common.v1 import metering_pb2 as _metering_pb2
from agentruntime.common.v1 import payload_pb2 as _payload_pb2
from agentruntime.common.v1 import tenant_pb2 as _tenant_pb2
from google.protobuf import duration_pb2 as _duration_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class TaskGuidanceKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TASK_GUIDANCE_KIND_UNSPECIFIED: _ClassVar[TaskGuidanceKind]
    TASK_GUIDANCE_KIND_SUBMIT: _ClassVar[TaskGuidanceKind]
    TASK_GUIDANCE_KIND_EDIT: _ClassVar[TaskGuidanceKind]
    TASK_GUIDANCE_KIND_RETRACT: _ClassVar[TaskGuidanceKind]
TASK_GUIDANCE_KIND_UNSPECIFIED: TaskGuidanceKind
TASK_GUIDANCE_KIND_SUBMIT: TaskGuidanceKind
TASK_GUIDANCE_KIND_EDIT: TaskGuidanceKind
TASK_GUIDANCE_KIND_RETRACT: TaskGuidanceKind

class TaskWorkflowInput(_message.Message):
    __slots__ = ("tenant", "task_id", "pinned_versions", "message", "inputs", "attachments", "submitted_at", "session_access", "priority", "budget", "effect_ceiling", "parent_task_id", "depth")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    PINNED_VERSIONS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    INPUTS_FIELD_NUMBER: _ClassVar[int]
    ATTACHMENTS_FIELD_NUMBER: _ClassVar[int]
    SUBMITTED_AT_FIELD_NUMBER: _ClassVar[int]
    SESSION_ACCESS_FIELD_NUMBER: _ClassVar[int]
    PRIORITY_FIELD_NUMBER: _ClassVar[int]
    BUDGET_FIELD_NUMBER: _ClassVar[int]
    EFFECT_CEILING_FIELD_NUMBER: _ClassVar[int]
    PARENT_TASK_ID_FIELD_NUMBER: _ClassVar[int]
    DEPTH_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    task_id: str
    pinned_versions: PinnedVersions
    message: str
    inputs: _struct_pb2.Struct
    attachments: _containers.RepeatedScalarFieldContainer[str]
    submitted_at: _timestamp_pb2.Timestamp
    session_access: _enums_pb2.SessionAccess
    priority: _enums_pb2.Priority
    budget: Budget
    effect_ceiling: _enums_pb2.Effect
    parent_task_id: str
    depth: int
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., task_id: _Optional[str] = ..., pinned_versions: _Optional[_Union[PinnedVersions, _Mapping]] = ..., message: _Optional[str] = ..., inputs: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., attachments: _Optional[_Iterable[str]] = ..., submitted_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., session_access: _Optional[_Union[_enums_pb2.SessionAccess, str]] = ..., priority: _Optional[_Union[_enums_pb2.Priority, str]] = ..., budget: _Optional[_Union[Budget, _Mapping]] = ..., effect_ceiling: _Optional[_Union[_enums_pb2.Effect, str]] = ..., parent_task_id: _Optional[str] = ..., depth: _Optional[int] = ...) -> None: ...

class PinnedVersions(_message.Message):
    __slots__ = ("recipe", "node_templates", "reasoning_templates", "model_profiles", "tenant_policy")
    class NodeTemplatesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    class ReasoningTemplatesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    class ModelProfilesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    RECIPE_FIELD_NUMBER: _ClassVar[int]
    NODE_TEMPLATES_FIELD_NUMBER: _ClassVar[int]
    REASONING_TEMPLATES_FIELD_NUMBER: _ClassVar[int]
    MODEL_PROFILES_FIELD_NUMBER: _ClassVar[int]
    TENANT_POLICY_FIELD_NUMBER: _ClassVar[int]
    recipe: str
    node_templates: _containers.ScalarMap[str, str]
    reasoning_templates: _containers.ScalarMap[str, str]
    model_profiles: _containers.ScalarMap[str, str]
    tenant_policy: str
    def __init__(self, recipe: _Optional[str] = ..., node_templates: _Optional[_Mapping[str, str]] = ..., reasoning_templates: _Optional[_Mapping[str, str]] = ..., model_profiles: _Optional[_Mapping[str, str]] = ..., tenant_policy: _Optional[str] = ...) -> None: ...

class Budget(_message.Message):
    __slots__ = ("usd_micros", "tool_calls", "wall_clock")
    USD_MICROS_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALLS_FIELD_NUMBER: _ClassVar[int]
    WALL_CLOCK_FIELD_NUMBER: _ClassVar[int]
    usd_micros: int
    tool_calls: int
    wall_clock: _duration_pb2.Duration
    def __init__(self, usd_micros: _Optional[int] = ..., tool_calls: _Optional[int] = ..., wall_clock: _Optional[_Union[datetime.timedelta, _duration_pb2.Duration, _Mapping]] = ...) -> None: ...

class TaskWorkflowResult(_message.Message):
    __slots__ = ("task_id", "status", "final_verdict", "deliver_node_id", "deliverable", "artifacts", "rounds", "usage", "error")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    FINAL_VERDICT_FIELD_NUMBER: _ClassVar[int]
    DELIVER_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    DELIVERABLE_FIELD_NUMBER: _ClassVar[int]
    ARTIFACTS_FIELD_NUMBER: _ClassVar[int]
    ROUNDS_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    status: _enums_pb2.TaskStatus
    final_verdict: _enums_pb2.Verdict
    deliver_node_id: str
    deliverable: _payload_pb2.JsonPayload
    artifacts: _containers.RepeatedScalarFieldContainer[str]
    rounds: int
    usage: _metering_pb2.Usage
    error: _error_pb2.Error
    def __init__(self, task_id: _Optional[str] = ..., status: _Optional[_Union[_enums_pb2.TaskStatus, str]] = ..., final_verdict: _Optional[_Union[_enums_pb2.Verdict, str]] = ..., deliver_node_id: _Optional[str] = ..., deliverable: _Optional[_Union[_payload_pb2.JsonPayload, _Mapping]] = ..., artifacts: _Optional[_Iterable[str]] = ..., rounds: _Optional[int] = ..., usage: _Optional[_Union[_metering_pb2.Usage, _Mapping]] = ..., error: _Optional[_Union[_error_pb2.Error, _Mapping]] = ...) -> None: ...

class TaskStateQueryResult(_message.Message):
    __slots__ = ("task_id", "status", "round_seq", "task_state")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ROUND_SEQ_FIELD_NUMBER: _ClassVar[int]
    TASK_STATE_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    status: _enums_pb2.TaskStatus
    round_seq: int
    task_state: _payload_pb2.JsonPayload
    def __init__(self, task_id: _Optional[str] = ..., status: _Optional[_Union[_enums_pb2.TaskStatus, str]] = ..., round_seq: _Optional[int] = ..., task_state: _Optional[_Union[_payload_pb2.JsonPayload, _Mapping]] = ...) -> None: ...

class HumanReplySignal(_message.Message):
    __slots__ = ("question_id", "option", "guidance", "replied_by_user_id", "replied_at")
    QUESTION_ID_FIELD_NUMBER: _ClassVar[int]
    OPTION_FIELD_NUMBER: _ClassVar[int]
    GUIDANCE_FIELD_NUMBER: _ClassVar[int]
    REPLIED_BY_USER_ID_FIELD_NUMBER: _ClassVar[int]
    REPLIED_AT_FIELD_NUMBER: _ClassVar[int]
    question_id: str
    option: str
    guidance: str
    replied_by_user_id: str
    replied_at: _timestamp_pb2.Timestamp
    def __init__(self, question_id: _Optional[str] = ..., option: _Optional[str] = ..., guidance: _Optional[str] = ..., replied_by_user_id: _Optional[str] = ..., replied_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class TaskGuidanceSignal(_message.Message):
    __slots__ = ("guidance_id", "kind", "text", "mode", "by_user_id", "at")
    GUIDANCE_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    BY_USER_ID_FIELD_NUMBER: _ClassVar[int]
    AT_FIELD_NUMBER: _ClassVar[int]
    guidance_id: str
    kind: TaskGuidanceKind
    text: str
    mode: _enums_pb2.GuidanceMode
    by_user_id: str
    at: _timestamp_pb2.Timestamp
    def __init__(self, guidance_id: _Optional[str] = ..., kind: _Optional[_Union[TaskGuidanceKind, str]] = ..., text: _Optional[str] = ..., mode: _Optional[_Union[_enums_pb2.GuidanceMode, str]] = ..., by_user_id: _Optional[str] = ..., at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...
