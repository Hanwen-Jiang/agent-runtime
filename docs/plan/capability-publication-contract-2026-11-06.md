# MX-PUB capability-only Registry publication

The existing A/B promotion evidence describes replacing a model or recipe.
Publishing measured capability scores for an unchanged model must not invent
an A/B baseline, cost-per-success number, or human waiver. Promote now also
accepts `registry_capability_evaluation.schema.json` when
`evaluation.value.kind` is `capability_scores`.

This path keeps the same publisher authentication. It is restricted to platform
Model Profiles and a named current default version. All fields except
`capability_scores` must exactly match that default. The resulting measurement
map must equal the baseline plus the explicitly certified measurements; silent
deletions and additional score changes fail. The baseline is checked again in
the promotion transaction to reject publication races.

The frozen `capmatrix-publication-v2` gates are unchanged: at least 40 distinct
items, complete repeats, known cost, contamination at most 5%, bootstrap CI half
width at most 0.15, and at least half the cell's candidates eligible. Registry
also verifies the shrinkage formula `(n*p+5)/(n+10)`, rounded to four decimals,
and the measurement's source evidence digest. Report URI and SHA-256, observation
date, baseline revision and all gate evidence are retained with the immutable
published Registry revision. Registry validates the certificate; the authorized
evaluation pipeline remains responsible for the raw observations and report.

The original promotion schema remains unchanged and continues to gate actual
model, price, routing capability or other Profile edits. Known invalid planning
scores and candidates with pending costs cannot use this publication path.

Validation: full Registry SQLite/Postgres suite passed, including rejection of
price changes, altered scores, stale baseline, insufficient samples/coverage,
unknown cost, incomplete repeats, contamination and wide confidence intervals.
All 378 schema fixture checks passed. No live capability score has been published
by these code checks; actual publication and loaded Registry revisions are logged
in the main Goal record after the live evidence gate.
