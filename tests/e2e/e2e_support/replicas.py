"""两个 task-service 副本的整套（TSP-2，docs/plan/workspace-read-latency-and-disk-2026-11-03.md 的 TS-PROD-01 ③）。

与会话级的 Stack 分开：自己的 Temporal 开发服务、Worker 与业务库，两个 task-service 副本 a、b 共用一个 Valkey（AR_VALKEY_URL）
与同一个业务库（SQLite 时同一个 AR_DATA_DIR，设了 AR_RELATIONAL_DSN 时同一个 schema，这时用生产模式）。
同一个 Temporal 上的 task-service 都是 Session 工作流的 Worker，Session 的 activity 落在哪个副本上不定，所以 Cache 必须共享。
"""

from __future__ import annotations

import os
import time
import urllib.request
from dataclasses import dataclass, field

from e2e_support.stack import (
    CONTINUE_AS_NEW_TASKS,
    ENV_TENANT_POLICIES,
    READ_ONLY_LIMIT,
    Process,
    Stack,
    free_port,
    wait_until,
)
from e2e_support.valkey import Valkey

REPLICAS = ("a", "b")


@dataclass
class ReplicaStack(Stack):
    valkey: Valkey | None = None
    ports: dict[str, int] = field(default_factory=dict)

    def url(self, replica: str) -> str:
        return f"http://127.0.0.1:{self.ports[replica]}"

    def start(self) -> None:
        self.valkey = Valkey.start()
        self.gate_dir.mkdir(parents=True, exist_ok=True)
        self.set_pins({"recipe": "stub@v1", "node_templates": {"stub_answer": "v1"}, "model_profiles": {"mock/model": "v1"}, "tenant_policy": "v1"})
        self.tenant_policies_file.write_text('{"tenants": {}}', "utf-8")
        self.faults.clear()
        self._start_temporal()
        self._build_task_service()
        for replica in REPLICAS:
            self.start_replica(replica)
        self.http_url = self.url(REPLICAS[0])
        self.start_worker()

    def start_replica(self, replica: str) -> None:
        assert self.valkey is not None and self.task_service_bin is not None
        port = self.ports.setdefault(replica, free_port())
        business = self.business_db.env()
        env = {
            **os.environ,
            "AR_TEMPORAL_ADDRESS": self.temporal_address,
            "AR_HTTP_ADDR": f"127.0.0.1:{port}",
            "AR_PUBLIC_BASE_URL": self.url(replica),
            "AR_DEV_SIGNING_KEY": self.signing_key,
            "AR_DATA_DIR": str(self.root / "data"),
            **business,
            # Postgres 时按生产模式跑：业务库与 Cache 都不许回落开发实现（TS-PROD-01）。
            "AR_PROFILE": "production" if business.get("AR_RELATIONAL_DSN") else "development",
            "AR_VALKEY_URL": self.valkey.url,
            "AR_PINNED_VERSIONS_FILE": str(self.pins_file),
            "AR_SESSION_READ_ONLY_LIMIT": str(READ_ONLY_LIMIT),
            "AR_SESSION_CAN_TASKS": str(CONTINUE_AS_NEW_TASKS),
            ENV_TENANT_POLICIES: str(self.tenant_policies_file),
            **self.shared_store_env(),
        }
        name = f"task-service-{replica}"
        p = Process(name, [str(self.task_service_bin), "serve"], env, self.root / "logs" / f"{name}.log")
        p.start()
        self.processes[name] = p
        time.sleep(0.5)
        if not p.alive():
            raise RuntimeError(f"{name} 没有起来")
        wait_until(lambda: urllib.request.urlopen(self.url(replica) + "/healthz", timeout=2).status == 200, f"{name} 就绪")

    def kill_replica(self, replica: str) -> None:
        self.processes[f"task-service-{replica}"].kill()

    def stop(self) -> None:
        for name in ("worker", *(f"task-service-{r}" for r in REPLICAS), "temporal"):
            if name in self.processes:
                self.processes[name].stop()
        if self.valkey is not None:
            self.valkey.stop()
        self.business_db.drop()
