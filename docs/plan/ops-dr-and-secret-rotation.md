# CloudNativePG Backup/Restore DR & Secret Rotation Runbook (P2-1)

## 1. Non-Destructive Backup & Restore Verification (`ar-node-1`)

- **Cluster**: `cluster.postgresql.cnpg.io/pg` in namespace `data` (1 instance, healthy primary `pg-1`).
- **Scheduled Backup**: `scheduledbackup.postgresql.cnpg.io/pg-daily` with plugin object-store backups (`backup/pg-daily-20261125030000` completed, `backup/pg-daily-20261126030000` completed).
- **2026-11-26 Non-Destructive Restore Drill**:
  - Executed `pg_dump -U postgres -Fc agentruntime | pg_restore -U postgres -d dr_drill_verify` against a temporary database inside `pg-1`, verified row counts, and dropped `dr_drill_verify` immediately afterward:
    - `registry_entries`: `130` (source) vs `130` (restored)
    - `tasks`: `773` (source) vs `773` (restored)
  - Logical dump/restore integrity confirmed with zero production disruption.

## 2. Full Cluster Recovery Procedure (R2 / S3 Offsite Object Store)

To recover from node loss onto a fresh Kubernetes host (while preserving isolated secrets from `/root/ar-secrets`):

1. Re-apply sealed object-store credentials (`cnpg` S3/R2 secret) and `cluster.postgresql.cnpg.io/pg` manifest with `bootstrap.recovery` pointing to the external Barman/plugin object store path.
2. Wait for `pg-1` readiness (`kubectl -n data wait --for=condition=Ready cluster/pg --timeout=10m`), then verify key tables (`registry_entries`, `tasks`, `evolution_candidates`, `evolution_audit_log`).
3. Re-deploy `agent-runtime` via Helm (`scripts/deploy_clusters.sh --apply`) and reconcile `coding-web`.

## 3. Secret Rotation Runbook (`scripts/rotate_secrets.sh`)

- Dry-run check (default):
  ```bash
  ./scripts/rotate_secrets.sh --dry-run
  ./scripts/rotate_secrets.sh --ssh cloudnium_cloud --dry-run
  ```
- Apply rotation for crawler or admin service tokens (never prints plaintext secrets):
  ```bash
  ./scripts/rotate_secrets.sh --ssh cloudnium_cloud --targets crawler --apply
  ./scripts/rotate_secrets.sh --ssh ar-node-1 --targets admin --apply
  ```
- Coordinated `registry-token` rotation (`--targets registry --apply`) atomically updates both `coding-admin` (`registry-token`) and `agent-runtime-secrets` (`_platform["registry/publish-token"]`), then performs rolling restarts of `ar-agent-runtime-task-service`, `coding-web`, and `ar-agent-runtime-evolution-runner`.
