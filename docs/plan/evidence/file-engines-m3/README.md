# M3 engine and file authority verification

These are reproducible manual probes for this worktree, not claims of production
rollout. Run with `PYTHONPATH=services/py-worker/src` and the fixed file-engines
Python environment. The visual probe also needs the approved LibreOffice and
Poppler executables on `PATH`.

- `engine_probe.py`: real DuckDB CSV aggregate; denied file SQL, SQL writes and
  multiple statements; column subset isolation; XLSX cell/formula edits;
  DOCX run-preserving replacement; PPTX text replacement; actual Notebook kernel
  output. Inputs remain separate from candidates.
- `visual_probe.py`: PPTX replacement image, immutable candidate, independent
  package reconstruction and LibreOffice/Poppler render comparison outside the
  declared changed page region; untouched page is pixel-identical. XLSX enlarged
  cell bounds and TSV aggregation also checked.
- `join_probe.py`: two explicitly mounted CSV inputs, selected-column table,
  SQL join and arithmetic producing 26; both source hashes retained.
- `authority-sqlite-race.log` and `authority-postgres.log`: temporary Go probe
  used real catalog migrations/providers. Checked private/read/write grants,
  multi-root revocation, consumer cancellation/retry, collection generation
  freeze, concurrent publish CAS, immutable aliases across rename/head changes,
  version author metadata, snapshot retention, orphan and derived cleanup,
  and original/derived image grants with post-revocation denial. Also verifies
  retained-version quota and owner/read/write UI permission projections. The
  PostgreSQL probe detected numeric SUM decoding; quota aggregates now explicitly
  cast to BIGINT before the shared provider decoding.
  The temporary test was removed after execution; a local replay copy is
  `/tmp/file-intelligence-authority-probe_test.go`.
- `gc-cli-result.json`: actual bounded `task-service file-gc` invocation against
  an isolated empty provider store. Deletion scenarios are covered by the
  authority probes, not by this empty-store CLI smoke check.

## Recalculation boundary

`recalculate.py` implements a separate system-Python UNO worker: private profile
and pipe, disabled macros/external refresh, explicit `calculateAll`, `storeToURL`,
and post-save formula/error/cache/expected-value checks. The Linux processing
image must supply `libreoffice-calc`, `python3-uno`, and `AR_UNO_PYTHON` pointing to
the matching system Python. The original macOS host lacked pyuno and Docker, so the initial local report
left actual UNO execution unverified. That gap was closed on 2026-11-10 by the
isolated Linux image and real `calculateAll` acceptance described in
[Linux validation](../file-engines-linux/README.md). Both date systems, stale
caches, formula failures, forbidden dependencies, preservation and timeout
cleanup were exercised. LibreOffice 24.2.7 does not support XLOOKUP; it returns an
explicit failed verification rather than a claimed result.

## Limits and publication

L1 verifies operation-bound OOXML member preservation. L2 requires independent
render verification with frozen allowed visual regions, and fails closed if the
renderer is missing or the ten-page verification limit is exceeded. Applying an
L2 edit alone returns a partial candidate with `verification.passed=false`.
Formula caches are explicitly invalidated after workbook editing; without a
successful UNO operation they remain `not_recalculated`. The engine returns
candidates and verification only; the authenticated File API performs publish
with source epoch/head CAS and current permission checks.
