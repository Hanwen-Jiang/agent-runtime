from agentruntime.common.v1 import enums_pb2 as _enums_pb2
from agentruntime.common.v1 import metering_pb2 as _metering_pb2
from agentruntime.common.v1 import payload_pb2 as _payload_pb2
from agentruntime.common.v1 import tenant_pb2 as _tenant_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ChatRole(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CHAT_ROLE_UNSPECIFIED: _ClassVar[ChatRole]
    CHAT_ROLE_SYSTEM: _ClassVar[ChatRole]
    CHAT_ROLE_USER: _ClassVar[ChatRole]
    CHAT_ROLE_ASSISTANT: _ClassVar[ChatRole]
    CHAT_ROLE_TOOL: _ClassVar[ChatRole]

class FinishReason(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FINISH_REASON_UNSPECIFIED: _ClassVar[FinishReason]
    FINISH_REASON_STOP: _ClassVar[FinishReason]
    FINISH_REASON_MAX_TOKENS: _ClassVar[FinishReason]
    FINISH_REASON_TOOL_CALLS: _ClassVar[FinishReason]
    FINISH_REASON_CONTENT_FILTER: _ClassVar[FinishReason]
CHAT_ROLE_UNSPECIFIED: ChatRole
CHAT_ROLE_SYSTEM: ChatRole
CHAT_ROLE_USER: ChatRole
CHAT_ROLE_ASSISTANT: ChatRole
CHAT_ROLE_TOOL: ChatRole
FINISH_REASON_UNSPECIFIED: FinishReason
FINISH_REASON_STOP: FinishReason
FINISH_REASON_MAX_TOKENS: FinishReason
FINISH_REASON_TOOL_CALLS: FinishReason
FINISH_REASON_CONTENT_FILTER: FinishReason

class CallContext(_message.Message):
    __slots__ = ("task_id", "node_id", "role", "task_family", "tier", "reasoning", "pinned_model_profile", "avoid_vendor", "max_cost_usd_micros")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    TASK_FAMILY_FIELD_NUMBER: _ClassVar[int]
    TIER_FIELD_NUMBER: _ClassVar[int]
    REASONING_FIELD_NUMBER: _ClassVar[int]
    PINNED_MODEL_PROFILE_FIELD_NUMBER: _ClassVar[int]
    AVOID_VENDOR_FIELD_NUMBER: _ClassVar[int]
    MAX_COST_USD_MICROS_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    node_id: str
    role: _enums_pb2.Role
    task_family: str
    tier: str
    reasoning: _enums_pb2.Reasoning
    pinned_model_profile: str
    avoid_vendor: str
    max_cost_usd_micros: int
    def __init__(self, task_id: _Optional[str] = ..., node_id: _Optional[str] = ..., role: _Optional[_Union[_enums_pb2.Role, str]] = ..., task_family: _Optional[str] = ..., tier: _Optional[str] = ..., reasoning: _Optional[_Union[_enums_pb2.Reasoning, str]] = ..., pinned_model_profile: _Optional[str] = ..., avoid_vendor: _Optional[str] = ..., max_cost_usd_micros: _Optional[int] = ...) -> None: ...

class ModelChoice(_message.Message):
    __slots__ = ("model_profile_id", "tier", "vendor", "context_window")
    MODEL_PROFILE_ID_FIELD_NUMBER: _ClassVar[int]
    TIER_FIELD_NUMBER: _ClassVar[int]
    VENDOR_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_WINDOW_FIELD_NUMBER: _ClassVar[int]
    model_profile_id: str
    tier: str
    vendor: str
    context_window: int
    def __init__(self, model_profile_id: _Optional[str] = ..., tier: _Optional[str] = ..., vendor: _Optional[str] = ..., context_window: _Optional[int] = ...) -> None: ...

class ChatMessage(_message.Message):
    __slots__ = ("role", "text", "tool_calls", "tool_call_id", "cache_breakpoint", "parts")
    ROLE_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALLS_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    CACHE_BREAKPOINT_FIELD_NUMBER: _ClassVar[int]
    PARTS_FIELD_NUMBER: _ClassVar[int]
    role: ChatRole
    text: str
    tool_calls: _containers.RepeatedCompositeFieldContainer[ToolCall]
    tool_call_id: str
    cache_breakpoint: bool
    parts: _containers.RepeatedCompositeFieldContainer[ContentPart]
    def __init__(self, role: _Optional[_Union[ChatRole, str]] = ..., text: _Optional[str] = ..., tool_calls: _Optional[_Iterable[_Union[ToolCall, _Mapping]]] = ..., tool_call_id: _Optional[str] = ..., cache_breakpoint: _Optional[bool] = ..., parts: _Optional[_Iterable[_Union[ContentPart, _Mapping]]] = ...) -> None: ...

class ContentPart(_message.Message):
    __slots__ = ("text", "image")
    TEXT_FIELD_NUMBER: _ClassVar[int]
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    text: str
    image: ImagePart
    def __init__(self, text: _Optional[str] = ..., image: _Optional[_Union[ImagePart, _Mapping]] = ...) -> None: ...

class ImagePart(_message.Message):
    __slots__ = ("object_uri", "mime_type", "sha256", "detail")
    OBJECT_URI_FIELD_NUMBER: _ClassVar[int]
    MIME_TYPE_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    object_uri: str
    mime_type: str
    sha256: str
    detail: str
    def __init__(self, object_uri: _Optional[str] = ..., mime_type: _Optional[str] = ..., sha256: _Optional[str] = ..., detail: _Optional[str] = ...) -> None: ...

class GetCapabilitiesRequest(_message.Message):
    __slots__ = ("tenant",)
    TENANT_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ...) -> None: ...

class GetCapabilitiesResponse(_message.Message):
    __slots__ = ("image_input", "image_mime_types", "max_images", "max_image_bytes", "max_image_pixels", "image_token_reserve")
    IMAGE_INPUT_FIELD_NUMBER: _ClassVar[int]
    IMAGE_MIME_TYPES_FIELD_NUMBER: _ClassVar[int]
    MAX_IMAGES_FIELD_NUMBER: _ClassVar[int]
    MAX_IMAGE_BYTES_FIELD_NUMBER: _ClassVar[int]
    MAX_IMAGE_PIXELS_FIELD_NUMBER: _ClassVar[int]
    IMAGE_TOKEN_RESERVE_FIELD_NUMBER: _ClassVar[int]
    image_input: bool
    image_mime_types: _containers.RepeatedScalarFieldContainer[str]
    max_images: int
    max_image_bytes: int
    max_image_pixels: int
    image_token_reserve: int
    def __init__(self, image_input: _Optional[bool] = ..., image_mime_types: _Optional[_Iterable[str]] = ..., max_images: _Optional[int] = ..., max_image_bytes: _Optional[int] = ..., max_image_pixels: _Optional[int] = ..., image_token_reserve: _Optional[int] = ...) -> None: ...

class ToolSpec(_message.Message):
    __slots__ = ("name", "description", "params_schema")
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    PARAMS_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    name: str
    description: str
    params_schema: _payload_pb2.JsonPayload
    def __init__(self, name: _Optional[str] = ..., description: _Optional[str] = ..., params_schema: _Optional[_Union[_payload_pb2.JsonPayload, _Mapping]] = ...) -> None: ...

class ToolCall(_message.Message):
    __slots__ = ("id", "name", "arguments")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    ARGUMENTS_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    arguments: _struct_pb2.Struct
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., arguments: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class GenerateParams(_message.Message):
    __slots__ = ("messages", "tools", "output_schema", "max_output_tokens", "thinking_budget_tokens")
    MESSAGES_FIELD_NUMBER: _ClassVar[int]
    TOOLS_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    MAX_OUTPUT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    THINKING_BUDGET_TOKENS_FIELD_NUMBER: _ClassVar[int]
    messages: _containers.RepeatedCompositeFieldContainer[ChatMessage]
    tools: _containers.RepeatedCompositeFieldContainer[ToolSpec]
    output_schema: _payload_pb2.JsonPayload
    max_output_tokens: int
    thinking_budget_tokens: int
    def __init__(self, messages: _Optional[_Iterable[_Union[ChatMessage, _Mapping]]] = ..., tools: _Optional[_Iterable[_Union[ToolSpec, _Mapping]]] = ..., output_schema: _Optional[_Union[_payload_pb2.JsonPayload, _Mapping]] = ..., max_output_tokens: _Optional[int] = ..., thinking_budget_tokens: _Optional[int] = ...) -> None: ...

class GenerateRequest(_message.Message):
    __slots__ = ("tenant", "call", "generation")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    CALL_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    call: CallContext
    generation: GenerateParams
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., call: _Optional[_Union[CallContext, _Mapping]] = ..., generation: _Optional[_Union[GenerateParams, _Mapping]] = ...) -> None: ...

class GenerateResponse(_message.Message):
    __slots__ = ("text", "result", "tool_calls", "rationale", "finish_reason", "model", "usage")
    TEXT_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALLS_FIELD_NUMBER: _ClassVar[int]
    RATIONALE_FIELD_NUMBER: _ClassVar[int]
    FINISH_REASON_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    text: str
    result: _struct_pb2.Value
    tool_calls: _containers.RepeatedCompositeFieldContainer[ToolCall]
    rationale: str
    finish_reason: FinishReason
    model: ModelChoice
    usage: _metering_pb2.Usage
    def __init__(self, text: _Optional[str] = ..., result: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., tool_calls: _Optional[_Iterable[_Union[ToolCall, _Mapping]]] = ..., rationale: _Optional[str] = ..., finish_reason: _Optional[_Union[FinishReason, str]] = ..., model: _Optional[_Union[ModelChoice, _Mapping]] = ..., usage: _Optional[_Union[_metering_pb2.Usage, _Mapping]] = ...) -> None: ...

class StreamGenerateRequest(_message.Message):
    __slots__ = ("tenant", "call", "generation")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    CALL_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    call: CallContext
    generation: GenerateParams
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., call: _Optional[_Union[CallContext, _Mapping]] = ..., generation: _Optional[_Union[GenerateParams, _Mapping]] = ...) -> None: ...

class StreamGenerateResponse(_message.Message):
    __slots__ = ("text_delta", "thinking_delta", "tool_call", "final", "usage")
    TEXT_DELTA_FIELD_NUMBER: _ClassVar[int]
    THINKING_DELTA_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_FIELD_NUMBER: _ClassVar[int]
    FINAL_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    text_delta: str
    thinking_delta: str
    tool_call: ToolCall
    final: GenerateResponse
    usage: _metering_pb2.Usage
    def __init__(self, text_delta: _Optional[str] = ..., thinking_delta: _Optional[str] = ..., tool_call: _Optional[_Union[ToolCall, _Mapping]] = ..., final: _Optional[_Union[GenerateResponse, _Mapping]] = ..., usage: _Optional[_Union[_metering_pb2.Usage, _Mapping]] = ...) -> None: ...

class ScoreRequest(_message.Message):
    __slots__ = ("tenant", "call", "query", "candidates", "rubric")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    CALL_FIELD_NUMBER: _ClassVar[int]
    QUERY_FIELD_NUMBER: _ClassVar[int]
    CANDIDATES_FIELD_NUMBER: _ClassVar[int]
    RUBRIC_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    call: CallContext
    query: str
    candidates: _containers.RepeatedScalarFieldContainer[str]
    rubric: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., call: _Optional[_Union[CallContext, _Mapping]] = ..., query: _Optional[str] = ..., candidates: _Optional[_Iterable[str]] = ..., rubric: _Optional[str] = ...) -> None: ...

class ScoreResponse(_message.Message):
    __slots__ = ("scores", "model", "usage")
    SCORES_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    scores: _containers.RepeatedScalarFieldContainer[float]
    model: ModelChoice
    usage: _metering_pb2.Usage
    def __init__(self, scores: _Optional[_Iterable[float]] = ..., model: _Optional[_Union[ModelChoice, _Mapping]] = ..., usage: _Optional[_Union[_metering_pb2.Usage, _Mapping]] = ...) -> None: ...

class EmbedRequest(_message.Message):
    __slots__ = ("tenant", "call", "texts", "embedding_version")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    CALL_FIELD_NUMBER: _ClassVar[int]
    TEXTS_FIELD_NUMBER: _ClassVar[int]
    EMBEDDING_VERSION_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    call: CallContext
    texts: _containers.RepeatedScalarFieldContainer[str]
    embedding_version: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., call: _Optional[_Union[CallContext, _Mapping]] = ..., texts: _Optional[_Iterable[str]] = ..., embedding_version: _Optional[str] = ...) -> None: ...

class Embedding(_message.Message):
    __slots__ = ("values",)
    VALUES_FIELD_NUMBER: _ClassVar[int]
    values: _containers.RepeatedScalarFieldContainer[float]
    def __init__(self, values: _Optional[_Iterable[float]] = ...) -> None: ...

class EmbedResponse(_message.Message):
    __slots__ = ("embeddings", "embedding_version", "model", "usage")
    EMBEDDINGS_FIELD_NUMBER: _ClassVar[int]
    EMBEDDING_VERSION_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    embeddings: _containers.RepeatedCompositeFieldContainer[Embedding]
    embedding_version: str
    model: ModelChoice
    usage: _metering_pb2.Usage
    def __init__(self, embeddings: _Optional[_Iterable[_Union[Embedding, _Mapping]]] = ..., embedding_version: _Optional[str] = ..., model: _Optional[_Union[ModelChoice, _Mapping]] = ..., usage: _Optional[_Union[_metering_pb2.Usage, _Mapping]] = ...) -> None: ...

class ClassifyRequest(_message.Message):
    __slots__ = ("tenant", "call", "texts", "labels")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    CALL_FIELD_NUMBER: _ClassVar[int]
    TEXTS_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    call: CallContext
    texts: _containers.RepeatedScalarFieldContainer[str]
    labels: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., call: _Optional[_Union[CallContext, _Mapping]] = ..., texts: _Optional[_Iterable[str]] = ..., labels: _Optional[_Iterable[str]] = ...) -> None: ...

class Classification(_message.Message):
    __slots__ = ("label", "confidence")
    LABEL_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    label: str
    confidence: float
    def __init__(self, label: _Optional[str] = ..., confidence: _Optional[float] = ...) -> None: ...

class ClassifyResponse(_message.Message):
    __slots__ = ("results", "model", "usage")
    RESULTS_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    results: _containers.RepeatedCompositeFieldContainer[Classification]
    model: ModelChoice
    usage: _metering_pb2.Usage
    def __init__(self, results: _Optional[_Iterable[_Union[Classification, _Mapping]]] = ..., model: _Optional[_Union[ModelChoice, _Mapping]] = ..., usage: _Optional[_Union[_metering_pb2.Usage, _Mapping]] = ...) -> None: ...
