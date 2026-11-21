import { matchingSelection } from "./selection-info.js";

// Completed artifacts and telemetry describe the same runs. Artifacts retain
// final scores after telemetry expires and win over unfinished scrape samples.
export function reportRuns(live, archive, benchmarks) {
  const rows = new Map((live?.runs || []).map((r) => [`${r.suite}:${r.run_id}`, r]));
  for (const r of archive?.reports?.runs || []) {
    if (!r.run_id) continue;
    const scores = {};
    for (const [key, value] of Object.entries({
      triage_accuracy: r.triage, shape_match_rate: r.shape,
      verdict_accuracy: r.reviewer,
      success_rate: r.task_total > 0 ? r.task_pass / r.task_total : null,
    })) if (Number.isFinite(value)) scores[key] = value;
    const n = r.reviewer_samples || r.task_total || 0;
    const key = `task-loop:${r.run_id}`, previous = rows.get(key) || {};
    rows.set(key, {
      ...previous, ...r, suite: "task-loop", subject: r.subject,
      model: r.model || previous.model || r.subject,
      status: "finished", finished: true, valid: r.valid,
      planned: n, done: n, sample_count: n, scores,
      cost_usd: r.total_usd, started_at: previous.started_at || r.created_at,
      updated_at: r.created_at, source: "report-artifact",
    });
  }
  // Explicit final TB summaries take precedence even when a simultaneous
  // live-feed refresh still carries a stale maximum or an unfinished score.
  for (const row of benchmarks?.runs || []) {
    if (row.source === "terminalbench_artifacts" && row.run_id) rows.set(`${row.suite}:${row.run_id}`, row);
    else if (row.run_id && row.selection_info) {
      const key = `${row.suite}:${row.run_id}`, liveRow = rows.get(key);
      if (matchingSelection(liveRow, row.selection_info)) rows.set(key, { ...liveRow, selection_info: row.selection_info });
    }
  }
  for (const audit of benchmarks?.exclusions || []) {
    const key = `${audit.suite}:${audit.run_id}`, row = rows.get(key);
    if (row) rows.set(key, { ...row, execution_valid: audit.execution_valid, invalid_reason: audit.invalid_reason,
      reason_zh: audit.reason_zh, reason_en: audit.reason_en });
  }
  return [...rows.values()].sort((a, b) =>
    String(b.updated_at || b.created_at || "").localeCompare(String(a.updated_at || a.created_at || "")));
}
