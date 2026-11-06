"""Give direct model evaluations one durable identity in model_usage.

AR_EVAL_GATEWAY_FACTORY=agentruntime_learning.evals.audited_gateway:from_env
requires AR_EVAL_RUN_ID, AR_EVAL_USAGE_DIR and the real Model Gateway address.
Wrap this factory with the existing spending guard. No provider bypass, prompts,
credentials or invented Registry revisions are written to the local journal.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from agentruntime_learning.evals.budget import BudgetError


def task_identity(run_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", run_id):
        raise ValueError("AR_EVAL_RUN_ID must be a stable, path-safe experiment name")
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    number = int.from_bytes(hashlib.sha256(run_id.encode()).digest()[:16], "big")
    chars = []
    for _ in range(26):
        chars.append(alphabet[number & 31])
        number >>= 5
    return "tsk_" + "".join(reversed(chars))


class AuditedGateway:
    def __init__(self, inner: Any, directory: Path, run_id: str, tenant: str = "evals", address: str = "") -> None:
        from agentruntime_learning.capability.matrix import DirectoryOwner

        self.inner, self.directory = inner, Path(directory)
        self.owner = DirectoryOwner(self.directory)
        self.lock = asyncio.Lock()
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", tenant):
            self.owner.close()
            raise ValueError("invalid explicitly configured evaluation tenant")
        self.task_id, self.tenant = task_identity(run_id), tenant
        self.identity = {"run_id": run_id, "task_id": self.task_id, "tenant_id": tenant,
                         "source_tenant_id": "evals", "gateway_address": address}
        self.manifest = self.directory / "identity.json"
        self.journal = self.directory / "calls.jsonl"
        try:
            if self.manifest.exists():
                if json.loads(self.manifest.read_text("utf-8")) != self.identity:
                    raise ValueError("direct-evaluation identity changed; use a new output directory")
            else:
                self.manifest.write_text(json.dumps(self.identity, indent=2) + "\n", "utf-8")
            rows = [json.loads(line) for line in self.journal.read_text("utf-8").splitlines() if line.strip()] if self.journal.exists() else []
            latest = {row["sequence"]: row for row in rows}
            self.sequence = max(latest, default=0)
            self.unresolved = any(row["state"] != "completed" for row in latest.values())
        except BaseException:
            self.owner.close()
            raise

    def _append(self, row: dict[str, Any]) -> None:
        with self.journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    async def _call(self, method: str, request: Any) -> Any:
        async with self.lock:
            self.owner.require(self.directory)
            if self.unresolved:
                raise BudgetError("direct evaluation has an unsettled call; reconcile model_usage", billed_cost_usd_micros=0)
            # Component evaluators use the reserved evals identity. An explicit
            # per-experiment tenant scopes its own allowance and ledger without
            # allowing arbitrary caller tenants to be silently rewritten.
            if request.tenant.tenant_id != "evals":
                raise BudgetError("evaluation tenant mismatch", billed_cost_usd_micros=0)
            if request.call.max_cost_usd_micros <= 0:
                raise BudgetError("direct evaluations require a bounded Gateway request", billed_cost_usd_micros=0)
            call = type(request).FromString(request.SerializeToString())
            call.tenant.tenant_id = self.tenant
            self.sequence += 1
            call.call.task_id = self.task_id
            call.call.node_id = f"eval_call_{self.sequence:08d}"
            row = {
                "sequence": self.sequence, "state": "pending", "method": method,
                "task_id": self.task_id, "node_id": call.call.node_id,
                "source_task_id": request.call.task_id, "source_node_id": request.call.node_id,
                "role": request.call.role, "pinned_model_profile": request.call.pinned_model_profile,
                "max_cost_usd_micros": request.call.max_cost_usd_micros,
                "request_sha256": hashlib.sha256(call.SerializeToString(deterministic=True)).hexdigest(),
            }
            self.unresolved = True
            self._append(row)
            try:
                response = await getattr(self.inner, method)(call)
            except BaseException as exc:
                billed = getattr(exc, "billed_cost_usd_micros", None)
                self._append({**row, "state": "unresolved", "error_type": type(exc).__name__,
                              "known_cost_usd_micros": billed if isinstance(billed, int) and billed >= 0 else None})
                raise
            if not response.HasField("usage") or response.usage.cost_usd_micros < 0:
                self._append({**row, "state": "unresolved", "error_type": "MissingUsage"})
                raise BudgetError("direct evaluation returned no valid usage")
            self._append({**row, "state": "completed", "known_cost_usd_micros": response.usage.cost_usd_micros,
                          "served_model_profile": response.model.model_profile_id,
                          "input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens})
            self.unresolved = False
            return response

    async def generate(self, request: Any) -> Any:
        return await self._call("generate", request)

    async def embed(self, request: Any) -> Any:
        return await self._call("embed", request)

    def close(self) -> None:
        if self.lock.locked():
            raise RuntimeError("cannot close an active evaluation call")
        self.owner.close()

    async def aclose(self) -> None:
        self.close()
        close = getattr(self.inner, "aclose", None)
        if close is not None:
            await close()


def from_env() -> AuditedGateway:
    from agentruntime_worker.executor.grpc_gateway import from_env as grpc_gateway

    run_id, directory = os.environ.get("AR_EVAL_RUN_ID", ""), os.environ.get("AR_EVAL_USAGE_DIR", "")
    task_identity(run_id)
    if not directory:
        raise ValueError("AR_EVAL_USAGE_DIR is required")
    return AuditedGateway(grpc_gateway(), Path(directory), run_id,
                          os.environ.get("AR_EVAL_TENANT", "evals"), os.environ.get("AR_MODEL_GATEWAY_ADDRESS", ""))


async def ledger(directory: Path) -> dict[str, Any]:
    """Read tenant-scoped, per-attempt authoritative costs; never settle rows."""
    from agentruntime_core.adapters.postgres import PostgresRelationalStoreProvider
    from agentruntime_core.tenancy import TenantContext

    identity = json.loads((Path(directory) / "identity.json").read_text("utf-8"))
    dsn = os.environ.get("AR_RELATIONAL_DSN", "")
    if not dsn:
        raise ValueError("AR_RELATIONAL_DSN is required for authoritative reconciliation")
    store = await PostgresRelationalStoreProvider.connect(dsn)
    try:
        async with store.bind(TenantContext(tenant_id=identity["tenant_id"])).transaction(read_only=True) as tx:
            rows = await tx.query(
                "SELECT reservation_id, task_id, node_id, model_profile_id, method, actual_usd_micros, "
                "reserved_usd_micros, registry_scope, registry_revision, registry_document_sha256 "
                "FROM model_usage WHERE task_id = $1 ORDER BY reserved_at, reservation_id", [identity["task_id"]])
        items = [dict(row) for row in rows]
    finally:
        await store.aclose()
    pending = [row for row in items if row["actual_usd_micros"] is None]
    return {**identity, "source": "model_usage", "attempts": items,
            "known_cost_usd_micros": sum(row["actual_usd_micros"] or 0 for row in items),
            "pending_reserved_usd_micros": sum(row["reserved_usd_micros"] for row in pending),
            "cost_known": bool(items) and not pending}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Read the authoritative ledger for a direct evaluation; never call a model or alter accounting")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args()
    report = asyncio.run(ledger(arguments.directory))
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "attempts"}))
