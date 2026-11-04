from agentruntime.common.v1 import payload_pb2 as _payload_pb2
from agentruntime.registry.v1 import registry_pb2 as _registry_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PutSkillRequest(_message.Message):
    __slots__ = ("tenant_id", "name", "document")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    DOCUMENT_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    name: str
    document: _payload_pb2.JsonPayload
    def __init__(self, tenant_id: _Optional[str] = ..., name: _Optional[str] = ..., document: _Optional[_Union[_payload_pb2.JsonPayload, _Mapping]] = ...) -> None: ...

class PutSkillResponse(_message.Message):
    __slots__ = ("skill", "created", "new_version")
    SKILL_FIELD_NUMBER: _ClassVar[int]
    CREATED_FIELD_NUMBER: _ClassVar[int]
    NEW_VERSION_FIELD_NUMBER: _ClassVar[int]
    skill: _registry_pb2.Skill
    created: bool
    new_version: bool
    def __init__(self, skill: _Optional[_Union[_registry_pb2.Skill, _Mapping]] = ..., created: _Optional[bool] = ..., new_version: _Optional[bool] = ...) -> None: ...

class ListSkillsRequest(_message.Message):
    __slots__ = ("tenant_id", "page_size", "page_token")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    PAGE_SIZE_FIELD_NUMBER: _ClassVar[int]
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    page_size: int
    page_token: str
    def __init__(self, tenant_id: _Optional[str] = ..., page_size: _Optional[int] = ..., page_token: _Optional[str] = ...) -> None: ...

class ListSkillsResponse(_message.Message):
    __slots__ = ("skills", "next_page_token")
    SKILLS_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    skills: _containers.RepeatedCompositeFieldContainer[_registry_pb2.Skill]
    next_page_token: str
    def __init__(self, skills: _Optional[_Iterable[_Union[_registry_pb2.Skill, _Mapping]]] = ..., next_page_token: _Optional[str] = ...) -> None: ...

class GetSkillRequest(_message.Message):
    __slots__ = ("tenant_id", "name", "version")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    name: str
    version: str
    def __init__(self, tenant_id: _Optional[str] = ..., name: _Optional[str] = ..., version: _Optional[str] = ...) -> None: ...

class GetSkillResponse(_message.Message):
    __slots__ = ("skill",)
    SKILL_FIELD_NUMBER: _ClassVar[int]
    skill: _registry_pb2.Skill
    def __init__(self, skill: _Optional[_Union[_registry_pb2.Skill, _Mapping]] = ...) -> None: ...

class RetireSkillRequest(_message.Message):
    __slots__ = ("tenant_id", "name")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    name: str
    def __init__(self, tenant_id: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class RetireSkillResponse(_message.Message):
    __slots__ = ("retired", "version")
    RETIRED_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    retired: bool
    version: str
    def __init__(self, retired: _Optional[bool] = ..., version: _Optional[str] = ...) -> None: ...
