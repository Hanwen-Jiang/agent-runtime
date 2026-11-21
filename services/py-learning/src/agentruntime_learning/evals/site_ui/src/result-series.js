// Pure, deterministic data preparation for results charts. No wall-clock dates,
// inferred zeroes, carry-forward points, or cross-model capability time series.
import { batchForRow, selectionInfoForRow } from "./selection-info.js";
import { FIXED_EXPERIMENTS, FIXED_TERMINAL_RUNS } from "./fixed-experiments.js";

const METRICS = {
  accuracy: ["答题准确率", "Answer accuracy", "ratio", "higher"],
  confirmed_share: ["已确认答对占计划题数", "Confirmed correct / planned questions", "ratio", "higher"],
  resolved_rate: ["解决率", "Resolved rate", "ratio", "higher"],
  success_rate: ["任务用例通过率", "Task-case pass rate", "ratio", "higher"],
  triage_accuracy: ["分诊准确率", "Triage accuracy", "ratio", "higher"],
  verdict_accuracy: ["判定准确率", "Verdict accuracy", "ratio", "higher"],
  shape_match_rate: ["规划匹配率", "Plan match rate", "ratio", "higher"],
  pass_rate: ["通过率", "Pass rate", "ratio", "higher"],
  correctness: ["正确性", "Correctness", "score", "higher"],
  honesty: ["真实性", "Honesty", "score", "higher"],
  relevancy: ["相关性", "Relevancy", "score", "higher"],
  mrr: ["平均倒数排名", "Mean reciprocal rank", "score", "higher"],
  recall: ["召回率", "Recall", "ratio", "higher"],
  hit: ["命中率", "Hit rate", "ratio", "higher"],
  llm_context_precision: ["上下文精确率", "Context precision", "ratio", "higher"],
  llm_context_recall: ["上下文召回率", "Context recall", "ratio", "higher"],
  "full.probe_pass_rate": ["完整压缩 · 探针通过率", "Full compression · Probe pass rate", "ratio", "higher"],
  "full.retention_rate": ["完整压缩 · 信息保留率", "Full compression · Retention rate", "ratio", "higher"],
  "mask_only.probe_pass_rate": ["仅遮蔽 · 探针通过率", "Mask only · Probe pass rate", "ratio", "higher"],
  "mask_only.retention_rate": ["仅遮蔽 · 信息保留率", "Mask only · Retention rate", "ratio", "higher"],
  "full.repeat_rate": ["完整压缩 · 重复执行率", "Full compression · Repeated action rate", "ratio", "lower"],
  "mask_only.repeat_rate": ["仅遮蔽 · 重复执行率", "Mask only · Repeated action rate", "ratio", "lower"],
  fallback_rate: ["回退率", "Fallback rate", "ratio", "lower"],
  cost_usd: ["运行费用", "Run cost", "usd", "lower"],
};
const SUITES = {
  "terminal-bench": ["Terminal-Bench", "Terminal-Bench"],
  swebench: ["SWE-bench", "SWE-bench"],
  "task-loop": ["Task Loop", "Task Loop"],
  context: ["上下文", "Context"],
  deepeval: ["交付质量", "Deliverable quality"],
  ragas: ["记忆检索", "Memory retrieval"],
  longmemeval: ["LongMemEval", "LongMemEval"],
  "longbench-context": ["LongBench v2 · Runtime 消融", "LongBench v2 · Runtime ablation"],
};
const finite = (v) => typeof v === "number" && Number.isFinite(v);
const positiveCount = (v) => Number.isInteger(v) && v > 0 ? v : null;
const string = (v) => typeof v === "string" ? v : "";
const reportsOf = (archive) => archive?.reports || archive || {};
const list = (v) => Array.isArray(v) ? v : [];
function stamp(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}(?:T|$)/.test(value)) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? { at: value, ms, date_precision: value.length === 10 ? "day" : "time" } : null;
}
function firstStamp(row) {
  for (const value of [row.started_at, row.created_at, row.completed_at, row.updated_at, row.date]) {
    const result = stamp(value);
    if (result) return result;
  }
  return null;
}
function metricSpec(key, explicit = {}) {
  const known = METRICS[key];
  if (known) return { label_zh: known[0], label_en: known[1], unit: known[2], direction: known[3] };
  const unit = ["rate", "ratio", "fraction"].includes(explicit.unit) ? "ratio" : explicit.unit ||
    (/ndcg|(?:^|[._])score$/.test(key) ? "score" : /(?:rate|accuracy|precision|recall)$/.test(key) || /^(?:recall|precision|hit)(?:_any|_all)?@\d+$/.test(key) ? "ratio" : null);
  if (!unit || !["ratio", "score", "number", "usd", "seconds"].includes(unit)) return null;
  return { label_zh: explicit.label_zh || key, label_en: explicit.label_en || key, unit,
    direction: explicit.direction === "lower" || /(?:error|fallback|repeat|latency|elapsed|cost|duration)/.test(key) ? "lower" : "higher" };
}
function validValue(value, spec) {
  return finite(value) && value >= 0 && (!["ratio", "score"].includes(spec.unit) || value <= 1);
}
function modelIdentity(row, archive = false) {
  const explicit = string(row.model || row.model_profile || row.model_profile_id);
  const subject = string(row.subject || "agent-runtime");
  // Only the system's revision suffix is normalized. Different actual model
  // profiles, model mixtures, planner-only subjects and retrievers remain apart.
  if (explicit) return `${subject.replace(/-rev\d+(?:[.-].*)?$/, "")}|${explicit.replace(/-rev\d+(?:[.-].*)?$/, "")}`;
  if (archive) {
    const labels = `${string(row.label_en)} ${string(row.label_zh)}`;
    const hint = labels.match(/Sonnet|Opus|GPT-[\d.]+|Gemini\s+[\d.]+\s*Flash/i)?.[0]?.toLowerCase();
    return `${subject}|${hint ? "label:" + hint : "model-unrecorded"}`;
  }
  return `${subject}|model-unrecorded`;
}
function protocolIdentity(row, fallback = "unrecorded") {
  const contract = row.scoring_contract;
  const bits = [row.protocol, row.protocol_version, row.dataset_version, row.eval_set_version, row.dataset_sha256,
    row.cohort_id, row.selection_sha256, typeof contract === "string" ? contract : contract?.implementation_sha256 || contract?.id].filter((v) => typeof v === "string" && v);
  return bits.length ? bits.join("|") : fallback;
}
function runLink(id, archive = false) {
  return `/results?${archive ? "view=archive&" : ""}q=${encodeURIComponent(id || "")}${archive ? "#runs" : ""}`;
}
function stableID(parts) { return parts.map((v) => encodeURIComponent(String(v ?? ""))).join("|"); }
function groupBase(row, metric, spec) {
  const suiteLabel = SUITES[row.suite] || [row.suite, row.suite];
  return { id: stableID([row.suite, row.split, metric, row.family, row.n ?? "unknown", row.protocol]),
    axis: "time", x_axis: "run", suite: row.suite, suite_label_zh: suiteLabel[0], suite_label_en: suiteLabel[1],
    split: row.split, metric, ...spec, subject: row.subject, model_family: row.family,
    batch_id: row.batch_id || batchForRow(row),
    sample_count: row.n, protocol: row.protocol, comparison_basis: "same_suite_split_model_sample_size",
    cohort: row.n ? `${row.n} · ${row.subject}` : row.subject,
    cohort_zh: `${row.n ? `${row.n} 个运行样本 · ` : "样本数未记录 · "}${row.subject}`,
    cohort_en: `${row.n ? `${row.n} run samples · ` : "Sample count unrecorded · "}${row.subject}`,
    note_zh: "同品类、切分、模型与样本规模的运行记录；题目集合未完整记录，数值变化不等于同题因果提升。",
    note_en: "Runs share category, split, model and sample size. Cohort identity is not fully recorded; changes do not establish a same-task causal improvement.", points: [] };
}
function referenceRun(row) {
  if (row.reference === true || row.is_reference === true) return true;
  const reference = /^(?:gold[-_]?smoke|oracle)(?:[-_\d]|$)/i;
  return [row.model, row.subject, row.run_id].some((value) => typeof value === "string" && reference.test(value));
}
// Invalidity from an audited source wins even when a second feed still carries
// an older score. A normal, actually graded zero remains a valid observation.
export function isChartEligibleRun(row) {
  return !referenceRun(row) && !(row.source === "terminalbench_artifacts" &&
    (row.complete !== true || row.graded_complete !== true || row.infra_failed > 0 || row.grading_errors > 0)) && row.valid !== false && row.quality_valid !== false && row.execution_valid !== false &&
    row.grading_complete !== false && !(typeof row.invalid_reason === "string" && row.invalid_reason.trim());
}
function liveRows(live) {
  return list(Array.isArray(live) ? live : live?.runs).flatMap((r) => {
    if (!isChartEligibleRun(r) || r.suite === "capability" ||
        (r.status !== "finished" && !(r.finished === true && !r.status))) return [];
    const time = firstStamp(r), n = positiveCount(r.sample_count ?? r.selected ?? r.planned);
    if (!time || !r.suite || !r.run_id) return [];
    return [{ ...time, selection_record: { ...r, selection_info: selectionInfoForRow(r) }, selection_info: selectionInfoForRow(r), batch_id: batchForRow(r), id: r.run_id, suite: r.suite, split: string(r.split), n,
      family: modelIdentity(r), subject: string(r.subject || r.model || r.suite),
      protocol: protocolIdentity(r), scores: r.scores || {}, metrics: r.metrics || {},
      label: string(r.model || r.subject || r.run_id), source: "live", href: runLink(r.run_id) }];
  });
}
function archiveRows(reports) {
  const out = [];
  for (const r of list(reports.runs)) {
    if (r.valid !== true || !isChartEligibleRun(r)) continue;
    const time = firstStamp(r); if (!time) continue;
    const n = positiveCount(r.task_total);
    const scores = { triage_accuracy: r.triage, shape_match_rate: r.shape, verdict_accuracy: r.reviewer };
    if (n && finite(r.task_pass) && r.task_pass >= 0 && r.task_pass <= n) scores.success_rate = r.task_pass / n;
    out.push({ ...time, selection_record: r, selection_info: selectionInfoForRow(r), batch_id: batchForRow(r), id: r.run_id || `archive:${r.group}:${r.subject}:${time.at}`, suite: "task-loop", split: string(r.split), n,
      family: modelIdentity(r, true), subject: r.subject || "worker", protocol: protocolIdentity(r), scores,
      label: [r.label_en || r.subject || r.group, r.variant].filter(Boolean).join(" · "),
      label_zh: [r.label_zh || r.subject, r.variant === "candidate" ? "候选版" : r.variant === "baseline" ? "基线版" : r.variant].filter(Boolean).join(" · "),
      label_en: [r.label_en || r.subject, r.variant].filter(Boolean).join(" · "),
      metric_samples: { verdict_accuracy: positiveCount(r.reviewer_samples) },
      source: r.source_kind ? "report-artifact" : "archive", href: runLink(r.run_id || r.group || r.subject, true) });
  }
  for (const r of list(reports.contexts)) {
    if (!isChartEligibleRun(r)) continue;
    const time = firstStamp(r); if (!time) continue;
    out.push({ ...time, selection_record: r, selection_info: selectionInfoForRow(r), batch_id: batchForRow(r), id: r.run_id || `archive-context:${r.group}:${r.split}:${time.at}`, suite: "context", split: string(r.split),
      n: positiveCount(r.sample_count ?? r.selected), family: "worker-context|archive-model-unrecorded", subject: "worker-context",
      protocol: protocolIdentity(r, "archive-context"),
      scores: { "full.probe_pass_rate": r.full_probe, "mask_only.probe_pass_rate": r.mask_probe, "full.retention_rate": r.full_retention, "full.repeat_rate": r.repeat_rate },
      label: r.group || "Context", source: "archive", href: `/results?view=archive&q=${encodeURIComponent(r.group || "context")}#contexts` });
  }
  for (const r of list(reports.benchmarks)) {
    if (!isChartEligibleRun(r) || r.complete !== true) continue;
    const time = firstStamp(r); if (!time || !r.run_id) continue;
    out.push({ ...time, selection_record: { ...r, selection_info: selectionInfoForRow(r) }, selection_info: selectionInfoForRow(r), batch_id: batchForRow(r), id: r.run_id, suite: "swebench", split: r.split || "verified", n: positiveCount(r.selected),
      family: modelIdentity({ ...r, subject: "agent-runtime" }), subject: "agent-runtime",
      protocol: protocolIdentity(r), scores: { resolved_rate: r.rate }, label: r.model || r.run_id,
      source: "archive", href: runLink(r.run_id, true) });
  }
  return out;
}
function finishGroups(groups, time = true, includeSingle = false) {
  return [...groups.values()].map((group) => {
    const points = [...group.points].sort(time ? (a, b) => Date.parse(a.at) - Date.parse(b.at) || a.id.localeCompare(b.id) : (a, b) => a.order - b.order);
    const first = points[0]?.value, last = points.at(-1)?.value;
    const delta = last - first;
    return { ...group, points, delta, improved: group.direction === "lower" ? delta < 0 : delta > 0,
      has_improvement: points.some((point, i) => i > 0 && (group.direction === "lower" ? point.value < points[i - 1].value : point.value > points[i - 1].value)),
      best_value: points.length ? (group.direction === "lower" ? Math.min : Math.max)(...points.map((p) => p.value)) : null };
  }).filter((g) => g.points.length >= (includeSingle ? 1 : 2) && (!time || includeSingle || new Set(g.points.map((p) => Date.parse(p.at))).size >= 2))
    .sort((a, b) => a.suite.localeCompare(b.suite) || a.metric.localeCompare(b.metric) || a.id.localeCompare(b.id));
}
/**
 * Time series from real dated observations. Unknown cohorts/sample counts stay
 * labelled unknown and never merge with known metadata. Multiple model rows in
 * a capability run are a matrix, not time points. Raw declines are preserved.
 */
export function buildTrendGroups(live, archive, benchmarks, { includeSingle = false } = {}) {
  const vetoed = new Set([
    ...list(benchmarks?.runs).map((r)=>({...r,suite:r.suite||"swebench"})),
    ...list(benchmarks?.exclusions),
    ...list(Array.isArray(live)?live:live?.runs),
    ...list(reportsOf(archive).benchmarks).map((r)=>({...r,suite:r.suite||"swebench"})),
  ].filter((r)=>r.run_id&&!isChartEligibleRun(r)).map((r)=>`${r.suite}\0${r.run_id}`));
  const benchmarkInput = [...list(benchmarks?.runs), ...(benchmarks?.source === "artifacts" ? FIXED_TERMINAL_RUNS.filter((fixed) => !list(benchmarks?.runs).some((row) => row.run_id === fixed.run_id)) : [])];
  const benchmarkRows = benchmarkInput.filter((r) => isChartEligibleRun(r) && r.complete === true && positiveCount(r.selected) && (r.suite === "terminal-bench" || /^[a-f0-9]{64}$/.test(r.cohort_sha256 || ""))).flatMap((r) => {
    const time = firstStamp(r); if (!time || !r.run_id) return [];
    return [{ ...time, selection_record: { ...r, selection_info: selectionInfoForRow(r) }, selection_info: selectionInfoForRow(r), batch_id: batchForRow(r), id:r.run_id, suite:r.suite || "swebench", split:r.split || "verified", n:r.selected,
      family:"agent-runtime|cohort:"+r.cohort_sha256, subject:"Agent Runtime", cohort_verified:true,
      protocol:r.protocol_sha256 || r.protocol || "official-workspace-patch-v1", scores:{resolved_rate:r.rate},
      label:`${r.model || "Agent Runtime"} · ${r.run_id}`, source:"benchmark-artifacts", href:runLink(r.run_id) }];
  });
  const terminalIDs = new Set(list(benchmarks?.runs).filter((row) => row.source === "terminalbench_artifacts").map((row) => `${row.suite}\0${row.run_id}`));
  const telemetry = Array.isArray(live) ? live : live?.runs;
  const filteredLive = list(telemetry).filter((row) => !terminalIDs.has(`${row.suite}\0${row.run_id}`));
  const rows = [...benchmarkRows, ...liveRows(filteredLive), ...archiveRows(reportsOf(archive))].filter((row)=>!vetoed.has(`${row.suite}\0${row.id}`));
  const groups = new Map(), seenRunMetrics = new Set(), seenObservations = new Map();
  for (const row of rows) {
    for (const [metric, value] of Object.entries(row.scores)) {
      const spec = metricSpec(metric, row.metrics?.[metric]);
      if (!spec || !validValue(value, spec)) continue;
      const metricRow = row.metric_samples?.[metric] ? { ...row, n: row.metric_samples[metric] } : row;
      const group = groupBase(metricRow, metric, spec);
      if (row.cohort_verified) Object.assign(group, {
        cohort_zh:`${row.n} 道相同题目 · 跨版本运行`, cohort_en:`${row.n} identical problems · Runtime revisions`,
        comparison_basis:"verified_cohort", note_zh:"题目清单哈希一致，时间取自原始运行记录；每个点保留当时的模型与版本。", note_en:"Identical question-list hashes; timestamps come from the original run manifests. Each point retains its model and revision."
      });
      const runKey = stableID([row.suite, row.id, metric]);
      const observation = stableID([group.id, row.ms, value.toFixed(6)]);
      if (seenRunMetrics.has(runKey) || (seenObservations.has(observation) && seenObservations.get(observation) !== row.source)) continue;
      seenRunMetrics.add(runKey); seenObservations.set(observation, row.source);
      if (!groups.has(group.id)) groups.set(group.id, group);
      groups.get(group.id).points.push({ selection_record: row.selection_record, selection_info: row.selection_info, batch_id: row.batch_id || group.batch_id, id: row.id, at: row.at, value, label: row.label, label_zh: row.label_zh || row.label,
        label_en: row.label_en || row.label, sample_count: metricRow.n, source: row.source, href: row.href, date_precision: row.date_precision });
    }
  }
  return finishGroups(groups, true, includeSingle);
}
/**
 * Explicit ordered experimental comparisons. `at` is null: these are labelled
 * stages/arms, never fabricated dates. Only same-problem tuning and validated
 * retrieval/Reviewer comparisons are included (Off/Rules structural rates are not).
 */
export function buildComparisonGroups(_live, archive, includeFixed = false) {
  const reports = reportsOf(archive), groups = new Map();
  // Reviewer runs are repeated measurements of the same questions, not added
  // independent samples. Compare only completed, valid pairs of the same repeat.
  const reviewerGroups = new Map();
  const hash = (value) => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
  const count = (value) => Number.isInteger(value) && value >= 0;
  const median = (values) => {
    const sorted = [...values].sort((a, b) => a - b), mid = Math.floor(sorted.length / 2);
    return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
  };
  for (const row of list(reports.runs)) {
    const n = positiveCount(row.reviewer_samples);
    if (row.subject !== "worker" || !["baseline", "candidate"].includes(row.variant) || !n ||
        ![row.comparison_id, row.eval_set_name, row.protocol, row.model, row.cohort_id, row.run_id].every((value) => string(value).trim()) ||
        !["dev", "holdout"].includes(row.split) || !hash(row.eval_set_version) || !hash(row.selection_sha256) ||
        !count(row.repeat) || !count(row.labels_confirmed) || !count(row.labels_draft) || row.labels_confirmed + row.labels_draft !== n ||
        row.model.includes(",") || /^(?:model-)?unrecorded$/.test(row.model)) continue;
    const modelCalls = row.models?.judge;
    // An explicit mixture or contradictory model provenance cannot be relabelled
    // as a fixed-model experiment simply because the display model matches.
    if (!modelCalls || Object.keys(modelCalls).length !== 1 || !positiveCount(modelCalls[row.model])) continue;
    const id = stableID(["comparison", "reviewer", row.comparison_id, row.eval_set_name, row.eval_set_version,
      row.selection_sha256, row.protocol, row.model, row.split, row.cohort_id, n, row.labels_confirmed, row.labels_draft]);
    if (!reviewerGroups.has(id)) reviewerGroups.set(id, { row, n, baseline: new Map(), candidate: new Map() });
    const cohort = reviewerGroups.get(id), arm = cohort[row.variant];
    const eligible = row.valid === true && isChartEligibleRun(row) && validValue(row.reviewer, metricSpec("verdict_accuracy"));
    const existing = arm.get(row.repeat);
    // Duplicate copies do not add repetitions. Conflicting copies (including an
    // invalid copy of an otherwise valid result) veto that repeat in both arms.
    if (!eligible || existing === null || existing && (existing.run_id !== row.run_id || existing.reviewer !== row.reviewer)) {
      arm.set(row.repeat, null);
    } else if (!existing) arm.set(row.repeat, row);
  }
  for (const [id, cohort] of reviewerGroups) {
    const { row, n } = cohort;
    const repeats = [...cohort.baseline.keys()].filter((repeat) => cohort.baseline.get(repeat) && cohort.candidate.get(repeat)).sort((a, b) => a - b);
    if (!repeats.length) continue;
    const paired = repeats.length, draft = row.labels_draft > 0;
    const used = repeats.flatMap((repeat) => [cohort.baseline.get(repeat), cohort.candidate.get(repeat)]);
    const dates = used.map((run) => stamp(run.created_at));
    // Actual completion evidence only; incomplete timestamps do not gain a date
    // from the current clock, report filename, or the comparison's identifier.
    const completed = dates.every(Boolean) ? dates.reduce((a, b) => a.ms > b.ms ? a : b).at : null;
    const draftZH = draft ? `；含 ${row.labels_draft} 道草稿，仅开发回归，不作为正式准入` : "；准确率对照不代表完整 S-10 门槛通过";
    const draftEN = draft ? `; includes ${row.labels_draft} draft questions, development regression only, not acceptance evidence` : "; an accuracy comparison does not establish the full S-10 gate";
    groups.set(id, {
      id, axis: "sequence", suite: "task-loop", suite_label_zh: "Task Loop", suite_label_en: "Task Loop", split: row.split,
      metric: "verdict_accuracy", ...metricSpec("verdict_accuracy"), sample_count: n, repeat_count: paired,
      comparison_id: row.comparison_id, comparison_basis: "reviewer_same_questions_paired_repeats",
      eval_set_name: row.eval_set_name, eval_set_version: row.eval_set_version, selection_sha256: row.selection_sha256,
      cohort_id: row.cohort_id, protocol: row.protocol, model_family: `worker|${row.model}`, completed_at: completed,
      labels_confirmed: row.labels_confirmed, labels_draft: row.labels_draft,
      subject: `Reviewer · ${row.cohort_id} · ${row.model}`,
      subject_zh: `Reviewer · ${row.cohort_id} · ${row.model} · ${paired} 组配对重复${draftZH}`,
      subject_en: `Reviewer · ${row.cohort_id} · ${row.model} · ${paired} paired repeats${draftEN}`,
      cohort: `${n} · ${row.cohort_id} · r=${paired}`,
      cohort_zh: `${n} 道相同题目 · 各 ${paired} 次重复`, cohort_en: `${n} identical questions · ${paired} repeats per arm`,
      note_zh: `同模型、同协议、同题集和选题哈希；每点为该臂已完成配对重复的准确率中位数。仅纳入双方都有效的同次重复，${paired} 次重复仍为 ${n} 道题，不扩充独立样本数${draftZH}。`,
      note_en: `Same model, protocol, dataset and selection hashes. Each point is the median accuracy across completed valid paired repeats. ${paired} repeats still represent ${n} questions, not a larger independent sample${draftEN}.`,
      points: ["baseline", "candidate"].map((variant, order) => {
        const runs = repeats.map((repeat) => cohort[variant].get(repeat));
        const armZH = variant === "baseline" ? "基线版" : "候选版", armEN = variant === "baseline" ? "Baseline" : "Candidate";
        return { id: `${id}:${variant}:median`, order, at: null, value: median(runs.map((run) => run.reviewer)),
          label: `${armEN} · ${row.cohort_id} · median r=${paired}${draft ? " · draft only" : ""}`,
          label_zh: `${armZH} · ${paired} 次中位数${draft ? " · 草稿回归（非正式准入）" : ""}`,
          label_en: `${armEN} · median of ${paired} repeats${draft ? " · draft regression, not acceptance" : ""}`,
          selection_record: runs[0], sample_count: n, repeat_count: paired, repeats, run_ids: runs.map((run) => run.run_id),
          source: "report-artifact", href: runLink(row.comparison_id, true) };
      }),
    });
  }
  const tuning = list(reports.tuning).filter((r) => !referenceRun(r) && r.same_problems === true && r.valid !== false && positiveCount(r.selected) && finite(r.rate) && r.rate >= 0 && r.rate <= 1);
  const tuningSizes = new Set(tuning.map((r) => r.selected));
  for (const n of tuningSizes) {
    const rows = tuning.filter((r) => r.selected === n);
    const id = `comparison:swebench:tuning:${n}`;
    groups.set(id, { id, axis: "sequence", suite: "swebench", suite_label_zh: "SWE-bench", suite_label_en: "SWE-bench", split: "verified",
      metric: "resolved_rate", ...metricSpec("resolved_rate"), subject: "Agent Runtime", sample_count: n,
      comparison_basis: "same_problems", cohort: `${n} · 同批样本`, cohort_zh: `${n} 道相同题目`, cohort_en: `${n} identical problems`,
      note_zh: "按报告中的基线与调优阶段顺序展示。", note_en: "Ordered by baseline and tuning stages in the report.",
      points: rows.map((r, order) => ({ selection_record: r, id: r.run_id, order, at: null, value: r.rate, label: r.short_en || r.run_id,
        label_zh: r.short_zh || r.short_en || r.run_id, label_en: r.short_en || r.run_id,
        sample_count: n, source: "archive", href: `/results?view=archive&q=${encodeURIComponent(r.run_id)}#tuning` })) });
  }
  const experiments = [...list(reports.experiments), ...(includeFixed ? FIXED_EXPERIMENTS.filter((fixed) => !list(reports.experiments).some((row) => row.id === fixed.id)) : [])];
  for (const r of experiments) {
    // Historical strategies share a question cohort, but their call protocols
    // differ. Plot confirmed correct / planned separately from paired accuracy.
    if (r.kind === "context-cohort-comparison") {
      const n = positiveCount(r.sample_count), variants = list(r.variants);
      if (!n || r.evaluation_type !== "ablation" || r.evaluation_scope !== "component" ||
          !hash(r.cohort_sha256) || !string(r.model_profile).trim() || !string(r.runtime_revision).trim() ||
          !string(r.comparison_id).trim() || variants.length < 2 ||
          new Set(variants.map((variant) => variant.id)).size !== variants.length) continue;
      const metrics = new Map(list(r.metrics).map((metric) => [metric.key, list(metric.values)]));
      const valueAt = (key, index) => metrics.get(key)?.[index];
      const shortLabels = {
        mask_only: ["原基线", "Baseline"], full: ["原完整策略", "Original full"],
        evidence: ["证据摘要", "Evidence summary"], uncompressed: ["未压缩", "Uncompressed"],
        source_retrieval: ["原文检索", "Source retrieval"],
      };
      const points = variants.map((variant, order) => {
        const planned = valueAt("planned_count", order), completed = valueAt("completed_count", order);
        const correct = valueAt("correct_count", order), wrong = valueAt("wrong_count", order);
        const valid = valueAt("valid_count", order), unscored = valueAt("unscored_count", order);
        const share = valueAt("confirmed_share", order);
        if (variant.status !== "completed" || ![planned, completed, correct, wrong, valid, unscored].every(count) ||
            planned !== n || completed !== planned || correct + wrong !== valid || valid + unscored !== completed ||
            !validValue(share, metricSpec("confirmed_share")) || Math.abs(share - correct / planned) > 1e-10) return null;
        const labelZH = variant.label_zh || variant.id, labelEN = variant.label_en || variant.id;
        return { selection_record: r, selection_info: r.selection_info, batch_id: r.batch_id, id: `${r.id}:${variant.id}`, variant: variant.id, order, at: null, value: share,
          label: labelEN, label_zh: labelZH, label_en: labelEN,
          short_label_zh: shortLabels[variant.id]?.[0] || labelZH,
          short_label_en: shortLabels[variant.id]?.[1] || labelEN,
          sample_count: planned, correct_count: correct, wrong_count: wrong, valid_count: valid, unscored_count: unscored,
          reported_delta: valueAt("correct_delta", order), source: "experiment",
          href: `/results?type=ablation#experiment-${encodeURIComponent(r.id)}` };
      });
      // A missing arm leaves a gap in the comparison, not an inferred zero or a
      // line joining the remaining arms as if the missing measurement existed.
      if (points.some((point) => point === null)) continue;
      const baseline = points.find((point) => point.variant === "mask_only");
      if (!baseline) continue;
      for (const point of points) {
        const delta = point.correct_count - baseline.correct_count;
        if (finite(point.reported_delta) && point.reported_delta === delta) {
          point.correct_delta = delta;
          point.delta_points = (point.value - baseline.value) * 100;
        }
        delete point.reported_delta;
      }
      const id = stableID(["comparison", r.id, "confirmed_share"]);
      groups.set(id, { id, axis: "sequence", batch_id: r.batch_id, suite: "longbench-context",
        suite_label_zh: SUITES["longbench-context"][0], suite_label_en: SUITES["longbench-context"][1], split: "development",
        metric: "confirmed_share", ...metricSpec("confirmed_share"), sample_count: n,
        subject: r.title_en || r.id, subject_zh: r.title_zh || r.id, subject_en: r.title_en || r.id,
        comparison_id: r.comparison_id, comparison_basis: "historical_shared_cohort", cohort_sha256: r.cohort_sha256,
        model_profile: r.model_profile, model_family: `agent-runtime|${r.model_profile}`, runtime_revision: r.runtime_revision,
        evaluation_type: "ablation", cohort: `${n} · shared question cohort`,
        cohort_zh: `${n} 道相同题目 · 原基线与上下文策略`, cohort_en: `${n} identical questions · baseline and context strategies`,
        note_zh: "横轴为方案；占比按已确认答对数除以计划题数计算，未判定不计为答错。各组调用协议有所不同，差值用于同题结果对照。",
        note_en: "The x-axis shows strategies. Confirmed correct answers use planned questions as the denominator; unscored calls are not incorrect answers. Protocols differ across strategies.",
        points });
      continue;
    }
    if (!["retrieval-comparison", "context-ablation"].includes(r.kind) || r.collection_complete !== true || r.quality_valid === false || !positiveCount(r.sample_count) || r.completed_count !== r.sample_count) continue;
    const contextAblation = r.kind === "context-ablation";
    if (contextAblation && (r.quality_valid !== true || r.evaluation_type !== "ablation" || r.evaluation_scope !== "component" ||
        !r.runtime_revision || !r.runtime_source_sha256 || !r.model_profile || !r.cohort_sha256 || !r.config_sha256 || !r.comparison_id)) continue;
    const suite = contextAblation ? "longbench-context" : "longmemeval";
    const labels = SUITES[suite];
    for (const metric of list(r.metrics)) {
      const spec = metricSpec(metric.key, metric);
      if (!spec || !validValue(metric.baseline, spec) || !validValue(metric.candidate, spec)) continue;
      const id = stableID(["comparison", r.id, metric.key]);
      groups.set(id, { id, axis: "sequence", batch_id: r.batch_id, suite, suite_label_zh: labels[0], suite_label_en: labels[1], split: "paired",
        metric: metric.key, ...spec, subject: r.title_en || r.id, subject_zh: r.title_zh || r.id, sample_count: r.sample_count,
        completed_at: stamp(r.completed_at)?.at || null, comparison_basis: "paired_arms", cohort: `${r.sample_count} · ${r.baseline_label} / ${r.candidate_label}`,
        cohort_zh: `${r.sample_count} 题 · ${r.baseline_label} / ${r.candidate_label}`, cohort_en: `${r.sample_count} cases · ${r.baseline_label} / ${r.candidate_label}`,
        evaluation_type: "ablation", runtime_revision: r.runtime_revision || null, model_profile: r.model_profile || null,
        note_zh: contextAblation ? `同一 Runtime 版本 ${r.runtime_revision}、模型 ${r.model_profile} 与上下文预算的组件消融；不与纯模型 LongBench 分数混排。` : "同一实验内的方案对照，横轴为方案而非日期。",
        note_en: contextAblation ? `Component ablation with Runtime revision ${r.runtime_revision}, model ${r.model_profile} and fixed context budget; separate from pure-model LongBench scores.` : "Paired configurations within one experiment; the x-axis is not time.",
        points: [["baseline", r.baseline_label || "Baseline"], ["candidate", r.candidate_label || "Candidate"]].map(([arm, label], order) => ({
          selection_record: r, selection_info: r.selection_info, batch_id: r.batch_id, id: `${r.id}:${arm}`, order, at: null, value: metric[arm], label, label_zh: label, label_en: label,
          sample_count: r.sample_count, source: "experiment", href: `/results?type=ablation#experiment-${encodeURIComponent(r.id)}`,
        })) });
    }
  }
  return finishGroups(groups, false);
}


const PRIMARY = {
  "terminal-bench": "resolved_rate", swebench: "resolved_rate", "task-loop": "success_rate",
  deepeval: "pass_rate", ragas: "mrr", context: "full.probe_pass_rate", longmemeval: "recall_any@10", "longbench-context": "confirmed_share",
};
const CATEGORY_ORDER = ["longbench-context", "longmemeval", "terminal-bench", "task-loop", "swebench", "deepeval", "ragas", "context"];

/** One card per category; comparable cohorts remain separate lines in that card. */
export function buildCategoryCharts(live, archive, benchmarks) {
  const timelines = buildTrendGroups(live, archive, benchmarks, { includeSingle: true });
  const dated = new Set(timelines.flatMap((group) => group.points.map((point) => `${group.suite}\0${group.metric}\0${point.id}`)));
  // The dated artifact feed already contains these tuning runs. Do not display
  // their older archive projection again as a separate comparison series.
  const comparisons = buildComparisonGroups(live, archive, Boolean(archive?.source === "archive" || archive?.experiment_source)).filter((group) =>
    !group.points.every((point) => dated.has(`${group.suite}\0${group.metric}\0${point.id}`)));
  const categories = new Map();
  for (const group of [...timelines, ...comparisons]) {
    if (!categories.has(group.suite)) categories.set(group.suite, { id: group.suite, suite: group.suite, metrics: [] });
    const category = categories.get(group.suite);
    let metric = category.metrics.find((item) => item.id === group.metric && item.unit === group.unit);
    if (!metric) {
      metric = { id: group.metric, metric: group.metric, label_zh: group.label_zh, label_en: group.label_en,
        unit: group.unit, direction: group.direction, views: [] };
      category.metrics.push(metric);
    }
    let view = metric.views.find((item) => item.axis === group.axis);
    if (!view) { view = { axis: group.axis, x_axis: group.x_axis, series: [] }; metric.views.push(view); }
    view.series.push(group);
  }
  return [...categories.values()].map((category) => {
    category.metrics = category.metrics.filter((metric) => metric.views.some((view) => view.series.reduce((count, series) => count + series.points.length, 0) >= 1));
    category.metrics.sort((a, b) => (a.id === PRIMARY[category.suite] ? -1 : b.id === PRIMARY[category.suite] ? 1 : a.id.localeCompare(b.id)));
    for (const metric of category.metrics) {
      for (const view of metric.views) view.series.sort((a, b) => (b.sample_count || 0) - (a.sample_count || 0) || a.id.localeCompare(b.id));
      const newest = (view) => Math.max(0, ...view.series.map((series) => Date.parse(series.completed_at || series.points.at(-1)?.at) || 0));
      metric.views.sort((a, b) => newest(b) - newest(a) || (a.axis === "time" ? -1 : 1));
      metric.default_axis = metric.views[0].axis;
    }
    category.default_metric = category.metrics[0]?.id;
    return category;
  }).filter((category) => category.metrics.length).sort((a, b) => {
    const ai = CATEGORY_ORDER.indexOf(a.suite), bi = CATEGORY_ORDER.indexOf(b.suite);
    return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi) || a.suite.localeCompare(b.suite);
  });
}
