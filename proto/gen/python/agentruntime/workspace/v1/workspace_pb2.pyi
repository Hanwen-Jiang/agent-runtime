import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class WorkspaceState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    WORKSPACE_STATE_UNSPECIFIED: _ClassVar[WorkspaceState]
    WORKSPACE_STATE_ACTIVE: _ClassVar[WorkspaceState]
    WORKSPACE_STATE_ARCHIVED: _ClassVar[WorkspaceState]
    WORKSPACE_STATE_DELETED: _ClassVar[WorkspaceState]
WORKSPACE_STATE_UNSPECIFIED: WorkspaceState
WORKSPACE_STATE_ACTIVE: WorkspaceState
WORKSPACE_STATE_ARCHIVED: WorkspaceState
WORKSPACE_STATE_DELETED: WorkspaceState

class GitCredential(_message.Message):
    __slots__ = ("username", "password")
    USERNAME_FIELD_NUMBER: _ClassVar[int]
    PASSWORD_FIELD_NUMBER: _ClassVar[int]
    username: str
    password: str
    def __init__(self, username: _Optional[str] = ..., password: _Optional[str] = ...) -> None: ...

class Workspace(_message.Message):
    __slots__ = ("workspace_id", "state", "repo_url", "head", "snapshot_id", "size_bytes", "files", "created_at", "updated_at")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    REPO_URL_FIELD_NUMBER: _ClassVar[int]
    HEAD_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_ID_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    state: WorkspaceState
    repo_url: str
    head: str
    snapshot_id: str
    size_bytes: int
    files: int
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    def __init__(self, workspace_id: _Optional[str] = ..., state: _Optional[_Union[WorkspaceState, str]] = ..., repo_url: _Optional[str] = ..., head: _Optional[str] = ..., snapshot_id: _Optional[str] = ..., size_bytes: _Optional[int] = ..., files: _Optional[int] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class CreateWorkspaceRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id", "repo_url", "ref", "credential")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    REPO_URL_FIELD_NUMBER: _ClassVar[int]
    REF_FIELD_NUMBER: _ClassVar[int]
    CREDENTIAL_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    repo_url: str
    ref: str
    credential: GitCredential
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., repo_url: _Optional[str] = ..., ref: _Optional[str] = ..., credential: _Optional[_Union[GitCredential, _Mapping]] = ...) -> None: ...

class CreateWorkspaceResponse(_message.Message):
    __slots__ = ("workspace",)
    WORKSPACE_FIELD_NUMBER: _ClassVar[int]
    workspace: Workspace
    def __init__(self, workspace: _Optional[_Union[Workspace, _Mapping]] = ...) -> None: ...

class ListWorkspacesRequest(_message.Message):
    __slots__ = ("tenant_id",)
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    def __init__(self, tenant_id: _Optional[str] = ...) -> None: ...

class ListWorkspacesResponse(_message.Message):
    __slots__ = ("workspaces",)
    WORKSPACES_FIELD_NUMBER: _ClassVar[int]
    workspaces: _containers.RepeatedCompositeFieldContainer[Workspace]
    def __init__(self, workspaces: _Optional[_Iterable[_Union[Workspace, _Mapping]]] = ...) -> None: ...

class GetDiffRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id", "base")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    BASE_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    base: str
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., base: _Optional[str] = ...) -> None: ...

class GetDiffResponse(_message.Message):
    __slots__ = ("diff", "files", "truncated", "head")
    DIFF_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    HEAD_FIELD_NUMBER: _ClassVar[int]
    diff: str
    files: _containers.RepeatedScalarFieldContainer[str]
    truncated: bool
    head: str
    def __init__(self, diff: _Optional[str] = ..., files: _Optional[_Iterable[str]] = ..., truncated: _Optional[bool] = ..., head: _Optional[str] = ...) -> None: ...

class CommitWorkspaceRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id", "message", "author_name", "author_email")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_NAME_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_EMAIL_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    message: str
    author_name: str
    author_email: str
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., message: _Optional[str] = ..., author_name: _Optional[str] = ..., author_email: _Optional[str] = ...) -> None: ...

class CommitWorkspaceResponse(_message.Message):
    __slots__ = ("sha", "committed")
    SHA_FIELD_NUMBER: _ClassVar[int]
    COMMITTED_FIELD_NUMBER: _ClassVar[int]
    sha: str
    committed: bool
    def __init__(self, sha: _Optional[str] = ..., committed: _Optional[bool] = ...) -> None: ...

class PushWorkspaceRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id", "branch", "credential")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    BRANCH_FIELD_NUMBER: _ClassVar[int]
    CREDENTIAL_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    branch: str
    credential: GitCredential
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., branch: _Optional[str] = ..., credential: _Optional[_Union[GitCredential, _Mapping]] = ...) -> None: ...

class PushWorkspaceResponse(_message.Message):
    __slots__ = ("sha", "branch", "reconciled")
    SHA_FIELD_NUMBER: _ClassVar[int]
    BRANCH_FIELD_NUMBER: _ClassVar[int]
    RECONCILED_FIELD_NUMBER: _ClassVar[int]
    sha: str
    branch: str
    reconciled: bool
    def __init__(self, sha: _Optional[str] = ..., branch: _Optional[str] = ..., reconciled: _Optional[bool] = ...) -> None: ...

class ArchiveWorkspaceRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ...) -> None: ...

class ArchiveWorkspaceResponse(_message.Message):
    __slots__ = ("snapshot_id", "files", "size_bytes")
    SNAPSHOT_ID_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    snapshot_id: str
    files: int
    size_bytes: int
    def __init__(self, snapshot_id: _Optional[str] = ..., files: _Optional[int] = ..., size_bytes: _Optional[int] = ...) -> None: ...

class DeleteWorkspaceRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ...) -> None: ...

class DeleteWorkspaceResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
