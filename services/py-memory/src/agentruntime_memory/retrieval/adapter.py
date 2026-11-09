"""LlamaIndex adapter over an already tenant-bound Runtime VectorStore.

No LlamaIndex provider plugin or default external embedding model is used.
Runtime owns encryption, tenancy, embedding versions and the single RRF pass.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from llama_index.core.retrievers import BaseRetriever
from llama_index.core.schema import BaseNode, MetadataMode, NodeWithScore, QueryBundle, TextNode
from llama_index.core.vector_stores.types import BasePydanticVectorStore, VectorStoreQuery, VectorStoreQueryResult
from pydantic import PrivateAttr

from agentruntime_core.errors import InvalidError
from agentruntime_core.providers.vector_store import Filter, VectorRecord, VectorStore
from agentruntime_memory.embedding import sparse_vector


class RuntimeVectorAdapter(BasePydanticVectorStore):
    stores_text: bool = False
    corpus: str
    embedding_version: str
    _store: VectorStore = PrivateAttr()
    _filter: Filter | None = PrivateAttr(default=None)

    def __init__(self, store: VectorStore, *, corpus: str, embedding_version: str, authorized_filter: Filter | None = None) -> None:
        super().__init__(corpus=corpus, embedding_version=embedding_version)
        self._store, self._filter = store, authorized_filter

    @property
    def client(self) -> VectorStore:
        return self._store

    def add(self, nodes: Sequence[BaseNode], **kwargs: Any) -> list[str]:
        raise RuntimeError("Use async_add with the Runtime asynchronous provider")

    def query(self, query: VectorStoreQuery, **kwargs: Any) -> VectorStoreQueryResult:
        raise RuntimeError("Use aquery with the Runtime asynchronous provider")

    def delete(self, ref_doc_id: str, **kwargs: Any) -> None:
        raise RuntimeError("Delete exact generation node IDs through adelete_nodes")

    async def async_add(self, nodes: Sequence[BaseNode], **kwargs: Any) -> list[str]:
        records = [VectorRecord(id=node.node_id, dense=tuple(node.get_embedding()), embedding_version=self.embedding_version,
                                sparse=sparse_vector(node.get_content(metadata_mode=MetadataMode.NONE)), fields=dict(node.metadata)) for node in nodes]
        await self._store.upsert(self.corpus, records)
        return [record.id for record in records]

    async def aquery(self, query: VectorStoreQuery, **kwargs: Any) -> VectorStoreQueryResult:
        if query.filters is not None:
            raise InvalidError("retrieval_filter", "Filter authority is supplied by the Runtime, not query metadata")
        sparse = sparse_vector(query.query_str or "")
        hits = await self._store.query(self.corpus, embedding_version=self.embedding_version,
                                       top_k=query.similarity_top_k, dense=query.query_embedding,
                                       sparse=sparse if sparse.indices else None, filter=self._filter)
        return VectorStoreQueryResult(ids=[hit.id for hit in hits], similarities=[hit.score for hit in hits],
                                      nodes=[TextNode(id_=hit.id, text="", metadata=dict(hit.fields)) for hit in hits])

    async def adelete_nodes(self, node_ids: list[str] | None = None, filters: Any = None, **kwargs: Any) -> None:
        if filters is not None or node_ids is None:
            raise InvalidError("retrieval_delete", "Deletion requires exact generation node IDs")
        await self._store.delete(self.corpus, node_ids)


class RuntimeRetriever(BaseRetriever):
    def __init__(self, adapter: RuntimeVectorAdapter, *, top_k: int) -> None:
        super().__init__()
        self.adapter, self.top_k = adapter, top_k

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        raise RuntimeError("Use aretrieve")

    async def _aretrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        result = await self.adapter.aquery(VectorStoreQuery(query_embedding=query_bundle.embedding,
                                                           query_str=query_bundle.query_str, similarity_top_k=self.top_k))
        return [NodeWithScore(node=node, score=score) for node, score in zip(result.nodes or [], result.similarities or [], strict=True)]
