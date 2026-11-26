"""独立运行 Memory Service：python -m agentruntime_memory。

环境变量：
- AR_MEMORY_ADDRESS：监听地址，默认 127.0.0.1:50061。
- AR_DEV_SIGNING_KEY：开发令牌的签名密钥（与 task-service 同一把），必填。
- AR_RELATIONAL_DSN：业务库用 Postgres（行级安全）；不设时用 AR_DATA_DIR 下的 SQLite。
- AR_MILVUS_URI 或 AR_QDRANT_URL：向量库，只能设一个；都不设时用内存实现（进程退出即丢，只适合本地试用，设了 AR_RELATIONAL_DSN 时拒绝启动）。
- AR_MODEL_GATEWAY_ADDRESS 与 AR_EMBEDDING_VERSION：经 Model Gateway 嵌入，版本是嵌入模型的 Profile id；都不设时用哈希嵌入（只给开发，设了 AR_RELATIONAL_DSN 时拒绝启动）。
- AR_DATA_DIR：SQLite 与默认密钥文件的目录，默认 ./.agentruntime/memory。
- AR_KNOWLEDGE_RERANK=1 且已配置模型网关时启用文档重排；AR_KNOWLEDGE_RERANK_TOP8_DEPTH
  设置返回上限 ≤8 时每个召回通道的候选数量，范围 1–40，默认 40。更大的查询保留每路 40 个候选。
- AR_SECRETS_FILE：开发版 SecretStore 的文件，默认 AR_DATA_DIR 下的 secrets.json。
- AR_SECRETS_SEED_FILE：启动时并进 AR_SECRETS_FILE 的种子文件（集群里是只读挂载的 K8s Secret；docs/plan/cloud-deploy.md D-04）。

提交的候选在后台处理。选向量库与 embedder 的规则见 agentruntime_memory.wiring。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from agentruntime_core.adapters.keyring import RelationalKeyRing
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider

from agentruntime_memory.cache import TenantQueryEmbeddingCache
from agentruntime_memory.identity import DevTokenAuthenticator, Identity
from agentruntime_memory.metrics import default_metrics, start_metrics_server
from agentruntime_memory.server import start_server
from agentruntime_memory.service import MemoryService
from agentruntime_memory.wiring import (
    code_navigation_enabled,
    embed_cache_config,
    embedder,
    knowledge_rerank_top8_depth,
    knowledge_reranker,
    rerank_margin_threshold,
    rerank_mode,
    vector_store,
    warmup_enabled,
)
from agentruntime_memory.retrieval import RetrievalEngine
from agentruntime_memory.knowledge import CatalogAuthorizer, KnowledgeService

log = logging.getLogger("agentruntime_memory")


async def serve() -> None:
    signing_key = os.environ.get("AR_DEV_SIGNING_KEY", "")
    if not signing_key:
        raise SystemExit("要设置 AR_DEV_SIGNING_KEY")
    data_dir = Path(os.environ.get("AR_DATA_DIR", ".agentruntime/memory"))
    data_dir.mkdir(parents=True, exist_ok=True)
    secrets_file = Path(os.environ.get("AR_SECRETS_FILE", data_dir / "secrets.json"))
    if seed := os.environ.get("AR_SECRETS_SEED_FILE", ""):
        LocalFileSecretStoreProvider(secrets_file).merge_seed(Path(seed))
    secrets = LocalFileSecretStoreProvider(secrets_file)
    dsn = os.environ.get("AR_RELATIONAL_DSN", "")
    if dsn:
        from agentruntime_core.adapters.postgres import PostgresRelationalStoreProvider

        relational = await PostgresRelationalStoreProvider.connect(dsn)
    else:
        relational = SQLiteRelationalStoreProvider(data_dir / "db")
    keyring = RelationalKeyRing(relational, secrets)
    vectors = vector_store(os.environ, keyring)
    embeddings = embedder(os.environ)
    reranker = knowledge_reranker(os.environ)
    metrics = default_metrics()
    cache_max, cache_ttl = embed_cache_config(os.environ)
    query_cache = TenantQueryEmbeddingCache(max_size=cache_max, ttl_seconds=cache_ttl, metrics=metrics)
    metrics_server = None
    if metrics_addr := os.environ.get("AR_MEMORY_METRICS_ADDRESS", "").strip():
        metrics_server = start_metrics_server(metrics_addr, metrics)
        log.info("Memory Service /metrics listening on %s", metrics_addr)
    retrieval = RetrievalEngine(
        vectors=vectors,
        embedder=embeddings,
        relational=relational,
        authorize=CatalogAuthorizer(relational),
        rerank=reranker,
        rerank_top8_depth=knowledge_rerank_top8_depth(os.environ),
        code_nav_enabled=code_navigation_enabled(os.environ),
        rerank_margin_threshold=rerank_margin_threshold(os.environ),
        query_cache=query_cache,
        metrics=metrics,
    )
    await retrieval.setup()
    if warmup_enabled(os.environ):
        warmup_id = Identity(tenant_id="_warmup", workspace_id="_warmup", user_id="_warmup")
        try:
            await embeddings.embed(warmup_id, ["warmup probe"])
            if reranker is not None:
                await reranker(warmup_id, "warmup probe", ["warmup candidate"])
            log.info("Memory Service warmup completed")
        except Exception as exc:  # noqa: BLE001
            log.warning("Memory Service warmup failed (continuing startup): %s", exc)
    # AR_MEMORY_RERANK_MODE（wiring.rerank_mode）：经验检索在 MMR 之后可选 BM25 重排，线上是 bm25_pool50。
    service = MemoryService(relational=relational, vectors=vectors, keyring=keyring, embedder=embeddings, retrieval=retrieval,
                            rerank_mode=rerank_mode(os.environ), metrics=metrics)
    await service.setup()
    server, port = await start_server(
        service, DevTokenAuthenticator(signing_key.encode()), os.environ.get("AR_MEMORY_ADDRESS", "127.0.0.1:50061"), drain_in_background=True,
        knowledge=KnowledgeService(retrieval), knowledge_internal_token=os.environ.get("AR_KNOWLEDGE_INTERNAL_TOKEN", ""),
    )
    log.info("Memory Service 在端口 %d 上监听", port)
    try:
        await server.wait_for_termination()
    finally:
        if metrics_server is not None:
            metrics_server.shutdown()
            metrics_server.server_close()
        await vectors.aclose()
        await relational.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())
