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
export function bestResults(runs = []) {
  const groups = new Map();
  for (const run of runs) {
    if (run.status !== "finished" || run.valid === false) continue;
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
