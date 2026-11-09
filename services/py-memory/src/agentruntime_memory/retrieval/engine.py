"""Bounded ingestion and retrieval, using LlamaIndex nodes and Runtime providers.

The SQL ledger is staged metadata, never collection publication authority.
Go's File Catalog owns active generations, snapshots and the final ACL check.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import Document, QueryBundle

from agentruntime_core.errors import ConflictError, DeniedError, InvalidError, NotFoundError
from agentruntime_core.providers.relational_store import Migration, RelationalStoreProvider
from agentruntime_core.providers.vector_store import AnyOf, Filter, Hit, VectorRecord, VectorStore, VectorStoreProvider
from agentruntime_memory.embedding import Embedder, sparse_vector
from agentruntime_memory.identity import Identity
from .adapter import RuntimeRetriever, RuntimeVectorAdapter

CORPUS = "document_chunks"
MIGRATIONS = (Migration(421, "unified_retrieval_generations", (
    "CREATE TABLE retrieval_generations (collection_id TEXT NOT NULL, generation TEXT NOT NULL, owner_id TEXT NOT NULL, workspace_id TEXT NOT NULL, embedding_version TEXT NOT NULL, manifest_hash TEXT NOT NULL, state TEXT NOT NULL, PRIMARY KEY(collection_id, generation))",
    "CREATE TABLE retrieval_nodes (node_id TEXT PRIMARY KEY, collection_id TEXT NOT NULL, generation TEXT NOT NULL, version_id TEXT NOT NULL, representation_id TEXT NOT NULL, block_id TEXT NOT NULL, body TEXT NOT NULL, anchor TEXT NOT NULL, ordinal BIGINT NOT NULL)",
)),)
Authorize = Callable[[Identity, str, str, Sequence[str]], Awaitable[set[str]]]
Rerank = Callable[[Identity, str, Sequence[str]], Awaitable[Sequence[float]]]
_ID = re.compile(r"^[A-Za-z0-9_.:@-]{1,200}$")


def _id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise InvalidError("retrieval_identity", f"Invalid {name}")
    return value


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


class RetrievalEngine:
    def __init__(self, *, vectors: VectorStoreProvider, embedder: Embedder,
                 relational: RelationalStoreProvider | None = None, authorize: Authorize | None = None,
                 rerank: Rerank | None = None, chunk_size: int = 600, chunk_overlap: int = 80) -> None:
        if not 100 <= chunk_size <= 1000 or not 0 <= chunk_overlap <= min(80, chunk_size // 2):
            raise ValueError("Invalid retrieval chunk bounds")
        self.vectors, self.embedder, self.relational = vectors, embedder, relational
        self.authorize, self.rerank = authorize, rerank
        # UTF-8 bytes are a conservative tokenizer bound, deterministic and
        # offline. No implicit tiktoken/model download occurs at request time.
        self.splitter = SentenceSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap,
            tokenizer=lambda text: list(text.encode("utf-8")),
            chunking_tokenizer_fn=lambda text: re.findall(r"[^.!?。！？\n]+[.!?。！？\n]*|[.!?。！？\n]+", text),
            include_metadata=False, include_prev_next_rel=False)
        self.chunker = f"llama-index-sentence-utf8bound-v1:{chunk_size}:{chunk_overlap}"
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    async def setup(self) -> None:
        if self.relational is not None:
            await self.relational.migrate(MIGRATIONS)

    async def embed(self, identity: Identity, texts: Sequence[str]) -> list[tuple[float, ...]]:
        vectors = await self.embedder.embed(identity, texts)
        if len(vectors) != len(texts) or any(not vector or not all(math.isfinite(x) for x in vector) for vector in vectors):
            raise InvalidError("invalid_embeddings", "Embedding service returned invalid vectors")
        return vectors

    async def upsert_bound(self, store: VectorStore, corpus: str, records: Sequence[VectorRecord]) -> None:
        await store.upsert(corpus, records)

    async def get_bound(self, store: VectorStore, corpus: str, ids: Sequence[str]) -> list[VectorRecord]:
        return await store.get(corpus, ids)

    async def delete_bound(self, store: VectorStore, corpus: str, ids: Sequence[str]) -> int:
        return await store.delete(corpus, ids)

    async def recall_bound(self, store: VectorStore, *, corpus: str, query: str, embedding_version: str,
                           top_k: int, dense: Sequence[float], authorized_filter: Filter | None = None) -> list[Hit]:
        adapter = RuntimeVectorAdapter(store, corpus=corpus, embedding_version=embedding_version, authorized_filter=authorized_filter)
        results = await RuntimeRetriever(adapter, top_k=top_k).aretrieve(QueryBundle(query_str=query, embedding=list(dense)))
        return [Hit(id=result.node.node_id, score=float(result.score or 0), fields=result.node.metadata) for result in results]

    def _store(self, identity: Identity):
        if not identity.user_id or not identity.workspace_id or self.relational is None:
            raise DeniedError("retrieval_scope", "Document retrieval requires a user and workspace")
        return self.relational.bind(identity.tenant_context())

    async def _authorized(self, identity: Identity, collection: str, generation: str, versions: Sequence[str]) -> set[str]:
        if self.authorize is None:
            raise DeniedError("retrieval_authority", "File authorization is not configured")
        return set(await self.authorize(identity, collection, generation, versions)) & set(versions)

    async def _generation(self, identity: Identity, collection: str, generation: str, *, owner_only: bool = False) -> dict[str, Any]:
        async with self._store(identity).transaction(read_only=True) as tx:
            row = await tx.query_one("SELECT * FROM retrieval_generations WHERE collection_id=$1 AND generation=$2 AND workspace_id=$3", (collection, generation, identity.workspace_id))
        if row is None or owner_only and row["owner_id"] != identity.user_id:
            raise NotFoundError("retrieval_generation", "Generation not found")
        return dict(row)

    async def index(self, identity: Identity, collection: str, generation: str, documents: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        collection, generation = _id(collection, "collection"), _id(generation, "generation")
        if not isinstance(documents,list) or not documents or len(documents) > 500 or any(not isinstance(document,dict) for document in documents) or len(_json(documents).encode()) > 20 << 20:
            raise InvalidError("retrieval_input_limit", "Index input exceeds ingestion bounds")
        versions = [_id(document.get("version_id"), "version_id") for document in documents]
        representations = [(version, _id(document.get("representation_id"), "representation_id")) for version,document in zip(versions,documents,strict=True)]
        if len(set(representations)) != len(representations):
            raise InvalidError("retrieval_duplicate_representation", "A generation may contain each immutable representation only once")
        if await self._authorized(identity, collection, generation, versions) != set(versions):
            raise DeniedError("retrieval_denied", "Index source is not authorized")
        manifest = _digest([identity.user_id, identity.workspace_id, documents, self.embedder.version, self.chunker])
        key = (identity.tenant_id, collection, generation)
        async with self._locks.setdefault(key, asyncio.Lock()):
            store = self._store(identity)
            async with store.transaction() as tx:
                await tx.execute("INSERT INTO retrieval_generations (collection_id,generation,owner_id,workspace_id,embedding_version,manifest_hash,state) VALUES ($1,$2,$3,$4,$5,$6,'building') ON CONFLICT DO NOTHING", (collection,generation,identity.user_id,identity.workspace_id,self.embedder.version,manifest))
                row = await tx.query_one("SELECT * FROM retrieval_generations WHERE collection_id=$1 AND generation=$2", (collection,generation))
                if row is None or row["manifest_hash"] != manifest or row["state"] == "deleted":
                    raise ConflictError("retrieval_generation_conflict", "Generation identity is immutable")
                if row["state"] == "staged":
                    count = await tx.query_one("SELECT COUNT(*) AS n FROM retrieval_nodes WHERE collection_id=$1 AND generation=$2", (collection,generation))
                    return {"generation":generation,"status":"staged","chunks":count["n"],"embedding_version":self.embedder.version,"chunker":self.chunker}
            staged = []
            for document in documents:
                version = document["version_id"]
                representation = _id(document.get("representation_id"), "representation_id")
                blocks = document.get("blocks", [])
                if not isinstance(blocks,list) or len(blocks)>10000:
                    raise InvalidError("retrieval_blocks", "Invalid source blocks")
                block_ids = set()
                for block in blocks:
                    if not isinstance(block,dict):
                        raise InvalidError("retrieval_block", "Invalid source block")
                    block_id = _id(block.get("id"), "block_id")
                    if block_id in block_ids:
                        raise InvalidError("retrieval_block_id", "Duplicate block identity")
                    block_ids.add(block_id)
                    text, anchor = block.get("text", ""), block.get("anchor", {})
                    if not isinstance(text,str) or not isinstance(anchor,dict):
                        raise InvalidError("retrieval_block", "Block text and anchor required")
                    if not text.strip():
                        continue
                    doc = Document(id_=version+":"+block_id, text=text)
                    nodes = self.splitter.get_nodes_from_documents([doc])
                    for ordinal,node in enumerate(nodes):
                        node.id_ = "chunk_"+_digest([identity.tenant_id,collection,generation,version,representation,block_id,self.embedder.version,self.chunker,ordinal,node.text])
                        node.metadata = {"owner_id":identity.user_id,"workspace_id":identity.workspace_id,"collection_id":collection,"generation":generation,"version_id":version,"representation_id":representation,"block_id":block_id}
                        selected = {**anchor,"block_id":block_id,"char_start":node.start_char_idx,"char_end":node.end_char_idx}
                        staged.append((node,selected,ordinal))
            if len(staged)>20000:
                raise InvalidError("retrieval_chunk_limit", "Generation contains too many chunks")
            adapter = RuntimeVectorAdapter(self.vectors.bind(identity.tenant_context()),corpus=CORPUS,embedding_version=self.embedder.version)
            # Register exact cleanup IDs before any external vector mutation.
            # A generation row lock fences deletion across service replicas.
            async with store.transaction() as tx:
                updated=await tx.execute("UPDATE retrieval_generations SET state=state WHERE collection_id=$1 AND generation=$2 AND manifest_hash=$3 AND state='building'",(collection,generation,manifest))
                if not updated:raise ConflictError("retrieval_generation_conflict", "Generation is not writable")
                for node,anchor,ordinal in staged:
                    await tx.execute("INSERT INTO retrieval_nodes (node_id,collection_id,generation,version_id,representation_id,block_id,body,anchor,ordinal) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT DO NOTHING", (node.node_id,collection,generation,node.metadata["version_id"],node.metadata["representation_id"],node.metadata["block_id"],node.text,_json(anchor),ordinal))
            for start in range(0,len(staged),32):
                batch = staged[start:start+32]
                dense = await self.embed(identity,[node.text for node,_,_ in batch])
                for (node,_,_),vector in zip(batch,dense,strict=True): node.embedding=list(vector)
                async with store.transaction() as tx:
                    updated=await tx.execute("UPDATE retrieval_generations SET state=state WHERE collection_id=$1 AND generation=$2 AND manifest_hash=$3 AND state='building'",(collection,generation,manifest))
                    if not updated:raise ConflictError("retrieval_generation_conflict", "Generation was cancelled during indexing")
                    await adapter.async_add([node for node,_,_ in batch])
            if await self._authorized(identity,collection,generation,versions)!=set(versions):
                raise DeniedError("retrieval_denied", "Index source authorization changed")
            async with store.transaction() as tx:
                updated=await tx.execute("UPDATE retrieval_generations SET state='staged' WHERE collection_id=$1 AND generation=$2 AND manifest_hash=$3 AND state='building'", (collection,generation,manifest))
                if not updated:
                    raise ConflictError("retrieval_generation_conflict", "Generation was cancelled during indexing")
            return {"generation":generation,"status":"staged","chunks":len(staged),"embedding_version":self.embedder.version,"chunker":self.chunker}

    async def search(self, identity: Identity, collection: str, generation: str, query: str, *, embedding_version: str, limit: int = 8, version_ids: Sequence[str] | None = None) -> dict[str, Any]:
        collection,generation=_id(collection,"collection"),_id(generation,"generation")
        if not isinstance(limit,(int,float)) or isinstance(limit,bool) or not math.isfinite(limit) or int(limit)!=limit or not 1<=limit<=20:
            raise InvalidError("retrieval_query", "Invalid result limit")
        limit=int(limit)
        if not isinstance(query,str) or not query.strip() or len(query)>20000:
            raise InvalidError("retrieval_query", "Invalid query or result limit")
        if version_ids is not None and (not isinstance(version_ids,list) or len(version_ids)>500 or any(not isinstance(version,str) for version in version_ids)):
            raise InvalidError("retrieval_versions", "Invalid version selection")
        row=await self._generation(identity,collection,generation)
        if row["state"]!="staged" or row["embedding_version"]!=embedding_version or embedding_version!=self.embedder.version:
            raise ConflictError("retrieval_generation_unavailable", "Pinned generation or embedding version unavailable")
        store=self._store(identity)
        async with store.transaction(read_only=True) as tx:
            rows=await tx.query("SELECT DISTINCT version_id FROM retrieval_nodes WHERE collection_id=$1 AND generation=$2",(collection,generation))
        versions={str(r["version_id"]) for r in rows}
        if version_ids is not None: versions &= set(version_ids)
        versions=await self._authorized(identity,collection,generation,sorted(versions))
        if not versions:return {"hits":[],"generation":generation,"coverage":"no_authorized_sources"}
        dense=(await self.embed(identity,[query]))[0]
        must={"owner_id":row["owner_id"],"workspace_id":identity.workspace_id,"collection_id":collection,"generation":generation,"version_id":AnyOf(tuple(sorted(versions)))}
        candidates=await self.recall_bound(self.vectors.bind(identity.tenant_context()),corpus=CORPUS,query=query,embedding_version=embedding_version,top_k=40,dense=dense,authorized_filter=Filter(must=must))
        hits=[]
        async with store.transaction(read_only=True) as tx:
            for candidate in candidates:
                node=await tx.query_one("SELECT * FROM retrieval_nodes WHERE node_id=$1 AND collection_id=$2 AND generation=$3",(candidate.id,collection,generation))
                if node is None or node["version_id"] not in versions:continue
                hits.append({"id":candidate.id,"score":candidate.score,"text":node["body"],"version_id":node["version_id"],"representation_id":node["representation_id"],"anchor":json.loads(node["anchor"]),"quote_hash":hashlib.sha256(node["body"].encode()).hexdigest()})
        if self.rerank and hits:
            allowed=await self._authorized(identity,collection,generation,[hit["version_id"] for hit in hits])
            hits=[hit for hit in hits if hit["version_id"] in allowed]
            scored=hits[:20]
            scores=await self.rerank(identity,query,[hit["text"] for hit in scored]) if scored else []
            if len(scores)!=len(scored) or any(not math.isfinite(value) for value in scores):raise InvalidError("retrieval_rerank", "Invalid rerank scores")
            for hit,score in zip(scored,scores,strict=True):hit["score"]=float(score)
            hits=sorted(scored,key=lambda hit:(-hit["score"],hit["id"]))
        allowed=await self._authorized(identity,collection,generation,[hit["version_id"] for hit in hits])
        if (await self._generation(identity,collection,generation))["state"]!="staged":
            raise ConflictError("retrieval_generation_deleted", "Generation was deleted during query")
        return {"hits":[hit for hit in hits if hit["version_id"] in allowed][:limit],"generation":generation,"embedding_version":embedding_version,"coverage":"retrieved_candidates","retrieval":{"top_k":40,"fusion":"runtime_rrf_60","sparse":"hashed_term_frequency","reranked":bool(self.rerank)}}

    async def resolve(self, identity: Identity, collection: str, generation: str, node_ids: Sequence[str]) -> dict[str, Any]:
        collection,generation=_id(collection,"collection"),_id(generation,"generation")
        if not isinstance(node_ids,list) or not node_ids or len(node_ids)>100 or any(not isinstance(node,str) for node in node_ids):
            raise InvalidError("retrieval_resolve", "Resolve requires 1 to 100 node IDs")
        if (await self._generation(identity,collection,generation))["state"]!="staged":
            raise ConflictError("retrieval_generation_unavailable", "Generation is not available")
        selected=[]
        async with self._store(identity).transaction(read_only=True) as tx:
            for node_id in dict.fromkeys(node_ids):
                row=await tx.query_one("SELECT * FROM retrieval_nodes WHERE node_id=$1 AND collection_id=$2 AND generation=$3",(node_id,collection,generation))
                if row:selected.append(dict(row))
        allowed=await self._authorized(identity,collection,generation,[row["version_id"] for row in selected])
        if (await self._generation(identity,collection,generation))["state"]!="staged":
            raise ConflictError("retrieval_generation_deleted", "Generation was deleted during resolve")
        return {"generation":generation,"evidence":[{"id":row["node_id"],"text":row["body"],"version_id":row["version_id"],"representation_id":row["representation_id"],"anchor":json.loads(row["anchor"]),"quote_hash":hashlib.sha256(row["body"].encode()).hexdigest()} for row in selected if row["version_id"] in allowed]}

    async def delete_generation(self, identity: Identity, collection: str, generation: str) -> dict[str, Any]:
        collection,generation=_id(collection,"collection"),_id(generation,"generation")
        await self._generation(identity,collection,generation,owner_only=True)
        async with self._store(identity).transaction() as tx:
            await tx.execute("UPDATE retrieval_generations SET state='deleted' WHERE collection_id=$1 AND generation=$2",(collection,generation))
            rows=await tx.query("SELECT node_id FROM retrieval_nodes WHERE collection_id=$1 AND generation=$2",(collection,generation))
        ids=[str(row["node_id"]) for row in rows]
        deleted=await self.delete_bound(self.vectors.bind(identity.tenant_context()),CORPUS,ids) if ids else 0
        async with self._store(identity).transaction() as tx:
            await tx.execute("DELETE FROM retrieval_nodes WHERE collection_id=$1 AND generation=$2",(collection,generation))
        return {"generation":generation,"status":"deleted","deleted_chunks":deleted}
