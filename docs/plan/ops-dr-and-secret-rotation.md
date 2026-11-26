# CloudNativePG Backup/Restore DR & Secret Rotation Runbook (P2-1)

## 1. Non-Destructive Backup & Restore Verification (`ar-node-1`)

- **Cluster**: `cluster.postgresql.cnpg.io/pg` in namespace `data` (1 instance, healthy primary `pg-1`).
- **Scheduled Backup**: `scheduledbackup.postgresql.cnpg.io/pg-daily` with plugin object-store backups (`backup/pg-daily-20261125030000` completed, `backup/pg-daily-20261126030000` completed).
- **2026-11-26 Non-Destructive Restore Drill**:
  - Executed `pg_dump -U postgres -Fc agentruntime | pg_restore -U postgres -d dr_drill_verify` against a temporary database inside `pg-1`, verified row counts, and dropped `dr_drill_verify` immediately afterward:
    - `registry_entries`: `130` (source) vs `130` (restored)
    - `tasks`: `773` (source) vs `773` (restored)
  - Logical dump/restore integrity confirmed with zero production disruption.
- **2026-11-27 Offsite R2 Cold-Restore Drill (`pg-dr-test`)**:
  - Deployed isolated `Cluster/pg-dr-test` (`instances: 1`, `10Gi` PVC) in namespace `data` with `bootstrap.recovery.source: pg-r2-source` pointing at `objectstore.barmancloud.cnpg.io/r2` (`s3://agentruntime-backups/pg-cluster`, `serverName: pg`).
  - Reached `Cluster in healthy state` in **12m24s** (`2026-11-26T18:19:35Z` → `18:31:59Z`, full base backup restore + 15+ hours of WAL replay from `2026-11-26T03:00:03Z`).
  - Verified all user databases (`agentruntime`, `coding_dance`, `coding_github`, `langfuse`, `temporal`, `temporal_visibility`) and exact table row counts matched live `pg-1`:
    - `agentruntime.registry_entries`: `139` (source) vs `139` (restored)
    - `agentruntime.tasks`: `773` (source) vs `773` (restored)
    - `coding_github.evolution_candidates`: `2` (restored)
  - Deleted `cluster.postgresql.cnpg.io/pg-dr-test` cleanly immediately after verification.

## 2. Full Cluster Recovery Procedure (R2 / S3 Offsite Object Store)

To recover from node loss onto a fresh Kubernetes host (while preserving isolated secrets from `/root/ar-secrets`):

1. Re-apply sealed object-store credentials (`cnpg` S3/R2 secret) and `cluster.postgresql.cnpg.io/pg` manifest with `bootstrap.recovery` pointing to the external Barman/plugin object store path.
2. Wait for `pg-1` readiness (`kubectl -n data wait --for=condition=Ready cluster/pg --timeout=10m`), then verify key tables (`registry_entries`, `tasks`, `evolution_candidates`, `evolution_audit_log`).
3. Re-deploy `agent-runtime` via Helm (`scripts/deploy_clusters.sh --apply`) and reconcile `coding-web`.

## 3. Secret Rotation Runbook (`scripts/rotate_secrets.sh`)

- Dry-run check (default):
  ```bash
  ./scripts/rotate_secrets.sh --dry-run
  ./scripts/rotate_secrets.sh --ssh ar-node-1 --ssh-jump win-gw --dry-run
  ```
  *(Verified 2026-11-27 via `win-gw` jump against `ar-node-1` across `crawler,admin,registry`.)*
- Apply rotation for crawler or admin service tokens (never prints plaintext secrets):
  ```bash
  ./scripts/rotate_secrets.sh --ssh ar-node-1 --targets crawler --apply
  ./scripts/rotate_secrets.sh --ssh ar-node-1 --targets admin --apply
  ```
  *(Note: `cloudnium_cloud` retired on 2026-11-27; optional overseas crawler stack moved to `deploy/modules/crawler-overseas/`.)*
- Coordinated `registry-token` rotation (`--targets registry --apply`) atomically updates both `coding-admin` (`registry-token`) and `agent-runtime-secrets` (`_platform["registry/publish-token"]`), then performs rolling restarts of `ar-agent-runtime-task-service`, `coding-web`, and `ar-agent-runtime-evolution-runner`.
