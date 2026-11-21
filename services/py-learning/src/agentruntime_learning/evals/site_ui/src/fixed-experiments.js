// Published fixed-cohort comparisons kept available when the live artifact
// directory is temporarily unavailable. These are aggregate public results.
export const FIXED_EXPERIMENTS = [
  {
    id: "memory-bm25-full-20261109-a", batch_id: "longmemeval-50", kind: "retrieval-comparison",
    evaluation_type: "ablation", evaluation_scope: "component", title_zh: "LongMemEval 记忆检索", title_en: "LongMemEval memory retrieval",
    completed_at: "2026-11-09T09:29:08.854908+00:00", sample_count: 50, completed_count: 50, collection_complete: true,
    baseline_label: "Legacy", candidate_label: "BM25",
    metrics: [
      { key: "recall_any@5", label_zh: "会话召回率 @5", label_en: "Session recall @5", baseline: .88, candidate: .96, unit: "rate" },
      { key: "recall_any@10", label_zh: "会话召回率 @10", label_en: "Session recall @10", baseline: .94, candidate: .98, unit: "rate" },
      { key: "ndcg_any@10", label_zh: "会话 NDCG @10", label_en: "Session NDCG @10", baseline: .7803707463957685, candidate: .86961529870049, unit: "score" },
    ],
    selection_info: { batch_id: "longmemeval-50", dataset: "LongMemEval_S", split: "development", selected_count: 50, method: "frozen_cohort", method_detail_zh: "固定 50 题历史题单；Legacy 与 BM25 使用同一题单。", method_detail_en: "Fixed historical 50-question cohort reused by Legacy and BM25.", source: "public-summary.json" },
  },
  {
    id: "longbench-context-20261110-fixed-40", batch_id: "longbench-40", kind: "context-cohort-comparison",
    evaluation_type: "ablation", evaluation_scope: "component", title_zh: "LongBench v2 · 40 题上下文策略对照", title_en: "LongBench v2 · 40-question context comparison",
    comparison_id: "longbench-context-20261110-fixed-40", runtime_revision: "d28c308f7d1589e7080e8232171d641b9993c4be", model_profile: "a6api/kimi-k3.1-pro@2026-11",
    cohort_sha256: "a50aeaac8abfd5aa69d00b4f31080ee11e15b5ac0d2b3c164bcca0456be83415", config_sha256: "ce10b68e6e575f0aaf81aae178e05c7e5165a5677d928f3aa36c85160a81733",
    completed_at: "2026-11-09T18:23:36.729278+00:00", sample_count: 40, completed_count: 40, collection_complete: true,
    selection_info: { batch_id: "longbench-40", dataset: "LongBench v2", split: "development", selected_count: 40, method: "seeded_hash_prefix", method_detail_zh: "按 seed=0 与题号的 SHA-256 排序取前 40 题，各策略复用同一题单。", method_detail_en: "Seed 0 SHA-256 ordering, first 40 questions reused by every strategy.", seed: 0, cohort_sha256: "a50aeaac8abfd5aa69d00b4f31080ee11e15b5ac0d2b3c164bcca0456be83415", version: "2b48e494f2c7a2f0af81aae178e05c7e1dde0fe9", source: "public-summary.json" },
    variants: [
      { id: "mask_only", label_zh: "仅省略早期工具输出", label_en: "Mask early tool output", protocol: "longbench-context-v1", source_run_id: "longbench-context-20261110-fixed-40-mask", status: "completed" },
      { id: "full", label_zh: "完整上下文管理", label_en: "Full context management", protocol: "longbench-context-v1", source_run_id: "longbench-context-20261110-fixed-40-full", status: "completed" },
    ],
    metrics: [
      { key: "planned_count", values: [40, 40] }, { key: "completed_count", values: [40, 40] }, { key: "valid_count", values: [40, 40] },
      { key: "correct_count", values: [19, 30] }, { key: "wrong_count", values: [21, 10] }, { key: "unscored_count", values: [0, 0] },
      { key: "confirmed_share", values: [.475, .75] }, { key: "correct_delta", values: [0, 11] },
    ],
  },
];

export const FIXED_TERMINAL_RUNS = [
  {
    run_id: "tb20-official-round1-20261110", suite: "terminal-bench", benchmark: "terminal-bench", subject: "agent-runtime",
    model: "agent-runtime-samevm-retry1", split: "2.0", evaluation_type: "runtime", source: "terminalbench_artifacts",
    started_at: "2026-11-09T20:56:25.912115Z", created_at: "2026-11-10T10:03:51.659870Z", updated_at: "2026-11-10T10:03:51.659870Z", completed_at: "2026-11-10T10:03:51.659870Z",
    selected: 20, planned: 20, started: 20, done: 20, graded_count: 20, resolved: 14, unresolved: 6, not_run: 0, infra_failed: 0, grading_errors: 0,
    rate_denominator: 20, denominator_kind: "officially_graded", rate: .7, complete: true, graded_complete: true, finished: true, status: "finished",
    scores: { resolved: 14, resolved_rate: .7 },
  },
];
