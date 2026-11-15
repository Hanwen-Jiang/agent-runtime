import { test } from "node:test";
import assert from "node:assert/strict";
import { bestResults, scoreForRun } from "../src/best-results.js";
const run = (id, suite, score, extra = {}) => ({
  run_id: id,
  suite,
  split: "dev",
  status: "finished",
  valid: true,
  scores: score,
  planned: 50,
  ...extra,
});
test("keeps the strongest completed run rather than a newer smoke run", () => {
  const rows = [
    run("old", "swebench", { resolved_rate: 0.76 }),
    run("new", "swebench", { resolved_rate: 0 }, { planned: 1 }),
    run("invalid", "swebench", { resolved_rate: 1 }, { valid: false }),
    run("running", "swebench", { resolved_rate: 1 }, { status: "running" }),
  ];
  assert.equal(bestResults(rows)[0].run.run_id, "old");
});
test("headline quality never comes from fallback rate or cost", () => {
  assert.deepEqual(
    scoreForRun(
      { fallback_rate: 0, success_rate: 0.384615, cost_usd: 0.4 },
      "task-loop",
    ),
    ["success_rate", 0.384615],
  );
  assert.equal(
    scoreForRun({ fallback_rate: 0, cost_usd: 1 }, "task-loop"),
    undefined,
  );
});
test("compares the same capability metric and keeps dataset splits separate", () => {
  const result = bestResults([
    run("routing", "capability", { "route.low": 1 }),
    run("execution", "capability", { "act.medium": 0.89 }),
    run("hard", "capability", { "act.medium": 0.2 }, { split: "hard" }),
  ]);
  assert.deepEqual(
    result.map((x) => x.run.run_id),
    ["execution", "hard"],
  );
});
test("rejects non-finite scores and prefers a larger sample on equal scores", () => {
  const result = bestResults([
    run("nan", "deepeval", { pass_rate: NaN }),
    run("small", "deepeval", { pass_rate: 0.75 }, { planned: 1 }),
    run("large", "deepeval", { pass_rate: 0.75 }, { planned: 8 }),
  ]);
  assert.equal(result[0].run.run_id, "large");
});
