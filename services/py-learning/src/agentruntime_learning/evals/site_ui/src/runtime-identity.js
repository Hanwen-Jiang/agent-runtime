import identities from "./runtime-identities.json" with {type:"json"};
import displayGroups from "./runtime-display-groups.json" with {type:"json"};
import archiveGroups from "./runtime-archive-groups.json" with {type:"json"};
import { evaluationType, runtimeRevision } from "./evaluation-info.js";

const mediumIds = new Set(displayGroups.medium);
// These two legacy telemetry names were reused instead of immutable run IDs.
const datedRuns = {deepeval:"2026-11-04T09:59:21Z","deepeval-b":"2026-11-04T10:28:17Z"};
function sameObservation(row,id) {
  return !Object.hasOwn(datedRuns,id) || Date.parse(row.started_at || row.created_at) === Date.parse(datedRuns[id]);
}
const mediumArchive = new Set(archiveGroups.map(key => JSON.stringify(key)));

const routes = {
  automatic: ["内核自动路由", "kernel routing"],
  fixed: ["固定模型", "fixed model"],
  mixed: ["混合路由", "mixed routing"],
  unknown: ["路由方式未记录", "routing unrecorded"],
};
const efforts = {
  low: ["思考等级低", "low reasoning effort"],
  medium: ["思考等级中", "medium reasoning effort"],
  high: ["思考等级高", "high reasoning effort"],
  mixed: ["思考等级混合", "mixed reasoning effort"],
};

export function isReferenceRun(row) {
  if (row.reference === true || row.is_reference === true) return true;
  return [row.model, row.subject, row.run_id].some(value =>
    typeof value === "string" && /^(?:gold[-_]?smoke|oracle)(?:[-_\d]|$)/i.test(value));
}

// Use recorded version fields and per-run evidence only. A run name can
// contain a model or an intended configuration without proving what ran.
function archiveKey(row) {
  return JSON.stringify([row.suite, row.group, row.created_at || row.date, row.subject || "", row.split || ""]);
}

export function runtimeEvidence(row) {
  if (!row || row.suite === "capability" || evaluationType(row) === "model" || isReferenceRun(row)) return null;
  const id = row.run_id || row.id;
  const record = sameObservation(row,id) && Object.hasOwn(identities, id) ? identities[id] : null;
  const runtimeSubject = [row.subject, row.model].some(value => typeof value === "string" &&
    /^(?:agent[-_ ]?runtime(?:$|[-_ ])|runtime-(?:[abc]-|rev[0-9])|worker(?:$|-planner$|-context$)|memory-service$)/i.test(value));
  if (!record && !mediumIds.has(id) && !mediumArchive.has(archiveKey(row)) && !runtimeSubject &&
      row.source !== "terminalbench_artifacts" && !(runtimeRevision(row) && evaluationType(row) !== "unknown")) return null;
  const tree = typeof row.runtime_tree === "string" && /^[a-f0-9]{40}$/i.test(row.runtime_tree) ? row.runtime_tree : null;
  const revision = runtimeRevision(row) || tree;
  return {...record, version: record?.version || revision?.slice(0,8) || null};
}

export function displayGroup(row) {
  if (!row || !runtimeEvidence(row)) return null;
  const id = row.run_id || row.id;
  if (id) return mediumIds.has(id) && sameObservation(row,id) ? "medium" : null;
  return mediumArchive.has(archiveKey(row)) ? "medium" : null;
}

export function runtimeLabel(row, t) {
  const evidence = runtimeEvidence(row);
  if (!evidence) return null;
  return evidence.version ? `agentruntime-${evidence.version}` : t("agentruntime（版本未记录）","agentruntime (version unrecorded)");
}

export function routingLabel(evidence, t) {
  return t(...(routes[evidence?.routing] || routes.unknown));
}

export function reasoningLabel(evidence, t) {
  return efforts[evidence?.effort] ? t(...efforts[evidence.effort]) : null;
}

export function subjectLabel(row, t, fallback) {
  return runtimeLabel(row,t) || fallback || row?.model || row?.subject || row?.run_id || "—";
}

export function matchesRunSearch(row, search) {
  const query = search?.trim().toLowerCase();
  if (!query) return true;
  const evidence = runtimeEvidence(row);
  const fields = [row.run_id, row.model, row.subject, row.suite, row.group, row.comparison_id,
    row.label_zh, row.label_en, row.short_zh, row.short_en, row.runtime_revision, row.runtime_tree];
  if (evidence) fields.push(runtimeLabel(row, (zh) => zh), runtimeLabel(row, (_, en) => en),
    evidence.version, evidence.routing, evidence.effort, displayGroup(row),
    ...(routes[evidence.routing] || routes.unknown), ...(efforts[evidence.effort] || []), ...(evidence.models || []));
  const tokens = fields.filter(value=>typeof value === "string").join(" ").toLowerCase();
  return query.split(/\s+/).every(word => tokens.includes(word));
}
