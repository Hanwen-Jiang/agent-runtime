import datetime

from agentruntime.common.v1 import error_pb2 as _error_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class WorkspaceLayout(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    WORKSPACE_LAYOUT_UNSPECIFIED: _ClassVar[WorkspaceLayout]
    WORKSPACE_LAYOUT_ROOT: _ClassVar[WorkspaceLayout]
    WORKSPACE_LAYOUT_MULTI: _ClassVar[WorkspaceLayout]

class RepoAccess(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    REPO_ACCESS_UNSPECIFIED: _ClassVar[RepoAccess]
    REPO_ACCESS_READ: _ClassVar[RepoAccess]
    REPO_ACCESS_WRITE: _ClassVar[RepoAccess]

class WorkspaceState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    WORKSPACE_STATE_UNSPECIFIED: _ClassVar[WorkspaceState]
    WORKSPACE_STATE_ACTIVE: _ClassVar[WorkspaceState]
    WORKSPACE_STATE_ARCHIVED: _ClassVar[WorkspaceState]
    WORKSPACE_STATE_DELETED: _ClassVar[WorkspaceState]

class NetworkMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    NETWORK_MODE_UNSPECIFIED: _ClassVar[NetworkMode]
    NETWORK_MODE_OPEN: _ClassVar[NetworkMode]
    NETWORK_MODE_RESTRICTED: _ClassVar[NetworkMode]
    NETWORK_MODE_DENY: _ClassVar[NetworkMode]

class FileType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FILE_TYPE_UNSPECIFIED: _ClassVar[FileType]
    FILE_TYPE_FILE: _ClassVar[FileType]
    FILE_TYPE_DIRECTORY: _ClassVar[FileType]
    FILE_TYPE_SYMLINK: _ClassVar[FileType]

class WorkspaceOperationStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    WORKSPACE_OPERATION_STATUS_UNSPECIFIED: _ClassVar[WorkspaceOperationStatus]
    WORKSPACE_OPERATION_STATUS_RUNNING: _ClassVar[WorkspaceOperationStatus]
    WORKSPACE_OPERATION_STATUS_COMPLETED: _ClassVar[WorkspaceOperationStatus]
    WORKSPACE_OPERATION_STATUS_FAILED: _ClassVar[WorkspaceOperationStatus]
WORKSPACE_LAYOUT_UNSPECIFIED: WorkspaceLayout
WORKSPACE_LAYOUT_ROOT: WorkspaceLayout
WORKSPACE_LAYOUT_MULTI: WorkspaceLayout
REPO_ACCESS_UNSPECIFIED: RepoAccess
REPO_ACCESS_READ: RepoAccess
REPO_ACCESS_WRITE: RepoAccess
WORKSPACE_STATE_UNSPECIFIED: WorkspaceState
WORKSPACE_STATE_ACTIVE: WorkspaceState
WORKSPACE_STATE_ARCHIVED: WorkspaceState
WORKSPACE_STATE_DELETED: WorkspaceState
NETWORK_MODE_UNSPECIFIED: NetworkMode
NETWORK_MODE_OPEN: NetworkMode
NETWORK_MODE_RESTRICTED: NetworkMode
NETWORK_MODE_DENY: NetworkMode
FILE_TYPE_UNSPECIFIED: FileType
FILE_TYPE_FILE: FileType
FILE_TYPE_DIRECTORY: FileType
FILE_TYPE_SYMLINK: FileType
WORKSPACE_OPERATION_STATUS_UNSPECIFIED: WorkspaceOperationStatus
WORKSPACE_OPERATION_STATUS_RUNNING: WorkspaceOperationStatus
WORKSPACE_OPERATION_STATUS_COMPLETED: WorkspaceOperationStatus
WORKSPACE_OPERATION_STATUS_FAILED: WorkspaceOperationStatus

class WorkspaceRepo(_message.Message):
    __slots__ = ("name", "url", "ref", "path", "access", "primary", "head", "branch", "attached_at")
    NAME_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    REF_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    ACCESS_FIELD_NUMBER: _ClassVar[int]
    PRIMARY_FIELD_NUMBER: _ClassVar[int]
    HEAD_FIELD_NUMBER: _ClassVar[int]
    BRANCH_FIELD_NUMBER: _ClassVar[int]
    ATTACHED_AT_FIELD_NUMBER: _ClassVar[int]
    name: str
    url: str
    ref: str
    path: str
    access: RepoAccess
    primary: bool
    head: str
    branch: str
    attached_at: _timestamp_pb2.Timestamp
    def __init__(self, name: _Optional[str] = ..., url: _Optional[str] = ..., ref: _Optional[str] = ..., path: _Optional[str] = ..., access: _Optional[_Union[RepoAccess, str]] = ..., primary: _Optional[bool] = ..., head: _Optional[str] = ..., branch: _Optional[str] = ..., attached_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class GitCredential(_message.Message):
    __slots__ = ("username", "password")
    USERNAME_FIELD_NUMBER: _ClassVar[int]
    PASSWORD_FIELD_NUMBER: _ClassVar[int]
    username: str
    password: str
    def __init__(self, username: _Optional[str] = ..., password: _Optional[str] = ...) -> None: ...

class Workspace(_message.Message):
    __slots__ = ("workspace_id", "state", "repo_url", "head", "snapshot_id", "size_bytes", "files", "created_at", "updated_at", "branch", "environment_id", "layout", "repos")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    REPO_URL_FIELD_NUMBER: _ClassVar[int]
    HEAD_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_ID_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    BRANCH_FIELD_NUMBER: _ClassVar[int]
    ENVIRONMENT_ID_FIELD_NUMBER: _ClassVar[int]
    LAYOUT_FIELD_NUMBER: _ClassVar[int]
    REPOS_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    state: WorkspaceState
    repo_url: str
    head: str
    snapshot_id: str
    size_bytes: int
    files: int
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    branch: str
    environment_id: str
    layout: WorkspaceLayout
    repos: _containers.RepeatedCompositeFieldContainer[WorkspaceRepo]
    def __init__(self, workspace_id: _Optional[str] = ..., state: _Optional[_Union[WorkspaceState, str]] = ..., repo_url: _Optional[str] = ..., head: _Optional[str] = ..., snapshot_id: _Optional[str] = ..., size_bytes: _Optional[int] = ..., files: _Optional[int] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., branch: _Optional[str] = ..., environment_id: _Optional[str] = ..., layout: _Optional[_Union[WorkspaceLayout, str]] = ..., repos: _Optional[_Iterable[_Union[WorkspaceRepo, _Mapping]]] = ...) -> None: ...

class WorkspaceEnvironment(_message.Message):
    __slots__ = ("environment_id", "setup_script", "start_command", "env", "network", "secret_headers")
    ENVIRONMENT_ID_FIELD_NUMBER: _ClassVar[int]
    SETUP_SCRIPT_FIELD_NUMBER: _ClassVar[int]
    START_COMMAND_FIELD_NUMBER: _ClassVar[int]
    ENV_FIELD_NUMBER: _ClassVar[int]
    NETWORK_FIELD_NUMBER: _ClassVar[int]
    SECRET_HEADERS_FIELD_NUMBER: _ClassVar[int]
    environment_id: str
    setup_script: str
    start_command: str
    env: str
    network: EnvironmentNetwork
    secret_headers: _containers.RepeatedCompositeFieldContainer[SecretHeader]
    def __init__(self, environment_id: _Optional[str] = ..., setup_script: _Optional[str] = ..., start_command: _Optional[str] = ..., env: _Optional[str] = ..., network: _Optional[_Union[EnvironmentNetwork, _Mapping]] = ..., secret_headers: _Optional[_Iterable[_Union[SecretHeader, _Mapping]]] = ...) -> None: ...

class EnvironmentNetwork(_message.Message):
    __slots__ = ("mode", "hosts")
    MODE_FIELD_NUMBER: _ClassVar[int]
    HOSTS_FIELD_NUMBER: _ClassVar[int]
    mode: NetworkMode
    hosts: _containers.RepeatedCompositeFieldContainer[NetworkHost]
    def __init__(self, mode: _Optional[_Union[NetworkMode, str]] = ..., hosts: _Optional[_Iterable[_Union[NetworkHost, _Mapping]]] = ...) -> None: ...

class NetworkHost(_message.Message):
    __slots__ = ("host", "include_subdomains")
    HOST_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_SUBDOMAINS_FIELD_NUMBER: _ClassVar[int]
    host: str
    include_subdomains: bool
    def __init__(self, host: _Optional[str] = ..., include_subdomains: _Optional[bool] = ...) -> None: ...

class SecretHeader(_message.Message):
    __slots__ = ("host", "include_subdomains", "secret_name", "header")
    HOST_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_SUBDOMAINS_FIELD_NUMBER: _ClassVar[int]
    SECRET_NAME_FIELD_NUMBER: _ClassVar[int]
    HEADER_FIELD_NUMBER: _ClassVar[int]
    host: str
    include_subdomains: bool
    secret_name: str
    header: str
    def __init__(self, host: _Optional[str] = ..., include_subdomains: _Optional[bool] = ..., secret_name: _Optional[str] = ..., header: _Optional[str] = ...) -> None: ...

class InitialFile(_message.Message):
    __slots__ = ("path", "content")
    PATH_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    path: str
    content: str
    def __init__(self, path: _Optional[str] = ..., content: _Optional[str] = ...) -> None: ...

class CreateWorkspaceRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id", "repo_url", "ref", "credential", "from_workspace_id", "files", "author_name", "author_email", "environment", "layout")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    REPO_URL_FIELD_NUMBER: _ClassVar[int]
    REF_FIELD_NUMBER: _ClassVar[int]
    CREDENTIAL_FIELD_NUMBER: _ClassVar[int]
    FROM_WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_NAME_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_EMAIL_FIELD_NUMBER: _ClassVar[int]
    ENVIRONMENT_FIELD_NUMBER: _ClassVar[int]
    LAYOUT_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    repo_url: str
    ref: str
    credential: GitCredential
    from_workspace_id: str
    files: _containers.RepeatedCompositeFieldContainer[InitialFile]
    author_name: str
    author_email: str
    environment: WorkspaceEnvironment
    layout: WorkspaceLayout
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., repo_url: _Optional[str] = ..., ref: _Optional[str] = ..., credential: _Optional[_Union[GitCredential, _Mapping]] = ..., from_workspace_id: _Optional[str] = ..., files: _Optional[_Iterable[_Union[InitialFile, _Mapping]]] = ..., author_name: _Optional[str] = ..., author_email: _Optional[str] = ..., environment: _Optional[_Union[WorkspaceEnvironment, _Mapping]] = ..., layout: _Optional[_Union[WorkspaceLayout, str]] = ...) -> None: ...

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
    __slots__ = ("tenant_id", "workspace_id", "base", "find_renames", "repo")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    BASE_FIELD_NUMBER: _ClassVar[int]
    FIND_RENAMES_FIELD_NUMBER: _ClassVar[int]
    REPO_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    base: str
    find_renames: bool
    repo: str
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., base: _Optional[str] = ..., find_renames: _Optional[bool] = ..., repo: _Optional[str] = ...) -> None: ...

class DiffStat(_message.Message):
    __slots__ = ("path", "added", "deleted", "binary")
    PATH_FIELD_NUMBER: _ClassVar[int]
    ADDED_FIELD_NUMBER: _ClassVar[int]
    DELETED_FIELD_NUMBER: _ClassVar[int]
    BINARY_FIELD_NUMBER: _ClassVar[int]
    path: str
    added: int
    deleted: int
    binary: bool
    def __init__(self, path: _Optional[str] = ..., added: _Optional[int] = ..., deleted: _Optional[int] = ..., binary: _Optional[bool] = ...) -> None: ...

class GetDiffResponse(_message.Message):
    __slots__ = ("diff", "files", "truncated", "head", "stats", "branch")
    DIFF_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    HEAD_FIELD_NUMBER: _ClassVar[int]
    STATS_FIELD_NUMBER: _ClassVar[int]
    BRANCH_FIELD_NUMBER: _ClassVar[int]
    diff: str
    files: _containers.RepeatedScalarFieldContainer[str]
    truncated: bool
    head: str
    stats: _containers.RepeatedCompositeFieldContainer[DiffStat]
    branch: str
    def __init__(self, diff: _Optional[str] = ..., files: _Optional[_Iterable[str]] = ..., truncated: _Optional[bool] = ..., head: _Optional[str] = ..., stats: _Optional[_Iterable[_Union[DiffStat, _Mapping]]] = ..., branch: _Optional[str] = ...) -> None: ...

class CommitWorkspaceRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id", "message", "author_name", "author_email", "repo")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_NAME_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_EMAIL_FIELD_NUMBER: _ClassVar[int]
    REPO_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    message: str
    author_name: str
    author_email: str
    repo: str
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., message: _Optional[str] = ..., author_name: _Optional[str] = ..., author_email: _Optional[str] = ..., repo: _Optional[str] = ...) -> None: ...

class CommitWorkspaceResponse(_message.Message):
    __slots__ = ("sha", "committed")
    SHA_FIELD_NUMBER: _ClassVar[int]
    COMMITTED_FIELD_NUMBER: _ClassVar[int]
    sha: str
    committed: bool
    def __init__(self, sha: _Optional[str] = ..., committed: _Optional[bool] = ...) -> None: ...

class PushWorkspaceRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id", "branch", "credential", "repo")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    BRANCH_FIELD_NUMBER: _ClassVar[int]
    CREDENTIAL_FIELD_NUMBER: _ClassVar[int]
    REPO_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    branch: str
    credential: GitCredential
    repo: str
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., branch: _Optional[str] = ..., credential: _Optional[_Union[GitCredential, _Mapping]] = ..., repo: _Optional[str] = ...) -> None: ...

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

class FileEntry(_message.Message):
    __slots__ = ("name", "type", "size")
    NAME_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    name: str
    type: FileType
    size: int
    def __init__(self, name: _Optional[str] = ..., type: _Optional[_Union[FileType, str]] = ..., size: _Optional[int] = ...) -> None: ...

class ListFilesRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id", "path")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    path: str
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., path: _Optional[str] = ...) -> None: ...

class ListFilesResponse(_message.Message):
    __slots__ = ("entries", "truncated")
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    entries: _containers.RepeatedCompositeFieldContainer[FileEntry]
    truncated: bool
    def __init__(self, entries: _Optional[_Iterable[_Union[FileEntry, _Mapping]]] = ..., truncated: _Optional[bool] = ...) -> None: ...

class ReadFileRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id", "path")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    path: str
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., path: _Optional[str] = ...) -> None: ...

class ReadFileResponse(_message.Message):
    __slots__ = ("size", "too_large", "binary", "content")
    SIZE_FIELD_NUMBER: _ClassVar[int]
    TOO_LARGE_FIELD_NUMBER: _ClassVar[int]
    BINARY_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    size: int
    too_large: bool
    binary: bool
    content: str
    def __init__(self, size: _Optional[int] = ..., too_large: _Optional[bool] = ..., binary: _Optional[bool] = ..., content: _Optional[str] = ...) -> None: ...

class GetLogRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id", "limit", "base", "repo")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    BASE_FIELD_NUMBER: _ClassVar[int]
    REPO_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    limit: int
    base: str
    repo: str
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., limit: _Optional[int] = ..., base: _Optional[str] = ..., repo: _Optional[str] = ...) -> None: ...

class Commit(_message.Message):
    __slots__ = ("sha", "author_name", "author_email", "authored_at", "subject")
    SHA_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_NAME_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_EMAIL_FIELD_NUMBER: _ClassVar[int]
    AUTHORED_AT_FIELD_NUMBER: _ClassVar[int]
    SUBJECT_FIELD_NUMBER: _ClassVar[int]
    sha: str
    author_name: str
    author_email: str
    authored_at: _timestamp_pb2.Timestamp
    subject: str
    def __init__(self, sha: _Optional[str] = ..., author_name: _Optional[str] = ..., author_email: _Optional[str] = ..., authored_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., subject: _Optional[str] = ...) -> None: ...

class GetLogResponse(_message.Message):
    __slots__ = ("commits",)
    COMMITS_FIELD_NUMBER: _ClassVar[int]
    commits: _containers.RepeatedCompositeFieldContainer[Commit]
    def __init__(self, commits: _Optional[_Iterable[_Union[Commit, _Mapping]]] = ...) -> None: ...

class DiscardChangesRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id", "repo")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    REPO_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    repo: str
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., repo: _Optional[str] = ...) -> None: ...

class DiscardChangesResponse(_message.Message):
    __slots__ = ("head",)
    HEAD_FIELD_NUMBER: _ClassVar[int]
    head: str
    def __init__(self, head: _Optional[str] = ...) -> None: ...

class SyncWorkspaceRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id", "branch", "credential", "repo")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    BRANCH_FIELD_NUMBER: _ClassVar[int]
    CREDENTIAL_FIELD_NUMBER: _ClassVar[int]
    REPO_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    branch: str
    credential: GitCredential
    repo: str
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., branch: _Optional[str] = ..., credential: _Optional[_Union[GitCredential, _Mapping]] = ..., repo: _Optional[str] = ...) -> None: ...

class SyncWorkspaceResponse(_message.Message):
    __slots__ = ("head", "branch")
    HEAD_FIELD_NUMBER: _ClassVar[int]
    BRANCH_FIELD_NUMBER: _ClassVar[int]
    head: str
    branch: str
    def __init__(self, head: _Optional[str] = ..., branch: _Optional[str] = ...) -> None: ...

class DeleteEnvironmentConfigRequest(_message.Message):
    __slots__ = ("tenant_id", "environment_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    ENVIRONMENT_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    environment_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., environment_id: _Optional[str] = ...) -> None: ...

class DeleteEnvironmentConfigResponse(_message.Message):
    __slots__ = ("workspaces",)
    WORKSPACES_FIELD_NUMBER: _ClassVar[int]
    workspaces: int
    def __init__(self, workspaces: _Optional[int] = ...) -> None: ...

class SetWorkspaceEnvironmentRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id", "environment")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    ENVIRONMENT_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    environment: WorkspaceEnvironment
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., environment: _Optional[_Union[WorkspaceEnvironment, _Mapping]] = ...) -> None: ...

class SetWorkspaceEnvironmentResponse(_message.Message):
    __slots__ = ("workspace",)
    WORKSPACE_FIELD_NUMBER: _ClassVar[int]
    workspace: Workspace
    def __init__(self, workspace: _Optional[_Union[Workspace, _Mapping]] = ...) -> None: ...

class AttachRepoRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id", "repo", "credential")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    REPO_FIELD_NUMBER: _ClassVar[int]
    CREDENTIAL_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    repo: WorkspaceRepo
    credential: GitCredential
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., repo: _Optional[_Union[WorkspaceRepo, _Mapping]] = ..., credential: _Optional[_Union[GitCredential, _Mapping]] = ...) -> None: ...

class AttachRepoResponse(_message.Message):
    __slots__ = ("repo", "workspace")
    REPO_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_FIELD_NUMBER: _ClassVar[int]
    repo: WorkspaceRepo
    workspace: Workspace
    def __init__(self, repo: _Optional[_Union[WorkspaceRepo, _Mapping]] = ..., workspace: _Optional[_Union[Workspace, _Mapping]] = ...) -> None: ...

class DetachRepoRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id", "repo", "discard_changes")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    REPO_FIELD_NUMBER: _ClassVar[int]
    DISCARD_CHANGES_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    repo: str
    discard_changes: bool
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., repo: _Optional[str] = ..., discard_changes: _Optional[bool] = ...) -> None: ...

class DetachRepoResponse(_message.Message):
    __slots__ = ("workspace", "discarded_files", "discarded_commits")
    WORKSPACE_FIELD_NUMBER: _ClassVar[int]
    DISCARDED_FILES_FIELD_NUMBER: _ClassVar[int]
    DISCARDED_COMMITS_FIELD_NUMBER: _ClassVar[int]
    workspace: Workspace
    discarded_files: int
    discarded_commits: int
    def __init__(self, workspace: _Optional[_Union[Workspace, _Mapping]] = ..., discarded_files: _Optional[int] = ..., discarded_commits: _Optional[int] = ...) -> None: ...

class ListReposRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ...) -> None: ...

class ListReposResponse(_message.Message):
    __slots__ = ("repos",)
    REPOS_FIELD_NUMBER: _ClassVar[int]
    repos: _containers.RepeatedCompositeFieldContainer[WorkspaceRepo]
    def __init__(self, repos: _Optional[_Iterable[_Union[WorkspaceRepo, _Mapping]]] = ...) -> None: ...

class StartWorkspaceOperationRequest(_message.Message):
    __slots__ = ("request_id", "create", "commit", "push", "archive", "delete", "sync", "discard", "attach_repo", "detach_repo")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    CREATE_FIELD_NUMBER: _ClassVar[int]
    COMMIT_FIELD_NUMBER: _ClassVar[int]
    PUSH_FIELD_NUMBER: _ClassVar[int]
    ARCHIVE_FIELD_NUMBER: _ClassVar[int]
    DELETE_FIELD_NUMBER: _ClassVar[int]
    SYNC_FIELD_NUMBER: _ClassVar[int]
    DISCARD_FIELD_NUMBER: _ClassVar[int]
    ATTACH_REPO_FIELD_NUMBER: _ClassVar[int]
    DETACH_REPO_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    create: CreateWorkspaceRequest
    commit: CommitWorkspaceRequest
    push: PushWorkspaceRequest
    archive: ArchiveWorkspaceRequest
    delete: DeleteWorkspaceRequest
    sync: SyncWorkspaceRequest
    discard: DiscardChangesRequest
    attach_repo: AttachRepoRequest
    detach_repo: DetachRepoRequest
    def __init__(self, request_id: _Optional[str] = ..., create: _Optional[_Union[CreateWorkspaceRequest, _Mapping]] = ..., commit: _Optional[_Union[CommitWorkspaceRequest, _Mapping]] = ..., push: _Optional[_Union[PushWorkspaceRequest, _Mapping]] = ..., archive: _Optional[_Union[ArchiveWorkspaceRequest, _Mapping]] = ..., delete: _Optional[_Union[DeleteWorkspaceRequest, _Mapping]] = ..., sync: _Optional[_Union[SyncWorkspaceRequest, _Mapping]] = ..., discard: _Optional[_Union[DiscardChangesRequest, _Mapping]] = ..., attach_repo: _Optional[_Union[AttachRepoRequest, _Mapping]] = ..., detach_repo: _Optional[_Union[DetachRepoRequest, _Mapping]] = ...) -> None: ...

class StartWorkspaceOperationResponse(_message.Message):
    __slots__ = ("operation_id",)
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    operation_id: str
    def __init__(self, operation_id: _Optional[str] = ...) -> None: ...

class GetWorkspaceOperationRequest(_message.Message):
    __slots__ = ("tenant_id", "workspace_id", "operation_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    workspace_id: str
    operation_id: str
    def __init__(self, tenant_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., operation_id: _Optional[str] = ...) -> None: ...

class GetWorkspaceOperationResponse(_message.Message):
    __slots__ = ("operation_id", "status", "result", "error")
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    operation_id: str
    status: WorkspaceOperationStatus
    result: _struct_pb2.Struct
    error: _error_pb2.Error
    def __init__(self, operation_id: _Optional[str] = ..., status: _Optional[_Union[WorkspaceOperationStatus, str]] = ..., result: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., error: _Optional[_Union[_error_pb2.Error, _Mapping]] = ...) -> None: ...
