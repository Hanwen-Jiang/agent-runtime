# Cloudflare scoped object transfer acceptance

The shared agent prepare/sign/upload/verify protocol now works on the deployed
Cloudflare fallback. The Python Worker and Go Sandbox Service receive metadata,
bounded excerpts and object references. Object bytes travel from the sandbox
agent through Cloudflare's container egress gateway to R2, not through either
application Worker or Sandbox Service. This is the same gateway boundary used
for network policy enforcement, not an additional Worker upload fallback.

The sandbox retains `enableInternet=false`. A static outbound handler for the
single virtual HTTP hostname `ar-transfer.internal` handles a random capability
path. That exception is explicit in rs-transfer; other public HTTP hostnames,
userinfo, ports and lookalikes remain rejected. The handler selects the Durable
Object by Cloudflare's trusted container ID, never a caller-supplied environment.
No actual R2 hostname is added to the container allowlist and no new TLS trust
root is installed. The capability's real signed HTTPS R2 URL and headers stay
in private Durable Object storage. Only recognized account-specific R2 hosts,
port443, GET/PUT, fixed sizes and <=125s expiry are accepted; redirect is denied.

Every request and every streamed chunk checks the persisted grant, method,
expiry, revoke state and aggregate bytes. At most three attempts can consume
three times the declared size. PUT retains checksum, Content-Length and
If-None-Match exactly, using a FixedLengthStream; GET is independently checked
by the agent's SHA256. Active revoke aborts its fetch and stream. The SDK's
persisted schedule is for cleanup only; delayed alarms cannot extend authority.
Revocation leaves a short tombstone so an in-flight mint cannot reopen access.
Terminal accounting remains available after environment destruction until the
control plane acknowledges it. A restart loses no grant/byte counter; an old
process cannot resurrect an expired capability.

Migration217 adds `sandbox_transfer_usage`. The Service atomically reserves
`3*size` against the existing tenant/day egress counter before minting. It
settles the actual byte count from the revoked gateway receipt and refunds the
unused reservation exactly once. Unknown outcomes remain charged and are
reconciled by the existing Service reconcile pass, including destroyed leases;
receipt acknowledgement releases remote state only after local settlement.
`Directory(ctx,false)` deliberately includes destroyed environment tombstones;
its owner remains discoverable for delayed transfer reconciliation. A focused
SQLite regression destroys the environment, confirms it is absent from the
active directory but still present in the full directory, then settles the
previously unknown transfer without refunding it as zero. The remote gateway's
owner-bound final count also survives environment teardown until ACK.
Internal workspace reads without a model Task use a lease-scoped audit label,
not a fabricated model-usage identity. FC/K8s retain their existing transfer
network implementations. The broker's Cloudflare archive path now enables
direct snapshots/restores through this same target transformation.

## Actual deployment and evidence

Cloudflare Worker version `3ae9361b-1b1a-45a1-ae79-bcba8566d553` serves the updated
bridge. Container image is `agentruntime-sandbox-universal:goal-cf-20261107`,
manifest `sha256:b27f0cfd05609238222e6f8e68d90b0462ec4ab287df78afaeed645446bd1ca1`.
Frozen agent SHA256 is recorded with source hashes in
`evidence/cf-scoped-20261107/manifest.json`.

The first two tests reached the previous agent while Cloudflare's image rollout
was in progress. They failed rather than accepting mixed versions; the Worker
was rolled back to `34c99eef-2dff-42e8-b0fc-4fae0d458336` while diagnosing. Raw
curl verified the new gateway independently, and the cause was confirmed by the
old agent's binary digest. A 100% rollout then completed; final acceptance
required the expected live agent digest before sending object data. Rollback
and original configuration records remain in `.goal-tools/cf-scoped-20261107`
and `.goal-tools/cf-release-goal-f1388e13`.

Real acceptance used one isolated VM per run and unique R2 object prefixes:

- `TestLiveCloudflareScopedObjectTransfer`: 11.17s passed. Immutable upload after
  source mutation, exact-byte/hash restore, actual counters, expiry/revocation,
  tenant separation, delayed mint rejection and in-flight upload interruption.
- `TestLiveCloudflareServiceDirectOutputAndQuota`: 3.89s passed using a real
  Go Service, SQLite ledger, real CF agent/gateway and R2. 20k stdout returned
  only an excerpt and object reference; a60k retry reservation settled to20k.
  The next60k reservation was denied under a60k daily cap and returned bounded
  pending output, without another R2 object or a lost reservation.
- Existing CF adapter conformance passed against a real local Rust agent;
  its five mount-only cases remain inapplicable to this archive backend. The
  two new live cases ran explicitly with zero skips.
- All ledger/broker/archive tests, Go vet, TS type checks, fixed-policy tests,
  and both rs-transfer tests passed. One early Service assertion incorrectly
  omitted the existing64-byte excerpt-marker allowance; it was corrected to the
  unchanged wire contract. Its single20k test object was explicitly verified
  and removed; final tests register prefix cleanup before assertions.

No model calls were made, model cost is $0. Cloudflare uses the existing account,
instance type and max_instances; its infrastructure bill is not yet available
and is not reported as zero. All test VMs and scoped R2 objects were deleted;
no bucket lifecycle, account plan, unrelated Worker or original eval artifact
was changed. Host credentials and signed URLs are absent from committed logs.
The recorded bytes are payload bytes accepted by the object-transfer gateway,
including interrupted attempts; they exclude TLS/TCP/HTTP framing overhead and
are not a claim about Cloudflare's billable bandwidth or total infrastructure cost.
The parent deployer must still publish the new Go Service code/migration217;
Cloudflare bridge deployment alone is not full core-service deployment.

References: [outbound gateway semantics](https://developers.cloudflare.com/containers/configuration/outbound-traffic/),
[Container scheduling](https://developers.cloudflare.com/containers/api/container-class/),
[rollout mixed-version behavior](https://developers.cloudflare.com/containers/configuration/rollouts/).
