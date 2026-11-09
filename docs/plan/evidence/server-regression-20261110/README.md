# Isolated Linux server regression evidence

Date: 2026-11-10. Host: the project test server `cloudnium_cloud`.
All commands used the dedicated directory
`/root/proj/agent-projs/.file-intelligence-validation-20261110`, bounded
`fi-20261110-*` containers, and temporary test backends. Production services
were not changed. JSON event logs are gzip-compressed without changing their
contents; `summary.json` records their SHA-256 hashes and actual counts.

## Complete suite and the two corrections

`worker-full.log` is the original complete Worker run with real Temporal and the
compiled Rust sandbox-agent: **1649 passed, 2 failed, 1 skipped**. The opt-in
live Jev call is the one skip. The failures were both patch-convention checks:

- `StageRunner` used `patches.CHILD_FILE_SCOPE_PATCH`, while the existing static
  convention requires an imported constant name. It now imports and uses
  `CHILD_FILE_SCOPE_PATCH` directly; the ID, branch semantics and emitted commands
  are unchanged.
- The N16 wrap-up test incorrectly required its historical patch to remain the
  last entry forever. It now verifies that its original adjacency to N16 is
  preserved. `KNOWN_AT_START` was extended to pin the full current 25-item prefix,
  so reordering/deletion remains forbidden. Both file-operation and child-scope
  IDs are present at the end of that prefix.

After these minimal changes the server ran:

```sh
cd /work/agent-runtime/services/py-worker
python -m pytest -q -p no:cacheprovider \
  tests/test_patches_at_start.py tests/test_sub_agent_wrap_up.py \
  tests/test_replay.py tests/test_stage_replay.py tests/test_workspace_queue.py
python -m pytest -q -p no:cacheprovider tests/test_workspace_replay.py
```

The focused run passed **23 tests**, including the original failures, actual
first-workflow-task marker ordering, all golden histories, command-reordering
mutation checks that must fail replay, and Task/SubAgent/Subgraph histories
created in the same session. The additional immutable workspace-history run
passed **3 tests**. The original full suite was not rerun after these two
corrections; the logs are not presented as a fabricated all-green full rerun.

`file-histories-replay-final.json` records an additional **19/19** replays of
saved real file-operation recovery/failure/cancellation histories and child-scope
histories from the earlier server acceptance. Histories were copied as-is and
not regenerated. Reproduce with `python docs/plan/evidence/server-regression-20261110/replay_saved_histories.py`. The final replay harness rejects empty directories; an earlier
empty-copy invocation was not counted as validation.

## Other server regressions collected here

- Task Service: 16 packages, 630 passing tests/subtests, no failures.
- Model Gateway: 5 packages, 191 passing tests/subtests, no failures.
- Memory: 211 passing tests with the real PostgreSQL/Qdrant test backends.
- Cross-language/schema/provider contracts: 440 passing tests.
- Buf lint and breaking-compatibility commands exited successfully (`buf.log`
  is empty on success).

These logs preserve the root integrator's completed server runs. Later
changed-path checks (collection idempotency, UI, OCR/UNO and semantic retrieval)
have separate evidence records and must not be inferred from these files.

The replay-focused container used `agentruntime-ci:3`, four CPUs, 8 GiB memory,
`/work/.venv-regression/bin/python`, Temporal CLI 1.9.1, and the existing server
Go toolchain. `GOMODCACHE=/work/cache/gomod`, `GOCACHE=/work/cache/worker-go`,
`AR_SANDBOX_AGENT_BIN=/work/bin/sandbox-agent` keep test state inside the isolated
validation directory. No temporary new pytest test file was added to the repo.

## Complete contract/deployment sweep and baseline comparison

The additional server sweep ran `pytest -q tests/contract tests/deploy -k 'not
test_go_modules_build_vet_and_test'` and reported **1042 passed, 4 failed,
26 skipped, 1 deselected**. Its raw output is `contracts-deploy-initial.log`.
The single deselected case recursively repeats the Go module build/vet/test
matrix, already run separately. The four failures were classified and handled:

1. The file lifecycle and operation schemas inlined enum values instead of
   referencing the shared definitions. The exact same values now live in
   `defs.schema.json` as `file_lifecycle`/`file_operation`, and the consumers use
   `$ref`. The convention check was retained, not weakened.
2. The lockfile check could not open the CI image's default uv cache. The rerun
   sets `UV_CACHE_DIR=/work/cache/regression-uv`, without changing repository
   cache policy or suppressing the check.
3. The checked-in Cloudflare image tag is `goal-cf-20261107`, while its existing
   assertion accepts only a numeric release or raw commit hash.
4. The existing Helm template emits `AR_SUB_AGENT_POLICY=off`, while its existing
   assertion expects the variable to be omitted entirely for `off`.

After the enum fix and cache override, the **entire contract suite passed 638
cases**, with the same one explicitly deselected nested Go matrix; `uv lock
--check` is included and passed. Buf lint and breaking checks also exited zero.
See `contracts-final-fixed.log`, `buf-final-fixed.log` and
`contracts-final-status.json`.

The last two deployment failures were independently reproduced against an
isolated `git archive fdac343dae9a893cb80692fa7c0da15538b40902` snapshot on the same
server, using the exact two test node IDs. **Both fail with the same assertions**
(`deploy-baseline-fdac343d.log`). The assertions and eight relevant source/config
files have identical baseline/current SHA-256 values, recorded in
`deploy-baseline-source-comparison.json`. They are pre-existing deployment
expectation mismatches, not regressions introduced by this server-validation
fix. Neither the assertions nor those deployment settings were changed here.
The combined contract/deployment sweep is therefore not represented as an
all-green run; feature-related contract failures are fixed, while these two
baseline inconsistencies remain explicitly recorded.

Subsequent root-integration decision: after the baseline comparison above was
recorded, the root integrator chose to update the two assertions to the already
checked-in semantics (allow the bounded `goal-cf-YYYYMMDD` release-tag family;
require an explicit `AR_SUB_AGENT_POLICY=off`). The baseline hashes and logs here
remain historical evidence from before those assertion edits. The final
standalone deployment rerun is recorded by the root integrator in the overall
server-acceptance report; this subsection does not claim its result in advance.
