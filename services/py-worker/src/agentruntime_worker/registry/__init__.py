"""Registry 的 Worker 一侧（设计书 3.1.2；T-08，细化见 docs/plan/registry.md）。

- GrpcRegistry：到 task-service 内部端口上 RegistryService 的 gRPC 客户端（client.py）。
- RegistryCatalog：Registry 版的 resolve_catalog activity；RegistryContextSource：Planner 的 Node Catalog 与 Recipe
  取自 Registry（catalog.py）。
- Worker 设了 AR_REGISTRY_ADDRESS 才换上这些；没设时仍用桩（端到端测试的默认不变）。
"""

from __future__ import annotations

from agentruntime_worker.registry.catalog import RegistryCatalog, RegistryContextSource
from agentruntime_worker.registry.client import (
    GrpcRegistry,
    MissingRegistryAddressError,
    closing_registries,
    from_env,
    registry_client,
)

__all__ = [
    "GrpcRegistry",
    "MissingRegistryAddressError",
    "RegistryCatalog",
    "RegistryContextSource",
    "closing_registries",
    "from_env",
    "registry_client",
]
