from agentruntime.common.v1 import options_pb2 as _options_pb2
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from typing import ClassVar as _ClassVar

DESCRIPTOR: _descriptor.FileDescriptor

class WorkflowType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    WORKFLOW_TYPE_UNSPECIFIED: _ClassVar[WorkflowType]
    WORKFLOW_TYPE_SESSION: _ClassVar[WorkflowType]
    WORKFLOW_TYPE_TASK: _ClassVar[WorkflowType]

class SignalName(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SIGNAL_NAME_UNSPECIFIED: _ClassVar[SignalName]
    SIGNAL_NAME_SUBMIT_TASK: _ClassVar[SignalName]
    SIGNAL_NAME_CANCEL_TASK: _ClassVar[SignalName]
    SIGNAL_NAME_HUMAN_REPLY: _ClassVar[SignalName]

class QueryName(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    QUERY_NAME_UNSPECIFIED: _ClassVar[QueryName]
    QUERY_NAME_SESSION_STATE: _ClassVar[QueryName]
    QUERY_NAME_TASK_STATE: _ClassVar[QueryName]

class TaskQueue(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TASK_QUEUE_UNSPECIFIED: _ClassVar[TaskQueue]
    TASK_QUEUE_SESSION: _ClassVar[TaskQueue]
    TASK_QUEUE_TASK: _ClassVar[TaskQueue]
    TASK_QUEUE_DECISION: _ClassVar[TaskQueue]
    TASK_QUEUE_MODEL: _ClassVar[TaskQueue]
    TASK_QUEUE_PROGRAM: _ClassVar[TaskQueue]
    TASK_QUEUE_OFFLINE: _ClassVar[TaskQueue]
WORKFLOW_TYPE_UNSPECIFIED: WorkflowType
WORKFLOW_TYPE_SESSION: WorkflowType
WORKFLOW_TYPE_TASK: WorkflowType
SIGNAL_NAME_UNSPECIFIED: SignalName
SIGNAL_NAME_SUBMIT_TASK: SignalName
SIGNAL_NAME_CANCEL_TASK: SignalName
SIGNAL_NAME_HUMAN_REPLY: SignalName
QUERY_NAME_UNSPECIFIED: QueryName
QUERY_NAME_SESSION_STATE: QueryName
QUERY_NAME_TASK_STATE: QueryName
TASK_QUEUE_UNSPECIFIED: TaskQueue
TASK_QUEUE_SESSION: TaskQueue
TASK_QUEUE_TASK: TaskQueue
TASK_QUEUE_DECISION: TaskQueue
TASK_QUEUE_MODEL: TaskQueue
TASK_QUEUE_PROGRAM: TaskQueue
TASK_QUEUE_OFFLINE: TaskQueue
