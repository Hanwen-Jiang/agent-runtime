from agentruntime.common.v1 import enums_pb2 as _enums_pb2
from agentruntime.common.v1 import payload_pb2 as _payload_pb2
from agentruntime.common.v1 import tenant_pb2 as _tenant_pb2
from agentruntime.workflow.v1 import task_pb2 as _task_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RegistryKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    REGISTRY_KIND_UNSPECIFIED: _ClassVar[RegistryKind]
    REGISTRY_KIND_NODE_TEMPLATE: _ClassVar[RegistryKind]
    REGISTRY_KIND_RECIPE: _ClassVar[RegistryKind]
    REGISTRY_KIND_REASONING_TEMPLATE: _ClassVar[RegistryKind]
    REGISTRY_KIND_MODEL_PROFILE: _ClassVar[RegistryKind]
    REGISTRY_KIND_TENANT_POLICY: _ClassVar[RegistryKind]
    REGISTRY_KIND_TASK_FAMILIES: _ClassVar[RegistryKind]

class ReleaseState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    RELEASE_STATE_UNSPECIFIED: _ClassVar[ReleaseState]
    RELEASE_STATE_SHADOW: _ClassVar[ReleaseState]
    RELEASE_STATE_DEFAULT: _ClassVar[ReleaseState]
    RELEASE_STATE_RETIRED: _ClassVar[ReleaseState]
REGISTRY_KIND_UNSPECIFIED: RegistryKind
REGISTRY_KIND_NODE_TEMPLATE: RegistryKind
REGISTRY_KIND_RECIPE: RegistryKind
REGISTRY_KIND_REASONING_TEMPLATE: RegistryKind
REGISTRY_KIND_MODEL_PROFILE: RegistryKind
REGISTRY_KIND_TENANT_POLICY: RegistryKind
REGISTRY_KIND_TASK_FAMILIES: RegistryKind
RELEASE_STATE_UNSPECIFIED: ReleaseState
RELEASE_STATE_SHADOW: ReleaseState
RELEASE_STATE_DEFAULT: ReleaseState
RELEASE_STATE_RETIRED: ReleaseState

class NodeTemplate(_message.Message):
    __slots__ = ("name", "version", "state", "executor", "effect", "role", "output", "output_schema_id", "params_schema", "defaults", "rollback_template", "supports_idempotency_lookup")
    NAME_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    EXECUTOR_FIELD_NUMBER: _ClassVar[int]
    EFFECT_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_SCHEMA_ID_FIELD_NUMBER: _ClassVar[int]
    PARAMS_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    DEFAULTS_FIELD_NUMBER: _ClassVar[int]
    ROLLBACK_TEMPLATE_FIELD_NUMBER: _ClassVar[int]
    SUPPORTS_IDEMPOTENCY_LOOKUP_FIELD_NUMBER: _ClassVar[int]
    name: str
    version: str
    state: ReleaseState
    executor: _enums_pb2.Executor
    effect: _enums_pb2.Effect
    role: _enums_pb2.Role
    output: _enums_pb2.ModelOutput
    output_schema_id: str
    params_schema: _payload_pb2.JsonPayload
    defaults: _struct_pb2.Struct
    rollback_template: str
    supports_idempotency_lookup: bool
    def __init__(self, name: _Optional[str] = ..., version: _Optional[str] = ..., state: _Optional[_Union[ReleaseState, str]] = ..., executor: _Optional[_Union[_enums_pb2.Executor, str]] = ..., effect: _Optional[_Union[_enums_pb2.Effect, str]] = ..., role: _Optional[_Union[_enums_pb2.Role, str]] = ..., output: _Optional[_Union[_enums_pb2.ModelOutput, str]] = ..., output_schema_id: _Optional[str] = ..., params_schema: _Optional[_Union[_payload_pb2.JsonPayload, _Mapping]] = ..., defaults: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., rollback_template: _Optional[str] = ..., supports_idempotency_lookup: _Optional[bool] = ...) -> None: ...

class Recipe(_message.Message):
    __slots__ = ("name", "version", "state", "task_family", "document")
    NAME_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    TASK_FAMILY_FIELD_NUMBER: _ClassVar[int]
    DOCUMENT_FIELD_NUMBER: _ClassVar[int]
    name: str
    version: str
    state: ReleaseState
    task_family: str
    document: _payload_pb2.JsonPayload
    def __init__(self, name: _Optional[str] = ..., version: _Optional[str] = ..., state: _Optional[_Union[ReleaseState, str]] = ..., task_family: _Optional[str] = ..., document: _Optional[_Union[_payload_pb2.JsonPayload, _Mapping]] = ...) -> None: ...

class ModelProfile(_message.Message):
    __slots__ = ("id", "version", "state", "document")
    ID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    DOCUMENT_FIELD_NUMBER: _ClassVar[int]
    id: str
    version: str
    state: ReleaseState
    document: _payload_pb2.JsonPayload
    def __init__(self, id: _Optional[str] = ..., version: _Optional[str] = ..., state: _Optional[_Union[ReleaseState, str]] = ..., document: _Optional[_Union[_payload_pb2.JsonPayload, _Mapping]] = ...) -> None: ...

class TenantPolicy(_message.Message):
    __slots__ = ("version", "document")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    DOCUMENT_FIELD_NUMBER: _ClassVar[int]
    version: str
    document: _payload_pb2.JsonPayload
    def __init__(self, version: _Optional[str] = ..., document: _Optional[_Union[_payload_pb2.JsonPayload, _Mapping]] = ...) -> None: ...

class TaskFamilies(_message.Message):
    __slots__ = ("version", "document", "platform_default")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    DOCUMENT_FIELD_NUMBER: _ClassVar[int]
    PLATFORM_DEFAULT_FIELD_NUMBER: _ClassVar[int]
    version: str
    document: _payload_pb2.JsonPayload
    platform_default: bool
    def __init__(self, version: _Optional[str] = ..., document: _Optional[_Union[_payload_pb2.JsonPayload, _Mapping]] = ..., platform_default: _Optional[bool] = ...) -> None: ...

class PublishRequest(_message.Message):
    __slots__ = ("tenant", "kind", "name", "document", "change_note")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    DOCUMENT_FIELD_NUMBER: _ClassVar[int]
    CHANGE_NOTE_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    kind: RegistryKind
    name: str
    document: _payload_pb2.JsonPayload
    change_note: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., kind: _Optional[_Union[RegistryKind, str]] = ..., name: _Optional[str] = ..., document: _Optional[_Union[_payload_pb2.JsonPayload, _Mapping]] = ..., change_note: _Optional[str] = ...) -> None: ...

class PublishResponse(_message.Message):
    __slots__ = ("name", "version", "state")
    NAME_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    name: str
    version: str
    state: ReleaseState
    def __init__(self, name: _Optional[str] = ..., version: _Optional[str] = ..., state: _Optional[_Union[ReleaseState, str]] = ...) -> None: ...

class PromoteRequest(_message.Message):
    __slots__ = ("tenant", "kind", "name", "version", "evaluation")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    EVALUATION_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    kind: RegistryKind
    name: str
    version: str
    evaluation: _payload_pb2.JsonPayload
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., kind: _Optional[_Union[RegistryKind, str]] = ..., name: _Optional[str] = ..., version: _Optional[str] = ..., evaluation: _Optional[_Union[_payload_pb2.JsonPayload, _Mapping]] = ...) -> None: ...

class PromoteResponse(_message.Message):
    __slots__ = ("previous_default_version",)
    PREVIOUS_DEFAULT_VERSION_FIELD_NUMBER: _ClassVar[int]
    previous_default_version: str
    def __init__(self, previous_default_version: _Optional[str] = ...) -> None: ...

class ResolvePinnedVersionsRequest(_message.Message):
    __slots__ = ("tenant", "task_family", "recipe")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    TASK_FAMILY_FIELD_NUMBER: _ClassVar[int]
    RECIPE_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    task_family: str
    recipe: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., task_family: _Optional[str] = ..., recipe: _Optional[str] = ...) -> None: ...

class ResolvePinnedVersionsResponse(_message.Message):
    __slots__ = ("pinned_versions",)
    PINNED_VERSIONS_FIELD_NUMBER: _ClassVar[int]
    pinned_versions: _task_pb2.PinnedVersions
    def __init__(self, pinned_versions: _Optional[_Union[_task_pb2.PinnedVersions, _Mapping]] = ...) -> None: ...

class GetNodeTemplateRequest(_message.Message):
    __slots__ = ("tenant", "name", "version")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    name: str
    version: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., name: _Optional[str] = ..., version: _Optional[str] = ...) -> None: ...

class GetNodeTemplateResponse(_message.Message):
    __slots__ = ("node_template",)
    NODE_TEMPLATE_FIELD_NUMBER: _ClassVar[int]
    node_template: NodeTemplate
    def __init__(self, node_template: _Optional[_Union[NodeTemplate, _Mapping]] = ...) -> None: ...

class ListNodeTemplatesRequest(_message.Message):
    __slots__ = ("tenant", "pinned_versions", "page_size", "page_token")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    PINNED_VERSIONS_FIELD_NUMBER: _ClassVar[int]
    PAGE_SIZE_FIELD_NUMBER: _ClassVar[int]
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    pinned_versions: _task_pb2.PinnedVersions
    page_size: int
    page_token: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., pinned_versions: _Optional[_Union[_task_pb2.PinnedVersions, _Mapping]] = ..., page_size: _Optional[int] = ..., page_token: _Optional[str] = ...) -> None: ...

class ListNodeTemplatesResponse(_message.Message):
    __slots__ = ("node_templates", "next_page_token")
    NODE_TEMPLATES_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    node_templates: _containers.RepeatedCompositeFieldContainer[NodeTemplate]
    next_page_token: str
    def __init__(self, node_templates: _Optional[_Iterable[_Union[NodeTemplate, _Mapping]]] = ..., next_page_token: _Optional[str] = ...) -> None: ...

class GetRecipeRequest(_message.Message):
    __slots__ = ("tenant", "name", "version")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    name: str
    version: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., name: _Optional[str] = ..., version: _Optional[str] = ...) -> None: ...

class GetRecipeResponse(_message.Message):
    __slots__ = ("recipe",)
    RECIPE_FIELD_NUMBER: _ClassVar[int]
    recipe: Recipe
    def __init__(self, recipe: _Optional[_Union[Recipe, _Mapping]] = ...) -> None: ...

class ListRecipesRequest(_message.Message):
    __slots__ = ("tenant", "task_family", "page_size", "page_token")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    TASK_FAMILY_FIELD_NUMBER: _ClassVar[int]
    PAGE_SIZE_FIELD_NUMBER: _ClassVar[int]
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    task_family: str
    page_size: int
    page_token: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., task_family: _Optional[str] = ..., page_size: _Optional[int] = ..., page_token: _Optional[str] = ...) -> None: ...

class ListRecipesResponse(_message.Message):
    __slots__ = ("recipes", "next_page_token")
    RECIPES_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    recipes: _containers.RepeatedCompositeFieldContainer[Recipe]
    next_page_token: str
    def __init__(self, recipes: _Optional[_Iterable[_Union[Recipe, _Mapping]]] = ..., next_page_token: _Optional[str] = ...) -> None: ...

class GetModelProfileRequest(_message.Message):
    __slots__ = ("tenant", "id", "version")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    id: str
    version: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., id: _Optional[str] = ..., version: _Optional[str] = ...) -> None: ...

class GetModelProfileResponse(_message.Message):
    __slots__ = ("model_profile",)
    MODEL_PROFILE_FIELD_NUMBER: _ClassVar[int]
    model_profile: ModelProfile
    def __init__(self, model_profile: _Optional[_Union[ModelProfile, _Mapping]] = ...) -> None: ...

class ListModelProfilesRequest(_message.Message):
    __slots__ = ("tenant", "page_size", "page_token")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    PAGE_SIZE_FIELD_NUMBER: _ClassVar[int]
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    page_size: int
    page_token: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., page_size: _Optional[int] = ..., page_token: _Optional[str] = ...) -> None: ...

class ListModelProfilesResponse(_message.Message):
    __slots__ = ("model_profiles", "next_page_token")
    MODEL_PROFILES_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    model_profiles: _containers.RepeatedCompositeFieldContainer[ModelProfile]
    next_page_token: str
    def __init__(self, model_profiles: _Optional[_Iterable[_Union[ModelProfile, _Mapping]]] = ..., next_page_token: _Optional[str] = ...) -> None: ...

class GetTenantPolicyRequest(_message.Message):
    __slots__ = ("tenant", "version")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    version: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., version: _Optional[str] = ...) -> None: ...

class GetTenantPolicyResponse(_message.Message):
    __slots__ = ("tenant_policy",)
    TENANT_POLICY_FIELD_NUMBER: _ClassVar[int]
    tenant_policy: TenantPolicy
    def __init__(self, tenant_policy: _Optional[_Union[TenantPolicy, _Mapping]] = ...) -> None: ...

class GetTaskFamiliesRequest(_message.Message):
    __slots__ = ("tenant",)
    TENANT_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ...) -> None: ...

class GetTaskFamiliesResponse(_message.Message):
    __slots__ = ("task_families",)
    TASK_FAMILIES_FIELD_NUMBER: _ClassVar[int]
    task_families: TaskFamilies
    def __init__(self, task_families: _Optional[_Union[TaskFamilies, _Mapping]] = ...) -> None: ...
