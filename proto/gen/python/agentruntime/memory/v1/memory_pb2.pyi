from agentruntime.common.v1 import payload_pb2 as _payload_pb2
from agentruntime.common.v1 import tenant_pb2 as _tenant_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class MemoryKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MEMORY_KIND_UNSPECIFIED: _ClassVar[MemoryKind]
    MEMORY_KIND_PREFERENCE: _ClassVar[MemoryKind]
    MEMORY_KIND_SUCCESS: _ClassVar[MemoryKind]
    MEMORY_KIND_FAILURE: _ClassVar[MemoryKind]
    MEMORY_KIND_FACT: _ClassVar[MemoryKind]
    MEMORY_KIND_RECIPE: _ClassVar[MemoryKind]

class SearchTrigger(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SEARCH_TRIGGER_UNSPECIFIED: _ClassVar[SearchTrigger]
    SEARCH_TRIGGER_PLANNING: _ClassVar[SearchTrigger]
    SEARCH_TRIGGER_NODE_START: _ClassVar[SearchTrigger]
    SEARCH_TRIGGER_ERROR: _ClassVar[SearchTrigger]
MEMORY_KIND_UNSPECIFIED: MemoryKind
MEMORY_KIND_PREFERENCE: MemoryKind
MEMORY_KIND_SUCCESS: MemoryKind
MEMORY_KIND_FAILURE: MemoryKind
MEMORY_KIND_FACT: MemoryKind
MEMORY_KIND_RECIPE: MemoryKind
SEARCH_TRIGGER_UNSPECIFIED: SearchTrigger
SEARCH_TRIGGER_PLANNING: SearchTrigger
SEARCH_TRIGGER_NODE_START: SearchTrigger
SEARCH_TRIGGER_ERROR: SearchTrigger

class SearchRequest(_message.Message):
    __slots__ = ("tenant", "trigger", "query", "task_family", "tools", "error_signature", "kinds", "token_budget", "top_k")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    TRIGGER_FIELD_NUMBER: _ClassVar[int]
    QUERY_FIELD_NUMBER: _ClassVar[int]
    TASK_FAMILY_FIELD_NUMBER: _ClassVar[int]
    TOOLS_FIELD_NUMBER: _ClassVar[int]
    ERROR_SIGNATURE_FIELD_NUMBER: _ClassVar[int]
    KINDS_FIELD_NUMBER: _ClassVar[int]
    TOKEN_BUDGET_FIELD_NUMBER: _ClassVar[int]
    TOP_K_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    trigger: SearchTrigger
    query: str
    task_family: str
    tools: _containers.RepeatedScalarFieldContainer[str]
    error_signature: str
    kinds: _containers.RepeatedScalarFieldContainer[MemoryKind]
    token_budget: int
    top_k: int
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., trigger: _Optional[_Union[SearchTrigger, str]] = ..., query: _Optional[str] = ..., task_family: _Optional[str] = ..., tools: _Optional[_Iterable[str]] = ..., error_signature: _Optional[str] = ..., kinds: _Optional[_Iterable[_Union[MemoryKind, str]]] = ..., token_budget: _Optional[int] = ..., top_k: _Optional[int] = ...) -> None: ...

class RetrievedItem(_message.Message):
    __slots__ = ("memory_item", "score")
    MEMORY_ITEM_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    memory_item: _payload_pb2.JsonPayload
    score: float
    def __init__(self, memory_item: _Optional[_Union[_payload_pb2.JsonPayload, _Mapping]] = ..., score: _Optional[float] = ...) -> None: ...

class SearchResponse(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[RetrievedItem]
    def __init__(self, items: _Optional[_Iterable[_Union[RetrievedItem, _Mapping]]] = ...) -> None: ...

class MemoryCandidate(_message.Message):
    __slots__ = ("memory_item", "source_task_id")
    MEMORY_ITEM_FIELD_NUMBER: _ClassVar[int]
    SOURCE_TASK_ID_FIELD_NUMBER: _ClassVar[int]
    memory_item: _payload_pb2.JsonPayload
    source_task_id: str
    def __init__(self, memory_item: _Optional[_Union[_payload_pb2.JsonPayload, _Mapping]] = ..., source_task_id: _Optional[str] = ...) -> None: ...

class SubmitCandidatesRequest(_message.Message):
    __slots__ = ("tenant", "candidates")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    CANDIDATES_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    candidates: _containers.RepeatedCompositeFieldContainer[MemoryCandidate]
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., candidates: _Optional[_Iterable[_Union[MemoryCandidate, _Mapping]]] = ...) -> None: ...

class SubmitCandidatesResponse(_message.Message):
    __slots__ = ("accepted",)
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    accepted: int
    def __init__(self, accepted: _Optional[int] = ...) -> None: ...

class Feedback(_message.Message):
    __slots__ = ("memory_id", "adopted", "task_succeeded")
    MEMORY_ID_FIELD_NUMBER: _ClassVar[int]
    ADOPTED_FIELD_NUMBER: _ClassVar[int]
    TASK_SUCCEEDED_FIELD_NUMBER: _ClassVar[int]
    memory_id: str
    adopted: bool
    task_succeeded: bool
    def __init__(self, memory_id: _Optional[str] = ..., adopted: _Optional[bool] = ..., task_succeeded: _Optional[bool] = ...) -> None: ...

class RecordFeedbackRequest(_message.Message):
    __slots__ = ("tenant", "task_id", "feedback")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    FEEDBACK_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    task_id: str
    feedback: _containers.RepeatedCompositeFieldContainer[Feedback]
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., task_id: _Optional[str] = ..., feedback: _Optional[_Iterable[_Union[Feedback, _Mapping]]] = ...) -> None: ...

class RecordFeedbackResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class DeleteRequest(_message.Message):
    __slots__ = ("tenant", "memory_id", "reason")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    MEMORY_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    memory_id: str
    reason: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., memory_id: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class DeleteResponse(_message.Message):
    __slots__ = ("deleted_memory_ids",)
    DELETED_MEMORY_IDS_FIELD_NUMBER: _ClassVar[int]
    deleted_memory_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, deleted_memory_ids: _Optional[_Iterable[str]] = ...) -> None: ...
