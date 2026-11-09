# Explicit collection selection review — 2026-11-10

A temporary real HTTP handler probe ran on the isolated Linux validation host
under `agentruntime-ci:3`, with 2 CPU, 3 GiB and Go's race detector. It used real
Ingress, SQLite, ObjectStore and file catalog handlers, with a deterministic
Knowledge transport fixture; it does not claim a model/retrieval quality test.

`collection-selection-final.log` records both temporary test groups passing.
The temporary source was removed from local and server packages afterwards; a
local replay copy is `/tmp/file-intelligence-handler-selection-final-probe_test.go`.

The focused checks were:

- A task selecting a 20-source collection has empty raw `attachments` and
  `versions`; the ObjectStore task manifest remains below 2 KiB.
- The selected generation stays frozen after reindexing and idempotent retry.
  A newer generation containing newly revoked B does not block the old task's
  legal frozen A scope; a new task using that current generation is denied.
- Submission retries first match the original submitter. Another user cannot
  reuse the same tenant/session client request ID to obtain its frozen scope.
- A plain task cannot gain collection scope by reusing its client request ID.
  The rejected attempt leaves no DB collection retrieval grant.
- An explicit selection cannot be removed by omitting its field or changing it
  to an empty list on retry. Workspace changes are rejected, including changing
  an originally empty workspace to a populated one.
- Foreign owner and tenant collection IDs are rejected.
- Collection selection never grants direct raw image access at `fileaccess`.
- After the active collection no longer contains an old source and that source
  has been trashed for over 30 days, the frozen task collection still protects
  its bytes from GC. Logical deletion nevertheless denies retrieval immediately.

Review found and fixed a retry-scope expansion bug: the old implementation wrote
new SQL collection scope before comparing an immutable object manifest, and
compared only attachments/workspace afterwards. The revised path checks the
existing manifest **before** creating any DB scope; new manifests bind schema
version, explicit/implicit selection mode, selected IDs, owner and workspace.
Workspace-bearing plain tasks also freeze an empty collection selection.
A read-only TaskStore lookup selects frozen, rather than current, collection
validation for an existing admission. If an interrupted admission has no
confirmed frozen scope, it fails closed rather than silently picking a new one. Legacy
manifests keep their existing recorded scope, never treating absent fields as
permission to add a collection. Automatic legacy collection freezing additionally
requires every frozen root to remain within the direct input set.

Ordinary tasks without a workspace or attachments remain compatible; the stricter
manifest check does not force those tasks through a workspace-only catalog API.
