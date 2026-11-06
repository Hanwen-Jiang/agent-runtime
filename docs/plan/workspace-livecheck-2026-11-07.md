# Workspace independent integration verification

Source: independent Runtime worktree `goal/livecheck-ws-20261107`, merging `cc2e00fa` with the patched async final-result activity (`abb69a68`) and D-25 safety fixes (`4f9de331`, `83fc74d8`). Coding worktree HEAD `7adbeba0` merges `50bab02` and contains the durable HTTP 202 journal/browser polling implementation plus personal configuration privacy fix. No deployment or model calls were performed.

Additional fix: a committed asynchronous admission with no saved result and a definitively missing Temporal execution now retains a queryable FAILED result with `workspace_operation_history_unavailable`. Its message explicitly states that effects are unknown and callers must reconcile instead of automatically repeating it. An unconfirmed Start cannot trigger this: the admission transaction commits only after Start confirms success or Describe finds the prior execution. Ordinary transport failures remain retryable and are not converted to terminal results. Concurrent result persistence keeps an already saved result.

Validation on the real SSH host with `agentruntime-ci:3`:

- `go test -race ./services/go-task-service/internal/workspaceapi -count=1`: passed, 2.597s. New coverage verifies retained unknown outcome after history removal, unchanged idempotent admission, and a query racing an unconfirmed Start whose transaction rolls back.
- Coding `go test -race ./internal/server -run TestWorkspaceHTTP -count=1`: passed, 1.150s; short admission, disconnect, duplicate/concurrent requests, restart, scoped status and persisted failures.
- Coding complete `internal/server` suite: 161 top-level tests passed, no skips. Its compiled test binary ran with `CODING_TEST_BROWSER=/usr/bin/chromium` in the matching copied source directory; real Chromium covered English/Chinese environment, workspace and multirepo pages.
- `pytest tests/test_workspace_queue.py tests/test_workspace_git_scope.py tests/test_workspace_multirepo.py -q -p no:cacheprovider`: 16 passed, 23.59s. Includes real Temporal local server, legacy static root-history replay, durable result retry/replay, real Git confinement and real Sandbox Service tests in both mounted and archived modes.

Logs at `/root/proj/agent-projs/.goal-tools/livecheck-ws-{runtime-go,http,web-full-fixed,python-fixed}.log`. The initial browser run used an incorrect cwd and failed two source-file checks despite passing Chromium cases; the corrected full run passed. The first Python run lacked `python` on agent PATH; corrected PATH uses the existing Python 3.13.16 toolchain and all cases pass. Neither environmental failure was suppressed or counted as passed.

Production deployment, Cloudflare-route smoke and broader Goal acceptance remain for the main integrator. Test environments were stopped by their fixtures; no tenant data or production settings were changed.
