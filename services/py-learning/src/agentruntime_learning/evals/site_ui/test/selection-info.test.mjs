import assert from "node:assert/strict";
import { test } from "node:test";
import { archivedSelections, hidesCaseIds, selectionInfoForRow, selectionMethod, selectionReference } from "../src/selection-info.js";
import { reportRuns } from "../src/report-runs.js";
import { archiveCandidates } from "../src/best-results.js";

const en = (_, english) => english;
const zh = (chinese) => chinese;
const evidence = { dataset: "SWE-bench Verified", method: "seeded_random", seed: 0, selected_count: 50 };

test("an archive may borrow a manifest only for the exact benchmark and run", () => {
  const fed = { runs: [{ suite: "swebench", run_id: "same", selection_info: evidence }] };
  const rows = [{ run_id: "same" }, { run_id: "other" }, { suite: "terminal-bench", run_id: "same" }];
  const old = { reports: { benchmarks: rows, tuning: rows } };
  const updated = archivedSelections(old, fed);
  for (const view of ["benchmarks", "tuning"]) {
    assert.deepEqual(updated.reports[view].map(row => row.selection_info), [evidence, undefined, undefined]);
  }
  assert.equal(rows[0].selection_info, undefined);
  const live = { runs: [{suite:"swebench", run_id:"same", done:48}, {suite:"terminal-bench", run_id:"same", done:3}] };
  const runs = reportRuns(live, null, fed);
  assert.equal(runs.find(r => r.suite === "swebench").done, 48);
  assert.deepEqual(runs.find(r => r.suite === "swebench").selection_info, evidence);
  assert.equal(runs.find(r => r.suite === "terminal-bench").selection_info, undefined);
  const winner = archiveCandidates({benchmarks:[{...rows[0], complete:true, rate:0.76, selected:50, selection_info:evidence}]});
  assert.deepEqual(winner[0].selection_info, evidence);
});

test("explanations use recorded localized methods and mark absent data explicitly", () => {
  assert.match(selectionMethod(evidence, zh), /种子/);
  assert.match(selectionMethod({}, en), /No selection method/);
  assert.match(selectionMethod({ method: "historical_unrecorded" }, zh), /未记录/);
  assert.equal(selectionMethod({ method: "frozen_cohort", method_detail_en: "Frozen before tuning." }, en), "Frozen before tuning.");
});

test("the historical init-50 cohort fills recorded selection evidence by digest", () => {
  const info = selectionInfoForRow({ suite: "swebench", split: "verified", selected: 50,
    cohort_sha256: "2d21d701620618b80532399f6e021eaa8ce227c3b5a813d72b85920f592c8f0f" });
  assert.equal(info.batch_id, "init-50");
  assert.equal(info.selected_count, 50);
  assert.equal(info.selection_ids.length, 50);
  assert.equal(info.seed, 0);
  assert.match(selectionMethod(info, zh), /init-50/);
});

test("new Mini 50 run IDs use the mini-50 batch label", () => {
  const info = selectionInfoForRow({ run_id: "mini50-gpt62-formal-a-20261121T1900Z", suite: "swebench", selected: 50 });
  assert.equal(info.batch_id, "mini-50");
  assert.equal(info.selected_count, 50);
  const retry = selectionInfoForRow({run_id:"mini50-quota-20261122-a",suite:"swebench",source:"swe_progress",selected:9});
  assert.equal(retry.selected_count,9);
  assert.equal(retry.batch_id,undefined);
  const ids = ["django__django-11885","sphinx-doc__sphinx-8721"];
  const live = selectionInfoForRow({source:"swe_progress",selected:2,selection_info:{selection_ids:ids,method:"explicit_ids"}});
  assert.deepEqual(live.selection_ids, ids);
});

test("unidentified historical rows do not inherit a fixed batch", () => {
  assert.deepEqual(selectionInfoForRow({ run_id: "old-terminal-run", suite: "terminal-bench" }), {});
});

test("matching run names cannot override a conflicting cohort digest or selected count", () => {
  const original = { dataset: "SWE", method: "explicit_ids", selected_count: 49 };
  const rows = [
    { run_id: "same", selected: 49, selection_info: original },
    { run_id: "same", selected: 50, cohort_sha256: "a".repeat(64) },
  ];
  const benchmarks = { runs: [{ suite: "swebench", run_id: "same", selection_info: { ...evidence, cohort_sha256: "b".repeat(64) } }] };
  const out = archivedSelections({ reports: { benchmarks: rows } }, benchmarks);
  assert.deepEqual(out.reports.benchmarks, rows);
  const live = { ...rows[0], suite: "swebench", run_id: "same" };
  assert.deepEqual(reportRuns({ runs: [live] }, null, benchmarks), [live]);
});

test("hidden selections win over public nested group labels; reference URLs are HTTPS", () => {
  assert.equal(hidesCaseIds({ split: "train" }, {split: "held-out"}), true);
  assert.equal(hidesCaseIds({ split: "hidden" }), true);
  assert.equal(hidesCaseIds({ split: "test" }), false);
  assert.equal(selectionReference("https://huggingface.co/datasets/demo"), "https://huggingface.co/datasets/demo");
  for (const url of ["javascript:alert(1)", "https://user:pass@example.com/", "/docs", undefined]) assert.equal(selectionReference(url), null);
});
