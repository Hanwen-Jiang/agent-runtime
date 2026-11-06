from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PersonalConfigFile(_message.Message):
    __slots__ = ("path", "content")
    PATH_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    path: str
    content: str
    def __init__(self, path: _Optional[str] = ..., content: _Optional[str] = ...) -> None: ...

class PersonalConfigDefaults(_message.Message):
    __slots__ = ("skill_names", "mcp_connection_ids", "environment_id")
    SKILL_NAMES_FIELD_NUMBER: _ClassVar[int]
    MCP_CONNECTION_IDS_FIELD_NUMBER: _ClassVar[int]
    ENVIRONMENT_ID_FIELD_NUMBER: _ClassVar[int]
    skill_names: _containers.RepeatedScalarFieldContainer[str]
    mcp_connection_ids: _containers.RepeatedScalarFieldContainer[str]
    environment_id: str
    def __init__(self, skill_names: _Optional[_Iterable[str]] = ..., mcp_connection_ids: _Optional[_Iterable[str]] = ..., environment_id: _Optional[str] = ...) -> None: ...

class PersonalConfigDocument(_message.Message):
    __slots__ = ("instructions", "files", "defaults")
    INSTRUCTIONS_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    DEFAULTS_FIELD_NUMBER: _ClassVar[int]
    instructions: str
    files: _containers.RepeatedCompositeFieldContainer[PersonalConfigFile]
    defaults: PersonalConfigDefaults
    def __init__(self, instructions: _Optional[str] = ..., files: _Optional[_Iterable[_Union[PersonalConfigFile, _Mapping]]] = ..., defaults: _Optional[_Union[PersonalConfigDefaults, _Mapping]] = ...) -> None: ...

class PersonalConfig(_message.Message):
    __slots__ = ("version", "digest", "document")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    DIGEST_FIELD_NUMBER: _ClassVar[int]
    DOCUMENT_FIELD_NUMBER: _ClassVar[int]
    version: str
    digest: str
    document: PersonalConfigDocument
    def __init__(self, version: _Optional[str] = ..., digest: _Optional[str] = ..., document: _Optional[_Union[PersonalConfigDocument, _Mapping]] = ...) -> None: ...

class PutPersonalConfigRequest(_message.Message):
    __slots__ = ("tenant_id", "document", "expected_version")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    DOCUMENT_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_VERSION_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    document: PersonalConfigDocument
    expected_version: str
    def __init__(self, tenant_id: _Optional[str] = ..., document: _Optional[_Union[PersonalConfigDocument, _Mapping]] = ..., expected_version: _Optional[str] = ...) -> None: ...

class PutPersonalConfigResponse(_message.Message):
    __slots__ = ("config", "new_version")
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    NEW_VERSION_FIELD_NUMBER: _ClassVar[int]
    config: PersonalConfig
    new_version: bool
    def __init__(self, config: _Optional[_Union[PersonalConfig, _Mapping]] = ..., new_version: _Optional[bool] = ...) -> None: ...

class GetPersonalConfigRequest(_message.Message):
    __slots__ = ("tenant_id", "version")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    version: str
    def __init__(self, tenant_id: _Optional[str] = ..., version: _Optional[str] = ...) -> None: ...

class GetPersonalConfigResponse(_message.Message):
    __slots__ = ("config",)
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    config: PersonalConfig
    def __init__(self, config: _Optional[_Union[PersonalConfig, _Mapping]] = ...) -> None: ...

class RetirePersonalConfigRequest(_message.Message):
    __slots__ = ("tenant_id",)
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    def __init__(self, tenant_id: _Optional[str] = ...) -> None: ...

class RetirePersonalConfigResponse(_message.Message):
    __slots__ = ("retired",)
    RETIRED_FIELD_NUMBER: _ClassVar[int]
    retired: bool
    def __init__(self, retired: _Optional[bool] = ...) -> None: ...
