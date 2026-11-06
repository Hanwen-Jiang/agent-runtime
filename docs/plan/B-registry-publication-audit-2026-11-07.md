# B Registry capability publication audit — 2026-11-07

Frozen source: `dcf11ca248d129bb7ec4cdd467539e2bcfd5c262`. Independent worktree `.worktrees/B-audit-registry-20261107/agent-runtime`. No live Registry call, deployment, model invocation, or frozen Memory/Gateway change.

## Confirmed promotion race and fix

Under PostgreSQL READ COMMITTED, a capability-only promotion can read default v1, then another authorized promotion commits a changed-price v3, then the original transaction retires *whatever is now default* and promotes its old v1-derived v2. The sequential baseline check does not protect the gap between SELECT and UPDATE. A deterministic real-Postgres regression reproduced success where rejection was required (`B-audit-registry-race.log`).

Retirement now compares the exact version read and checks affected rows. Losing the comparison rolls back and retries. On a capability promotion, the retry rechecks its certificate baseline and rejects it; the concurrent model/price publication survives. This is a portable SQL change with no schema or migration changes. The regression pauses only the first publisher immediately after its actual database baseline read; the second publisher uses the unwrapped real store and HTTP service.

Validation: existing full Registry SQLite/Postgres race suite passed before adding the interleaving test (57.894s). After the fix, capability gates/authentication/normal promotion plus the new real-Postgres interleaving passed five times with `-race` (19.851s). The subsequent full Registry race run failed an existing independent `TestPersonalConfigConcurrentIdenticalPublish/postgres`: identical concurrent documents produced user/v2; this path does not invoke the modified promotion method. It is **not** reported as a full-suite pass and is being handled separately. Existing Python publication suites: 16 passed, 4.51s.

## Additional confirmed publication completeness gap

`matrix.parse_plan` freezes n=100, r=1, seed=0, extend=false. `SyntheticEval(pool=500).items(100,0)` supplies the exact expected 100 IDs. Results retaining only the same first40 IDs for two candidates, each with known cost, correct plan hash and frozen deployment, are still publishable. `score_results` passes repeats but not the frozen base item IDs/count to the publication gate. `mx_registry_publication.prepare` derives its ledger sample coverage from the rows that exist, not the full pre-registered cohort.

Reproduction: `.goal-tools/B_partial_cohort_regression.py`, run with `PYTHONPATH=services/py-learning/tests:services/py-learning/src:services/py-worker/src:libs/py-core/src:proto/gen/python /opt/evals/venv-2d94680/bin/python -m pytest -q -p no:cacheprovider <repro>`; expected rejection fails in0.45s (`B-partial-cohort-red.log`). Fix should compare the actual frozen base cohort returned by the selected evaluator, so a dataset with fewer than n items remains valid when its whole selected cohort is present. Do not equate the whole source dataset with the pre-registered sample. Current historical BFCL candidates already have100 distinct items (Fable20 is excluded), so this finding does not invalidate those complete candidates. B did not change scoring or A's publication implementation.

## Boundaries reviewed

Publisher authentication is checked before both promotion schemas. Capability-only evidence is platform Model Profile only; all non-score fields and every retained/unmodified score dimension are compared. Publish rejects duplicate score dimensions and invalid tiers. Schema gates enforce >=40 items, complete repeats, known cost, contamination<=5%, coverage and bounded CI; code checks source digest and score formula. The authorized evaluator validates raw reports/ledger; Registry does not itself rerun the experiment. Python paths verify dataset/source/report hashes, known ledger attempts, unchanged strict quality scores, current default revision/content, and recover lost ACKs through status queries rather than blind replay.

## Resource window

Light tests used GOMAXPROCS4 and the cached CI toolchain. Go windows (UTC): original full suite20:03:15–20:04:24; fixed full suite20:07:44–20:08:49; targeted repeated suite20:10:02–20:10:25. These overlap another thread's `fc-pin-measure-20261107` measurement and must be considered before publishing those timing samples. No further heavy suite was started after the coordinator's instruction.

## Independent PersonalConfig race resolved

The earlier full-suite failure was made deterministic with real PostgreSQL interleavings. An empty-default read followed by another publisher committing v1 caused the first writer to read MAX(version)=1 separately and create identical user/v2. The same two-snapshot window also accepted expected_version=user/v1 after another writer had already committed user/v2, incorrectly overwriting it with user/v3. Both cases failed against the frozen source (`B-audit-personal-race-red.log`).

The fix reads the latest entry (version, document and active/retired state) in one query, allocates the next version from that same snapshot, and retires an active default using a version comparison plus affected-row check. Conflicts retry: an identical concurrent publication reuses the winning version; an explicit stale expected version fails. Retired entries still advance the immutable version counter without being treated as an active default. No new migration, schema or permission change.

Validation: PersonalConfig privacy/immutability/none-pinning, identical concurrent publish, retry selection pinning and both deterministic PostgreSQL interleavings all passed five times with race detection (40.749s), including SQLite and PostgreSQL for the existing tests. `git diff --check` passed. UTC test window20:14:40–20:15:28; no new broad suite was run. The earlier full-suite failure remains recorded rather than relabeled as green. The separate Python fixed-cohort completeness finding remains handed off to its owner; current complete100-item historical candidates were not invalidated. No paid calls or online changes occurred.
