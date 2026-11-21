import test from "node:test";
import assert from "node:assert/strict";
import { reportRuns } from "../src/report-runs.js";

test("a new completed artifact enters recent runs without telemetry or an archive rebuild", () => {
  const first = { reports: { runs: [] } };
  assert.deepEqual(reportRuns({ runs: [] }, first), []);
  const report = { run_id: "review-candidate-1", subject: "worker", model: "fixed-model",
    split: "dev", reviewer_samples: 16, reviewer: 0.625, task_total: 0,
    valid: true, total_usd: 0.12, created_at: "2026-11-10T01:00:00Z" };
  const [row] = reportRuns({ runs: [] }, { reports: { runs: [report] } });
  assert.equal(row.run_id, report.run_id);
  assert.equal(row.done, 16);
  assert.equal(row.scores.verdict_accuracy, 0.625);
  assert.equal(row.scores.success_rate, undefined);
  assert.equal(row.model, "fixed-model");
});

test("final artifact supersedes an incomplete scrape without duplicate runs", () => {
  const live = { runs: [{ suite: "task-loop", run_id: "r1", status: "running", done: 3,
    started_at: "2026-11-10T00:00:00Z", scores: { verdict_accuracy: 1 }, valid: true }] };
  const archive = { reports: { runs: [{ run_id: "r1", reviewer_samples: 8,
    reviewer: 0.5, valid: false, created_at: "2026-11-10T00:02:00Z" }] } };
  const rows = reportRuns(live, archive);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].status, "finished");
  assert.equal(rows[0].valid, false);
  assert.equal(rows[0].scores.verdict_accuracy, 0.5);
  assert.equal(rows[0].started_at, live.runs[0].started_at);
});

test("final Terminal-Bench summary replaces telemetry values including unknown cost", () => {
  const stale = { suite:"terminal-bench", run_id:"tb20-final", status:"finished", scores:{resolved_rate:1}, cost_usd:99, planned:20, done:20 };
  const summary = { suite:"terminal-bench", run_id:"tb20-final", source:"terminalbench_artifacts", status:"partial", complete:false, scores:{resolved_rate:.5}, cost_usd:null, known_cost_usd:2, pending_reserved_usd:1, selected:20, graded_count:2, not_run:18 };
  const rows = reportRuns({runs:[stale]}, {}, {runs:[summary]});
  assert.equal(rows.length,1);assert.deepEqual(rows[0],summary);
  assert.equal(rows[0].scores.resolved_rate,.5);assert.equal(rows[0].cost_usd,null);assert.equal(rows[0].not_run,18);
});
