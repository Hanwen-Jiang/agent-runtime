# Isolated paired evaluation hard routing

`AR_MODEL_ROUTING_OVERRIDES_FILE` is an optional startup-only Gateway configuration.
It is not enabled on production by default. Example for the isolated experiment:

```json
{"tenants":{"gh-goalpair-20261107":{
  "plan":{"profile":"a6api/claude-sonnet-5-5@2026-10","tier":"strong@medium"},
  "judge":{"profile":"a6api/claude-sonnet-5-5@2026-10","tier":"strong@medium"},
  "act":{"profile":"a6api/deepseek-v4-flash@2026-10","tier":"mid@low"},
  "transform":{"profile":"a6api/deepseek-v4-flash@2026-10","tier":"mid@low"},
  "deliver":{"profile":"a6api/deepseek-v4-flash@2026-10","tier":"mid@low"},
  "route":{"profile":"a6api/claude-haiku-5-5@2026-10","tier":"small@low"},
  "summary":{"profile":"a6api/claude-haiku-5-5@2026-10","tier":"small@low"},
  "probe":{"profile":"a6api/claude-haiku-5-5@2026-10","tier":"small@low"},
  "embedding":{"profile":"a6api/text-embedding-3-large@2026-10","tier":"embed@default"}
}}}
```

Startup rejects unknown fields, invalid tenant/role names, missing profiles and
class/tier mismatches. No Profile class, price, capability or Registry revision is
modified. The deployed JSON, image and immutable Profile export must be frozen
and observed before any new paid Task.

Routing precedence is Embed→embedding, node ID containing `#summary`/`#probe`,
then the actual `CallContext.Role`. Main and child tool loops already send ACT,
including read/deliver-labelled nodes, so both use the act route; no fictitious
sub-agent role is inferred. Node roles without a declared mapping fail closed
for the configured tenant. Other tenants and deployments without this file keep
the prior behavior.

The override replaces the requested Tier and sets an internal RequiredProfile.
Conflicting explicit caller pins are rejected before any provider call. Only that
Profile can be selected or retried; another model is never a fallback. Existing
tenant allowlists, availability and required capabilities still filter it. Actual
served Profile/Tier and attempt price/Registry/usage evidence use the unchanged
response and model_usage paths.

Validation: complete router, config, Gateway and command packages passed; Go vet
passed. New tests cover forced tiers, main/child ACT, summary/probe/embedding,
actual ledger identity, conflicting pins, missing roles, unknown tenants, single
candidate retries, fallback rejection, allowlist/capability/availability denial
and startup failures. No Task/model request or deployment was performed here.
The prior $0.085565 Task remains an invalid-configuration observation and must
not be merged into the newly frozen pair; it still reduces the shared $40 cap.
