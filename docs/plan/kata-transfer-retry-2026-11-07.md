# Kata workspace snapshot transfer recovery

The production Web test cloned a small Git repository into Kata, then failed
while snapshotting with `transfer_upload_failed`. The workspace API could not
finish the attachment. This record concerns that failure, not the earlier empty
manifest JSON `null` fix.

Three independent `gh-goalkata-debug-*` scopes exercised the deployed Kata
backend and the real R2 account. Model allowlists were empty and model budgets
zero. A scoped, five-minute R2/GitHub grant authorized only the diagnostic
traffic. Signed URLs and proxy credentials stayed in memory/stdin; reports
contain only hashes, sizes, curl exit/HTTP/TLS status, and cleanup outcomes.

- Prepared agent uploads and independent curl probes of 0, 5 and 4096 bytes all
  succeeded: HTTP 200, curl exit 0, `ssl_verify_result=0`, CONNECT 200, matching
  stored digests. Zero-byte content and missing CA trust were not the cause.
- One cloned 27-file Hello-World workspace failed two snapshot attempts; the
  egress log included transient 10-second `connect_failed` results among
  successful R2 connections. Another identical fixture snapshot completed with
  all 27 files and no missing blocks. A 4245–4247-byte downstream count also
  appeared for successful uploads, so it does not establish a TLS failure.
- All three temporary scopes' VMs, R2 objects and database rows were cleaned.
  No production tenant policy was changed, and no model call was made.

The snapshot uploader attempted each block only once, although the immutable
agent staging identity supports safe retries. It also failed to reconcile a PUT
whose acknowledgement was lost. The fix retains the same prepared bytes and
key, queries HEAD after each attempt, and treats the constrained SignedUpload
object as complete when its recorded length matches. Only an explicit NotFound
plus a transient upload failure permits another PUT; a failed HEAD never does.
Each retry gets a fresh signature and a scoped network authorization that is
closed before the next attempt. At most three attempts occur, with cancellation
aware delays. A permanent failure cannot publish a partial snapshot.

The regression uses the real deployed Rust agent binary over HTTP with injected
object-store responses. Before the change, transient failures and lost ACK both
failed. After it, two 503 responses recover on attempt three, a committed PUT
with a lost ACK is accepted without another PUT, three persistent failures stop,
and an unknown HEAD result does not cause another upload. Full Archive and
Sandbox Service suites passed (0.040s and 46.775s), and `go vet` passed.

Evidence is under `docs/plan/evidence/kata-transfer-20261107`. Production Web
acceptance must be repeated after deploying the updated Sandbox Service; these
diagnostics and regression tests are not themselves a new production rollout.
