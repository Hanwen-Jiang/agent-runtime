// Evaluation identity is separate from model identity. Missing Runtime versions
// remain missing; never invent a revision from a model label or a run ID.
export const EVALUATION_TYPES = {
  model: ["模型基准", "Model benchmark"],
  runtime: ["Runtime 版本评测", "Runtime evaluation"],
  ablation: ["消融 / 配对对照", "Ablation / paired comparison"],
  unknown: ["类型未记录", "Type unrecorded"],
};
const known = (v) => Object.hasOwn(EVALUATION_TYPES, v);
export function evaluationType(row = {}) {
  if (known(row.evaluation_type)) return row.evaluation_type;
  if (row.comparison_id && row.variant) return "ablation";
  if (["paired-policy", "retrieval-comparison", "context-ablation"].includes(row.kind) || row.suite === "context") return "ablation";
  if (row.suite === "capability") return "model";
  if (["swebench", "terminal-bench", "task-loop"].includes(row.suite) ||
      ["worker", "worker-planner"].includes(row.subject)) return "runtime";
  if (["deepeval", "ragas"].includes(row.suite) && /^(agent-runtime|agentruntime_memory|memory-service)/.test(row.subject || row.model || "")) return "runtime";
  return "unknown";
}
export function evaluationScope(row = {}) {
  if (["component", "end_to_end"].includes(row.evaluation_scope)) return row.evaluation_scope;
  if (["swebench", "terminal-bench"].includes(row.suite)) return "end_to_end";
  if (["capability", "task-loop", "context"].includes(row.suite) ||
      ["worker", "worker-planner"].includes(row.subject) || row.kind === "retrieval-comparison") return "component";
  return "unknown";
}
export function runtimeRevision(row = {}) {
  const revision = row.runtime_revision || row.code_revision;
  return typeof revision === "string" && /^[a-f0-9]{7,40}$/.test(revision) ? revision : null;
}
export function matchesEvaluationType(row, type) {
  return !type || evaluationType(row) === type;
}
export function filterEvaluationArchive(archive, type) {
  if (!type || !archive) return archive;
  const reports = archive.reports || {};
  return { ...archive, reports: { ...reports,
    runs: (reports.runs || []).filter((row) => matchesEvaluationType({ suite: "task-loop", ...row }, type)),
    contexts: type === "ablation" ? reports.contexts || [] : [],
    benchmarks: type === "runtime" ? reports.benchmarks || [] : [],
    tuning: type === "runtime" ? reports.tuning || [] : [],
    experiments: (reports.experiments || []).filter((row) => matchesEvaluationType(row, type)),
  } };
}
