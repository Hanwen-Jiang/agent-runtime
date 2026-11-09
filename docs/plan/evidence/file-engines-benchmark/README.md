# Server file benchmark and fixed retrieval comparison

Date: 2026-11-10 (Asia/Shanghai). This is a **synthetic, frozen** 120-file / 690 automatic-anchor / 100-query benchmark, executed on the authorized Linux server in dedicated containers. It is not the design's human-annotated release set. **Human-reviewed anchors: 0; the 500-manually-reviewed-anchor gate remains open.**

## Results and decision

- Parsed all 120 files; 688/690 automatic text-and-anchor targets matched (99.71%). Text/non-raster formats: 540/540. Raster OCR: 148/150. All original SHA hashes unchanged; Notebook reads did not execute code.
- Five abnormal inputs rejected explicitly (four failed, one unsupported). A 100,000-row CSV query twice produced exactly 50,000 selected rows and sum 2,450,000.
- Completed all 100 queries on the original exact BM25 and real E5 hybrid paths, then exactly one bounded learned-reranking candidate on the same frozen input and relevance labels. Final reranked container exit 0, OOM false, network none, 4 CPU / 8 GiB.
- Reranking improves Recall@8 from 29.60% to 50.27% and Hit@8 from 79% to 95%. **This is an improvement, not proof of complete cross-document recall or production quality.** Text-format recall regresses from 50% to 42%; per-format results are in `run/comparison.json`.
- Reranked p95 is 6.360 seconds, so **the two-second retrieval SLO is not met** by this CPU candidate. Baseline hybrid p95 is 0.265 seconds on this synthetic local backend. Keep learned reranking opt-in; do not enable it as an unconditional production default or advertise all release quality gates passed.

| Method | Recall@8 | Hit@8 | MRR@8 | unique-source nDCG@8 | Relevant hit precision | Citation integrity | p95 seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| exact | 28.27% | 78.00% | 0.479 | 0.285 | 19.38% | 100.00% | 0.027 |
| hybrid | 29.60% | 79.00% | 0.559 | 0.316 | 20.25% | 100.00% | 0.265 |
| reranked | 50.27% | 95.00% | 0.930 | 0.600 | 34.88% | 100.00% | 6.360 |

Citation integrity means returned quotes and anchors match stored parser source. It does not mean relevance or answer accuracy. Expected sources per query number 5–6, so theoretical Recall@8 is 100%, not constrained by a >8 denominator. Actual single-chunk representability averages 91.13%; extraction/chunk splitting explains only part of the remaining missed recall. Failure examples are preserved without changing the denominator.

## Parse coverage

| Format | Files | Exact automatic targets | p95 seconds |
| --- | ---: | ---: | ---: |
| abnormal | 5 | expected rejection | — |
| archive | 5 | 30/30 | 0.033 |
| docx | 15 | 90/90 | 0.071 |
| image | 5 | 29/30 | 4.786 |
| notebook | 4 | 24/24 | 0.027 |
| pptx | 15 | 90/90 | 0.061 |
| scan_pdf | 20 | 119/120 | 12.366 |
| table | 6 | 36/36 | 0.183 |
| text | 10 | 60/60 | 0.027 |
| text_pdf | 20 | 120/120 | 23.638 |
| xlsx | 15 | 90/90 | 0.046 |

The two miss cases are `scan_pdf_17`, page 3, and `image_02.webp`: the English emergency-exception clause is visibly legible, but OCR returns a garbled first line with confidence .622/.694. The raw blocks correctly carry `low_confidence=true`. Original top-level warnings were empty; the subsequent visibility fix is separately tested and does not rewrite this frozen parse output, source text or relevance gold. Bbox evaluation checks normalized bounds and source page/cell/shape location, not human-drawn geometric IoU.

## Provenance, model choice and limits

Frozen manifest SHA256: `3639f4f6d66325098d3530f797c74e32a2e36c032c9f75f7e07082eb1dbd2a17`.
Corpus tar SHA256: `0f3dc779162fe0ddc377f329ebcd601ca80f12cc84f15a40aeebffb0f1c42f8f`.
Processing image: `sha256:fb638071d954329c16dba47b9292e4efec3dddd9075fe7fa35cfc2b18c3229d7`. This is the image used for the original parse run; later OCR-status metadata fixes are reported separately.

E5: `intfloat/multilingual-e5-small`, revision `614241f622f53c4eeff9890bdc4f31cfecc418b3` (MIT). Reranker: `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`, revision `1427fd652930e4ba29e8149678df786c240d8825` (Apache 2.0). File SHA manifests are in `models/`; weights are intentionally not committed. Real CPU inference uses the existing GatewayEmbedder and GatewayReranker request/response interfaces. The Knowledge gRPC, LlamaIndex engine, Runtime in-memory VectorStore and SQLite ledger are real. This benchmark does not use remote Model Gateway networking, production Milvus, a deployed production scorer or LLM answer generation.

With a configured reranker only, the bounded candidate path unions dense40 and sparse40 using the same tenant/ACL filter (at most 80 unique candidates), then applies real scoring and returns eight. Current ACL is checked before and after scoring. No-reranker behavior and Memory's scoring remain unchanged. This is the only algorithm candidate evaluated; no relevance labels/model versions were tuned after seeing results.

The final run used 140 embedding calls / 22,774 input tokens and 101 scorer calls / 6,380 candidate pairs / 255,349 input tokens. Paid API requests: **0**. These are local-model tokens and CPU measurements, not hosted-provider charges. The original long scorer attempt expired its once-signed five-minute synthetic token after 41 completed queries and is preserved in `reranked-expired-token.log`. The harness now signs a fresh short-lived assertion per RPC without weakening validation. The final full 100-query rerun is the sole source of final reranked metrics; every raw ranking was checkpointed.

## What is verified separately

All benchmark authority checks passed: internal-token denial; twenty files with two representations each; ungranted/cross-tenant/cross-workspace reads denied; revoked sources not resolved; deletion clears vectors and blocks deleted generations. The injected authority here is not a substitute for the separate SQL Catalog / HTTP / Temporal acceptance.

Regression evidence: Memory 194 local tests passed; **211 server tests passed**, including Postgres/Qdrant. Actual Knowledge/ModelGateway gRPC fixture, indexing-delete race, revocation and stable citation probes pass. `rerank_authority_probe.py` additionally checks candidate bounds, revocation and generation deletion during scoring, and rejection of wrong-length/nonfinite score responses. Its synthetic embeddings validate protocol/security plumbing, not semantic quality.

## Review and replay

- `run/summary.json`: format-level parse results and final metric comparison.
- `run/comparison.json`: per-query ceilings, metrics, format/language breakdowns, unchanged-gold OCR misses and ranking examples.
- `run/retrieval-comparison.csv`: compact full 100-query metrics.
- `run/retrieval-summary.json`: untouched original exact/hybrid raw rankings.
- `run/retrieval-reranked-summary.json` and `run/reranked-query-checkpoints/`: final learned-ranker raw rankings and timings.
- `run/review.html`: standalone human-readable review index. Run `tar -xzf corpus.tar.gz -C run` to activate links to byte-identical source files.
- `run/human-anchor-review.csv`: 690 pending review rows, empty reviewer/date fields. Automatic checks or an AI visual spot check do not count as human review.
- `reranked-executed-benchmark.py`: exact source copied from the server after the run; its SHA matches the launch manifest. The current harness adds the final comparison summary to reporting only.
- Container configurations, source SHA manifests and success/failure logs remain beside this README; full server artifacts remain under `/root/proj/agent-projs/.file-intelligence-validation-20261110/evidence/benchmark`.

Reproduction commands and precise metric definitions are in `scripts/file-engines/BENCHMARK.md`. No production service, tenant data or deployment was modified by these benchmark runs.
