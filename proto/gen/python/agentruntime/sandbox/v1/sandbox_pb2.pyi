from agentruntime.common.v1 import tenant_pb2 as _tenant_pb2
from agentruntime.environment.v1 import environment_pb2 as _environment_pb2
from agentruntime.workspace.v1 import workspace_pb2 as _workspace_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ListFilesRequest(_message.Message):
    __slots__ = ("tenant", "workspace_id", "path")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    workspace_id: str
    path: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., workspace_id: _Optional[str] = ..., path: _Optional[str] = ...) -> None: ...

class ReadFileRequest(_message.Message):
    __slots__ = ("tenant", "workspace_id", "path")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    workspace_id: str
    path: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., workspace_id: _Optional[str] = ..., path: _Optional[str] = ...) -> None: ...

class GrepRequest(_message.Message):
    __slots__ = ("tenant", "workspace_id", "pattern", "path", "max_matches")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    PATTERN_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    MAX_MATCHES_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    workspace_id: str
    pattern: str
    path: str
    max_matches: int
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., workspace_id: _Optional[str] = ..., pattern: _Optional[str] = ..., path: _Optional[str] = ..., max_matches: _Optional[int] = ...) -> None: ...

class GrepMatch(_message.Message):
    __slots__ = ("path", "line", "text")
    PATH_FIELD_NUMBER: _ClassVar[int]
    LINE_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    path: str
    line: int
    text: str
    def __init__(self, path: _Optional[str] = ..., line: _Optional[int] = ..., text: _Optional[str] = ...) -> None: ...

class GrepResponse(_message.Message):
    __slots__ = ("matches", "truncated")
    MATCHES_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    matches: _containers.RepeatedCompositeFieldContainer[GrepMatch]
    truncated: bool
    def __init__(self, matches: _Optional[_Iterable[_Union[GrepMatch, _Mapping]]] = ..., truncated: _Optional[bool] = ...) -> None: ...

class GrepLeaseResponse(_message.Message):
    __slots__ = ("matches", "truncated")
    MATCHES_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    matches: _containers.RepeatedCompositeFieldContainer[GrepMatch]
    truncated: bool
    def __init__(self, matches: _Optional[_Iterable[_Union[GrepMatch, _Mapping]]] = ..., truncated: _Optional[bool] = ...) -> None: ...

class GetDiffRequest(_message.Message):
    __slots__ = ("tenant", "workspace_id", "base", "find_renames")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    BASE_FIELD_NUMBER: _ClassVar[int]
    FIND_RENAMES_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    workspace_id: str
    base: str
    find_renames: bool
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., workspace_id: _Optional[str] = ..., base: _Optional[str] = ..., find_renames: _Optional[bool] = ...) -> None: ...

class GetLogRequest(_message.Message):
    __slots__ = ("tenant", "workspace_id", "limit", "base")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    BASE_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    workspace_id: str
    limit: int
    base: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., workspace_id: _Optional[str] = ..., limit: _Optional[int] = ..., base: _Optional[str] = ...) -> None: ...

class ListFilesResponse(_message.Message):
    __slots__ = ("response",)
    RESPONSE_FIELD_NUMBER: _ClassVar[int]
    response: _workspace_pb2.ListFilesResponse
    def __init__(self, response: _Optional[_Union[_workspace_pb2.ListFilesResponse, _Mapping]] = ...) -> None: ...

class ReadFileResponse(_message.Message):
    __slots__ = ("response",)
    RESPONSE_FIELD_NUMBER: _ClassVar[int]
    response: _workspace_pb2.ReadFileResponse
    def __init__(self, response: _Optional[_Union[_workspace_pb2.ReadFileResponse, _Mapping]] = ...) -> None: ...

class GetDiffResponse(_message.Message):
    __slots__ = ("response",)
    RESPONSE_FIELD_NUMBER: _ClassVar[int]
    response: _workspace_pb2.GetDiffResponse
    def __init__(self, response: _Optional[_Union[_workspace_pb2.GetDiffResponse, _Mapping]] = ...) -> None: ...

class GetLogResponse(_message.Message):
    __slots__ = ("response",)
    RESPONSE_FIELD_NUMBER: _ClassVar[int]
    response: _workspace_pb2.GetLogResponse
    def __init__(self, response: _Optional[_Union[_workspace_pb2.GetLogResponse, _Mapping]] = ...) -> None: ...

class DescribeTenantRequest(_message.Message):
    __slots__ = ("tenant",)
    TENANT_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ...) -> None: ...

class DescribeTenantResponse(_message.Message):
    __slots__ = ("backends", "archive_mode", "workspace_max_bytes", "max_workspaces", "gateway", "kind", "isolation")
    BACKENDS_FIELD_NUMBER: _ClassVar[int]
    ARCHIVE_MODE_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_MAX_BYTES_FIELD_NUMBER: _ClassVar[int]
    MAX_WORKSPACES_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    ISOLATION_FIELD_NUMBER: _ClassVar[int]
    backends: _containers.RepeatedScalarFieldContainer[str]
    archive_mode: bool
    workspace_max_bytes: int
    max_workspaces: int
    gateway: bool
    kind: str
    isolation: str
    def __init__(self, backends: _Optional[_Iterable[str]] = ..., archive_mode: _Optional[bool] = ..., workspace_max_bytes: _Optional[int] = ..., max_workspaces: _Optional[int] = ..., gateway: _Optional[bool] = ..., kind: _Optional[str] = ..., isolation: _Optional[str] = ...) -> None: ...

class Spec(_message.Message):
    __slots__ = ("runtime", "cpu_millis", "memory_mb", "idle_timeout_s", "workspace_id", "image")
    RUNTIME_FIELD_NUMBER: _ClassVar[int]
    CPU_MILLIS_FIELD_NUMBER: _ClassVar[int]
    MEMORY_MB_FIELD_NUMBER: _ClassVar[int]
    IDLE_TIMEOUT_S_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    runtime: str
    cpu_millis: int
    memory_mb: int
    idle_timeout_s: int
    workspace_id: str
    image: str
    def __init__(self, runtime: _Optional[str] = ..., cpu_millis: _Optional[int] = ..., memory_mb: _Optional[int] = ..., idle_timeout_s: _Optional[int] = ..., workspace_id: _Optional[str] = ..., image: _Optional[str] = ...) -> None: ...

class AcquireRequest(_message.Message):
    __slots__ = ("tenant", "spec", "task_id", "writable", "reuse", "busy_for_ms", "prepare", "cooling", "project_environment", "flush_writable")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    SPEC_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    WRITABLE_FIELD_NUMBER: _ClassVar[int]
    REUSE_FIELD_NUMBER: _ClassVar[int]
    BUSY_FOR_MS_FIELD_NUMBER: _ClassVar[int]
    PREPARE_FIELD_NUMBER: _ClassVar[int]
    COOLING_FIELD_NUMBER: _ClassVar[int]
    PROJECT_ENVIRONMENT_FIELD_NUMBER: _ClassVar[int]
    FLUSH_WRITABLE_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    spec: Spec
    task_id: str
    writable: bool
    reuse: bool
    busy_for_ms: int
    prepare: bool
    cooling: bool
    project_environment: bool
    flush_writable: bool
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., spec: _Optional[_Union[Spec, _Mapping]] = ..., task_id: _Optional[str] = ..., writable: _Optional[bool] = ..., reuse: _Optional[bool] = ..., busy_for_ms: _Optional[int] = ..., prepare: _Optional[bool] = ..., cooling: _Optional[bool] = ..., project_environment: _Optional[bool] = ..., flush_writable: _Optional[bool] = ...) -> None: ...

class Backend(_message.Message):
    __slots__ = ("name", "kind", "isolation")
    NAME_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    ISOLATION_FIELD_NUMBER: _ClassVar[int]
    name: str
    kind: str
    isolation: str
    def __init__(self, name: _Optional[str] = ..., kind: _Optional[str] = ..., isolation: _Optional[str] = ...) -> None: ...

class Prepared(_message.Message):
    __slots__ = ("environment_id", "setup", "start")
    ENVIRONMENT_ID_FIELD_NUMBER: _ClassVar[int]
    SETUP_FIELD_NUMBER: _ClassVar[int]
    START_FIELD_NUMBER: _ClassVar[int]
    environment_id: str
    setup: str
    start: str
    def __init__(self, environment_id: _Optional[str] = ..., setup: _Optional[str] = ..., start: _Optional[str] = ...) -> None: ...

class Lease(_message.Message):
    __slots__ = ("lease_id", "backend", "backend_env_id", "reused", "fallback_from", "fallback_reason", "network", "test_env_names", "archive", "writable", "restored_snapshot", "prepared", "record", "cost_usd", "environment_json")
    LEASE_ID_FIELD_NUMBER: _ClassVar[int]
    BACKEND_FIELD_NUMBER: _ClassVar[int]
    BACKEND_ENV_ID_FIELD_NUMBER: _ClassVar[int]
    REUSED_FIELD_NUMBER: _ClassVar[int]
    FALLBACK_FROM_FIELD_NUMBER: _ClassVar[int]
    FALLBACK_REASON_FIELD_NUMBER: _ClassVar[int]
    NETWORK_FIELD_NUMBER: _ClassVar[int]
    TEST_ENV_NAMES_FIELD_NUMBER: _ClassVar[int]
    ARCHIVE_FIELD_NUMBER: _ClassVar[int]
    WRITABLE_FIELD_NUMBER: _ClassVar[int]
    RESTORED_SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    PREPARED_FIELD_NUMBER: _ClassVar[int]
    RECORD_FIELD_NUMBER: _ClassVar[int]
    COST_USD_FIELD_NUMBER: _ClassVar[int]
    ENVIRONMENT_JSON_FIELD_NUMBER: _ClassVar[int]
    lease_id: str
    backend: Backend
    backend_env_id: str
    reused: bool
    fallback_from: str
    fallback_reason: str
    network: str
    test_env_names: _containers.RepeatedScalarFieldContainer[str]
    archive: bool
    writable: bool
    restored_snapshot: str
    prepared: Prepared
    record: _environment_pb2.EnvRecord
    cost_usd: float
    environment_json: str
    def __init__(self, lease_id: _Optional[str] = ..., backend: _Optional[_Union[Backend, _Mapping]] = ..., backend_env_id: _Optional[str] = ..., reused: _Optional[bool] = ..., fallback_from: _Optional[str] = ..., fallback_reason: _Optional[str] = ..., network: _Optional[str] = ..., test_env_names: _Optional[_Iterable[str]] = ..., archive: _Optional[bool] = ..., writable: _Optional[bool] = ..., restored_snapshot: _Optional[str] = ..., prepared: _Optional[_Union[Prepared, _Mapping]] = ..., record: _Optional[_Union[_environment_pb2.EnvRecord, _Mapping]] = ..., cost_usd: _Optional[float] = ..., environment_json: _Optional[str] = ...) -> None: ...

class AcquireResponse(_message.Message):
    __slots__ = ("lease",)
    LEASE_FIELD_NUMBER: _ClassVar[int]
    lease: Lease
    def __init__(self, lease: _Optional[_Union[Lease, _Mapping]] = ...) -> None: ...

class ManifestRequest(_message.Message):
    __slots__ = ("tenant", "lease_id")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    LEASE_ID_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    lease_id: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., lease_id: _Optional[str] = ...) -> None: ...

class ExecRequest(_message.Message):
    __slots__ = ("tenant", "lease_id", "argv", "timeout_ms", "stdin", "env", "max_output_bytes", "offload_inline_bytes")
    class EnvEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    TENANT_FIELD_NUMBER: _ClassVar[int]
    LEASE_ID_FIELD_NUMBER: _ClassVar[int]
    ARGV_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    STDIN_FIELD_NUMBER: _ClassVar[int]
    ENV_FIELD_NUMBER: _ClassVar[int]
    MAX_OUTPUT_BYTES_FIELD_NUMBER: _ClassVar[int]
    OFFLOAD_INLINE_BYTES_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    lease_id: str
    argv: _containers.RepeatedScalarFieldContainer[str]
    timeout_ms: int
    stdin: bytes
    env: _containers.ScalarMap[str, str]
    max_output_bytes: int
    offload_inline_bytes: int
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., lease_id: _Optional[str] = ..., argv: _Optional[_Iterable[str]] = ..., timeout_ms: _Optional[int] = ..., stdin: _Optional[bytes] = ..., env: _Optional[_Mapping[str, str]] = ..., max_output_bytes: _Optional[int] = ..., offload_inline_bytes: _Optional[int] = ...) -> None: ...

class ExecResponse(_message.Message):
    __slots__ = ("has_exit_code", "exit_code", "stdout", "stderr", "timed_out", "cpu_time_ms", "stdout_truncated", "stderr_truncated", "stdout_object", "stderr_object")
    HAS_EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    STDOUT_FIELD_NUMBER: _ClassVar[int]
    STDERR_FIELD_NUMBER: _ClassVar[int]
    TIMED_OUT_FIELD_NUMBER: _ClassVar[int]
    CPU_TIME_MS_FIELD_NUMBER: _ClassVar[int]
    STDOUT_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    STDERR_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    STDOUT_OBJECT_FIELD_NUMBER: _ClassVar[int]
    STDERR_OBJECT_FIELD_NUMBER: _ClassVar[int]
    has_exit_code: bool
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    cpu_time_ms: int
    stdout_truncated: bool
    stderr_truncated: bool
    stdout_object: ObjectReference
    stderr_object: ObjectReference
    def __init__(self, has_exit_code: _Optional[bool] = ..., exit_code: _Optional[int] = ..., stdout: _Optional[bytes] = ..., stderr: _Optional[bytes] = ..., timed_out: _Optional[bool] = ..., cpu_time_ms: _Optional[int] = ..., stdout_truncated: _Optional[bool] = ..., stderr_truncated: _Optional[bool] = ..., stdout_object: _Optional[_Union[ObjectReference, _Mapping]] = ..., stderr_object: _Optional[_Union[ObjectReference, _Mapping]] = ...) -> None: ...

class WriteFileRequest(_message.Message):
    __slots__ = ("tenant", "lease_id", "path", "data")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    LEASE_ID_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    lease_id: str
    path: str
    data: bytes
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., lease_id: _Optional[str] = ..., path: _Optional[str] = ..., data: _Optional[bytes] = ...) -> None: ...

class WriteFileResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ReadLeaseFileRequest(_message.Message):
    __slots__ = ("tenant", "lease_id", "path")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    LEASE_ID_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    lease_id: str
    path: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., lease_id: _Optional[str] = ..., path: _Optional[str] = ...) -> None: ...

class ListLeaseDirRequest(_message.Message):
    __slots__ = ("tenant", "lease_id", "path")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    LEASE_ID_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    lease_id: str
    path: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., lease_id: _Optional[str] = ..., path: _Optional[str] = ...) -> None: ...

class ReadLeaseFileResponse(_message.Message):
    __slots__ = ("data",)
    DATA_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    def __init__(self, data: _Optional[bytes] = ...) -> None: ...

class DirEntry(_message.Message):
    __slots__ = ("name", "type", "size")
    NAME_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    name: str
    type: str
    size: int
    def __init__(self, name: _Optional[str] = ..., type: _Optional[str] = ..., size: _Optional[int] = ...) -> None: ...

class ListLeaseDirResponse(_message.Message):
    __slots__ = ("entries",)
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    entries: _containers.RepeatedCompositeFieldContainer[DirEntry]
    def __init__(self, entries: _Optional[_Iterable[_Union[DirEntry, _Mapping]]] = ...) -> None: ...

class GrepLeaseRequest(_message.Message):
    __slots__ = ("tenant", "lease_id", "pattern", "path", "max_matches")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    LEASE_ID_FIELD_NUMBER: _ClassVar[int]
    PATTERN_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    MAX_MATCHES_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    lease_id: str
    pattern: str
    path: str
    max_matches: int
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., lease_id: _Optional[str] = ..., pattern: _Optional[str] = ..., path: _Optional[str] = ..., max_matches: _Optional[int] = ...) -> None: ...

class ManifestEntry(_message.Message):
    __slots__ = ("path", "type", "mode", "size", "sha256", "link")
    PATH_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    LINK_FIELD_NUMBER: _ClassVar[int]
    path: str
    type: str
    mode: int
    size: int
    sha256: str
    link: str
    def __init__(self, path: _Optional[str] = ..., type: _Optional[str] = ..., mode: _Optional[int] = ..., size: _Optional[int] = ..., sha256: _Optional[str] = ..., link: _Optional[str] = ...) -> None: ...

class ManifestResponse(_message.Message):
    __slots__ = ("entries",)
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    entries: _containers.RepeatedCompositeFieldContainer[ManifestEntry]
    def __init__(self, entries: _Optional[_Iterable[_Union[ManifestEntry, _Mapping]]] = ...) -> None: ...

class ExportArchiveRequest(_message.Message):
    __slots__ = ("tenant", "lease_id", "max_bytes")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    LEASE_ID_FIELD_NUMBER: _ClassVar[int]
    MAX_BYTES_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    lease_id: str
    max_bytes: int
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., lease_id: _Optional[str] = ..., max_bytes: _Optional[int] = ...) -> None: ...

class ExportArchiveResponse(_message.Message):
    __slots__ = ("data",)
    DATA_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    def __init__(self, data: _Optional[bytes] = ...) -> None: ...

class ImportArchiveHeader(_message.Message):
    __slots__ = ("tenant", "lease_id", "size", "max_bytes")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    LEASE_ID_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    MAX_BYTES_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    lease_id: str
    size: int
    max_bytes: int
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., lease_id: _Optional[str] = ..., size: _Optional[int] = ..., max_bytes: _Optional[int] = ...) -> None: ...

class ImportArchiveRequest(_message.Message):
    __slots__ = ("header", "chunk")
    HEADER_FIELD_NUMBER: _ClassVar[int]
    CHUNK_FIELD_NUMBER: _ClassVar[int]
    header: ImportArchiveHeader
    chunk: bytes
    def __init__(self, header: _Optional[_Union[ImportArchiveHeader, _Mapping]] = ..., chunk: _Optional[bytes] = ...) -> None: ...

class ImportArchiveResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class PreviewAddressRequest(_message.Message):
    __slots__ = ("tenant", "lease_id", "port")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    LEASE_ID_FIELD_NUMBER: _ClassVar[int]
    PORT_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    lease_id: str
    port: int
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., lease_id: _Optional[str] = ..., port: _Optional[int] = ...) -> None: ...

class PreviewAddressResponse(_message.Message):
    __slots__ = ("address",)
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    address: str
    def __init__(self, address: _Optional[str] = ...) -> None: ...

class ReleaseRequest(_message.Message):
    __slots__ = ("tenant", "lease_id", "keep", "cpu_time_ms", "skip_snapshot")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    LEASE_ID_FIELD_NUMBER: _ClassVar[int]
    KEEP_FIELD_NUMBER: _ClassVar[int]
    CPU_TIME_MS_FIELD_NUMBER: _ClassVar[int]
    SKIP_SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    lease_id: str
    keep: bool
    cpu_time_ms: int
    skip_snapshot: bool
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., lease_id: _Optional[str] = ..., keep: _Optional[bool] = ..., cpu_time_ms: _Optional[int] = ..., skip_snapshot: _Optional[bool] = ...) -> None: ...

class ReleaseResponse(_message.Message):
    __slots__ = ("lease",)
    LEASE_FIELD_NUMBER: _ClassVar[int]
    lease: Lease
    def __init__(self, lease: _Optional[_Union[Lease, _Mapping]] = ...) -> None: ...

class SnapshotRequest(_message.Message):
    __slots__ = ("tenant", "lease_id")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    LEASE_ID_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    lease_id: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., lease_id: _Optional[str] = ...) -> None: ...

class SnapshotResponse(_message.Message):
    __slots__ = ("snapshot_id", "bytes", "files")
    SNAPSHOT_ID_FIELD_NUMBER: _ClassVar[int]
    BYTES_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    snapshot_id: str
    bytes: int
    files: int
    def __init__(self, snapshot_id: _Optional[str] = ..., bytes: _Optional[int] = ..., files: _Optional[int] = ...) -> None: ...

class DiscardRequest(_message.Message):
    __slots__ = ("tenant", "lease_id", "reason")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    LEASE_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    lease_id: str
    reason: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., lease_id: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class DiscardResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class DestroySessionRequest(_message.Message):
    __slots__ = ("tenant", "workspace_id", "session_id")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    workspace_id: str
    session_id: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., workspace_id: _Optional[str] = ..., session_id: _Optional[str] = ...) -> None: ...

class DestroyWorkspaceRequest(_message.Message):
    __slots__ = ("tenant", "workspace_id")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    workspace_id: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., workspace_id: _Optional[str] = ...) -> None: ...

class DestroyTenantRequest(_message.Message):
    __slots__ = ("tenant",)
    TENANT_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ...) -> None: ...

class DestroySessionResponse(_message.Message):
    __slots__ = ("destroyed",)
    DESTROYED_FIELD_NUMBER: _ClassVar[int]
    destroyed: int
    def __init__(self, destroyed: _Optional[int] = ...) -> None: ...

class DestroyWorkspaceResponse(_message.Message):
    __slots__ = ("destroyed",)
    DESTROYED_FIELD_NUMBER: _ClassVar[int]
    destroyed: int
    def __init__(self, destroyed: _Optional[int] = ...) -> None: ...

class DestroyTenantResponse(_message.Message):
    __slots__ = ("destroyed",)
    DESTROYED_FIELD_NUMBER: _ClassVar[int]
    destroyed: int
    def __init__(self, destroyed: _Optional[int] = ...) -> None: ...

class PreparedTransfer(_message.Message):
    __slots__ = ("transfer_id", "sha256", "size_bytes")
    TRANSFER_ID_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    transfer_id: str
    sha256: str
    size_bytes: int
    def __init__(self, transfer_id: _Optional[str] = ..., sha256: _Optional[str] = ..., size_bytes: _Optional[int] = ...) -> None: ...

class ObjectReference(_message.Message):
    __slots__ = ("object_key", "object_uri", "sha256", "size_bytes")
    OBJECT_KEY_FIELD_NUMBER: _ClassVar[int]
    OBJECT_URI_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    object_key: str
    object_uri: str
    sha256: str
    size_bytes: int
    def __init__(self, object_key: _Optional[str] = ..., object_uri: _Optional[str] = ..., sha256: _Optional[str] = ..., size_bytes: _Optional[int] = ...) -> None: ...

class PrepareTransferRequest(_message.Message):
    __slots__ = ("tenant", "lease_id", "task_id", "path", "name")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    LEASE_ID_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    lease_id: str
    task_id: str
    path: str
    name: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., lease_id: _Optional[str] = ..., task_id: _Optional[str] = ..., path: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class PrepareTransferResponse(_message.Message):
    __slots__ = ("transfer",)
    TRANSFER_FIELD_NUMBER: _ClassVar[int]
    transfer: PreparedTransfer
    def __init__(self, transfer: _Optional[_Union[PreparedTransfer, _Mapping]] = ...) -> None: ...

class CommitTransferRequest(_message.Message):
    __slots__ = ("tenant", "lease_id", "transfer_id")
    TENANT_FIELD_NUMBER: _ClassVar[int]
    LEASE_ID_FIELD_NUMBER: _ClassVar[int]
    TRANSFER_ID_FIELD_NUMBER: _ClassVar[int]
    tenant: _tenant_pb2.TenantContext
    lease_id: str
    transfer_id: str
    def __init__(self, tenant: _Optional[_Union[_tenant_pb2.TenantContext, _Mapping]] = ..., lease_id: _Optional[str] = ..., transfer_id: _Optional[str] = ...) -> None: ...

class CommitTransferResponse(_message.Message):
    __slots__ = ("object",)
    OBJECT_FIELD_NUMBER: _ClassVar[int]
    object: ObjectReference
    def __init__(self, object: _Optional[_Union[ObjectReference, _Mapping]] = ...) -> None: ...
