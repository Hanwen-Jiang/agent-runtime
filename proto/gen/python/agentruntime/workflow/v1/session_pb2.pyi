from agentruntime.common.v1 import tenant_pb2 as _tenant_pb2
from agentruntime.workflow.v1 import task_pb2 as _task_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SessionWorkflowInput(_message.Message):
    __slots__ = ("tenant", "pending", "running_write_task_id", "running_read_only_task_ids", "submitted", "accepted_tasks", "run_generation")
    class SubmittedEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    TENANT_FIELD_NUMBER: _ClassVar[int]
    PENDING_FIELD_NUMBER: _ClassVar[int]
    RUNNING_WRITE_TASK_ID_FIELD_NUMBER: _ClassVar[int]
    RUNNING_READ_ONLY_TASK_IDS_FIELD_NUMBER: _ClassVar[int]
    SUBMITTED_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_TASKS_FIELD_NUMBER: _ClassVar[int]
    RUN_GENERATION_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    pending: _containers.RepeatedCompositeFieldContainer[SubmitTaskSignal]
    running_write_task_id: str
    running_read_only_task_ids: _containers.RepeatedScalarFieldContainer[str]
    submitted: _containers.ScalarMap[str, str]
    accepted_tasks: int
    run_generation: int
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., pending: _Optional[_Iterable[_Union[SubmitTaskSignal, _Mapping]]] = ..., running_write_task_id: _Optional[str] = ..., running_read_only_task_ids: _Optional[_Iterable[str]] = ..., submitted: _Optional[_Mapping[str, str]] = ..., accepted_tasks: _Optional[int] = ..., run_generation: _Optional[int] = ...) -> None: ...

class SubmitTaskSignal(_message.Message):
    __slots__ = ("client_request_id", "task")
    CLIENT_REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    TASK_FIELD_NUMBER: _ClassVar[int]
    client_request_id: str
    task: _task_pb2.TaskWorkflowInput
    def __init__(self, client_request_id: _Optional[str] = ..., task: _Optional[_Union[_task_pb2.TaskWorkflowInput, _Mapping]] = ...) -> None: ...

class CancelTaskSignal(_message.Message):
    __slots__ = ("task_id", "reason")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    reason: str
    def __init__(self, task_id: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class SessionState(_message.Message):
    __slots__ = ("session_id", "queued_task_ids", "running_write_task_id", "running_read_only_task_ids", "accepted_tasks", "run_generation")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    QUEUED_TASK_IDS_FIELD_NUMBER: _ClassVar[int]
    RUNNING_WRITE_TASK_ID_FIELD_NUMBER: _ClassVar[int]
    RUNNING_READ_ONLY_TASK_IDS_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_TASKS_FIELD_NUMBER: _ClassVar[int]
    RUN_GENERATION_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    queued_task_ids: _containers.RepeatedScalarFieldContainer[str]
    running_write_task_id: str
    running_read_only_task_ids: _containers.RepeatedScalarFieldContainer[str]
    accepted_tasks: int
    run_generation: int
    def __init__(self, session_id: _Optional[str] = ..., queued_task_ids: _Optional[_Iterable[str]] = ..., running_write_task_id: _Optional[str] = ..., running_read_only_task_ids: _Optional[_Iterable[str]] = ..., accepted_tasks: _Optional[int] = ..., run_generation: _Optional[int] = ...) -> None: ...
