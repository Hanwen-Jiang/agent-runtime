# Direct transfer and chunk restore live verification, 2026-11-07

Independent worktree: `.worktrees/livecheck-transfer-20261107/agent-runtime`. Base `3c9ffcb5`, atomic restore/commit protection `699531ac` (same change as `66c9d210`), coordinated chunk writer gate `d0d4a15f` (same change as `cc2e00fa`). No deployed services or live user workspace modified.

This increment uses manifest v2 for ordered chunks, accepts historical v1 (including Go empty entries=null), and rejects invalid versions, unsafe/noncanonical paths, duplicate entries, non-directory parents, chunk length/digest mismatch and invalid origin references before copying or restoring metadata. Unknown/cancelled upload acknowledgments retain the reservation for 15 minutes, exceeding the signed grant and bounded transfer lifetime; a missing receipt is not proof that the PUT stopped.

Deployment remains reader-first: update Worker and Sandbox Service readers before enabling `sandboxService.chunkedSnapshots`. The disabled flag only suppresses new chunked writes; it must not suppress reading existing v2 archives. No second upload protocol was added.

## Verified

- Rust suites: 82 tests passed (fc-manager 33 unit + 20 acceptance, agent 27, rs-transfer 2). `.goal-tools/livecheck-transfer-rust.log`.
- Real sandbox-agent HTTP: 6 tests passed, including ordered large file blocks, repeated batch with zero new downloads, immutable prepared bytes, failure recovery and atomic workspace publication. `.goal-tools/livecheck-transfer-http.log`.
- Python direct delivery / chunk checks: 26 passed; existing archive/copy compatibility: 11 passed. `.goal-tools/livecheck-transfer-python.log`, `livecheck-transfer-compat.log`.
- Go direct transfer: four real-agent tests passed (220.420s), including 68 MiB file splitting, repeated snapshot reuse, only changed blocks uploaded and full restored bytes verified. Debug SHA processing accounted for most runtime; optimized agent was subsequently built. `.goal-tools/livecheck-transfer-go.log`.
- Full Go Sandbox Service server package passed with optimized agent (41.377s); archive and config packages passed. `.goal-tools/livecheck-transfer-go-final.log`, `livecheck-transfer-go.log`.
- Real R2: 11 security subcases passed (6.33s), including immutable sandbox-agent PUT and GET restore plus checksum, length, conditional upload, tampering, method/key and expiration rejection. Only this run's scoped objects were removed; temporary 0600 credential environment file was removed; no bucket lifecycle change. `.goal-tools/livecheck-transfer-r2.log`.

These tests establish real agent and ObjectStore transfer behavior. Firecracker node-cache/vsock live verification is a separate remaining check; no mock result or test framework skip is represented as that check.

Model expenditure: $0. No model requests were made. No new production deployment in this worktree.

## Real Firecracker node cache and vsock

The additional opt-in Go test `TestLiveFirecrackerBlockCacheAndTenantIsolation` completed against two real Firecracker VMs and real R2 in **7.34s**, with no skip. Manager had a fresh isolated state directory, cache cap 96 KiB, no warm pool or memory snapshots, 1 vCPU / 256 MiB / 256 MiB scratch per VM. Runtime images included curl and CA certificates; the old deployed Python runtime was found to lack curl and cannot support direct transfer until rebuilt.

Observed behavior: first restore downloaded one block, repeating the batch downloaded zero; a grant issued for VM A was rejected with HTTP 403 when used through VM B's vsock connection even after the object was cached; a separately authorized VM B request hit the shared cache; same-size corruption in this isolated cache caused checksum failure and a fresh R2 fetch. Agent PUT through vsock reached real R2; the repeated prepared-transfer commit succeeded without replacing its immutable bytes. Final counters: hits=2, misses=2, corruptions=1. Capacity/LRU and simultaneous deduplication remain covered by the Rust behavior tests rather than being inferred from these counters.

Artifacts: `.goal-tools/fc-blob-live/{test.log,manager.log,configuration.json,binaries.json,cleanup.json,sweep.log}`. Reproduction uses the opt-in flag `AR_LIVE_FC_BLOB=1`, dedicated `AR_FC_TEST_URL/TOKEN/STATE`, live `AR_S3_*` settings, and `go test ./libs/go-core/adapters/fcsandbox -run '^TestLiveFirecrackerBlockCacheAndTenantIsolation$' -count=1 -v`. Harness `.goal-tools/fc_blob_live_run.py` obtains R2 settings only from the active Sandbox Service deployment references, keeps credentials in memory/private temporary files and never prints them.

Cleanup verified: test_exit=0, sweep_exit=0, remaining VM records=0. R2 objects under the unique test prefix were deleted by test cleanups; the manager token file, cgroup and isolated manager/cache directory were removed. Model cost remains $0. This verifies the real Firecracker data path; production rollout remains a separate root-agent step.
