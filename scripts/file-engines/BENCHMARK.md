# File intelligence synthetic benchmark

`benchmark.py` is a manual acceptance harness for design §17. It does **not** replace the fixed human-labeled release set. The corpus contains 120 generated non-private files, 690 automatically generated golden anchor targets, and 100 authored-template queries across 20 bilingual policy topics. Human-reviewed anchor count is explicitly zero until a person records an independent review.

The 120 documents are 20 native-text PDF, 20 scan/mixed PDF, 15 each DOCX/XLSX/PPTX, and 35 text/table/image/notebook/archive/abnormal files. Variants include 30-page PDF evidence on page 12, a 20-page scanned document, two-column native PDFs routed through Docling, rotated/blurred scans, Chinese/English, typed/hidden/formula sheets, slide notes/tables, encoded text, 100,000-row CSV, and rejected corrupt/encrypted/traversal/compression inputs. Different versions contain different thresholds; these are related synthetic scenarios, not 120 real business documents. Scans use generated typography, not handwriting, photographed pages, or a human-labeled scanned-table evaluation.

## Running

Generate in a disposable environment with the engine's document libraries plus `reportlab==4.4.10` (generation-only dependency):

```sh
python scripts/file-engines/benchmark.py generate --work /workspace/benchmark
sha256sum /workspace/benchmark/manifest.json > /workspace/benchmark/manifest.sha256
python scripts/file-engines/benchmark.py parse --work /workspace/benchmark
python scripts/file-engines/benchmark.py retrieve --work /workspace/benchmark --model-dir /models/e5
# Optional one bounded candidate: preserves retrieval-summary.json and writes
# retrieval-reranked-summary.json for a fixed-gold comparison.
python scripts/file-engines/benchmark.py retrieve --work /workspace/benchmark --model-dir /models/e5 --reranker-dir /models/reranker
python scripts/file-engines/benchmark.py report --work /workspace/benchmark
```

Use an empty output directory. `parse --only docx,xlsx,...` supports disjoint format batches; retry in a new output directory while keeping frozen input files and manifest unchanged. The frozen tarball is the byte-identical replay artifact. Regeneration reproduces semantic scenarios; Office/PDF creation metadata and raster fonts may change byte hashes. Archive original corpus, `freeze.json`, parser JSON/artifacts, model manifest, container image ID, package manifest, raw retrieval results, and summary before reporting results. Never rewrite expected values to match parser output.

Run the reviewed engine image with `--cpus=4 --memory=8g`. The retrieval evaluation layer needs `llama-index-core==0.14.25`, `grpcio==1.84.0`, `protobuf==7.36.2`, and `cryptography==50.0.2`; the engine already has PyTorch/Transformers. Runtime source is imported from its repository, not copied/mocked. This does not exercise production Milvus, remote Model Gateway networking or LLM answer generation. The optional reranker run uses real local CPU inference through the existing GatewayReranker interface.

## Retrieval model and metrics

The official [multilingual-e5-small model card](https://huggingface.co/intfloat/multilingual-e5-small) declares MIT licensing, 384-dimensional embeddings, mean pooling, L2 normalization, and `query: `/`passage: ` prefixes. The evaluation fixes revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`; downloaded files are SHA-256 recorded in `benchmark-model.json`. The model performs real CPU inference. No hash embedding is used for dense similarity, and no paid service is called.

The harness exercises real loopback Knowledge gRPC, LlamaIndex chunking, Runtime GatewayEmbedder request/response validation, SQLite metadata, and in-memory VectorStore hybrid RRF. The model adapter runs locally. A second generation indexes 20 files with two representations each; authority/revocation/deletion paths are also checked. Those injected-authority checks do not replace SQL Catalog/HTTP authorization or Temporal lifecycle evidence.

Each query's expected sources and facet are frozen before execution. Recall@8 is the fraction of expected source files for which at least one returned fragment contains the exact relevant clause **and** the corresponding source anchor. It therefore includes extraction/OCR losses. The exact baseline uses un-hashed BM25 lexical matching over the same LlamaIndex nodes. Hybrid uses the production sparse+dense RRF path with real E5 embeddings and no reranker. Both return at most eight chunks, without artificial per-file diversification.

`citation_integrity` checks that a returned quote and anchor still match its parser source; it measures provenance, not question relevance. `relevant_hit_precision` separately measures relevance. Report both, Recall@8, format-specific recall, latency, token/model call counts, and zero paid requests. Source golden checks use raw OOXML XML, Poppler `pdftotext`, or independently decoded source structure; raster truth still needs human visual review. Normalization removes whitespace/punctuation and lowercases text; this is an exact normalized match metric, not character error rate or a claim that every critical OCR character is accurate.

A synthetic run cannot satisfy the design's >=500 manually checked anchors or >=99% accuracy on a fixed **human** gold set. Nor does it by itself prove UI previews, model vision payloads, least-privilege child tools, concurrent publish, cancellation, or all lifecycle gates; link separate evidence for those before a release decision.

Anchor checks validate the declared page, paragraph, sheet/cell, slide/shape, line containment, notebook cell, or archive member. Bounding boxes are checked for valid normalized bounds; this run does **not** measure pixel-level box IoU against human-drawn regions. Whole-page native PDF anchors remain whole-page references. OCR sentences split across lines may be matched after concatenating same-page blocks; this is reported as page-level text localization, not proof of word-box accuracy.

When mounting this repository into the built processing image, set `BENCHMARK_ENGINE_SOURCE=/opt/engine` to test the image's immutable engine code. Without this variable, the script intentionally tests repository sources for local development. Capture the selected source path and image ID in execution evidence.

## One bounded reranking comparison

The optional run uses the official [cross-encoder/mmarco-mMiniLMv2-L12-H384-v1](https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1) model at revision `1427fd652930e4ba29e8149678df786c240d8825` (Apache 2.0); downloaded weights and tokenizer SHA-256 are recorded. It scores the union of the strongest 40 dense and 40 sparse candidates, at most 80 unique chunks, then returns eight. Current ACL is checked before scoring and again before returning. This path is enabled only when a reranker is configured; the no-reranker and Memory scoring paths remain unchanged.

The original exact-BM25 and hybrid outputs are preserved. `report` also emits `comparison.json`, `retrieval-comparison.csv`, and `review.html` with all query metrics and source anchor review links. Hit@8 and MRR measure whether and how early any relevant clause appears. Unique-source nDCG rewards each relevant source only once, preventing duplicate chunks from one file from inflating the metric. Relevant precision and citation integrity remain separate. Theoretical Recall@8 ceilings and single-chunk representability diagnose metric ceilings without changing relevance labels. A slow learned scorer is not reported as meeting the two-second retrieval target.

Extract the byte-identical `corpus.tar.gz` into the directory containing `review.html` before using its source links. Review CSV entries start pending, with empty reviewer/date fields. Automatic golden checks and model visual spot checks never become human annotation records.

The deployed `AR_KNOWLEDGE_RERANK=1` path calls the configured Model Gateway scorer. This benchmark's local cross-encoder is an evaluation adapter and is **not** silently installed as that deployment's scorer. Its quality/latency does not prove a different production scorer's behavior. Keep reranking opt-in and repeat the frozen evaluation with the intended production profile before advertising production quality or the two-second SLO.

Every Knowledge RPC signs a fresh short-lived synthetic assertion. The initial long reranker run exposed the harness's one-time five-minute credential expiry; the failure is retained separately. The final fixed run checkpoints each query's actual hits, metrics and latency before continuing. Token expiry enforcement stays enabled.

The frozen parser run retains its original 688/690 automatic matches and raw blocks. A subsequent OCR visibility correction propagates existing block-level `low_confidence` to top-level warnings/coverage and `partial` status; it does not change text, anchor or score. Its two-case replay is separate evidence, not a replacement of this frozen parse run or a retuned relevance set.
