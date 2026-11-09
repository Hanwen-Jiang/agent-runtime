"""Internal Knowledge RPC: bounded JSON operations behind File Catalog authority.

The Go proxy must resolve collection generation/task snapshots before calling.
Every operation also checks user identity and the current file ACL. The internal
service token is mandatory; an end-user bearer alone cannot ingest invented IR.
"""
from __future__ import annotations

import hmac
from collections.abc import Sequence
from typing import Any

import grpc
from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import payload_pb2
from agentruntime_core.errors import DeniedError, InvalidError, ProviderError
from agentruntime_core.providers.relational_store import RelationalStoreProvider
from agentruntime_memory.identity import Authenticator, Identity, bind_request
from agentruntime_memory.retrieval import RetrievalEngine

SCHEMA_ID = "knowledge_operation_v1"


class CatalogAuthorizer:
    """Read current relational File ACL; no vector payload or user filter is authority."""
    def __init__(self, relational: RelationalStoreProvider) -> None:
        self.relational = relational

    async def __call__(self, identity: Identity, collection: str, generation: str, versions: Sequence[str]) -> set[str]:
        if not identity.user_id or not identity.workspace_id:
            return set()
        allowed = set()
        async with self.relational.bind(identity.tenant_context()).transaction(read_only=True) as tx:
            for version in set(versions):
                row = await tx.query_one("SELECT f.file_id,f.created_by FROM runtime_files f JOIN runtime_file_versions v ON v.file_id=f.file_id WHERE v.version_id=$1 AND f.workspace_id=$2 AND f.lifecycle='active'", (version,identity.workspace_id))
                if row is None:
                    continue
                if row["created_by"] == identity.user_id:
                    allowed.add(version)
                    continue
                grant = await tx.query_one("SELECT file_id FROM runtime_file_grants WHERE file_id=$1 AND workspace_id=$2 AND permission IN ('read','write','owner') AND ((subject_type='user' AND subject_id=$3) OR (subject_type='workspace' AND subject_id=$2)) LIMIT 1", (row["file_id"],identity.workspace_id,identity.user_id))
                if grant is not None:
                    allowed.add(version)
        return allowed


class KnowledgeService:
    def __init__(self, engine: RetrievalEngine) -> None:
        self.engine=engine

    async def execute(self, identity: Identity, operation: str, payload: dict[str,Any]) -> dict[str,Any]:
        required={"index":{"collection_id","generation","documents"},"search":{"collection_id","generation","embedding_version","query"},"resolve":{"collection_id","generation","node_ids"},"delete_generation":{"collection_id","generation"}}
        optional={"index":set(),"search":{"limit","version_ids"},"resolve":set(),"delete_generation":set()}
        if operation not in required or not isinstance(payload,dict) or not required[operation]<=set(payload) or set(payload)-required[operation]-optional[operation]:
            raise InvalidError("knowledge_payload", "Unknown operation or invalid payload fields")
        collection,generation=payload["collection_id"],payload["generation"]
        if operation=="index":
            return await self.engine.index(identity,collection,generation,payload["documents"])
        if operation=="search":
            return await self.engine.search(identity,collection,generation,payload["query"],embedding_version=payload["embedding_version"],limit=payload.get("limit",8),version_ids=payload.get("version_ids"))
        if operation=="resolve":
            return await self.engine.resolve(identity,collection,generation,payload["node_ids"])
        return await self.engine.delete_generation(identity,collection,generation)


def register_knowledge(server: grpc.aio.Server, service: KnowledgeService, authenticator: Authenticator, internal_token: str) -> None:
    # Lazy import preserves existing Memory-only process startup while rolling
    # out the new generated protocol before enabling the Knowledge service.
    from agentruntime.memory.v1 import knowledge_pb2, knowledge_pb2_grpc
    from agentruntime_memory.server import _status

    class Servicer(knowledge_pb2_grpc.KnowledgeServiceServicer):
        async def Execute(self, request: Any, context: grpc.aio.ServicerContext) -> Any:
            try:
                metadata=dict(context.invocation_metadata() or ())
                supplied=str(metadata.get("x-ar-knowledge-token",""))
                if not internal_token or not hmac.compare_digest(supplied.encode(),internal_token.encode()):
                    raise DeniedError("knowledge_service_auth", "Knowledge requires the internal File API caller")
                scheme,_,token=str(metadata.get("authorization","")).partition(" ")
                if scheme.lower()!="bearer":raise DeniedError("unauthenticated","Bearer identity required")
                identity=bind_request(authenticator.authenticate(token),request.tenant if request.HasField("tenant") else None)
                value=json_format.MessageToDict(request.payload.value)
                result=await service.execute(identity,request.operation,value)
                payload=struct_pb2.Value();json_format.ParseDict(result,payload)
                return knowledge_pb2.KnowledgeServiceExecuteResponse(result=payload_pb2.JsonPayload(schema_id=SCHEMA_ID,value=payload))
            except ProviderError as error:
                await context.abort(_status(error),f"{error.code}: {error.message}")
            except Exception:
                await context.abort(grpc.StatusCode.UNAVAILABLE,"knowledge_internal: retrieval operation failed")
    knowledge_pb2_grpc.add_KnowledgeServiceServicer_to_server(Servicer(),server)
