# Flagship Capability Matrix Round 2 (2026-11-27)

- **Mode**: `live_gateway_matrix_round2` (`tenant=ablation-20261127`, live `ar-agent-runtime-model-gateway` via SSH tunnel)
- **Paired Slice**: BFCL (`8`, `act`), JudgeBench (`6`×2 consistent, `judge`), RewardBench2 (`6`, `judge`), FRAMES (`6`, `plan_reason`)
- **Total Spend**: `$0.1646` (cap `$0.95`)
- **Status**: Partial live capture — `gemini-4-argon` completed all `26` trials (`12` OK / `8` `deadline_exceeded` / `6` `grpc_unavailable` when SSH tunnel dropped at 04:51 CST); `deepseek-v4-pro`, `qwen3.8-max`, and `kimi-k3.1-pro` encountered `blocked_by_network_drop` (`grpc_unavailable`, `$0` billed) and are excluded from capability ranking until re-run.

| Model | Execution Status | Valid/Completed Trials | BFCL (`act`) | JudgeBench (Consistent) | RewardBench2 (4-way) | FRAMES (`plan`) | Cost / Total | P50 / P95 (ms) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `a6api/gemini-4-argon@2026-11` | Live (`12` OK, `14` tunnel drop/timeout) | 26 | **100.0%** (`8/8`) | 16.7% (`1/6`) | 33.3% (`2/6`) | 0.0% (`0/6`, unreached) | $0.164592 | 25316.26 / 96578.31 |
| `a6api/deepseek-v4-pro@2026-11` | `blocked_by_network_drop` (`grpc_unavailable`) | 0 valid | — | — | — | — | $0.000000 | — |
| `a6api/qwen3.8-max@2026-11` | `blocked_by_network_drop` (`grpc_unavailable`) | 0 valid | — | — | — | — | $0.000000 | — |
| `a6api/kimi-k3.1-pro@2026-11` | `blocked_by_network_drop` (`grpc_unavailable`) | 0 valid | — | — | — | — | $0.000000 | — |
