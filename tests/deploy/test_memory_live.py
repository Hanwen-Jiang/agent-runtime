"""docs/plan/memory-live.md：Chart 把 Milvus、真 embedding 与 Worker 接 Memory Service 接起来。默认都不变。"""

from __future__ import annotations

from typing import Any

from deploy_tests import CLOUD_VALUES, by_kind, env_of, pod_spec, render

SET = [
    "external.milvus.uri=http://milvus.data.svc:19530",
    "memory.embeddingVersion=a6api/text-embedding-3-large@2026-10",
    "worker.memory.enabled=true",
]


def deployment(manifests: list[dict[str, Any]], suffix: str) -> dict[str, Any]:
    [found] = [m for m in by_kind(manifests, "Deployment") if m["metadata"]["name"].endswith(suffix)]
    return found


def env(manifests: list[dict[str, Any]], suffix: str) -> dict[str, dict[str, Any]]:
    [container] = pod_spec(deployment(manifests, suffix))["containers"]
    return env_of(container)


def test_off_by_default() -> None:
    manifests = render()
    memory = env(manifests, "-memory")
    assert "AR_MILVUS_URI" not in memory and "AR_EMBEDDING_VERSION" not in memory
    assert "AR_MEMORY_ADDRESS" not in env(manifests, "-worker-task")


def test_on_in_the_cloud_values() -> None:
    """V1-MEM（2026-11-06）：云上接 Milvus 与真 embedding，Worker 接 Memory Service。"""
    manifests = render(CLOUD_VALUES)
    memory = env(manifests, "-memory")
    assert memory["AR_MILVUS_URI"]["value"] == "http://milvus.data.svc:19530" and "AR_QDRANT_URL" not in memory
    assert memory["AR_EMBEDDING_VERSION"]["value"] == "a6api/text-embedding-3-large@2026-10"
    assert "AR_MEMORY_ADDRESS" in env(manifests, "-worker-task")


def test_memory_gets_milvus_and_the_gateway() -> None:
    memory = env(render(CLOUD_VALUES, set_values=[*SET, "external.qdrant.url="]), "-memory")
    assert memory["AR_MILVUS_URI"]["value"] == "http://milvus.data.svc:19530"
    assert memory["AR_EMBEDDING_VERSION"]["value"] == "a6api/text-embedding-3-large@2026-10"
    assert "model-gateway" in memory["AR_MODEL_GATEWAY_ADDRESS"]["value"]
    assert "AR_QDRANT_URL" not in memory


def test_every_worker_pool_that_plans_or_acts_gets_the_address() -> None:
    manifests = render(CLOUD_VALUES, set_values=[*SET, "external.qdrant.url="])
    for pool in ("decision", "model"):
        worker = env(manifests, f"-worker-{pool}")
        assert "-memory" in worker["AR_MEMORY_ADDRESS"]["value"]
        assert "AR_DEV_SIGNING_KEY" in worker, "令牌要签名密钥"
