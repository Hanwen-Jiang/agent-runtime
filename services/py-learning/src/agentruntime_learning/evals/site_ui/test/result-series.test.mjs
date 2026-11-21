import test from "node:test";
import assert from "node:assert/strict";
import { buildTrendGroups, buildComparisonGroups, buildCategoryCharts, isChartEligibleRun } from "../src/result-series.js";

const run = (id, value, day, overrides = {}) => ({ run_id: id, suite: "terminal-bench", split: "2.0", model: "agent-runtime-rev29", subject: "agent-runtime-rev29", planned: 20,
  status: "finished", scores: { resolved_rate: value }, started_at: `2026-11-${String(day).padStart(2, "0")}T12:00:00Z`, ...overrides });

test("dated repeated tests preserve raw improvement, decline and zero", () => {
  const rows = [run("third", .25, 3), run("first", 0, 1), run("second", .5, 2), run("bad", .8, 4, { valid: false }), run("pending", 1, 5, { status: "running" })];
  const original = structuredClone(rows);
  const [group] = buildTrendGroups({ runs: rows }, {});
  assert.deepEqual(group.points.map((p) => p.id), ["first", "second", "third"]);
  assert.deepEqual(group.points.map((p) => p.value), [0, .5, .25]);
  assert.equal(group.delta, .25);
  assert.equal(group.best_value, .5);
  assert.equal(group.axis, "time");
  assert.equal(group.x_axis, "run");
  assert.equal(group.points[0].sample_count, 20);
  assert.deepEqual(rows, original);
});

test("category trend views preserve equal-run axis metadata", () => {
  const categories = buildCategoryCharts({ runs: [run("first", .2, 1), run("second", .4, 2)] }, {}, {});
  const metric = categories.find((category) => category.suite === "terminal-bench")?.metrics.find((entry) => entry.id === "resolved_rate");
  assert.equal(metric?.views.find((view) => view.axis === "time")?.x_axis, "run");
});

test("the init-50 digest labels and carries the historical batch", () => {
  const rows = [1, 2].map((day, index) => ({ run_id: index ? "gpt55-tune1" : "gpt55-base50", suite: "swebench", split: "verified", model: "agent-runtime-gpt-5.5", selected: 50, rate: index ? .68 : .64, complete: true, created_at: `2026-11-0${day}T12:00:00Z`, cohort_sha256: "2d21d701620618b80532399f6e021eaa8ce227c3b5a813d72b85920f592c8f0f", protocol: "official-v1" }));
  const group = buildTrendGroups({}, {}, { runs: rows })[0];
  assert.equal(group.batch_id, "init-50");
  assert.ok(group.points.every((point) => point.batch_id === "init-50"));
  const label = buildCategoryCharts({}, {}, { runs: rows })[0].metrics[0].views[0].series[0].batch_id;
  assert.equal(label, "init-50");
});

test("published fixed comparisons remain available when the live experiment feed is empty", () => {
  const categories = buildCategoryCharts({}, { source: "archive", reports: { experiments: [] } }, {});
  const memory = categories.find((category) => category.suite === "longmemeval")?.metrics.find((metric) => metric.id === "recall_any@5");
  assert.deepEqual(memory?.views[0].series[0].points.map((point) => point.value), [.88, .96]);
  assert.ok(memory?.views[0].series[0].points.every((point) => point.batch_id === "longmemeval-50"));
  const context = categories.find((category) => category.suite === "longbench-context")?.metrics.find((metric) => metric.id === "confirmed_share");
  assert.deepEqual(context?.views[0].series[0].points.map((point) => point.value), [.475, .75]);
  assert.ok(context?.views[0].series[0].points.every((point) => point.batch_id === "longbench-40"));
});

test("the 2026-11-10 Terminal-Bench official result remains chartable when the artifact feed is stale", () => {
  const categories = buildCategoryCharts({}, {}, { source: "artifacts", runs: [] });
  const metric = categories.find((category) => category.suite === "terminal-bench")?.metrics.find((entry) => entry.id === "resolved_rate");
  assert.ok(metric);
  assert.ok(metric.views.some((view) => view.series.some((series) => series.points.some((point) => point.id === "tb20-official-round1-20261110" && point.value === .7))));
});

test("sample sizes, splits, actual models and protocol versions form separate groups", () => {
  const dimensions = [{ planned: 1 }, { planned: 20 }, { planned: 20, split: "other" }, { planned: 20, model: "different-model" }, { planned: 20, protocol_version: "v2" }];
  const rows = dimensions.flatMap((overrides, i) => [run(`a${i}`, .3, 1, overrides), run(`b${i}`, .4, 2, overrides)]);
  const groups = buildTrendGroups({ runs: rows }, {});
  assert.equal(groups.length, 5);
  assert.ok(groups.every((g) => g.points.length === 2));
});

test("system revisions can form a time series without merging different model candidates", () => {
  const [group] = buildTrendGroups({ runs: [run("a", .2, 1, { model: "agent-runtime-rev27", subject: "agent-runtime-rev27" }), run("b", .4, 2)] }, {});
  assert.equal(group.points.length, 2);
  assert.match(group.note_en, /not fully recorded/);
  assert.deepEqual(buildTrendGroups({ runs: [run("a", .2, 1, { suite: "capability", model: "model-a" }), run("b", .4, 2, { suite: "capability", model: "model-b" })] }, {}), []);
});

test("missing scores, dates and nonfinite values never become fabricated zero points", () => {
  const rows = [run("a", .2, 1), run("b", null, 2), run("c", NaN, 3), run("d", Infinity, 4), run("e", .6, 5, { started_at: null }), run("f", 2, 6)];
  assert.deepEqual(buildTrendGroups({ runs: rows }, {}), []);
  assert.deepEqual(buildTrendGroups({}, { reports: { benchmarks: [{ run_id: "date-in-id-20261101", complete: true, selected: 50, rate: .64 }, { run_id: "date-in-id-20261102", complete: true, selected: 50, rate: .68 }] } }), []);
});

test("lower-is-better metrics retain correct improvement direction", () => {
  const rows = [run("a", 0, 1, { suite: "context", scores: { "full.repeat_rate": .4 } }), run("b", 0, 2, { suite: "context", scores: { "full.repeat_rate": .1 } })];
  const [group] = buildTrendGroups(rows, {});
  assert.equal(group.direction, "lower");
  assert.ok(group.improved && group.delta < 0);
  assert.equal(group.best_value, .1);
});

test("live/archive duplicates are not counted twice and repeated same-day trials remain", () => {
  const live = { runs: [run("shared", .6, 1, { suite: "swebench", split: "verified", planned: 50 }), run("next", .7, 2, { suite: "swebench", split: "verified", planned: 50 }), run("same-day-repeat", .7, 2, { suite: "swebench", split: "verified", planned: 50 })] };
  const archive = { reports: { benchmarks: [{ run_id: "shared", created_at: "2026-11-01T12:00:00Z", model: "agent-runtime-rev29", selected: 50, complete: true, rate: .6 }] } };
  const groups = buildTrendGroups(live, archive);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].points.length, 3);
  assert.equal(groups[0].points.filter((p) => p.id === "shared").length, 1);
});

test("archive zero task denominators stay unknown and do not create 0% task success", () => {
  const reports = { runs: [1, 2].map((day) => ({ group: "history", subject: "worker", split: "holdout", valid: true, task_pass: 0, task_total: 0, triage: .8, created_at: `2026-11-0${day}T12:00:00Z` })) };
  const groups = buildTrendGroups({}, { reports });
  assert.equal(groups.length, 1);
  assert.equal(groups[0].metric, "triage_accuracy");
  assert.equal(groups[0].sample_count, null);
  assert.match(groups[0].cohort_en, /unrecorded/);
});

test("known dates with day precision remain dates rather than invented event timestamps", () => {
  const groups = buildTrendGroups({}, { reports: { contexts: [{ group: "one", split: "dev", date: "2026-11-01", full_probe: .8 }, { group: "two", split: "dev", date: "2026-11-02", full_probe: .9 }] } });
  assert.equal(groups[0].points[0].at, "2026-11-01");
  assert.equal(groups[0].points[0].date_precision, "day");
});

test("same-problem tuning uses stage order and retains regressions without invented dates", () => {
  const tuning = [
    { run_id: "base", selected: 50, same_problems: true, rate: .64, short_zh: "基线", short_en: "Baseline" },
    { run_id: "tune1", selected: 50, same_problems: true, rate: .68, short_en: "Tuning 1" },
    { run_id: "tune2", selected: 50, same_problems: true, rate: .62, short_en: "Tuning 2" },
    { run_id: "other", selected: 10, same_problems: false, rate: 1 },
  ];
  const [group] = buildComparisonGroups({}, { reports: { tuning } });
  assert.equal(group.axis, "sequence");
  assert.equal(group.comparison_basis, "same_problems");
  assert.deepEqual(group.points.map((p) => p.value), [.64, .68, .62]);
  assert.ok(group.points.every((p) => p.at === null));
  assert.equal(group.improved, false);
  assert.equal(group.has_improvement, true);
});

test("retrieval comparisons use paired arms; incompatible policy structure rates are excluded", () => {
  const experiments = [
    { id: "memory-new", kind: "retrieval-comparison", sample_count: 50, completed_count: 50, collection_complete: true,
      baseline_label: "Legacy", candidate_label: "BM25", completed_at: "2026-11-09T09:00:00Z", metrics: [{ key: "ndcg_any@10", unit: "score", baseline: .78, candidate: .87 }] },
    { id: "policy", kind: "paired-policy", sample_count: 22, completed_count: 22, collection_complete: true,
      metrics: [{ key: "planning_structure_pass_rate", unit: "rate", baseline: 1, candidate: .54 }] },
  ];
  const [group] = buildComparisonGroups({}, { reports: { experiments } });
  assert.equal(group.axis, "sequence");
  assert.equal(group.suite, "longmemeval");
  assert.deepEqual(group.points.map((p) => p.label), ["Legacy", "BM25"]);
  assert.ok(group.points.every((p) => p.at === null));
  assert.equal(group.completed_at, "2026-11-09T09:00:00Z");
  assert.deepEqual(buildComparisonGroups({}, { reports: { experiments: experiments.map((r) => ({ ...r, quality_valid: false })) } }), []);
});

test('verified benchmark manifests include latest 76% across model revisions and deduplicate live runs',()=>{
  const cohort='a'.repeat(64);
  const runs=[.64,.68,.62,.76].map((rate,i)=>({run_id:i===3?'t18-rev19':'stage-'+i,suite:'swebench',split:'verified',model:i===3?'agent-runtime-rev19':'agent-runtime-gpt-5.5',selected:50,rate,complete:true,created_at:`2026-11-0${i+1}T10:00:00Z`,cohort_sha256:cohort,protocol:'official-workspace-patch-v1'}));
  const live={runs:[run('t18-rev19',.76,4,{suite:'swebench',split:'verified',planned:50})]};
  const groups=buildTrendGroups(live,{}, {runs});
  assert.equal(groups.length,1);
  assert.deepEqual(groups[0].points.map(p=>p.value),[.64,.68,.62,.76]);
  assert.equal(groups[0].comparison_basis,'verified_cohort');
  assert.equal(groups[0].points.at(-1).id,'t18-rev19');
  assert.equal(groups[0].points.at(-1).sample_count,50);
});


test("one SWE category combines 50/5 samples and singleton batches without duplicating tuning", () => {
  const fifty=[.64,.68,.62,.60,.76].map((rate,i)=>({run_id:i<3?["base","tune1","tune2"][i]:`fifty-${i}`,suite:"swebench",split:"verified",model:`agent-runtime-rev${i}`,selected:50,rate,complete:true,created_at:`2026-11-0${i+1}T10:00:00Z`,cohort_sha256:"a".repeat(64),protocol:"official-v1"}));
  const five=[0,0,.4].map((rate,i)=>({run_id:`five-${i}`,suite:"swebench",split:"verified",model:`agent-runtime-rev${i}`,selected:5,rate,complete:true,created_at:`2026-11-0${i+1}T11:00:00Z`,cohort_sha256:"b".repeat(64),protocol:"official-v1"}));
  const singleton={...five[0],run_id:"single",selected:1,cohort_sha256:"c".repeat(64)};
  const archive={reports:{tuning:fifty.slice(0,3).map(r=>({...r,same_problems:true}))}};
  const categories=buildCategoryCharts({},archive,{runs:[...fifty,...five,singleton]});
  assert.equal(categories.length,1);assert.equal(categories[0].suite,"swebench");
  const metric=categories[0].metrics[0];assert.equal(metric.id,"resolved_rate");assert.deepEqual(metric.views.map(v=>v.axis),["time"]);
  const lines=metric.views[0].series;
  assert.deepEqual(lines.map(s=>s.sample_count),[50,5,1]);
  assert.deepEqual(lines[0].points.map(p=>p.value),[.64,.68,.62,.60,.76]);
  assert.deepEqual(lines[1].points.map(p=>p.value),[0,0,.4]);
  assert.equal(lines[2].points.length,1);
});

test("multiple metrics and time/comparison views stay inside one category", () => {
  const live={runs:[run("one",.2,1,{suite:"longmemeval",scores:{"recall_any@10":.8,"ndcg_any@10":.7}}),run("two",.3,2,{suite:"longmemeval",scores:{"recall_any@10":.9,"ndcg_any@10":.8}})]};
  const archive={reports:{experiments:[{id:"paired-memory",kind:"retrieval-comparison",collection_complete:true,sample_count:50,completed_count:50,completed_at:"2026-11-03T10:00:00Z",baseline_label:"Legacy",candidate_label:"BM25",metrics:[{key:"recall_any@10",unit:"rate",baseline:.9,candidate:.98},{key:"ndcg_any@10",unit:"score",baseline:.8,candidate:.87}]}]}};
  const [category]=buildCategoryCharts(live,archive);
  assert.equal(category.suite,"longmemeval");assert.equal(category.metrics.length,2);assert.equal(category.default_metric,"recall_any@10");
  assert.deepEqual(category.metrics[0].views.map(v=>v.axis),["sequence","time"]);
  assert.equal(category.metrics[0].views[0].series[0].points[0].at,null);
});


test("oracle and gold harness checks are excluded without filtering failed agent runs",()=>{
 const base={suite:"swebench",split:"verified",selected:1,complete:true,created_at:"2026-11-01T10:00:00Z",cohort_sha256:"a".repeat(64)};
 const runs=[{...base,run_id:"agent-failed1",model:"agent-runtime",rate:0},{...base,run_id:"agent-failed2",model:"agent-runtime",rate:0,created_at:"2026-11-02T10:00:00Z"},{...base,run_id:"gold-smoke",model:"gold-smoke",rate:1},{...base,run_id:"gold-smoke-2",model:"gold-smoke",rate:1},{...base,run_id:"oracle-test",model:"oracle",rate:1}];
 const [category]=buildCategoryCharts({}, {}, {runs});
 assert.deepEqual(category.metrics[0].views[0].series[0].points.map(p=>p.id),["agent-failed1","agent-failed2"]);
 assert.deepEqual(category.metrics[0].views[0].series[0].points.map(p=>p.value),[0,0]);
});


test("explicit execution invalidity vetoes a stale live score without removing real zeroes",()=>{
  const live={runs:[run("bad-artifact",0,1,{suite:"swebench",split:"verified",planned:1}),run("real-zero",0,2,{suite:"swebench",split:"verified",planned:1}),run("real-pass",1,3,{suite:"swebench",split:"verified",planned:1})]};
  const artifacts={runs:[],exclusions:[{run_id:"bad-artifact",suite:"swebench",execution_valid:false,invalid_reason:"run_id_reused"}]};
  const [category]=buildCategoryCharts(live,{},artifacts);
  assert.deepEqual(category.metrics[0].views[0].series[0].points.map(p=>p.id),["real-zero","real-pass"]);
  assert.equal(isChartEligibleRun({rate:0,valid:true}),true);
  assert.equal(isChartEligibleRun({rate:0,execution_valid:false}),false);
  assert.equal(isChartEligibleRun({rate:.7,invalid_reason:"invalid_grading"}),false);
});

const reviewerRun = (variant, repeat, value, overrides = {}) => ({
  run_id: `reviewer-${variant}-${repeat}`, subject: "worker", variant, repeat,
  comparison_id: "reviewer-paired-fixed-model-v1", protocol: "reviewer-only-v2",
  eval_set_name: "task-loop", eval_set_version: "a".repeat(64), selection_sha256: "b".repeat(64),
  model: "a6api/kimi-k3.1-pro@2026-11", models: {judge: {"a6api/kimi-k3.1-pro@2026-11": 16}},
  cohort_id: "legacy-dev", split: "dev", reviewer_samples: 16, labels_confirmed: 16, labels_draft: 0,
  reviewer: value, valid: true, created_at: `2026-11-10T01:0${repeat}:00Z`, ...overrides,
});
const reviewerComparisons = (runs) => buildComparisonGroups({}, {reports: {runs}});

test("Reviewer comparison uses arm medians of valid paired repeats without inflating sample size", () => {
  const rows = [.25, .625, .75].map((value, i) => reviewerRun("baseline", i, value)).concat(
    [.875, .75, .5].map((value, i) => reviewerRun("candidate", i, value)));
  const original = structuredClone(rows);
  const [group] = reviewerComparisons(rows);
  assert.equal(group.metric, "verdict_accuracy");
  assert.equal(group.axis, "sequence");
  assert.equal(group.comparison_basis, "reviewer_same_questions_paired_repeats");
  assert.deepEqual(group.points.map(point => point.value), [.625, .75]);
  assert.equal(group.sample_count, 16);
  assert.equal(group.repeat_count, 3);
  assert.ok(group.points.every(point => point.sample_count === 16 && point.repeat_count === 3 && point.at === null));
  assert.deepEqual(group.points[0].run_ids, ["reviewer-baseline-0", "reviewer-baseline-1", "reviewer-baseline-2"]);
  assert.match(group.points[0].label_zh, /3 次中位数/);
  assert.match(group.note_zh, /3 次重复仍为 16 道题/);
  assert.equal(group.completed_at, "2026-11-10T01:02:00Z");
  assert.deepEqual(rows, original);
});

test("Reviewer regression and below-gate results remain visible, including a true zero", () => {
  const [group] = reviewerComparisons([reviewerRun("baseline", 0, .625), reviewerRun("candidate", 0, 0)]);
  assert.deepEqual(group.points.map(point => point.value), [.625, 0]);
  assert.equal(group.delta, -.625);
  assert.equal(group.improved, false);
  assert.match(group.subject_zh, /不代表完整 S-10 门槛通过/);
});

test("Reviewer draft regression, legacy dev and holdout are separate cohorts with honest labels", () => {
  const specs = [
    {},
    {cohort_id:"regression-dev",eval_set_name:"reviewer-regressions-dev",eval_set_version:"c".repeat(64),selection_sha256:"d".repeat(64),reviewer_samples:10,labels_confirmed:0,labels_draft:10},
    {cohort_id:"legacy-holdout",split:"holdout",selection_sha256:"e".repeat(64),reviewer_samples:8,labels_confirmed:8},
  ];
  const rows = specs.flatMap((spec, i) => [reviewerRun("baseline", 0, .5, {...spec,run_id:`b${i}`}),reviewerRun("candidate", 0, .625, {...spec,run_id:`c${i}`})]);
  const groups = reviewerComparisons(rows);
  assert.equal(groups.length,3);
  assert.deepEqual(groups.map(g=>g.sample_count).sort((a,b)=>a-b),[8,10,16]);
  const draft = groups.find(g=>g.cohort_id==="regression-dev");
  assert.equal(draft.labels_draft,10);
  assert.match(draft.subject_zh,/草稿.*不作为正式准入/);
  assert.ok(draft.points.every(point=>/非正式准入/.test(point.label_zh)));
  assert.match(draft.points[0].label,/draft only/);
  const category = buildCategoryCharts({}, {reports:{runs:rows}}).find(c=>c.suite==="task-loop");
  assert.equal(category.metrics.find(m=>m.id==="verdict_accuracy").views.find(v=>v.axis==="sequence").series.length,3);
});

test("Reviewer comparisons never cross mismatching model, protocol, questions, size or cohort", () => {
  for (const changed of [
    {comparison_id:"other"}, {cohort_id:"other"}, {eval_set_name:"reviewer-regressions-dev"},
    {eval_set_version:"c".repeat(64)}, {selection_sha256:"d".repeat(64)}, {protocol:"reviewer-only-v3"},
    {model:"different-model",models:{judge:{"different-model":16}}}, {split:"holdout"},
    {reviewer_samples:8,labels_confirmed:8}, {labels_confirmed:0,labels_draft:16},
  ]) assert.deepEqual(reviewerComparisons([reviewerRun("baseline",0,.5),reviewerRun("candidate",0,.75,changed)]),[],JSON.stringify(changed));
});

test("missing provenance, unknown validity and mixed-model rows cannot create Reviewer comparisons", () => {
  for (const changed of [
    {comparison_id:null},{cohort_id:null},{eval_set_version:null},{selection_sha256:null},{protocol:null},
    {model:null},{model:"model-unrecorded"},{run_id:null},{repeat:null},{repeat:-1},{reviewer_samples:null},
    {valid:null},{valid:false},{reviewer:NaN},{reviewer:1.1},{labels_draft:null},{labels_confirmed:15},
    {models:{judge:{"a6api/kimi-k3.1-pro@2026-11":15,"other-model":1}}},
    {models:null},{models:{}},{models:{judge:{"other-model":16}}},{subject:"planner"},{reference:true},
  ]) assert.deepEqual(reviewerComparisons([reviewerRun("baseline",0,.5),reviewerRun("candidate",0,.75,changed)]),[],JSON.stringify(changed));
  // An early unlabeled result with the same numerical score cannot be used as a baseline.
  assert.deepEqual(reviewerComparisons([{subject:"worker",valid:true,reviewer:.5},reviewerRun("candidate",0,.75)]),[]);
});

test("unpaired and invalid repeats do not bias the paired median while a run is still completing", () => {
  const rows = [reviewerRun("baseline",0,.5),reviewerRun("candidate",0,.625),
    reviewerRun("baseline",1,1),reviewerRun("baseline",2,.875),reviewerRun("candidate",2,.875,{valid:false})];
  const [partial] = reviewerComparisons(rows);
  assert.equal(partial.repeat_count,1);
  assert.deepEqual(partial.points.map(p=>p.value),[.5,.625]);
  assert.deepEqual(partial.points[0].repeats,[0]);
  const [updated] = reviewerComparisons([...rows,reviewerRun("candidate",1,.75)]);
  assert.equal(updated.repeat_count,2);
  assert.deepEqual(updated.points.map(p=>p.value),[.75,.6875]);
  assert.equal(updated.improved,false);
});

test("Reviewer duplicate copies do not count twice and conflicting or invalid copies veto a repeat", () => {
  const base=reviewerRun("baseline",0,.5),candidate=reviewerRun("candidate",0,.75);
  assert.equal(reviewerComparisons([base,candidate,{...base},{...candidate}])[0].repeat_count,1);
  for(const changed of [{reviewer:.25},{valid:false},{run_id:"different-baseline-run"}]) {
    assert.deepEqual(reviewerComparisons([base,candidate,{...base,...changed}]),[]);
    assert.deepEqual(reviewerComparisons([{...base,...changed},candidate,base]),[]);
  }
});

test("Reviewer sequential comparisons do not fabricate timestamps", () => {
  const [group]=reviewerComparisons([reviewerRun("baseline",0,.5,{created_at:null}),reviewerRun("candidate",0,.75)]);
  assert.equal(group.completed_at,null);
  assert.ok(group.points.every(p=>p.at===null));
});

test("partial final TB artifact suppresses a stale live peak without treating not_run as zero",()=>{
 const live={runs:[run("old-complete",.25,1),run("tb-final",1,2)]};
 const partial={suite:"terminal-bench",run_id:"tb-final",source:"terminalbench_artifacts",status:"partial",complete:false,graded_complete:false,selected:20,graded_count:1,not_run:19,rate:1,scores:{resolved_rate:1}};
 assert.deepEqual(buildTrendGroups(live,{}, {runs:[partial]}),[]);
 const full={...partial,complete:true,graded_complete:true,graded_count:20,not_run:0,rate:.4,status:"finished",scores:{resolved_rate:.4},created_at:"2026-11-02T10:00:00Z",cohort_sha256:"a".repeat(64)};
 const rows=buildTrendGroups(live,{}, {runs:[full]}, {includeSingle:true}).flatMap(g=>g.points);
 assert.equal(rows.filter(p=>p.id==='tb-final').length,1);assert.equal(rows.find(p=>p.id==='tb-final').value,.4);
});
