# Taskless workspace transfer audit identity

Production Web acceptance reached repository attachment successfully, then
failed while copying the workspace into a new session. Copy first acquires a
read-only workspace lease without a model Task. Kata restores use the audited
egress gateway, which resolves grants through `sandbox_envs.task_id`; an empty
task ID therefore prevented the otherwise authorized restore.

The Broker now assigns `workspace-transfer-<lease-id>` only when that ready
lease has no Task. The conditional transaction cannot overwrite a concurrently
bound real Task and preserves busy/expiry fields. It creates no `tasks` or
`model_usage` row. Each lease and tenant retains its own identity, exact storage
hostname grant, quota and revocation. Cloudflare's internal transfer path uses
the same persisted identity. Reusing that environment for a real Task revokes
remaining internal transfer grants before changing the binding.

Restoration occurs before `withEnv`. When the Broker must issue proxy credentials
at that point, it now reads the current tenant policy and signs its actual egress
quota instead of an empty quota. Policy errors fail closed. Existing normal
proxy credentials retain their previous path.

Regression uses real ready leases and SQLite/Postgres: distinct lease/tenant
grants, revoking one without changing another, preserving a real Task, rejecting
stale binding, preserving busy protection, validating signed quota claims, and
revoking old internal grants before actual reuse. Full Broker and Sandbox Service
packages passed (0.034s and 48.608s); `go vet` and whitespace checks passed.

A production Kata reproduction script is prepared at
`.goal-tools/kata-taskless-diagnose.py`: clone/snapshot in a new isolated zero-model
tenant, revoke original grants, acquire a taskless read-only lease, verify the
restored README and lease-specific revoked audit rows, then clean the isolated
VMs, objects and rows. It must run only after the updated Sandbox Service rollout;
the code regression does not claim that production acceptance has already run.
