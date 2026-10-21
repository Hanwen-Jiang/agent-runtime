"""独立运行 Memory Service：python -m agentruntime_memory。

环境变量：
- AR_MEMORY_ADDRESS：监听地址，默认 127.0.0.1:50061。
- AR_DEV_SIGNING_KEY：开发令牌的签名密钥（与 task-service 同一把），必填。
- AR_RELATIONAL_DSN：业务库用 Postgres（行级安全）；不设时用 AR_DATA_DIR 下的 SQLite。
- AR_QDRANT_URL：向量库用 Qdrant；不设时用内存实现（进程退出即丢，只适合本地试用）。
- AR_DATA_DIR：SQLite 与默认密钥文件的目录，默认 ./.agentruntime/memory。
- AR_SECRETS_FILE：开发版 SecretStore 的文件，默认 AR_DATA_DIR 下的 secrets.json。

嵌入暂用确定的哈希嵌入；Model Gateway 接好后换成 GatewayEmbedder。提交的候选在后台处理。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from agentruntime_core.adapters.keyring import RelationalKeyRing
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.dev.vector_store import InMemoryVectorStoreProvider

from agentruntime_memory.embedding import HashingEmbedder
from agentruntime_memory.identity import DevTokenAuthenticator
from agentruntime_memory.server import start_server
from agentruntime_memory.service import MemoryService

log = logging.getLogger("agentruntime_memory")


async def serve() -> None:
    signing_key = os.environ.get("AR_DEV_SIGNING_KEY", "")
    if not signing_key:
        raise SystemExit("要设置 AR_DEV_SIGNING_KEY")
    data_dir = Path(os.environ.get("AR_DATA_DIR", ".agentruntime/memory"))
    data_dir.mkdir(parents=True, exist_ok=True)
    secrets = LocalFileSecretStoreProvider(Path(os.environ.get("AR_SECRETS_FILE", data_dir / "secrets.json")))
    dsn = os.environ.get("AR_RELATIONAL_DSN", "")
    if dsn:
        from agentruntime_core.adapters.postgres import PostgresRelationalStoreProvider

        relational = await PostgresRelationalStoreProvider.connect(dsn)
    else:
        relational = SQLiteRelationalStoreProvider(data_dir / "db")
    keyring = RelationalKeyRing(relational, secrets)
    qdrant_url = os.environ.get("AR_QDRANT_URL", "")
    if qdrant_url:
        from agentruntime_core.adapters.qdrant import QdrantVectorStoreProvider

        vectors = QdrantVectorStoreProvider(url=qdrant_url, keys=keyring)
    else:
        log.warning("没有设置 AR_QDRANT_URL：向量放在内存里，进程退出即丢")
        vectors = InMemoryVectorStoreProvider()
    service = MemoryService(relational=relational, vectors=vectors, keyring=keyring, embedder=HashingEmbedder())
    await service.setup()
    server, port = await start_server(
        service, DevTokenAuthenticator(signing_key.encode()), os.environ.get("AR_MEMORY_ADDRESS", "127.0.0.1:50061"), drain_in_background=True
    )
    log.info("Memory Service 在端口 %d 上监听", port)
    try:
        await server.wait_for_termination()
    finally:
        await vectors.aclose()
        await relational.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())
