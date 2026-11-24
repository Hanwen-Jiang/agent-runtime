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

test("includes a stronger valid historical run instead of limiting best to telemetry", () => {
  const live = [run("latest", "task-loop", { success_rate: 0.384615 })];
  const archive = {
    runs: [
      {
        group: "earlier-best",
        created_at: "2026-10-28T13:41:43Z",
        subject: "worker",
        split: "dev",
        valid: true,
        task_pass: 13,
        task_total: 26,
      },
      {
        group: "invalid",
        split: "dev",
        valid: false,
        task_pass: 26,
        task_total: 26,
      },
    ],
  };
  const [best] = bestResults(live, archive);
  assert.equal(best.score[1], 0.5);
  assert.equal(best.run.archive_query, "earlier-best");
  assert.equal(best.run.archive, true);
});
test("only completed benchmark archives contribute scores", () => {
  const [best] = bestResults([], {
    benchmarks: [
      {
        run_id: "complete",
        model: "model",
        complete: true,
        rate: 0.68,
        selected: 50,
      },
      { run_id: "incomplete", complete: false, rate: 1, selected: 1 },
    ],
  });
  assert.equal(best.run.run_id, "complete");
});

test("older archive for a SWE run cannot supersede its final artifact with a higher rate", () => {
  const run = {suite:"swebench", run_id:"same", split:"verified", source:"benchmark_artifacts",
    status:"finished", scores:{resolved_rate:.52}, planned:50};
  const [best] = bestResults([run], {benchmarks:[{run_id:"same", selected:50, complete:true, rate:.70}]});
  assert.deepEqual(best.run, run);
});

test('reviewed infrastructure exclusions are not selected as best but valid zero remains',()=>{
 const rows=[{run_id:'infra',suite:'terminal-bench',split:'2.0',status:'finished',scores:{resolved_rate:.25}},{run_id:'measured',suite:'terminal-bench',split:'2.0',status:'finished',scores:{resolved_rate:0}}];
 const [best]=bestResults(rows,{},[{suite:'terminal-bench',run_id:'infra',execution_valid:false}]);
 assert.equal(best.run.run_id,'measured');assert.equal(best.score[1],0);
});

test("partially graded Terminal-Bench cannot become the best complete result",()=>{
 const runs=[{run_id:"full",suite:"terminal-bench",split:"2.0",status:"finished",scores:{resolved_rate:.4}},
 {run_id:"partial",suite:"terminal-bench",split:"2.0",status:"finished",source:"terminalbench_artifacts",complete:false,graded_complete:false,scores:{resolved_rate:1}}];
 assert.equal(bestResults(runs)[0].run.run_id,"full");
});
