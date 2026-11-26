# Tool-Schema Pre-Execution Validation Paired Ablation (2026-11-27)

- **Mode**: `live_gateway_paired` (live `ar-agent-runtime-model-gateway` via SSH tunnel)
- **Paired trials**: 40 (`20` balanced BFCL items × `2` models: `qwen3.8-flash` & `gemini-3.8-flash`)
- **Total live spend**: `$0.0181` (cap `$0.75`)

| Metric | Arm A (Baseline) | Arm B (+Schema Guard & Repair) | Delta |
| --- | ---: | ---: | ---: |
| Schema Valid Rate | 100.0% | 100.0% | +0.0% |
| BFCL AST Pass Rate | 92.5% | 92.5% | +0.0% |
| Avoided Bad Executions | — | 0 | +0 |
| Repaired Calls (Live) | — | 0 (extra turns: 0) | +0 |
| Live Cost (USD) | $0.0181 | $0.0181 | +$0.0000 |
| P50 / P95 Latency (ms) | 5564.96 / 9990.91 | 5564.96 / 9990.91 | — |

## Per-Model Breakdown

| Model | Trials | Schema Valid A -> B | AST Pass A -> B | Avoided Bad Exec | Repaired | Cost A -> B |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `a6api/qwen3.8-flash@2026-11` | 20 | 100.0% -> 100.0% | 85.0% -> 85.0% | 0 | 0 | $0.0032 -> $0.0032 |
| `a6api/gemini-3.8-flash@2026-11` | 20 | 100.0% -> 100.0% | 100.0% -> 100.0% | 0 | 0 | $0.0149 -> $0.0149 |

## Deterministic Schema-Guard Stress Verification

- Injected malformed payloads across all 20 selected BFCL tasks: **92/92** intercepted pre-execution (**100.0%**).
