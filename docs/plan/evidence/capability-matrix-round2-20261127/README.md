# Flagship Capability Matrix Round 2 (2026-11-27)

- **Mode**: `live_gateway_matrix_round2` (`tenant=ablation-20261127`, live `ar-agent-runtime-model-gateway`)
- **Paired Slice**: BFCL (`8`, `act`), JudgeBench (`6`×2 consistent, `judge`), RewardBench2 (`6`, `judge`), FRAMES (`6`, `plan_reason`)
- **Total Spend**: `$0.9900` (cap `$1.00`)
- **Pareto Undominated (Score ↑ vs Cost/Trial ↓)**: `a6api/deepseek-v4-pro@2026-11`, `a6api/kimi-k3.1-pro@2026-11`

| Model | Valid/Att | Errors | Overall (95% CI) | BFCL (`act`) | JudgeBench | RewardBench2 | FRAMES (`plan`) | Role `judge` | Cost / Trial | P50 / P95 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `a6api/gemini-4-argon@2026-11` | 23/26 | 3 | **73.9%** [53.5%, 87.5%] | 100.0% | 33.3% | 66.7% | 66.7% | 55.6% | $0.00783 | 8213.34 / 32264.22 |
| `a6api/deepseek-v4-pro@2026-11` | 26/26 | 0 | **88.5%** [71.0%, 96.0%] | 100.0% | 100.0% | 83.3% | 66.7% | 91.7% | $0.00404 | 6330.37 / 56379.97 |
| `a6api/qwen3.8-max@2026-11` | 25/26 | 1 | **88.0%** [70.0%, 95.8%] | 100.0% | 100.0% | 83.3% | 66.7% | 90.9% | $0.01633 | 16477.74 / 78988.94 |
| `a6api/kimi-k3.1-pro@2026-11` | 25/26 | 1 | **92.0%** [75.0%, 97.8%] | 87.5% | 100.0% | 100.0% | 83.3% | 100.0% | $0.00988 | 13190.81 / 97484.89 |
