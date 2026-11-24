// Published engineering experiment cards derived from frozen, SHA-256 verified exports.
// Live /api/reports.json cards take precedence whenever an experiment ID is served by the backend.
export const PUBLISHED_ENGINEERING_CARDS = [
  {
    id: "engineering-20261107-sandbox-warmpool",
    kind: "engineering-comparison",
    evaluation_type: "ablation",
    evaluation_scope: "component",
    title_zh: "沙箱启动速度 · Firecracker 预热池",
    title_en: "Sandbox startup latency · Firecracker warm pool",
    baseline_label: "冷启动 · Cold boot",
    candidate_label: "预热池 · Warm pool",
    evaluated_on: "2026-11-07",
    sample_count: 20,
    completed_count: 20,
    collection_complete: true,
    quality_valid: true,
    cost_known: true,
    actual_cost_usd: 0,
    known_cost_usd: 0,
    metrics: [
      { key: "first_cmd_p95", label_zh: "首条命令完成 P95", label_en: "First command P95", unit: "seconds", lower_is_better: true, baseline: 1.2278638388961554, candidate: 0.052358060027472675 },
      { key: "first_cmd_p50", label_zh: "首条命令完成 P50", label_en: "First command P50", unit: "seconds", lower_is_better: true, baseline: 1.0252873069839552, candidate: 0.02452952996827662 },
      { key: "create_p95", label_zh: "实例创建 API P95", label_en: "Create API P95", unit: "seconds", lower_is_better: true, baseline: 1.1911785019328818, candidate: 0.005021307966671884 },
    ],
    limitations_zh: [
      "固定同规格 Firecracker microVM（1 vCPU、256 MiB 内存、Python 镜像），冷启动与预热池各测 20 次（失败 0 次）；首条命令完成 P95 由 1.228 s 降至 0.052 s（降幅 95.7%）。",
    ],
    limitations_en: [
      "Fixed Firecracker microVM spec (1 vCPU, 256 MiB RAM, Python image), 20 sequential runs each (0 failures); first-command P95 dropped from 1.228 s to 0.052 s (-95.7%).",
    ],
    selection_info: {
      dataset: "Firecracker sandbox startup",
      method: "explicit_ids",
      split: "validation",
      source: "cold/samples.jsonl + warm/samples.jsonl",
      selected_count: 20,
      method_detail_zh: "同规格 Firecracker microVM 各逐次测量 20 次，计时从创建请求发出到首条 shell 命令完成。",
      method_detail_en: "20 sequential runs each on identical microVM specs, timed from create API request to first shell command completion.",
    },
  },
  {
    id: "engineering-20261125-workspace-concurrency",
    kind: "engineering-comparison",
    evaluation_type: "ablation",
    evaluation_scope: "component",
    title_zh: "高并发工作区接口 · 200 并发压测",
    title_en: "High-concurrency workspace API · 200 concurrent clients",
    baseline_label: "优化前 · Baseline",
    candidate_label: "高并发优化 · Candidate",
    evaluated_on: "2026-11-25",
    sample_count: 14000,
    completed_count: 14000,
    collection_complete: true,
    quality_valid: true,
    cost_known: true,
    actual_cost_usd: 0,
    known_cost_usd: 0,
    metrics: [
      { key: "overall_qps", label_zh: "综合吞吐 QPS（4副本·200并发）", label_en: "Overall QPS (4 replicas · 200 conc.)", unit: "number", baseline: 61476.0, candidate: 190230.3 },
      { key: "overall_p50", label_zh: "综合中位延迟 P50", label_en: "Overall P50 latency", unit: "seconds", lower_is_better: true, baseline: 0.002816, candidate: 0.000084 },
      { key: "running_qps", label_zh: "运行态轮询 QPS（6000 请求）", label_en: "Running poll QPS (6k reqs)", unit: "number", baseline: 78198.9, candidate: 353454.0 },
      { key: "completed_qps", label_zh: "完成态读取 QPS（6000 请求）", label_en: "Completed poll QPS (6k reqs)", unit: "number", baseline: 90783.1, candidate: 479624.3 },
    ],
    limitations_zh: [
      "4 个 Go Server 副本共享卷、200 并发客户端共发起 14,000 次请求（2,000 热点重复提交 + 6,000 运行态轮询 + 6,000 完成态读取），全部成功且仅触发 1 次下游创建；通过 RWMutex、singleflight、终态缓存与跨副本锁退避将综合吞吐由 6.15 万提至 19.02 万 req/s（3.09×）。",
    ],
    limitations_en: [
      "4 Go replicas on a shared volume with 200 concurrent clients issuing 14,000 requests (100% succeeded, 1 downstream create across 2,000 retries); RWMutex + singleflight + immutable terminal cache + lock backoff raised throughput from 61.5k to 190.2k req/s (3.09x).",
    ],
    selection_info: {
      dataset: "Coding Agent high-concurrency benchmark",
      method: "explicit_ids",
      split: "validation",
      source: "baseline.json + candidate.json",
      selected_count: 3,
      method_detail_zh: "覆盖 200 并发热点同键重复提交、运行态状态轮询风暴与完成态高频读取三类压测场景。",
      method_detail_en: "Covers 200-concurrency hot duplicate submits, running status poll storms, and completed status read storms.",
    },
  },
];

export function mergeEngineeringExperiments(liveExperiments = []) {
  const existing = new Set((liveExperiments || []).map((row) => row.id));
  return [
    ...(liveExperiments || []),
    ...PUBLISHED_ENGINEERING_CARDS.filter((card) => !existing.has(card.id)),
  ];
}
