// A score is comparable only within one suite, split and metric. Cost and
// fallback/error rates are not task-quality headline metrics.
export const primaryMetrics = {
  "terminal-bench": ["resolved_rate"],
  swebench: ["resolved_rate"],
  capability: ["act.medium", "judge.medium", "plan.medium", "route.low"],
  "task-loop": ["success_rate", "triage_accuracy", "verdict_accuracy"],
  deepeval: ["pass_rate", "correctness"],
  context: ["full.probe_pass_rate", "full.retention_rate"],
  ragas: ["mrr", "recall", "llm_context_precision"],
};
const hasScore = (scores, key) =>
  typeof scores?.[key] === "number" && Number.isFinite(scores[key]);
export function scoreForRun(scores = {}, suite) {
  const key = (
    primaryMetrics[suite] || [
      "resolved_rate",
      "success_rate",
      "pass_rate",
      "accuracy",
      "score",
    ]
  ).find((key) => hasScore(scores, key));
  return key ? [key, scores[key]] : undefined;
}
export function archiveCandidates(reports = {}) {
  const rows = [];
  for (const r of reports.runs || []) {
    if (
      r.valid !== true ||
      !Number.isFinite(r.task_total) ||
      r.task_total <= 0 ||
      !Number.isFinite(r.task_pass) ||
      r.task_pass < 0 ||
      r.task_pass > r.task_total
    )
      continue;
    rows.push({
      original_record: {suite:"task-loop", ...r},
      selection_info: r.selection_info,
      run_id: `archive:${r.group}:${r.created_at}`,
      suite: "task-loop",
      split: r.split || "",
      status: "finished",
      valid: true,
      planned: r.task_total,
      done: r.task_total,
      model: r.subject || r.group,
      updated_at: r.created_at,
      scores: { success_rate: r.task_pass / r.task_total },
      archive: true,
      archive_query: r.group || r.subject || "",
    });
  }
  for (const r of reports.benchmarks || []) {
    if (
      r.complete !== true ||
      !Number.isFinite(r.rate) ||
      r.rate < 0 ||
      r.rate > 1
    )
      continue;
    rows.push({
      original_record: {suite:"swebench", ...r},
      selection_info: r.selection_info,
      run_id: r.run_id,
      suite: "swebench",
      split: "verified",
      status: "finished",
      valid: true,
      planned: r.selected,
      done: r.selected,
      model: r.model || r.run_id,
      scores: { resolved_rate: r.rate },
      archive: true,
      archive_query: "",
    });
  }
  return rows;
}
export function bestResults(runs = [], reports = {}, exclusions = []) {
  const excluded = new Set((exclusions || []).map((r) => `${r.suite}\0${r.run_id}`));
  const groups = new Map();
  const official = new Set(runs.filter(r => r.source === "benchmark_artifacts").map(r => `${r.suite}\0${r.run_id}`));
  const archive = archiveCandidates(reports).filter(r => !official.has(`${r.suite}\0${r.run_id}`));
  for (const run of [...runs, ...archive]) {
    if (run.status !== "finished" || run.valid === false || run.execution_valid === false || excluded.has(`${run.suite}\0${run.run_id}`)) continue;
    if (run.source === "terminalbench_artifacts" && (run.complete !== true || run.graded_complete !== true || run.infra_failed > 0 || run.grading_errors > 0)) continue;
    const group = `${run.suite}\0${run.split || ""}`;
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(run);
  }
  const output = [];
  for (const candidates of groups.values()) {
    const suite = candidates[0].suite;
    const metric = (
      primaryMetrics[suite] || [
        "resolved_rate",
        "success_rate",
        "pass_rate",
        "accuracy",
        "score",
      ]
    ).find((key) => candidates.some((run) => hasScore(run.scores, key)));
    if (!metric) continue;
    const comparable = candidates.filter((run) => hasScore(run.scores, metric));
    comparable.sort(
      (a, b) =>
        b.scores[metric] - a.scores[metric] ||
        (b.planned || 0) - (a.planned || 0) ||
        (Date.parse(b.updated_at) || 0) - (Date.parse(a.updated_at) || 0),
    );
    output.push({
      run: comparable[0],
      score: [metric, comparable[0].scores[metric]],
    });
  }
  return output;
}
