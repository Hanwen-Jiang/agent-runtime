from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from typing import ClassVar as _ClassVar

DESCRIPTOR: _descriptor.FileDescriptor

class Executor(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EXECUTOR_UNSPECIFIED: _ClassVar[Executor]
    EXECUTOR_MODEL: _ClassVar[Executor]
    EXECUTOR_PROGRAM: _ClassVar[Executor]
    EXECUTOR_HUMAN: _ClassVar[Executor]

class Effect(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EFFECT_UNSPECIFIED: _ClassVar[Effect]
    EFFECT_NONE: _ClassVar[Effect]
    EFFECT_READ: _ClassVar[Effect]
    EFFECT_WRITE_REVERSIBLE: _ClassVar[Effect]
    EFFECT_WRITE_IRREVERSIBLE: _ClassVar[Effect]

class Role(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ROLE_UNSPECIFIED: _ClassVar[Role]
    ROLE_FETCH: _ClassVar[Role]
    ROLE_TRANSFORM: _ClassVar[Role]
    ROLE_PLAN: _ClassVar[Role]
    ROLE_ROUTE: _ClassVar[Role]
    ROLE_JUDGE: _ClassVar[Role]
    ROLE_AGGREGATE: _ClassVar[Role]
    ROLE_ACT: _ClassVar[Role]
    ROLE_GATE: _ClassVar[Role]
    ROLE_DELIVER: _ClassVar[Role]

class Mode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MODE_UNSPECIFIED: _ClassVar[Mode]
    MODE_TEMPLATE: _ClassVar[Mode]
    MODE_TOOL_LOOP: _ClassVar[Mode]
    MODE_SUB_AGENT: _ClassVar[Mode]
    MODE_SUB_TASK: _ClassVar[Mode]

class Quality(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    QUALITY_UNSPECIFIED: _ClassVar[Quality]
    QUALITY_NONE: _ClassVar[Quality]
    QUALITY_REVISE: _ClassVar[Quality]
    QUALITY_ENSEMBLE: _ClassVar[Quality]
    QUALITY_SEARCH: _ClassVar[Quality]

class Reasoning(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    REASONING_UNSPECIFIED: _ClassVar[Reasoning]
    REASONING_DIRECT: _ClassVar[Reasoning]
    REASONING_STEPWISE: _ClassVar[Reasoning]
    REASONING_PLAN_FIRST: _ClassVar[Reasoning]
    REASONING_TEMPLATED: _ClassVar[Reasoning]

class Verdict(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    VERDICT_UNSPECIFIED: _ClassVar[Verdict]
    VERDICT_DONE: _ClassVar[Verdict]
    VERDICT_CONTINUE: _ClassVar[Verdict]
    VERDICT_REPAIR: _ClassVar[Verdict]
    VERDICT_REPLAN: _ClassVar[Verdict]
    VERDICT_ROLLBACK: _ClassVar[Verdict]
    VERDICT_REROUTE: _ClassVar[Verdict]
    VERDICT_ESCALATE: _ClassVar[Verdict]
    VERDICT_ABORT: _ClassVar[Verdict]

class TriageLevel(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TRIAGE_LEVEL_UNSPECIFIED: _ClassVar[TriageLevel]
    TRIAGE_LEVEL_FAST: _ClassVar[TriageLevel]
    TRIAGE_LEVEL_LIGHT: _ClassVar[TriageLevel]
    TRIAGE_LEVEL_FULL: _ClassVar[TriageLevel]
    TRIAGE_LEVEL_REPLAN: _ClassVar[TriageLevel]

class ModelOutput(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MODEL_OUTPUT_UNSPECIFIED: _ClassVar[ModelOutput]
    MODEL_OUTPUT_GENERATE: _ClassVar[ModelOutput]
    MODEL_OUTPUT_SCORE: _ClassVar[ModelOutput]
    MODEL_OUTPUT_EMBED: _ClassVar[ModelOutput]
    MODEL_OUTPUT_CLASSIFY: _ClassVar[ModelOutput]

class NodeStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    NODE_STATUS_UNSPECIFIED: _ClassVar[NodeStatus]
    NODE_STATUS_PENDING: _ClassVar[NodeStatus]
    NODE_STATUS_READY: _ClassVar[NodeStatus]
    NODE_STATUS_RUNNING: _ClassVar[NodeStatus]
    NODE_STATUS_SUCCEEDED: _ClassVar[NodeStatus]
    NODE_STATUS_FAILED: _ClassVar[NodeStatus]
    NODE_STATUS_SKIPPED: _ClassVar[NodeStatus]
    NODE_STATUS_CANCELLED: _ClassVar[NodeStatus]
    NODE_STATUS_UNKNOWN: _ClassVar[NodeStatus]

class FailureClass(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FAILURE_CLASS_UNSPECIFIED: _ClassVar[FailureClass]
    FAILURE_CLASS_TRANSIENT: _ClassVar[FailureClass]
    FAILURE_CLASS_INVALID_OUTPUT: _ClassVar[FailureClass]
    FAILURE_CLASS_DENIED: _ClassVar[FailureClass]
    FAILURE_CLASS_UPSTREAM_FAILED: _ClassVar[FailureClass]

class TaskStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TASK_STATUS_UNSPECIFIED: _ClassVar[TaskStatus]
    TASK_STATUS_QUEUED: _ClassVar[TaskStatus]
    TASK_STATUS_RUNNING: _ClassVar[TaskStatus]
    TASK_STATUS_DONE: _ClassVar[TaskStatus]
    TASK_STATUS_ABORTED: _ClassVar[TaskStatus]
    TASK_STATUS_CANCELLED: _ClassVar[TaskStatus]
    TASK_STATUS_FAILED: _ClassVar[TaskStatus]

class SessionAccess(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SESSION_ACCESS_UNSPECIFIED: _ClassVar[SessionAccess]
    SESSION_ACCESS_READ_ONLY: _ClassVar[SessionAccess]
    SESSION_ACCESS_READ_WRITE: _ClassVar[SessionAccess]

class Priority(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PRIORITY_UNSPECIFIED: _ClassVar[Priority]
    PRIORITY_INTERACTIVE: _ClassVar[Priority]
    PRIORITY_BATCH: _ClassVar[Priority]
EXECUTOR_UNSPECIFIED: Executor
EXECUTOR_MODEL: Executor
EXECUTOR_PROGRAM: Executor
EXECUTOR_HUMAN: Executor
EFFECT_UNSPECIFIED: Effect
EFFECT_NONE: Effect
EFFECT_READ: Effect
EFFECT_WRITE_REVERSIBLE: Effect
EFFECT_WRITE_IRREVERSIBLE: Effect
ROLE_UNSPECIFIED: Role
ROLE_FETCH: Role
ROLE_TRANSFORM: Role
ROLE_PLAN: Role
ROLE_ROUTE: Role
ROLE_JUDGE: Role
ROLE_AGGREGATE: Role
ROLE_ACT: Role
ROLE_GATE: Role
ROLE_DELIVER: Role
MODE_UNSPECIFIED: Mode
MODE_TEMPLATE: Mode
MODE_TOOL_LOOP: Mode
MODE_SUB_AGENT: Mode
MODE_SUB_TASK: Mode
QUALITY_UNSPECIFIED: Quality
QUALITY_NONE: Quality
QUALITY_REVISE: Quality
QUALITY_ENSEMBLE: Quality
QUALITY_SEARCH: Quality
REASONING_UNSPECIFIED: Reasoning
REASONING_DIRECT: Reasoning
REASONING_STEPWISE: Reasoning
REASONING_PLAN_FIRST: Reasoning
REASONING_TEMPLATED: Reasoning
VERDICT_UNSPECIFIED: Verdict
VERDICT_DONE: Verdict
VERDICT_CONTINUE: Verdict
VERDICT_REPAIR: Verdict
VERDICT_REPLAN: Verdict
VERDICT_ROLLBACK: Verdict
VERDICT_REROUTE: Verdict
VERDICT_ESCALATE: Verdict
VERDICT_ABORT: Verdict
TRIAGE_LEVEL_UNSPECIFIED: TriageLevel
TRIAGE_LEVEL_FAST: TriageLevel
TRIAGE_LEVEL_LIGHT: TriageLevel
TRIAGE_LEVEL_FULL: TriageLevel
TRIAGE_LEVEL_REPLAN: TriageLevel
MODEL_OUTPUT_UNSPECIFIED: ModelOutput
MODEL_OUTPUT_GENERATE: ModelOutput
MODEL_OUTPUT_SCORE: ModelOutput
MODEL_OUTPUT_EMBED: ModelOutput
MODEL_OUTPUT_CLASSIFY: ModelOutput
NODE_STATUS_UNSPECIFIED: NodeStatus
NODE_STATUS_PENDING: NodeStatus
NODE_STATUS_READY: NodeStatus
NODE_STATUS_RUNNING: NodeStatus
NODE_STATUS_SUCCEEDED: NodeStatus
NODE_STATUS_FAILED: NodeStatus
NODE_STATUS_SKIPPED: NodeStatus
NODE_STATUS_CANCELLED: NodeStatus
NODE_STATUS_UNKNOWN: NodeStatus
FAILURE_CLASS_UNSPECIFIED: FailureClass
FAILURE_CLASS_TRANSIENT: FailureClass
FAILURE_CLASS_INVALID_OUTPUT: FailureClass
FAILURE_CLASS_DENIED: FailureClass
FAILURE_CLASS_UPSTREAM_FAILED: FailureClass
TASK_STATUS_UNSPECIFIED: TaskStatus
TASK_STATUS_QUEUED: TaskStatus
TASK_STATUS_RUNNING: TaskStatus
TASK_STATUS_DONE: TaskStatus
TASK_STATUS_ABORTED: TaskStatus
TASK_STATUS_CANCELLED: TaskStatus
TASK_STATUS_FAILED: TaskStatus
SESSION_ACCESS_UNSPECIFIED: SessionAccess
SESSION_ACCESS_READ_ONLY: SessionAccess
SESSION_ACCESS_READ_WRITE: SessionAccess
PRIORITY_UNSPECIFIED: Priority
PRIORITY_INTERACTIVE: Priority
PRIORITY_BATCH: Priority
