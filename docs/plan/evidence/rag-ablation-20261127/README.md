# py-memory 4-Arm Developer RAG Ablation (2026-11-27)

- **Held-out Dataset**: `services/py-learning/evalsets/memory-rag/dev-qa-heldout.jsonl` (`18` repo QA with verified `file:start-end` ranges + `6` unanswerable items across `17` indexed source files)

| Arm | Hit@1 | Hit@5 | Source Recall@5 | Citation Line Prec | Abstention | Rerank Skip | Cache Hit | P50 / P95 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `arm1_bm25_line_window` | 50.0% | 77.8% | 77.8% | 33.3% | 100.0% | 0.0% | 0.0% | 4.072 / 5.355 |
| `arm2_hybrid_rrf_line_window` | 55.6% | 66.7% | 66.7% | 50.0% | 100.0% | 0.0% | 0.0% | 8.678 / 9.558 |
| `arm3_ast_rrf_unconditional_rerank` | 77.8% | 83.3% | 83.3% | 77.8% | 83.3% | 0.0% | 0.0% | 14.432 / 17.246 |
| `arm4_ast_rrf_gated_rerank_cold` | 77.8% | 83.3% | 83.3% | 77.8% | 83.3% | 4.2% | 0.0% | 13.717 / 15.094 |
| `arm4_ast_rrf_gated_rerank_warm` | 77.8% | 83.3% | 83.3% | 77.8% | 83.3% | 4.2% | 100.0% | 13.375 / 15.158 |
