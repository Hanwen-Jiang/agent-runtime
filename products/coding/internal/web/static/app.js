// Coding Agent 网页（原生 JS，无构建步骤）。版式参照 Claude：纸面底色、衬线体的回答、卡片式输入框（docs/plan/coding-agent-mvp.md 第 7、8 节）。
// 工作区（基线仓库）下有很多会话；每个会话一段对话加一份自己的工作副本，可以同时跑。
// 左栏：上面是新建会话、搜索、工作区、进行中，下面按工作区分组列出会话。中间：会话（每个任务一轮：你的消息、工作过程、回答，
// 输入框随阶段变化）、工作区一览或进行中的任务。右栏是当前会话的环境、上下文、输出、改动与后台任务；diff、文件与提交历史在宽抽屉里打开。
// DOM 一律用 textContent 构造，不拼 HTML。界面文字一律经 i18n.js 的 t() 取（中文与英文）。

import { LANGS, lang, locale, setLang, t } from "./i18n.js";

const EVENT_TYPES = [
  "task_accepted", "round_started", "stage_started", "node_started", "node_finished",
  "thinking_delta", "tool_status", "token_delta", "human_input_required", "verdict", "task_finished",
];
const STATUS = {
  TASK_STATUS_QUEUED: [t("status.queued"), "run"],
  TASK_STATUS_RUNNING: [t("status.running"), "run"],
  TASK_STATUS_DONE: [t("status.done"), "ok"],
  TASK_STATUS_ABORTED: [t("status.aborted"), "err"],
  TASK_STATUS_CANCELLED: [t("status.cancelled"), "warn"],
  TASK_STATUS_FAILED: [t("status.failed"), "err"],
};
const TERMINAL = new Set(["TASK_STATUS_DONE", "TASK_STATUS_ABORTED", "TASK_STATUS_CANCELLED", "TASK_STATUS_FAILED"]);
const QUIET = new Set(["heartbeat", "token_delta", "thinking_delta"]);
const REPLAY_LIMIT = 12;

// 沙箱等级就是 Task 的 effect 上限；模型倾向交给 Planner 选 Tier（服务端见 runsettings.go）。
const SANDBOX = [
  { id: "read", icon: "eye", label: t("sandbox.read"), desc: t("sandbox.readDesc") },
  { id: "write_reversible", icon: "edit", label: t("sandbox.write"), desc: t("sandbox.writeDesc") },
  { id: "full", icon: "globe", label: t("sandbox.full"), desc: t("sandbox.fullDesc"), disabled: true },
];
const MODELS = [
  { id: "economy", icon: "leaf", label: t("model.economy"), desc: t("model.economyDesc") },
  { id: "balanced", icon: "gauge", label: t("model.balanced"), desc: t("model.balancedDesc") },
  { id: "best", icon: "zap", label: t("model.best"), desc: t("model.bestDesc") },
];
const VERDICTS = Object.fromEntries(Object.entries({
  done: "ok", continue: "info", repair: "warn", replan: "warn", rollback: "warn", reroute: "warn", escalate: "warn", abort: "err",
}).map(([k, kind]) => [k, [t("verdict." + k), kind]]));
const SECTIONS = ["env", "context", "output", "changes", "background"];

const ls = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* 无痕窗口等：只在本次有效 */ } },
};

function setting(key, allowed, fallback) {
  const v = ls.get("cw_" + key);
  return allowed.includes(v) ? v : fallback;
}

const state = {
  config: { dev_login: false, github_login: false, sandbox_control: false },
  me: null,
  projects: [], // 工作区
  sessions: [], // 全部会话，最近活动的在前
  samples: [],
  project: null, // 当前工作区
  session: null, // 当前会话；为空时是草稿（第一条消息发出时才开会话）
  view: "session", // session、workspaces 或 activity
  groupsClosed: new Set((ls.get("cw_groups_closed") || "").split(",").filter(Boolean)),
  groupsAll: new Set(), // 展开了全部会话的工作区
  tasks: [], // 旧 → 新
  runs: new Map(), // taskId → { events, done, es, promise }
  detail: new Map(), // taskId → { state, usage, cost, roundSeq }
  open: new Set(), // 展开的工作过程
  closed: new Set(), // 用户手动收起的
  replied: new Set(),
  changes: null,
  env: null,
  active: [],
  filesPath: "",
  sending: false,
  drawer: null,
  settings: {
    sandbox: setting("sandbox", ["read", "write_reversible"], "write_reversible"),
    model: setting("model", ["economy", "balanced", "best"], "balanced"),
  },
  statusOpen: ls.get("cw_status") ? ls.get("cw_status") === "1" : window.innerWidth > 1180,
  sideCollapsed: ls.get("cw_side") === "0",
  sideOpen: false,
  sections: new Set((ls.get("cw_sections") ?? SECTIONS.join(",")).split(",").filter((s) => SECTIONS.includes(s))),
};

const ui = {}; // 常驻的 DOM 节点（重画时只换内容）

// 当前会话的接口前缀；草稿（还没开出会话）时为 null。
function sbase() {
  return state.project && state.session ? `/api/projects/${state.project.id}/sessions/${state.session.id}` : null;
}

function inSession() {
  return state.view === "session" && !!state.session;
}

function sessionURL(projectId, sessionId) {
  return `/api/projects/${projectId}/sessions/${sessionId}`;
}

// ---------------------------------------------------------------- DOM

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v === null || v === undefined) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of [children].flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const ICONS = {
  plus: "M12 5v14M5 12h14",
  send: "M12 19V5M5 12l7-7 7 7",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  branch: "M6 3v12M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 9a9 9 0 0 1-9 9",
  check: "M20 6 9 17l-5-5",
  x: "M18 6 6 18M6 6l12 12",
  alert: "M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
  chevron: "M9 18l6-6-6-6",
  chevDown: "M6 9l6 6 6-6",
  panelRight: "M3 5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM15 3v18",
  panelLeft: "M3 5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM9 3v18",
  file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6",
  fileText: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M8 13h8M8 17h5",
  trash: "M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6",
  download: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3",
  commit: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM3 12h6M15 12h6",
  external: "M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6",
  sparkles: "M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z",
  layers: "M12 2 2 7l10 5 10-5zM2 17l10 5 10-5M2 12l10 5 10-5",
  play: "M6 4l13 8-13 8z",
  shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  terminal: "M4 17l6-6-6-6M12 19h8",
  refresh: "M3 12a9 9 0 0 1 15-6.7L21 8M21 3v5h-5M21 12a9 9 0 0 1-15 6.7L3 16M3 21v-5h5",
  logout: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
  user: "M20 21a8 8 0 0 0-16 0M12 13a5 5 0 1 0 0-10 5 5 0 0 0 0 10z",
  link: "M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7",
  menu: "M3 6h18M3 12h18M3 18h18",
  stop: "M7 7h10v10H7z",
  clock: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2",
  code: "M16 18l6-6-6-6M8 6l-6 6 6 6",
  box: "M21 8 12 3 3 8v8l9 5 9-5zM3 8l9 5 9-5M12 13v8",
  copy: "M9 9h11v11H9zM5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1",
  history: "M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5M12 7v5l3 2",
  diff: "M12 3v14M5 10h14M5 21h14",
  eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
  edit: "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z",
  globe: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM2 12h20M12 2a15 15 0 0 1 0 20M12 2a15 15 0 0 0 0 20",
  gauge: "M12 14l4-4M3.3 17a9 9 0 1 1 17.4 0",
  leaf: "M11 20A7 7 0 0 1 4 13c0-6 7-9 16-9 0 9-3 16-9 16zM4 20l8-8",
  zap: "M13 2 3 14h9l-1 8 10-12h-9z",
  server: "M3 4h18v6H3zM3 14h18v6H3zM7 7h.01M7 17h.01",
  activity: "M22 12h-4l-3 9L9 3l-3 9H2",
  sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
  monitor: "M3 4h18v12H3zM8 20h8M12 16v4",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  queue: "M3 6h13M3 12h13M3 18h8M18 15v6M15 18h6",
  list: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3",
  compose: "M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7M18.5 2.5a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4z",
  grid: "M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z",
  chat: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",
};
const GITHUB_PATH = "M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z";

function icon(name, cls = "") {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  const github = name === "github";
  svg.setAttribute("viewBox", github ? "0 0 16 16" : "0 0 24 24");
  svg.setAttribute("class", `icon ${github ? "filled" : ""} ${cls}`);
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", github ? GITHUB_PATH : ICONS[name] || ICONS.box);
  svg.append(path);
  return svg;
}

// 标志：缺口朝右的环（Task Loop：规划、执行、复核）加一根光标，读作“C|”。Agent 工作时光标闪烁。
function logo(cls = "") {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", `icon logo bold ${cls}`);
  svg.setAttribute("aria-hidden", "true");
  for (const [d, part] of [["M16.6 6.9A8 8 0 1 0 16.6 17.1", "ring"], ["M20 9v6", "cursor"]]) {
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", d);
    path.setAttribute("class", part);
    svg.append(path);
  }
  return svg;
}

function mount(node) {
  const app = document.getElementById("app");
  app.className = "";
  app.replaceChildren(node);
}

function toast(text, isError = false) {
  const el = h("div", { class: "toast" + (isError ? " err" : "") }, text);
  document.body.append(el);
  setTimeout(() => el.remove(), isError ? 6000 : 2600);
}

function copy(text) {
  navigator.clipboard.writeText(text).then(() => toast(t("common.copied")), () => toast(t("common.copyFailed"), true));
}

function fmtTime(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleString(locale, { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function fmtClock(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function fmtDuration(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return t("dur.s", { s });
  const m = Math.floor(s / 60);
  return s % 60 ? t("dur.ms", { m, s: s % 60 }) : t("dur.m", { m });
}

function fmtTimer(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function fmtAgo(iso) {
  const ms = Date.now() - new Date(iso);
  if (isNaN(ms)) return "";
  if (ms < 60e3) return t("ago.now");
  if (ms < 3600e3) return t("ago.min", { n: Math.floor(ms / 60e3) });
  if (ms < 86400e3) return t("ago.hour", { n: Math.floor(ms / 3600e3) });
  return fmtTime(iso);
}

function fmtSize(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function fmtUSD(x) {
  if (!x) return "$0";
  return x < 0.01 ? "<$0.01" : `$${x.toFixed(2)}`;
}

function fmtTokens(n) {
  if (n < 1000) return String(n);
  if (n < 1e6) return `${(n / 1000).toFixed(n < 1e4 ? 1 : 0)}k`;
  return `${(n / 1e6).toFixed(1)}M`;
}

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function statusChip(status) {
  const [label, kind] = STATUS[status] || [status || t("status.unknown"), ""];
  return h("span", { class: `chip ${kind}` }, kind === "run" ? h("span", { class: "spinner" }) : null, label);
}

function verdictChip(verdict) {
  const key = String(verdict || "").replace(/^VERDICT_/, "").toLowerCase();
  const [label, kind] = VERDICTS[key] || [key || "—", ""];
  return h("span", { class: `chip ${kind}`, title: key }, label);
}

function initials(name) {
  return (name || "?").slice(0, 2).toUpperCase();
}

function pick(obj, ...keys) {
  for (const k of keys) if (obj && obj[k] !== undefined && obj[k] !== null && obj[k] !== "") return obj[k];
  return undefined;
}

function isTyping(el) {
  return el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
}

// ---------------------------------------------------------------- Markdown（只造 DOM）

function md(text) {
  const frag = document.createDocumentFragment();
  const lines = String(text || "").replace(/\r\n/g, "\n").split("\n");
  let i = 0;
  const para = [];
  const flush = () => {
    if (!para.length) return;
    const p = h("p");
    para.forEach((l, k) => { if (k) p.append(h("br")); inline(p, l); });
    frag.append(p);
    para.length = 0;
  };
  const isRow = (l) => /^\s*\|.*\|\s*$/.test(l);
  const isRule = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^\s*```\s*([\w+#.-]*)\s*$/);
    if (fence) {
      flush();
      const body = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) body.push(lines[i++]);
      i++;
      frag.append(codeBlock(body.join("\n"), fence[1]));
      continue;
    }
    if (isRow(line) && i + 1 < lines.length && isRule(lines[i + 1])) {
      flush();
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && isRow(lines[i])) rows.push(cells(lines[i++]));
      frag.append(h("div", { class: "md-table" }, h("table", {},
        h("thead", {}, h("tr", {}, head.map((c) => inline(h("th"), c)))),
        h("tbody", {}, rows.map((r) => h("tr", {}, head.map((_, k) => inline(h("td"), r[k] || ""))))),
      )));
      continue;
    }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flush();
      frag.append(h("hr"));
      i++;
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      flush();
      frag.append(inline(h(`h${heading[1].length}`), heading[2]));
      i++;
      continue;
    }
    if (/^\s*([-*•]|\d+[.)])\s+/.test(line)) {
      flush();
      const first = line.match(/^\s*(\d+)[.)]\s+/);
      const list = h(first ? "ol" : "ul", first && first[1] !== "1" ? { start: first[1] } : {});
      while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) {
        list.append(inline(h("li"), lines[i].replace(/^\s*([-*•]|\d+[.)])\s+/, "")));
        i++;
      }
      frag.append(list);
      continue;
    }
    if (/^>\s?/.test(line)) {
      flush();
      const quote = h("blockquote");
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        if (quote.childNodes.length) quote.append(h("br"));
        inline(quote, lines[i].replace(/^>\s?/, ""));
        i++;
      }
      frag.append(quote);
      continue;
    }
    if (!line.trim()) flush();
    else para.push(line);
    i++;
  }
  flush();
  return frag;
}

function cells(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

function codeBlock(code, lang) {
  return h("div", { class: "codeblock" },
    h("div", { class: "cb-head" }, h("span", { class: "grow" }, lang || t("code.lang")),
      h("button", { class: "btn ghost xs", onclick: () => copy(code) }, icon("copy", "xs"), t("common.copy"))),
    h("pre", {}, h("code", {}, code)),
  );
}

function inline(el, text) {
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\[[^\]]+\]\((https?:\/\/[^)\s]+)\))/g;
  let last = 0;
  let m;
  while ((m = re.exec(text))) {
    if (m.index > last) el.append(text.slice(last, m.index));
    if (m[1]) el.append(h("code", {}, m[1].slice(1, -1)));
    else if (m[2]) el.append(h("strong", {}, m[2].slice(2, -2)));
    else if (m[3]) el.append(h("a", { href: m[4], target: "_blank", rel: "noopener noreferrer" }, m[3].slice(1, m[3].indexOf("]"))));
    last = re.lastIndex;
  }
  if (last < text.length) el.append(text.slice(last));
  return el;
}

// ---------------------------------------------------------------- API

async function api(method, path, body) {
  const opts = { method, headers: { "X-Coding-Web": "1" }, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401 && path !== "/api/me") {
    state.me = null;
    renderLogin(t("api.expired"));
    throw new Error("unauthorized");
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) throw new Error((data && data.error) || t("api.failed", { status: res.status }));
  return data;
}

function report(ex) {
  if (ex && ex.message !== "unauthorized") toast(ex.message, true);
}

// ---------------------------------------------------------------- 登录方式与 Git 平台

function providerList() {
  return state.config.providers || [];
}

function providerInfo(id) {
  return providerList().find((p) => p.id === id) || { id, name: id || "Git", url: "", git: true };
}

function connection(id) {
  return ((state.me && state.me.connections) || []).find((c) => c.provider === id) || null;
}

function gitConnections() {
  return ((state.me && state.me.connections) || []).filter((c) => c.git);
}

// 各家的标志：GitHub、Gitee、GitLab、Gitea、Codeberg、Forgejo、Bitbucket 取自 Simple Icons（CC0，simple-icons@13），
// 用品牌色；Google 用它规定的四色 G。用在登录按钮、“账号与平台”、仓库与工作区旁边。
const BRANDS = {
  gitee: { color: "#C71D23", d: "M11.984 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.016 0zm6.09 5.333c.328 0 .593.266.592.593v1.482a.594.594 0 0 1-.593.592H9.777c-.982 0-1.778.796-1.778 1.778v5.63c0 .327.266.592.593.592h5.63c.982 0 1.778-.796 1.778-1.778v-.296a.593.593 0 0 0-.592-.593h-4.15a.592.592 0 0 1-.592-.592v-1.482a.593.593 0 0 1 .593-.592h6.815c.327 0 .593.265.593.592v3.408a4 4 0 0 1-4 4H5.926a.593.593 0 0 1-.593-.593V9.778a4.444 4.444 0 0 1 4.445-4.444h8.296Z" },
  gitlab: { color: "#FC6D26", d: "m23.6004 9.5927-.0337-.0862L20.3.9814a.851.851 0 0 0-.3362-.405.8748.8748 0 0 0-.9997.0539.8748.8748 0 0 0-.29.4399l-2.2055 6.748H7.5375l-2.2057-6.748a.8573.8573 0 0 0-.29-.4412.8748.8748 0 0 0-.9997-.0537.8585.8585 0 0 0-.3362.4049L.4332 9.5015l-.0325.0862a6.0657 6.0657 0 0 0 2.0119 7.0105l.0113.0087.03.0213 4.976 3.7264 2.462 1.8633 1.4995 1.1321a1.0085 1.0085 0 0 0 1.2197 0l1.4995-1.1321 2.4619-1.8633 5.006-3.7489.0125-.01a6.0682 6.0682 0 0 0 2.0094-7.003z" },
  gitea: { color: "#609926", d: "M4.209 4.603c-.247 0-.525.02-.84.088-.333.07-1.28.283-2.054 1.027C-.403 7.25.035 9.685.089 10.052c.065.446.263 1.687 1.21 2.768 1.749 2.141 5.513 2.092 5.513 2.092s.462 1.103 1.168 2.119c.955 1.263 1.936 2.248 2.89 2.367 2.406 0 7.212-.004 7.212-.004s.458.004 1.08-.394c.535-.324 1.013-.893 1.013-.893s.492-.527 1.18-1.73c.21-.37.385-.729.538-1.068 0 0 2.107-4.471 2.107-8.823-.042-1.318-.367-1.55-.443-1.627-.156-.156-.366-.153-.366-.153s-4.475.252-6.792.306c-.508.011-1.012.023-1.512.027v4.474l-.634-.301c0-1.39-.004-4.17-.004-4.17-1.107.016-3.405-.084-3.405-.084s-5.399-.27-5.987-.324c-.187-.011-.401-.032-.648-.032zm.354 1.832h.111s.271 2.269.6 3.597C5.549 11.147 6.22 13 6.22 13s-.996-.119-1.641-.348c-.99-.324-1.409-.714-1.409-.714s-.73-.511-1.096-1.52C1.444 8.73 2.021 7.7 2.021 7.7s.32-.859 1.47-1.145c.395-.106.863-.12 1.072-.12zm8.33 2.554c.26.003.509.127.509.127l.868.422-.529 1.075a.686.686 0 0 0-.614.359.685.685 0 0 0 .072.756l-.939 1.924a.69.69 0 0 0-.66.527.687.687 0 0 0 .347.763.686.686 0 0 0 .867-.206.688.688 0 0 0-.069-.882l.916-1.874a.667.667 0 0 0 .237-.02.657.657 0 0 0 .271-.137 8.826 8.826 0 0 1 1.016.512.761.761 0 0 1 .286.282c.073.21-.073.569-.073.569-.087.29-.702 1.55-.702 1.55a.692.692 0 0 0-.676.477.681.681 0 1 0 1.157-.252c.073-.141.141-.282.214-.431.19-.397.515-1.16.515-1.16.035-.066.218-.394.103-.814-.095-.435-.48-.638-.48-.638-.467-.301-1.116-.58-1.116-.58s0-.156-.042-.27a.688.688 0 0 0-.148-.241l.516-1.062 2.89 1.401s.48.218.583.619c.073.282-.019.534-.069.657-.24.587-2.1 4.317-2.1 4.317s-.232.554-.748.588a1.065 1.065 0 0 1-.393-.045l-.202-.08-4.31-2.1s-.417-.218-.49-.596c-.083-.31.104-.691.104-.691l2.073-4.272s.183-.37.466-.497a.855.855 0 0 1 .35-.077z" },
  codeberg: { color: "#2185D0", d: "M11.955.49A12 12 0 0 0 0 12.49a12 12 0 0 0 1.832 6.373L11.838 5.928a.187.14 0 0 1 .324 0l10.006 12.935A12 12 0 0 0 24 12.49a12 12 0 0 0-12-12 12 12 0 0 0-.045 0zm.375 6.467l4.416 16.553a12 12 0 0 0 5.137-4.213z" },
  forgejo: { color: "#FB923C", d: "M16.7773 0c1.6018 0 2.9004 1.2986 2.9004 2.9005s-1.2986 2.9004-2.9004 2.9004c-1.0854 0-2.0315-.596-2.5288-1.4787H12.91c-2.3322 0-4.2272 1.8718-4.2649 4.195l-.0007 2.1175a7.0759 7.0759 0 0 1 4.148-1.4205l.1176-.001 1.3385.0002c.4973-.8827 1.4434-1.4788 2.5288-1.4788 1.6018 0 2.9004 1.2986 2.9004 2.9005s-1.2986 2.9004-2.9004 2.9004c-1.0854 0-2.0315-.596-2.5288-1.4787H12.91c-2.3322 0-4.2272 1.8718-4.2649 4.195l-.0007 2.319c.8827.4973 1.4788 1.4434 1.4788 2.5287 0 1.602-1.2986 2.9005-2.9005 2.9005-1.6018 0-2.9004-1.2986-2.9004-2.9005 0-1.0853.596-2.0314 1.4788-2.5287l-.0002-9.9831c0-3.887 3.1195-7.0453 6.9915-7.108l.1176-.001h1.3385C14.7458.5962 15.692 0 16.7773 0ZM7.2227 19.9052c-.6596 0-1.1943.5347-1.1943 1.1943s.5347 1.1943 1.1943 1.1943 1.1944-.5347 1.1944-1.1943-.5348-1.1943-1.1944-1.1943Zm9.5546-10.4644c-.6596 0-1.1944.5347-1.1944 1.1943s.5348 1.1943 1.1944 1.1943c.6596 0 1.1943-.5347 1.1943-1.1943s-.5347-1.1943-1.1943-1.1943Zm0-7.7346c-.6596 0-1.1944.5347-1.1944 1.1943s.5348 1.1943 1.1944 1.1943c.6596 0 1.1943-.5347 1.1943-1.1943s-.5347-1.1943-1.1943-1.1943Z" },
  bitbucket: { color: "#2684FF", d: "M.778 1.213a.768.768 0 00-.768.892l3.263 19.81c.084.5.515.868 1.022.873H19.95a.772.772 0 00.77-.646l3.27-20.03a.768.768 0 00-.768-.891zM14.52 15.53H9.522L8.17 8.466h7.561z" },
};
const GOOGLE_G = [
  ["#EA4335", "M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"],
  ["#4285F4", "M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"],
  ["#FBBC05", "M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"],
  ["#34A853", "M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"],
];

function brandSVG(viewBox, paths, size, title) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", viewBox);
  svg.setAttribute("class", `icon brand ${size}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", title);
  for (const [fill, d] of paths) {
    const path = document.createElementNS(ns, "path");
    path.setAttribute("fill", fill);
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

function badge(id, size = "sm") {
  if (id === "github") return icon("github", size);
  const info = providerInfo(id);
  if (id === "google") return brandSVG("0 0 48 48", GOOGLE_G, size, info.name);
  // Gitea 这一家指向 Codeberg 或 Forgejo 时用它们自己的标志。
  const key = id === "gitea" && /codeberg/i.test(info.name) ? "codeberg" : id === "gitea" && /forgejo/i.test(info.name) ? "forgejo" : id;
  const b = BRANDS[key];
  if (b) return brandSVG("0 0 24 24", [[b.color, b.d]], size, info.name);
  const el = h("span", { class: `pv-badge ${size}`, title: info.name }, String(id || "?").slice(0, 2));
  el.style.background = "#76736b";
  return el;
}

// 工作区的标记：Git 平台上的仓库用平台标记，示例用盒子，其余用文件夹。
function projectMark(p, size = "sm") {
  if (p.git_provider) return badge(p.git_provider, size);
  return icon(p.source && p.source.startsWith("sample:") ? "box" : "folder", size);
}

function repoURL(p) {
  const base = (providerInfo(p.git_provider).url || "").replace(/\/$/, "");
  return base && p.repo ? `${base}/${p.repo}` : "";
}

function requestName(p) {
  return providerInfo(p.git_provider).request_name || "PR";
}

// 地址里 #…?linked=gitlab 或 ?error=… 是 OAuth 回调带回来的结果。
function hashParams() {
  return new URLSearchParams(location.hash.split("?")[1] || "");
}

// openAccount 是“账号与平台”：已连接的登录方式、连接别家、断开。
async function openAccount() {
  closePop();
  if (document.querySelector("dialog.account")) return;
  const body = h("div", { class: "d-body" }, h("div", { class: "row muted" }, h("span", { class: "spinner" }), t("common.loading")));
  const dialog = h("dialog", { class: "account" },
    h("div", { class: "d-head" }, h("h2", {}, t("account.title")), h("div", { class: "muted small" }, t("account.who", { tenant: state.me.tenant, user: state.me.user }))),
    body,
    h("div", { class: "d-foot" }, h("button", { class: "btn ghost", onclick: () => dialog.close() }, t("common.close"))),
  );
  const draw = (acct) => {
    const conns = acct.connections || [];
    const rows = providerList().filter((p) => p.configured || state.config.dev_login || conns.some((c) => c.provider === p.id)).map((p) => {
      const c = conns.find((x) => x.provider === p.id);
      const who = c && c.name && c.name !== c.login ? t("account.nameParen", { login: c.login, name: c.name }) : c && c.login;
      const status = c ? t("account.connected", { who }) : p.configured ? t("account.notConnected") : t("account.notConfigured");
      return h("div", { class: "conn-row" + (c ? " on" : "") },
        badge(p.id, "md"),
        h("div", { class: "grow" },
          h("div", { class: "t" }, p.name, p.git ? null : h("span", { class: "chip" }, t("account.loginOnly"))),
          h("div", { class: "m" }, status)),
        c ? h("button", { class: "btn sm", onclick: () => disconnect(p, draw) }, t("account.disconnect"))
          : p.configured ? h("a", { class: "btn sm primary", href: `/api/auth/${p.id}/login?link=1` }, t("account.connect")) : null);
    });
    body.replaceChildren(h("div", { class: "conn-list" }, rows));
  };
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  try { draw(await api("GET", "/api/account")); } catch (ex) { report(ex); }
}

async function disconnect(p, draw) {
  if (!confirm(t("account.disconnectConfirm", { name: p.name }))) return;
  try {
    const r = await api("POST", `/api/account/connections/${p.id}/disconnect`);
    state.me = await api("GET", "/api/me");
    draw(r);
    renderSide();
    toast(t("account.disconnected", { name: p.name }));
  } catch (ex) { report(ex); }
}

// ---------------------------------------------------------------- 外观

function applyTheme() {
  const t = ls.get("cw_theme");
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
}

// ---------------------------------------------------------------- 弹出菜单

let pop = null;

function openPop(anchor, build, opts = {}) {
  if (pop && pop.anchor === anchor) return closePop();
  closePop();
  const el = h("div", { class: "pop", role: "menu" });
  append(el, build(closePop));
  const r = anchor.getBoundingClientRect();
  if (opts.matchWidth) el.style.width = `${Math.max(r.width, 220)}px`;
  document.body.append(el);
  const left = Math.min(Math.max(8, r.left), window.innerWidth - el.offsetWidth - 8);
  let top = r.top - el.offsetHeight - 6;
  if (top < 8) top = Math.min(r.bottom + 6, window.innerHeight - el.offsetHeight - 8);
  el.style.left = `${left}px`;
  el.style.top = `${top}px`;
  anchor.classList.add("open");
  pop = { el, anchor };
}

function closePop() {
  if (!pop) return;
  pop.el.remove();
  pop.anchor.classList.remove("open");
  pop = null;
  if (ui.barDirty) updateComposer(true);
}

function popItem(ic, title, desc, selected, onclick, disabled = false) {
  return h("button", { class: "pop-item", disabled, onclick },
    icon(ic, "sm"), h("div", { class: "grow" }, h("div", { class: "t" }, title), desc ? h("div", { class: "d" }, desc) : null),
    selected ? icon("check", "sm check") : null);
}

// ---------------------------------------------------------------- 登录

function renderLogin(message = "") {
  closeStreams();
  closeDrawer();
  closePop();
  const hashError = new URLSearchParams(location.hash.split("?")[1] || "").get("error");
  const err = h("div", { class: "err-text" }, message || hashError || "");
  const tenant = h("input", { class: "field", placeholder: t("login.tenantPh"), autocomplete: "organization", value: ls.get("cw_tenant") || "" });
  const user = h("input", { class: "field", placeholder: t("login.userPh"), autocomplete: "username", value: ls.get("cw_user") || "" });
  const submit = h("button", { class: "btn primary block", type: "submit" }, t("login.devSubmit"));
  const form = h("form", { class: "col", onsubmit: async (e) => {
    e.preventDefault();
    submit.disabled = true;
    err.textContent = "";
    try {
      const me = await api("POST", "/api/login", { tenant: tenant.value, user: user.value });
      ls.set("cw_tenant", me.tenant);
      ls.set("cw_user", me.user);
      state.me = await api("GET", "/api/me");
      await enterApp();
    } catch (ex) {
      if (ex.message !== "unauthorized") err.textContent = ex.message;
    } finally {
      submit.disabled = false;
    }
  } },
    h("div", {}, h("label", { class: "lbl" }, t("login.tenant")), tenant),
    h("div", {}, h("label", { class: "lbl" }, t("login.user")), user),
    submit,
  );
  // 第三方登录：GitHub 是主按钮，其余 Git 平台收在它下面的“其他 Git 平台”里，Google 单独一个。
  // 开发环境（开着开发登录）里没配置的也列出来、不可点，说明还能接哪些。
  const usable = (p) => p && (p.configured || state.config.dev_login);
  const oauthButton = (p, cls, label) => p.configured
    ? h("a", { class: `btn block ${cls}`, href: `/api/auth/${p.id}/login` }, badge(p.id), h("span", {}, label))
    : h("button", { class: `btn block ${cls}`, type: "button", disabled: true, title: t("login.notConfiguredTitle", { name: p.name, env: `CODING_${p.id.toUpperCase()}` }) },
      badge(p.id), h("span", {}, label), h("span", { class: "off" }, t("login.notConfigured")));
  const all = providerList();
  const gh = all.find((p) => p.id === "github");
  const google = all.find((p) => p.id === "google");
  const others = all.filter((p) => p.git && p.id !== "github" && usable(p));
  const otherList = h("div", { class: "other-providers" + (others.some((p) => p.configured) && !usable(gh) ? "" : " hidden") },
    others.map((p) => oauthButton(p, "provider-btn", p.name)));
  const otherToggle = others.length ? h("button", {
    class: "other-toggle", type: "button",
    onclick: () => { otherList.classList.toggle("hidden"); otherToggle.classList.toggle("open"); },
  }, h("span", {}, t("login.otherGit")), h("span", { class: "names" }, others.map((p) => p.name).join(" · ")), icon("chevDown", "xs caret")) : null;
  const thirdParty = [
    usable(gh) ? oauthButton(gh, "github-btn", t("login.with", { name: gh.name })) : null,
    otherToggle, otherList,
    usable(google) ? oauthButton(google, "google-btn", t("login.with", { name: google.name })) : null,
  ].filter(Boolean);
  const hasThirdParty = usable(gh) || others.length || usable(google);
  mount(h("div", { class: "login-page" },
    h("div", { class: "login-card" },
      h("div", { class: "hero" },
        h("div", { class: "mark big" }, logo("lg")),
        h("h1", {}, "Coding Agent"),
        h("p", {}, t("login.tagline")),
      ),
      h("div", { class: "login-box" },
        thirdParty,
        hasThirdParty && state.config.dev_login ? h("div", { class: "sep" }, t("login.or")) : null,
        state.config.dev_login ? form : null,
        !hasThirdParty && !state.config.dev_login ? h("div", { class: "note" }, t("login.none")) : null,
        err,
      ),
      langSwitch(),
    ),
  ));
}

// langSwitch 是登录页底部的语言切换：每种语言用它自己的名字。
function langSwitch() {
  return h("div", { class: "lang-switch", role: "group", "aria-label": t("menu.language") },
    icon("globe", "xs"),
    LANGS.map((l) => h("button", { class: l.id === lang ? "active" : "", lang: l.tag, "aria-pressed": String(l.id === lang), onclick: () => setLang(l.id) }, l.label)));
}

async function logout() {
  await api("POST", "/api/logout").catch(() => {});
  state.me = null;
  state.project = null;
  state.session = null;
  renderLogin();
}

// ---------------------------------------------------------------- 主界面与路由

async function enterApp() {
  [state.projects, state.samples, state.sessions] = await Promise.all([api("GET", "/api/projects"), api("GET", "/api/samples"), api("GET", "/api/sessions")]);
  loadActive();
  await route();
}

function findProject(id) {
  return state.projects.find((p) => p.id === id) || null;
}

function findSession(pid, sid) {
  return state.sessions.find((s) => s.project_id === pid && s.id === sid) || null;
}

// 地址：#/w/<工作区>/s/<会话>、#/w/<工作区>/new（新会话草稿）、#/workspaces、#/activity。
async function route() {
  const hash = location.hash;
  let m;
  if ((m = hash.match(/^#\/w\/([^/]+)\/s\/([^/?]+)/)) && findSession(m[1], m[2])) return openSession(m[1], m[2]);
  if ((m = hash.match(/^#\/w\/([^/?]+)\/new/)) && findProject(m[1])) return openDraft(m[1]);
  if (hash.startsWith("#/workspaces")) return go("workspaces");
  if (hash.startsWith("#/activity")) return go("activity");
  return landing();
}

// landing：最近的会话；没有会话时开最近工作区的草稿；什么都没有时是欢迎页。
function landing() {
  const latest = state.sessions[0];
  if (latest && findProject(latest.project_id)) return openSession(latest.project_id, latest.id);
  if (state.projects[0]) return openDraft(state.projects[0].id);
  resetSessionState();
  state.project = null;
  state.session = null;
  state.view = "session";
  setHash("");
  renderApp();
}

let routedHash = "";
function setHash(hash) {
  routedHash = hash;
  if (location.hash !== hash) location.hash = hash;
}

function resetSessionState() {
  closeStreams();
  closeDrawer();
  state.tasks = [];
  state.runs = new Map();
  state.detail = new Map();
  state.changes = null;
  state.env = null;
  state.filesPath = "";
}

async function openSession(pid, sid) {
  resetSessionState();
  state.view = "session";
  state.project = findProject(pid);
  state.session = findSession(pid, sid);
  state.sideOpen = false;
  setHash(`#/w/${pid}/s/${sid}`);
  if (state.session) {
    const base = sbase();
    try {
      const tasks = await api("GET", `${base}/tasks`);
      if (sbase() !== base) return;
      state.tasks = tasks.slice().reverse();
    } catch (ex) { report(ex); }
  }
  renderApp();
  if (state.session) {
    loadChanges();
    loadEnv();
    const focus = focusTask();
    if (focus) loadDetail(focus);
    replayTasks();
  }
}

// openDraft 打开工作区里的新会话草稿：第一条消息发出时才真正开会话（复制工作副本）。
function openDraft(pid) {
  resetSessionState();
  state.view = "session";
  state.project = findProject(pid);
  state.session = null;
  state.sideOpen = false;
  setHash(`#/w/${pid}/new`);
  renderApp();
  if (ui.input) ui.input.focus();
}

function go(view) {
  resetSessionState();
  state.view = view;
  state.session = null;
  state.sideOpen = false;
  setHash(`#/${view}`);
  renderApp();
}

// newSession 是左栏的“新建会话”：在当前工作区（没有时取最近活动的一个）开草稿；还没有工作区就先建一个。
function newSession() {
  const p = state.project || findProject((state.sessions[0] || {}).project_id) || state.projects[0];
  if (p) openDraft(p.id);
  else openNewProject();
}

function appClass() {
  return ["app",
    state.sideCollapsed && "side-collapsed",
    state.sideOpen && "side-open",
    !(state.statusOpen && inSession()) && "status-closed",
  ].filter(Boolean).join(" ");
}

function applyLayout() {
  if (ui.root) ui.root.className = appClass();
}

function renderApp() {
  closePop();
  ui.side = h("aside", { class: "side" });
  ui.topbar = h("header", { class: "topbar" });
  ui.scroll = h("div", { class: "scroll", id: "thread" });
  const composing = state.view === "session" && !!state.project;
  ui.dock = composing ? dock() : null;
  if (!composing) { ui.composer = null; ui.input = null; }
  ui.statusBody = h("div", { class: "status-body" });
  ui.root = h("div", { class: appClass() },
    ui.side,
    h("main", { class: "main" }, ui.topbar, ui.scroll, ui.dock),
    h("aside", { class: "status" },
      h("div", { class: "status-head" }, h("span", { class: "h grow" }, t("status.panel")),
        h("button", { class: "btn ghost sm icon-only", title: t("status.hide"), onclick: toggleStatus }, icon("x", "sm"))),
      ui.statusBody),
  );
  mount(ui.root);
  renderSide();
  renderTopbar();
  if (state.view === "workspaces") renderWorkspaces();
  else if (state.view === "activity") renderActivity();
  else renderThread(true);
  if (composing) updateComposer(true);
  renderStatus();
  updateTitle();
}

function toggleStatus() {
  state.statusOpen = !state.statusOpen;
  ls.set("cw_status", state.statusOpen ? "1" : "0");
  applyLayout();
  renderTopbar();
  if (state.statusOpen) renderStatus();
}

function updateTitle() {
  const live = inSession() && currentTask();
  const name = state.view === "workspaces" ? t("nav.workspaces")
    : state.view === "activity" ? t("nav.activity")
      : state.session ? state.session.title || t("session.untitled")
        : state.project ? t("session.newIn", { name: state.project.name }) : "";
  document.title = `${live ? "● " : ""}${name ? name + " · " : ""}Coding Agent`;
}

// 事件来得密：同一帧里只重画一次输入框、右栏、左栏与进行中的列表。
let chromePending = false;
function scheduleChrome() {
  if (chromePending) return;
  chromePending = true;
  requestAnimationFrame(() => {
    chromePending = false;
    if (!state.me) return;
    renderSide();
    if (state.view === "session" && state.project) updateComposer();
    renderStatus();
    if (state.view === "activity") renderActivity();
    updateTitle();
  });
}

// ---------------------------------------------------------------- 左栏

function sourceLabel(p) {
  if (p.repo) return `${providerInfo(p.git_provider).name} · ${p.repo}`;
  if (!p.source || p.source === "empty") return t("source.empty");
  if (p.source.startsWith("sample:")) return t("source.sample");
  return p.source.replace(/^https:\/\//, "");
}

function fmtShortAgo(iso) {
  const ms = Date.now() - new Date(iso);
  if (isNaN(ms)) return "";
  if (ms < 60e3) return t("ago.now");
  if (ms < 3600e3) return t("short.min", { n: Math.floor(ms / 60e3) });
  if (ms < 86400e3) return t("short.hour", { n: Math.floor(ms / 3600e3) });
  if (ms < 7 * 86400e3) return t("short.day", { n: Math.floor(ms / 86400e3) });
  const d = new Date(iso);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

// 有任务在跑的会话（<工作区>/<会话>）：来自“进行中”，再加上当前会话。
function liveSessions() {
  const live = new Set(state.active.map((a) => `${a.project_id}/${a.session_id}`));
  if (inSession() && currentTask()) live.add(`${state.project.id}/${state.session.id}`);
  return live;
}

// 工作区的顺序：按其中最近活动的会话，没有会话的按创建时间。
function orderedProjects() {
  const last = new Map();
  for (const s of state.sessions) if (!last.has(s.project_id)) last.set(s.project_id, s.updated_at);
  const at = (p) => new Date(last.get(p.id) || p.created_at).getTime() || 0;
  return state.projects.slice().sort((a, b) => at(b) - at(a));
}

function renderSide() {
  if (!ui.side || !state.me) return;
  const me = state.me;
  const name = me.login || me.user;
  const live = liveSessions();
  const nav = (ic, label, onclick, opts = {}) => h("button", { class: "nav-item" + (opts.active ? " active" : ""), title: opts.title || label, onclick },
    icon(ic, "sm"), h("span", { class: "grow" }, label),
    opts.live ? h("span", { class: "spinner" }) : null,
    opts.count ? h("span", { class: "count" }, opts.count) : null,
    opts.kbd ? h("span", { class: "kbd" }, opts.kbd) : null);
  ui.side.replaceChildren(
    h("div", { class: "side-head" },
      h("div", { class: "brand grow" }, h("span", { class: "mark" }, logo()), "Coding Agent"),
      h("button", { class: "btn ghost sm icon-only only-wide", title: t("side.collapse"), onclick: () => { state.sideCollapsed = true; ls.set("cw_side", "0"); applyLayout(); renderTopbar(); } }, icon("panelLeft", "sm")),
      h("button", { class: "btn ghost sm icon-only only-narrow", title: t("common.close"), onclick: () => { state.sideOpen = false; applyLayout(); } }, icon("x", "sm"))),
    h("nav", { class: "side-nav" },
      nav("compose", t("nav.newSession"), newSession, { title: t("nav.newSessionTitle") }),
      nav("search", t("nav.search"), openSearch, { kbd: "Ctrl K" }),
      nav("grid", t("nav.workspaces"), () => go("workspaces"), { active: state.view === "workspaces", count: state.projects.length || null }),
      nav("activity", t("nav.activity"), () => go("activity"), { active: state.view === "activity", count: state.active.length || null, live: state.active.length > 0 }),
    ),
    h("div", { class: "side-label" }, t("side.sessions")),
    h("div", { class: "side-list" },
      state.projects.length ? orderedProjects().map((p) => workspaceGroup(p, live)) : h("div", { class: "side-empty" }, t("side.noWorkspaces"))),
    // 整个个人信息块都是按钮：点哪里都打开账号、外观与语言的菜单，菜单与这一块一样宽。
    h("div", { class: "side-foot" },
      h("button", { class: "profile", title: t("menu.title"), "aria-haspopup": "menu", onclick: (e) => openPop(e.currentTarget, userMenu, { matchWidth: true }) },
        h("span", { class: "avatar" }, me.avatar_url ? h("img", { src: me.avatar_url, alt: "" }) : initials(name)),
        h("span", { class: "grow who" }, h("span", { class: "n ellipsis" }, name), h("span", { class: "m ellipsis" }, t("side.tenant", { tenant: me.tenant }))),
        icon("more", "sm more"))),
  );
}

const GROUP_MAX = 6;

function workspaceGroup(p, live) {
  const closed = state.groupsClosed.has(p.id);
  const sessions = state.sessions.filter((s) => s.project_id === p.id);
  const drafting = state.view === "session" && !state.session && state.project && state.project.id === p.id;
  const all = state.groupsAll.has(p.id);
  const shown = all ? sessions : sessions.slice(0, GROUP_MAX);
  const toggle = () => {
    if (closed) state.groupsClosed.delete(p.id); else state.groupsClosed.add(p.id);
    ls.set("cw_groups_closed", [...state.groupsClosed].join(","));
    renderSide();
  };
  const anyLive = sessions.some((s) => live.has(`${p.id}/${s.id}`));
  return h("div", { class: "ws-group" + (closed ? " closed" : "") },
    h("div", { class: "ws-head" },
      h("button", { class: "ws-toggle", title: `${p.name} · ${sourceLabel(p)}`, onclick: toggle },
        icon("chevron", "xs chev"), projectMark(p, "sm"), h("span", { class: "t ellipsis" }, p.name),
        closed && anyLive ? h("span", { class: "spinner" }) : null),
      h("button", { class: "btn ghost xs icon-only ws-add", title: t("ws.newIn", { name: p.name }), onclick: () => openDraft(p.id) }, icon("plus", "xs"))),
    closed ? null : h("div", { class: "ws-sessions" },
      drafting ? h("div", { class: "side-item session active draft" }, icon("compose", "xs"), h("span", { class: "t grow ellipsis" }, t("session.new"))) : null,
      shown.map((s) => sessionItem(p, s, live.has(`${p.id}/${s.id}`))),
      sessions.length > GROUP_MAX ? h("button", { class: "more-link", onclick: () => { if (all) state.groupsAll.delete(p.id); else state.groupsAll.add(p.id); renderSide(); } },
        all ? t("ws.less") : t("ws.showAll", { n: sessions.length })) : null,
      !sessions.length && !drafting ? h("button", { class: "more-link", onclick: () => openDraft(p.id) }, t("ws.noSessions")) : null,
    ),
  );
}

function sessionItem(p, s, isLive) {
  const active = inSession() && state.project.id === p.id && state.session.id === s.id;
  const open = () => { if (!active) openSession(p.id, s.id); else { state.sideOpen = false; applyLayout(); } };
  return h("div", {
    class: "side-item session" + (active ? " active" : ""),
    role: "button", tabindex: 0, title: s.title || t("session.untitled"),
    onclick: open,
    onkeydown: (e) => { if (e.key === "Enter") open(); },
  },
    isLive ? h("span", { class: "spinner", title: t("session.live") }) : s.pr_number ? badge(p.git_provider, "xs") : icon("chat", "xs"),
    h("span", { class: "t grow ellipsis" }, s.title || t("session.untitled")),
    h("span", { class: "when" }, fmtShortAgo(s.updated_at)),
    h("button", { class: "btn ghost xs icon-only item-more", title: t("session.menuTitle"), onclick: (e) => { e.stopPropagation(); openPop(e.currentTarget, (close) => sessionMenu(p, s, close)); } }, icon("more", "xs")),
  );
}

function sessionMenu(p, s, close) {
  return [
    popItem("edit", t("session.rename"), null, false, () => { close(); renameSession(p, s); }),
    popItem("trash", t("session.delete"), s.workspace_id !== p.id ? t("session.deleteCopy") : t("session.deleteRecord"), false, () => { close(); deleteSession(p, s); }),
  ];
}

function setSession(s) {
  state.sessions = state.sessions.map((x) => (x.project_id === s.project_id && x.id === s.id ? s : x));
  if (state.session && state.session.project_id === s.project_id && state.session.id === s.id) state.session = s;
}

async function renameSession(p, s) {
  const title = prompt(t("session.titlePrompt"), s.title || "");
  if (!title || !title.trim()) return;
  try {
    setSession(await api("POST", `${sessionURL(p.id, s.id)}/rename`, { title }));
    renderSide();
    renderTopbar();
    updateTitle();
  } catch (ex) { report(ex); }
}

async function deleteSession(p, s) {
  const title = s.title || t("session.untitled");
  if (!confirm(t(s.workspace_id !== p.id ? "session.deleteConfirmCopy" : "session.deleteConfirm", { title }))) return;
  try {
    await api("DELETE", sessionURL(p.id, s.id));
    state.sessions = state.sessions.filter((x) => !(x.project_id === p.id && x.id === s.id));
    toast(t("session.deleted"));
    if (state.session && state.session.project_id === p.id && state.session.id === s.id) {
      const next = state.sessions.find((x) => x.project_id === p.id);
      if (next) openSession(p.id, next.id); else openDraft(p.id);
    } else renderSide();
  } catch (ex) { report(ex); }
}

function userMenu(close) {
  const theme = ls.get("cw_theme") || "system";
  const set = (value) => { ls.set("cw_theme", value); applyTheme(); close(); };
  const conns = state.me.connections || [];
  const names = conns.map((c) => providerInfo(c.provider).name).join(t("list.sep"));
  return [
    popItem("link", t("account.title"), conns.length ? t("account.connectedList", { n: conns.length, names }) : t("account.connectHint"), false, () => { close(); openAccount(); }),
    h("div", { class: "pop-sep" }),
    h("div", { class: "pop-title" }, t("menu.appearance")),
    popItem("monitor", t("theme.system"), null, theme === "system", () => set("system")),
    popItem("sun", t("theme.light"), null, theme === "light", () => set("light")),
    popItem("moon", t("theme.dark"), null, theme === "dark", () => set("dark")),
    h("div", { class: "pop-sep" }),
    h("div", { class: "pop-title" }, t("menu.language")),
    ...LANGS.map((l) => popItem("globe", l.label, null, l.id === lang, () => { close(); setLang(l.id); })),
    h("div", { class: "pop-sep" }),
    popItem("logout", t("menu.logout"), null, false, () => { close(); logout(); }),
  ];
}

// ---------------------------------------------------------------- 顶栏

function renderTopbar() {
  if (!ui.topbar) return;
  const p = state.project;
  const s = state.session;
  const parts = [
    h("button", { class: "btn ghost sm icon-only only-narrow", title: t("side.open"), onclick: () => { state.sideOpen = true; applyLayout(); } }, icon("menu", "sm")),
    state.sideCollapsed ? h("button", { class: "btn ghost sm icon-only only-wide", title: t("side.expand"), onclick: () => { state.sideCollapsed = false; ls.set("cw_side", "1"); applyLayout(); renderTopbar(); } }, icon("panelLeft", "sm")) : null,
  ];
  if (state.view === "workspaces") parts.push(h("span", { class: "title" }, t("nav.workspaces")));
  else if (state.view === "activity") parts.push(h("span", { class: "title" }, t("nav.activity")));
  else if (p) {
    parts.push(h("button", { class: "crumb ellipsis", title: t("ws.overview"), onclick: () => go("workspaces") }, p.name));
    parts.push(h("span", { class: "crumb-sep" }, "/"));
    parts.push(h("span", { class: "title ellipsis" }, s ? s.title || t("session.untitled") : t("session.new")));
    parts.push(h("div", { class: "chips" },
      p.repo ? h("a", { class: "chip", href: repoURL(p), target: "_blank", rel: "noopener" }, badge(p.git_provider, "xs"), p.repo) : null,
      s && s.branch ? h("span", { class: "chip run mono" }, icon("branch", "xs"), s.branch) : p.base_branch ? h("span", { class: "chip mono" }, icon("branch", "xs"), p.base_branch) : null,
      s && s.pr_url ? h("a", { class: "chip ok", href: s.pr_url, target: "_blank", rel: "noopener" }, `PR #${s.pr_number}`, icon("external", "xs")) : null,
    ));
  }
  parts.push(h("span", { class: "grow" }));
  if (inSession()) {
    parts.push(h("button", { class: "btn ghost sm icon-only", title: t("drawer.title"), onclick: () => openDrawer("changes") }, icon("diff", "sm")));
    parts.push(h("button", { class: "btn ghost sm icon-only", title: t("session.menuTitle"), onclick: (e) => openPop(e.currentTarget, (close) => sessionMenu(p, s, close)) }, icon("more", "sm")));
    parts.push(h("button", { class: "btn ghost sm icon-only", title: state.statusOpen ? t("status.hide") : t("status.show"), onclick: toggleStatus }, icon("panelRight", "sm")));
  }
  ui.topbar.replaceChildren(...parts.filter(Boolean));
}

// ---------------------------------------------------------------- 工作区一览、进行中、搜索

function renderWorkspaces() {
  if (!ui.scroll) return;
  const live = liveSessions();
  const cards = orderedProjects().map((p) => {
    const sessions = state.sessions.filter((s) => s.project_id === p.id);
    const running = sessions.filter((s) => live.has(`${p.id}/${s.id}`)).length;
    return h("div", { class: "ws-card" },
      h("div", { class: "row" },
        h("span", { class: "ws-icon" }, projectMark(p, "sm")),
        h("div", { class: "grow" }, h("div", { class: "name ellipsis" }, p.name), h("div", { class: "src ellipsis" }, sourceLabel(p)))),
      h("div", { class: "stats" },
        h("span", {}, t("ws.sessions", { n: sessions.length })),
        running ? h("span", { class: "chip run" }, h("span", { class: "spinner" }), t("ws.running", { n: running })) : null,
        sessions[0] ? h("span", { class: "muted" }, t("ws.recent", { ago: fmtAgo(sessions[0].updated_at) })) : null),
      sessions.length ? h("div", { class: "ws-recent" }, sessions.slice(0, 3).map((s) => h("button", { class: "link-row", onclick: () => openSession(p.id, s.id) },
        live.has(`${p.id}/${s.id}`) ? h("span", { class: "spinner" }) : icon("chat", "xs"), h("span", { class: "grow ellipsis" }, s.title || t("session.untitled")), h("span", { class: "when" }, fmtShortAgo(s.updated_at))))) : null,
      h("div", { class: "actions" },
        h("button", { class: "btn primary sm", onclick: () => openDraft(p.id) }, icon("plus", "sm"), t("nav.newSession")),
        h("span", { class: "grow" }),
        h("button", { class: "btn ghost sm danger", onclick: () => deleteProject(p) }, icon("trash", "sm"), t("ws.delete"))),
    );
  });
  ui.scroll.replaceChildren(h("div", { class: "page" },
    h("div", { class: "page-head" },
      h("div", { class: "grow" }, h("h1", {}, t("nav.workspaces"))),
      h("button", { class: "btn primary", onclick: () => openNewProject() }, icon("plus", "sm"), t("ws.create"))),
    cards.length ? h("div", { class: "ws-grid" }, cards) : welcome(),
  ));
}

function renderActivity() {
  if (!ui.scroll) return;
  const rows = state.active.map((a) => h("div", { class: "act-row", role: "button", tabindex: 0, onclick: () => jumpTo(a.project_id, a.session_id, a.task.id) },
    a.task.status === "TASK_STATUS_RUNNING" ? h("span", { class: "spinner" }) : icon("clock", "sm"),
    h("div", { class: "grow" },
      h("div", { class: "t" }, a.task.message),
      h("div", { class: "m" }, `${a.project_name} / ${a.session_title || t("session.untitled")} · ${fmtAgo(a.task.created_at)}`)),
    statusChip(a.task.status),
    h("button", { class: "btn sm", onclick: (e) => { e.stopPropagation(); cancelTask(a.task.id, sessionURL(a.project_id, a.session_id)); } }, icon("stop", "xs"), t("common.stop")),
  ));
  ui.scroll.replaceChildren(h("div", { class: "page" },
    h("div", { class: "page-head" },
      h("div", { class: "grow" }, h("h1", {}, t("nav.activity")))),
    rows.length ? h("div", { class: "act-list" }, rows)
      : h("div", { class: "empty-page" }, icon("check", "lg"), h("div", {}, t("activity.empty")),
        h("button", { class: "btn sm", onclick: newSession }, icon("compose", "sm"), t("nav.newSession"))),
  ));
}

// openSearch 是搜索面板（Ctrl/⌘ K）：按标题与工作区名找会话，按名字与来源找工作区。
function openSearch() {
  closePop();
  if (document.querySelector("dialog.palette")) return;
  const input = h("input", { class: "pal-input", placeholder: t("search.placeholder"), "aria-label": t("nav.search") });
  const list = h("div", { class: "pal-list" });
  const dialog = h("dialog", { class: "palette" }, h("div", { class: "pal-head" }, icon("search", "sm"), input, h("span", { class: "kbd" }, "Esc")), list);
  let items = [];
  let sel = 0;
  const pick = (i) => { const it = items[i]; if (!it) return; dialog.close(); it.run(); };
  const draw = () => {
    const q = input.value.trim().toLowerCase();
    const has = (text) => String(text || "").toLowerCase().includes(q);
    const sessions = state.sessions.filter((s) => !q || has(s.title) || has((findProject(s.project_id) || {}).name)).slice(0, 30).map((s) => ({
      group: t("side.sessions"), mark: () => icon("chat", "sm"), title: s.title || t("session.untitled"), sub: `${(findProject(s.project_id) || {}).name || ""} · ${fmtAgo(s.updated_at)}`, run: () => openSession(s.project_id, s.id),
    }));
    const projects = state.projects.filter((p) => !q || has(p.name) || has(sourceLabel(p))).slice(0, 10).map((p) => ({
      group: t("nav.workspaces"), mark: () => projectMark(p, "sm"), title: p.name, sub: t("search.newIn", { source: sourceLabel(p) }), run: () => openDraft(p.id),
    }));
    items = [...sessions, ...projects];
    sel = Math.min(sel, Math.max(0, items.length - 1));
    const out = [];
    items.forEach((it, i) => {
      if (i === 0 || items[i - 1].group !== it.group) out.push(h("div", { class: "pal-group" }, it.group));
      out.push(h("button", { class: "pal-item" + (i === sel ? " sel" : ""), onclick: () => pick(i), onmousemove: () => { if (sel !== i) { sel = i; draw(); } } },
        it.mark(), h("span", { class: "t ellipsis" }, it.title), h("span", { class: "m ellipsis" }, it.sub)));
    });
    list.replaceChildren(...(out.length ? out : [h("div", { class: "pal-empty" }, t("search.none"))]));
    const cur = list.querySelector(".pal-item.sel");
    if (cur) cur.scrollIntoView({ block: "nearest" });
  };
  input.addEventListener("input", () => { sel = 0; draw(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(sel + 1, items.length - 1); draw(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(sel - 1, 0); draw(); }
    else if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); pick(sel); }
  });
  dialog.addEventListener("close", () => dialog.remove());
  dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
  document.body.append(dialog);
  dialog.showModal();
  draw();
  input.focus();
}

// ---------------------------------------------------------------- 任务的派生状态

function statusOf(t) {
  const run = state.runs.get(t.id);
  const fin = run && run.events.find((e) => e.type === "task_finished");
  return (fin && fin.value.status) || t.status;
}

function liveTasks() {
  return state.tasks.filter((t) => !TERMINAL.has(statusOf(t)));
}

// 同一项目（Session）的任务串行：最早的那个没结束的就是正在跑的。
function currentTask() {
  return liveTasks()[0] || null;
}

function focusTask() {
  return currentTask() || state.tasks[state.tasks.length - 1] || null;
}

function lastFinished() {
  for (let i = state.tasks.length - 1; i >= 0; i--) {
    const run = state.runs.get(state.tasks[i].id);
    const fin = run && run.events.find((e) => e.type === "task_finished");
    if (fin) return { task: state.tasks[i], result: fin.value };
  }
  return null;
}

function pendingAsk(task) {
  const run = state.runs.get(task.id);
  if (!run) return null;
  const events = run.events;
  for (let i = events.length - 1; i >= 0; i--) {
    const ev = events[i];
    if (ev.type !== "human_input_required") continue;
    const v = ev.value || {};
    const qid = pick(v, "question_id", "questionId");
    if (state.replied.has(qid) || events.slice(i + 1).some((e) => !QUIET.has(e.type))) return null;
    const raw = pick(v, "options", "choices") || ["approve", "reject"];
    const options = raw.map((o) => (typeof o === "string" ? { id: o, label: o } : { id: pick(o, "id", "value", "option"), label: pick(o, "label", "text", "id", "value") }));
    return { qid, question: pick(v, "question", "prompt", "text", "summary", "message") || t("ask.default"), options };
  }
  return null;
}

function phase() {
  if (!state.project) return { mode: "none" };
  const live = liveTasks();
  if (live.length) {
    const current = live[0];
    const ask = pendingAsk(current);
    return { mode: ask ? "waiting" : "running", current, queued: live.slice(1), ask };
  }
  const last = state.tasks[state.tasks.length - 1];
  if (!last) return { mode: "empty" };
  return { mode: statusOf(last) === "TASK_STATUS_DONE" ? "done" : "failed", last };
}

function liveInfo(task) {
  const run = state.runs.get(task.id);
  const events = run ? run.events : [];
  let round = 0;
  for (const e of events) if (e.type === "round_started") round = num(pick(e.value, "round_seq")) || round;
  const steps = workSteps(events);
  const last = steps[steps.length - 1];
  const now = last ? stepInfo(last).now : task.status === "TASK_STATUS_QUEUED" ? t("status.queued") : t("run.connecting");
  const elapsed = events.length ? Date.now() - new Date(events[0].time) : 0;
  return { round, now, elapsed, steps: steps.length };
}

// ---------------------------------------------------------------- 对话

function renderThread(stick = false) {
  const thread = ui.scroll;
  if (!thread) return;
  const nearBottom = stick || thread.scrollHeight - thread.scrollTop - thread.clientHeight < 120;
  let content;
  if (!state.project) content = welcome();
  else if (!state.tasks.length) content = projectEmpty();
  else content = state.tasks.map(turn);
  thread.replaceChildren(h("div", { class: "thread" }, content));
  if (nearBottom) thread.scrollTop = thread.scrollHeight;
}

function renderTurn(taskId) {
  const old = document.getElementById("turn-" + taskId);
  const t = state.tasks.find((x) => x.id === taskId);
  if (!old || !t) return renderThread();
  const thread = ui.scroll;
  const nearBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 120;
  old.replaceWith(turn(t));
  if (nearBottom) thread.scrollTop = thread.scrollHeight;
}

function scrollToTurn(id) {
  const el = document.getElementById("turn-" + id);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
}

function welcome() {
  const sample = state.samples[0];
  const conns = gitConnections();
  return h("div", { class: "hero-empty" },
    h("div", { class: "mark big" }, logo("lg")),
    h("h2", {}, t("welcome.title")),
    h("div", { class: "suggestions" },
      sample ? suggestion("box", t("welcome.sample"), sample.description, () => quickSample(sample)) : null,
      suggestion("branch", conns.length ? t("welcome.myRepos") : t("welcome.clone"),
        conns.length ? t("welcome.myReposDesc", { names: conns.map((c) => providerInfo(c.provider).name).join(t("list.sep")) }) : t("welcome.cloneDesc"),
        () => openNewProject("git")),
      suggestion("folder", t("source.empty"), t("welcome.emptyDesc"), () => openNewProject("empty")),
    ),
  );
}

function suggestion(ic, title, desc, onclick) {
  return h("button", { class: "suggestion", onclick }, icon(ic), h("div", {}, h("div", { class: "t" }, title), h("div", { class: "d" }, desc)));
}

async function quickSample(sample) {
  try {
    const p = await api("POST", "/api/projects", { name: sample.name, sample: sample.id });
    state.projects.unshift(p);
    openDraft(p.id);
    setComposer(sample.prompt);
  } catch (ex) { report(ex); }
}

// projectEmpty 是新会话（草稿或还没有任务的会话）的开场：给几条起手的任务。
function projectEmpty() {
  const sample = state.samples.find((s) => state.project.source === "sample:" + s.id);
  const ideas = [
    sample ? ["sparkles", t("idea.fixSample"), sample.prompt] : null,
    ["eye", t("idea.explain"), t("idea.explainPrompt")],
    ["check", t("idea.fix"), t("idea.fixPrompt")],
    ["terminal", t("idea.tests"), t("idea.testsPrompt")],
  ].filter(Boolean);
  return h("div", { class: "hero-empty" },
    h("div", { class: "mark big" }, logo("lg")),
    h("h2", {}, t("empty.title", { name: state.project.name })),
    h("div", { class: "suggestions" }, ideas.map(([ic, title, prompt]) => suggestion(ic, title, prompt.length > 60 ? prompt.slice(0, 58) + "…" : prompt, () => setComposer(prompt)))),
  );
}

function modelLabel(id) {
  return (MODELS.find((m) => m.id === id) || {}).label || id;
}

function turn(task) {
  const run = state.runs.get(task.id);
  const events = run ? run.events : [];
  const finished = events.find((e) => e.type === "task_finished");
  const status = statusOf(task);
  const running = !TERMINAL.has(status);
  let tail = null;
  if (finished) tail = result(task, finished.value);
  else if (running) tail = waitingLine(task);
  else if (!run) tail = h("div", { class: "result-foot" }, statusChip(status), h("button", { class: "btn ghost sm", onclick: () => loadRun(task, true) }, t("turn.load")));
  return h("section", { class: "turn", id: "turn-" + task.id },
    h("div", { class: "msg-user" },
      h("div", { class: "bubble" }, task.message),
      h("div", { class: "meta" }, fmtTime(task.created_at),
        task.sandbox === "read" ? h("span", { class: "chip info" }, icon("eye", "xs"), t("sandbox.read")) : null,
        task.model && task.model !== "balanced" ? h("span", { class: "chip" }, modelLabel(task.model)) : null),
    ),
    h("div", { class: "msg-agent" },
      h("div", { class: "agent-mark" + (running ? " live" : ""), title: running ? t("turn.working") : null }, logo()),
      h("div", { class: "agent-body" }, worklog(task, events, running, finished), asks(events), tail),
    ),
  );
}

function waitingLine(task) {
  const live = liveTasks();
  const ahead = live.indexOf(task);
  if (ahead > 0) {
    return h("div", { class: "waiting-line" }, icon("clock", "sm"), t("queue.ahead", { n: ahead }),
      h("button", { class: "btn ghost xs", onclick: () => cancelTask(task.id) }, t("queue.cancel")));
  }
  const run = state.runs.get(task.id);
  if (run && run.events.length) return null; // 进度在工作过程与输入框上方
  return h("div", { class: "waiting-line" }, h("span", { class: "spinner" }), run ? t("run.starting") : t("run.connecting"));
}

function asks(events) {
  return events.filter((e) => e.type === "human_input_required").map((ev) => {
    const v = ev.value || {};
    const qid = pick(v, "question_id", "questionId");
    const q = pick(v, "question", "prompt", "text", "summary", "message") || t("ask.short");
    const answered = state.replied.has(qid) || events.some((e) => e.event_id > ev.event_id && !QUIET.has(e.type));
    return answered
      ? h("div", { class: "ask-inline done" }, icon("check", "sm"), h("span", {}, t("ask.replied", { q })))
      : h("div", { class: "ask-inline" }, icon("alert", "sm"), h("span", {}, h("strong", {}, t("ask.needs")), q));
  });
}

function worklog(task, events, running, finished) {
  const steps = workSteps(events);
  if (!steps.length) return null;
  const isOpen = state.open.has(task.id) || (running && !state.closed.has(task.id));
  const first = events[0];
  const elapsed = first ? (finished ? new Date(finished.time) : Date.now()) - new Date(first.time) : 0;
  const head = running
    ? [h("span", { class: "spinner" }), h("span", { class: "grow", id: "wl-" + task.id }, workingText(steps.length, elapsed))]
    : [icon(finished && finished.value.status === "TASK_STATUS_DONE" ? "check" : "clock", "sm lead"), h("span", { class: "grow" }, t("work.done", { dur: fmtDuration(elapsed), n: steps.length }))];
  return h("div", { class: "worklog" + (isOpen ? " open" : "") },
    h("button", { class: "worklog-head", onclick: () => {
      if (isOpen) { state.open.delete(task.id); state.closed.add(task.id); } else { state.open.add(task.id); state.closed.delete(task.id); }
      renderTurn(task.id);
    } }, head, icon("chevron", "sm chev")),
    h("div", { class: "worklog-steps" }, steps.map(step)),
  );
}

function workingText(steps, elapsed) {
  return t("work.live", { n: steps }) + (elapsed ? " · " + fmtDuration(elapsed) : "");
}

function workSteps(events) {
  return events.filter((e) => !QUIET.has(e.type) && e.type !== "task_finished" && e.type !== "human_input_required");
}

function stepInfo(ev) {
  const v = ev.value || {};
  let ic = "box", kind = "", title = ev.type, detail = "", chips = null, now = "";
  switch (ev.type) {
    case "task_accepted": ic = "check"; kind = "ok"; title = t("step.accepted"); now = t("step.acceptedNow"); break;
    case "round_started":
      ic = "play"; kind = "run"; title = t("step.round", { n: pick(v, "round_seq") ?? "" }); now = t("step.roundNow");
      detail = v.rerun ? t("verdict.replan") : "";
      break;
    case "stage_started": {
      ic = "layers"; kind = "run";
      const goal = pick(v, "goal") || t("step.execute");
      title = t("step.plan", { goal });
      now = t("step.planNow", { goal });
      detail = pick(v, "exit_when") ? t("step.exitWhen", { x: v.exit_when }) : "";
      if (v.triage_level) detail = (detail ? detail + "\n" : "") + "Triage: " + v.triage_level;
      const nodes = Array.isArray(v.nodes) ? v.nodes : [];
      chips = nodes.length ? h("div", { class: "step-chips" }, nodes.map((n) => h("span", { class: "chip mono" }, typeof n === "string" ? n : pick(n, "id") || t("step.node")))) : null;
      break;
    }
    case "node_started": {
      const id = pick(v, "node_id") || t("step.node");
      ic = "terminal"; title = t("step.nodeStart", { id }); now = t("step.nodeRunning", { id });
      break;
    }
    case "node_finished": {
      const st = String(pick(v, "status") || "");
      const failed = /fail|denied|error/i.test(st);
      const id = pick(v, "node_id") || t("step.node");
      ic = failed ? "x" : "check"; kind = failed ? "err" : "ok";
      title = t(failed ? "step.nodeFailed" : "step.nodeDone", { id });
      now = t(failed ? "step.nodeFailedNow" : "step.nodeDoneNow", { id });
      detail = [pick(v, "role"), st && st !== "succeeded" ? st : ""].filter(Boolean).join(" · ");
      break;
    }
    case "tool_status": ic = "terminal"; title = t("step.tool", { name: pick(v, "tool", "name") || "" }); detail = pick(v, "status", "message") || ""; break;
    case "verdict": {
      const key = String(pick(v, "verdict") || "").replace(/^VERDICT_/, "").toLowerCase();
      const [label, k] = VERDICTS[key] || [key || "—", ""];
      ic = "shield"; kind = k === "info" ? "run" : k;
      title = t("step.verdict", { label, key });
      now = t("step.verdictNow", { label });
      detail = pick(v, "reason") || "";
      break;
    }
  }
  return { ic, kind, title, detail, chips, now: now || title };
}

function step(ev) {
  const s = stepInfo(ev);
  return h("div", { class: `step ${s.kind}` },
    h("div", { class: "step-icon" }, icon(s.ic, "xs")),
    h("div", { class: "step-main" },
      h("div", { class: "step-title" }, s.title),
      s.detail ? h("div", { class: "step-detail" }, s.detail) : null,
      s.chips,
      h("details", { class: "raw" }, h("summary", {}, t("step.raw")), h("pre", {}, JSON.stringify(ev.value || {}, null, 2))),
    ),
    h("span", { class: "step-time" }, fmtClock(ev.time)),
  );
}

function deliverableText(result) {
  const d = result.deliverable && result.deliverable.value !== undefined ? result.deliverable.value : result.deliverable;
  if (typeof d === "string") return d;
  if (!d || typeof d !== "object") return "";
  const t = pick(d, "answer", "summary", "text", "content", "result", "report", "message");
  if (typeof t === "string") {
    const trimmed = t.trim();
    if (trimmed.startsWith("{")) {
      try {
        const inner = JSON.parse(trimmed);
        const s = pick(inner, "answer", "summary", "text", "content", "result");
        if (typeof s === "string") return s;
      } catch { /* 不是 JSON，原样用 */ }
    }
    return t;
  }
  return objectMarkdown(d);
}

// 结构化的交付（没有 text、answer 这类字段）按字段排成小节，比原样的 JSON 好读。
function objectMarkdown(d) {
  const inlineValue = (v) => (v !== null && typeof v === "object" ? "`" + JSON.stringify(v) + "`" : String(v));
  const out = [];
  for (const [k, v] of Object.entries(d)) {
    out.push(`### ${k.replace(/_/g, " ")}`);
    if (Array.isArray(v)) out.push(v.map((x) => `- ${inlineValue(x)}`).join("\n"));
    else if (v !== null && typeof v === "object") out.push(Object.entries(v).map(([kk, vv]) => `- **${kk}**: ${inlineValue(vv)}`).join("\n"));
    else out.push(String(v));
    out.push("");
  }
  return out.join("\n");
}

function taskCost(t) {
  const d = state.detail.get(t.id);
  return d && d.cost !== undefined ? d.cost : t.cost_usd || 0;
}

function result(task, r) {
  const text = deliverableText(r);
  const ok = r.status === "TASK_STATUS_DONE";
  const cost = taskCost(task);
  const rounds = r.rounds || task.rounds;
  const n = state.changes ? state.changes.files.length : 0;
  return h("div", { class: "col" },
    text ? h("div", { class: "answer" }, md(text)) : null,
    r.error ? h("div", { class: "error-box" }, typeof r.error === "string" ? r.error : pick(r.error, "message") || JSON.stringify(r.error)) : null,
    h("div", { class: "result-foot" },
      statusChip(r.status),
      rounds ? h("span", { class: "chip" }, t("rounds", { n: rounds })) : null,
      cost ? h("span", { class: "chip" }, fmtUSD(cost)) : null,
      h("span", { class: "grow" }),
      text ? h("button", { class: "btn ghost xs", onclick: () => copy(text) }, icon("copy", "xs"), t("common.copy")) : null,
      ok && n && task === state.tasks[state.tasks.length - 1] ? h("button", { class: "btn ghost xs", onclick: () => openDrawer("changes") }, icon("diff", "xs"), t("changes.review")) : null,
    ),
  );
}

// ---------------------------------------------------------------- 事件流

function closeStreams() {
  for (const run of state.runs.values()) if (run.es) run.es.close();
}

function loadRun(t, force = false) {
  if (state.runs.has(t.id) && !force) return state.runs.get(t.id).promise;
  const base = sbase();
  const run = { events: [], done: false, es: null };
  state.runs.set(t.id, run);
  run.promise = new Promise((resolve) => {
    const es = new EventSource(`${base}/tasks/${t.id}/events`);
    run.es = es;
    const seen = new Set();
    let timer = null;
    const finish = () => { es.close(); run.done = true; clearTimeout(timer); resolve(); };
    const onEvent = (msg) => {
      if (sbase() !== base) return finish();
      let data;
      try { data = JSON.parse(msg.data); } catch { return; }
      const id = Number(data.event_id || msg.lastEventId || 0);
      if (id && seen.has(id)) return;
      if (id) seen.add(id);
      const type = msg.type === "message" ? String(data.type || "").replace(/^TASK_EVENT_TYPE_/, "").toLowerCase() : msg.type;
      const ev = { event_id: id, type, time: data.emitted_at, value: (data.payload && data.payload.value) || {} };
      run.events.push(ev);
      const replaying = TERMINAL.has(t.status) && !run.live;
      if (type === "task_finished") {
        t.status = ev.value.status || t.status;
        if (!state.closed.has(t.id)) state.open.delete(t.id);
        finish();
        if (!replaying) afterFinish(t);
      } else if (type === "node_finished" && !replaying) {
        throttledChanges();
      }
      scheduleTurn(t.id);
      scheduleChrome();
    };
    run.live = !TERMINAL.has(t.status);
    for (const name of EVENT_TYPES) es.addEventListener(name, onEvent);
    es.onmessage = onEvent;
    es.onerror = () => {
      // 结束了的任务流关掉后浏览器会重连；已经拿到 task_finished 就不再连。
      if (run.done) es.close();
    };
    if (TERMINAL.has(t.status)) timer = setTimeout(finish, 15000);
  });
  return run.promise;
}

const pendingTurns = new Set();
function scheduleTurn(id) {
  if (pendingTurns.has(id)) return;
  pendingTurns.add(id);
  requestAnimationFrame(() => { pendingTurns.delete(id); renderTurn(id); });
}

async function replayTasks() {
  const base = sbase();
  const recent = state.tasks.slice(-REPLAY_LIMIT);
  // 先接上进行中的（实时），再按顺序重放已结束的（浏览器对同一主机的连接数有限）。
  for (const t of recent) if (!TERMINAL.has(t.status)) loadRun(t);
  for (const t of recent) {
    if (sbase() !== base) return;
    if (TERMINAL.has(t.status)) await loadRun(t);
  }
}

function afterFinish(t) {
  loadChanges();
  loadEnv();
  loadDetail(t);
  loadActive();
  refreshTasks();
  const next = currentTask();
  if (next) loadDetail(next);
}

// cancelTask 停止一个任务；base 是它所在会话的接口前缀，默认当前会话。
async function cancelTask(taskId, base = sbase()) {
  if (!base || !confirm(t("task.stopConfirm"))) return;
  try {
    await api("POST", `${base}/tasks/${taskId}/cancel`);
    toast(t("task.stopRequested"));
    loadActive();
  } catch (ex) { report(ex); }
}

// ---------------------------------------------------------------- 数据

async function loadChanges() {
  const base = sbase();
  if (!base) return;
  try {
    const c = await api("GET", `${base}/changes`);
    if (sbase() === base) { state.changes = c; scheduleChrome(); }
  } catch { /* 下次再取 */ }
}

// 任务运行中按节点完成刷新改动，最多每 15 秒一次（读改动会动 git 的 index，别和 Agent 的命令抢）。
let lastChanges = 0;
function throttledChanges() {
  if (Date.now() - lastChanges < 15000) return;
  lastChanges = Date.now();
  loadChanges();
}

async function loadEnv() {
  const base = sbase();
  if (!base) return;
  try {
    const e = await api("GET", `${base}/env`);
    if (sbase() === base) { state.env = e; scheduleChrome(); }
  } catch { /* 下次再取 */ }
}

async function loadActive() {
  try {
    state.active = await api("GET", "/api/tasks/active");
    renderSide();
    scheduleChrome();
  } catch { /* 下次再取 */ }
}

async function loadDetail(t) {
  const base = sbase();
  if (!base) return;
  try {
    const data = await api("GET", `${base}/tasks/${t.id}`);
    if (sbase() !== base) return;
    const rt = data.runtime || {};
    const ts = rt.taskState && rt.taskState.value ? rt.taskState.value : null;
    const rounds = ts && Array.isArray(ts.rounds) ? ts.rounds : [];
    state.detail.set(t.id, {
      state: ts,
      usage: rt.usage || null,
      roundSeq: rt.roundSeq || 0,
      cost: rounds.length ? rounds.reduce((s, r) => s + num(r.cost_usd), 0) : undefined,
    });
    const rec = state.tasks.find((x) => x.id === t.id);
    if (rec && data.task) Object.assign(rec, data.task, { status: TERMINAL.has(rec.status) ? rec.status : data.task.status });
    scheduleChrome();
  } catch { /* 下次再取 */ }
}

async function refreshTasks() {
  const base = sbase();
  if (!base) return;
  try {
    const list = await api("GET", `${base}/tasks`);
    if (sbase() !== base) return;
    const byId = new Map(list.map((t) => [t.id, t]));
    for (const t of state.tasks) {
      const n = byId.get(t.id);
      if (n) Object.assign(t, n, { status: TERMINAL.has(t.status) && !TERMINAL.has(n.status) ? t.status : n.status });
    }
    scheduleChrome();
  } catch { /* 下次再取 */ }
}

// ---------------------------------------------------------------- 输入框

// 输入框是一张卡片：上沿是运行状态或待确认的问题，中间是输入与发送；沙箱、模型倾向与用量放在卡片外的下方。
function dock() {
  ui.next = h("div", { class: "next-steps" });
  ui.cStatus = h("div", { class: "hidden" });
  ui.cAsk = h("div", { class: "hidden" });
  ui.cRight = h("div", { class: "c-right" });
  ui.cOpts = h("div", { class: "hidden" });
  ui.input = h("textarea", { class: "c-input", rows: 1, id: "composer", "aria-label": t("composer.aria") });
  ui.input.addEventListener("input", () => { autosize(); refreshSend(); });
  ui.input.addEventListener("keydown", onComposerKey);
  ui.composer = h("div", { class: "composer" }, ui.cStatus, ui.cAsk, h("div", { class: "c-row" }, ui.input, ui.cRight), ui.cOpts);
  ui.tools = h("div", { class: "c-tools" });
  ui.barKey = "";
  return h("div", { class: "dock" }, h("div", { class: "dock-inner" }, ui.next, ui.composer, ui.tools));
}

function autosize() {
  if (!ui.input) return;
  ui.input.style.height = "auto";
  ui.input.style.height = Math.min(ui.input.scrollHeight, 260) + "px";
}

function refreshSend() {
  if (ui.send) ui.send.disabled = !ui.input.value.trim() || state.sending;
}

function onComposerKey(e) {
  if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
  e.preventDefault();
  const ph = phase();
  if (ph.mode === "waiting") {
    // 等确认时 Enter 只在有补充说明、且有“带说明重新规划”这样的选项时生效。
    const guided = ph.ask.options.find((o) => /guidance|replan/i.test(o.id));
    if (guided && ui.input.value.trim()) reply(ph.current, ph.ask, guided.id);
    return;
  }
  submit();
}

function updateComposer(force = false) {
  if (!ui.composer || !state.project) return;
  const ph = phase();
  ui.composer.className = "composer" + (ph.mode === "waiting" ? " waiting" : "");

  if (ph.mode === "running" || ph.mode === "waiting") {
    const info = liveInfo(ph.current);
    ui.cStatus.className = "c-status";
    ui.cStatus.replaceChildren(...[
      ph.mode === "running" ? h("span", { class: "spinner" }) : icon("alert", "sm"),
      h("span", { class: "grow ellipsis" }, info.round ? t("composer.round", { n: info.round }) : "", h("span", { class: "what" }, ph.mode === "waiting" ? t("composer.waiting") : info.now)),
      ph.queued.length ? h("span", { class: "chip" }, t("composer.queued", { n: ph.queued.length })) : null,
      h("span", { class: "when", id: "c-elapsed" }, info.elapsed ? fmtTimer(info.elapsed) : ""),
      h("button", { class: "btn xs", title: t("composer.stopTitle"), onclick: () => cancelTask(ph.current.id) }, icon("stop", "xs"), t("common.stop")),
    ].filter(Boolean));
  } else {
    ui.cStatus.className = "hidden";
    ui.cStatus.replaceChildren();
  }

  if (ph.mode === "waiting") {
    ui.cAsk.className = "c-ask";
    ui.cAsk.replaceChildren(h("div", { class: "h" }, icon("alert", "xs"), t("ask.title")), h("div", { class: "q" }, ph.ask.question));
  } else {
    ui.cAsk.className = "hidden";
    ui.cAsk.replaceChildren();
  }

  ui.input.placeholder = ["empty", "done", "failed", "running", "waiting"].includes(ph.mode) ? t("composer.ph." + ph.mode) : "";

  // 工具条与下一步只在内容变了时重画，免得打断打开着的菜单。
  const m = meter();
  const key = [ph.mode, ph.ask && ph.ask.qid, state.settings.sandbox, state.settings.model, meterText(m),
    state.changes ? state.changes.files.length : -1, ph.mode === "failed" || ph.mode === "done" ? ph.last.id : "",
    state.env && state.env.sandbox ? state.env.sandbox.mode : ""].join("|");
  if (!force && key !== ui.barKey && pop && ui.tools.contains(pop.anchor)) {
    ui.barDirty = true; // 菜单开着：等它关上再重画
  } else if (force || key !== ui.barKey) {
    ui.barKey = key;
    ui.barDirty = false;
    if (ph.mode === "waiting") {
      // 等确认时，发送按钮换成问题给的选项。
      ui.cRight.replaceChildren();
      ui.cOpts.className = "c-opts";
      ui.cOpts.replaceChildren(...ph.ask.options.map((o) => h("button", {
        class: "btn sm" + (/approve|yes|continue/i.test(o.id) ? " primary" : ""),
        onclick: () => reply(ph.current, ph.ask, o.id),
      }, optionLabel(o.label))));
    } else {
      ui.cRight.replaceChildren(sendButton(ph));
      ui.cOpts.className = "hidden";
      ui.cOpts.replaceChildren();
    }
    ui.tools.replaceChildren(sandboxTool(), modelTool(), meterTool(m), h("span", { class: "grow" }), h("div", { class: "c-hint" }, hint(ph)));
    ui.next.replaceChildren(...nextSteps(ph));
  }
  refreshSend();
}

function sendButton(ph) {
  const queue = ph.mode === "running";
  ui.send = h("button", { class: "send" + (queue ? " queue" : ""), title: queue ? t("composer.queueTitle") : t("composer.sendTitle"), onclick: () => submit() },
    icon(queue ? "queue" : "send", "sm"), queue ? t("composer.queue") : null);
  return ui.send;
}

function hint(ph) {
  const k = (key, text) => h("span", {}, h("span", { class: "kbd" }, key), " ", text);
  if (ph.mode === "running") return [k("Esc", t("hint.stop")), k("Enter", t("hint.queue")), k("Shift+Enter", t("hint.newline"))];
  if (ph.mode === "waiting") return [];
  return [k("Enter", t("hint.send")), k("Shift+Enter", t("hint.newline"))];
}

const OPTION_LABELS = {
  approve: "opt.approve", reject: "opt.reject", continue_as_is: "opt.continue", replan_with_guidance: "opt.replanGuided", stop_and_wrap_up: "opt.wrapUp",
  continue: "opt.continue", replan: "opt.replan", stop: "opt.wrapUp",
};

function optionLabel(label) {
  return OPTION_LABELS[label] ? t(OPTION_LABELS[label]) : label;
}

function nextSteps(ph) {
  const chip = (ic, label, onclick, primary) => h("button", { class: "next-step" + (primary ? " primary" : ""), onclick }, icon(ic, "xs"), label);
  if (ph.mode === "done") {
    const n = state.changes ? state.changes.files.length : 0;
    const p = state.project;
    const s = state.session || {};
    const out = [];
    if (n) out.push(chip("diff", t("next.review", { n }), () => openDrawer("changes"), true));
    if (n && p.repo && connection(p.git_provider)) {
      const rn = requestName(p);
      out.push(h("button", { class: "next-step", onclick: () => openDrawer("changes", { focus: "publish" }) }, badge(p.git_provider, "xs"),
        s.pr_number ? t("publish.pushTo", { rn, n: s.pr_number }) : t("publish.pushDraft", { rn })));
    }
    else if (n) out.push(chip("commit", t("next.commit"), () => openDrawer("changes", { focus: "commit" })));
    out.push(chip("edit", t("next.refine"), () => setComposer(t("next.refinePrompt"))));
    out.push(chip("fileText", t("next.explain"), () => setComposer(t("next.explainPrompt"))));
    return out;
  }
  if (ph.mode === "failed") {
    const last = ph.last;
    return [
      chip("refresh", t("next.retry"), () => submit(last.message), true),
      chip("edit", t("next.edit"), () => setComposer(last.message)),
      chip("list", t("next.where"), () => { state.open.add(last.id); state.closed.delete(last.id); renderTurn(last.id); scrollToTurn(last.id); }),
    ];
  }
  return [];
}

function setSetting(key, value) {
  state.settings[key] = value;
  ls.set("cw_" + key, value);
  updateComposer(true);
  renderStatus();
}

function sandboxDesc(s) {
  if (s.id !== "write_reversible") return s.desc;
  const mode = state.env && state.env.sandbox && state.env.sandbox.mode;
  return mode === "host" ? t("sandbox.hostDesc") : s.desc;
}

function sandboxTool() {
  const cur = SANDBOX.find((s) => s.id === state.settings.sandbox) || SANDBOX[1];
  const can = state.config.sandbox_control;
  return h("button", {
    class: "tool" + (cur.id === "read" ? " read-only" : ""),
    disabled: !can,
    title: can ? t("sandbox.title") : t("sandbox.fixed"),
    onclick: (e) => openPop(e.currentTarget, (close) => [
      h("div", { class: "pop-title" }, t("sandbox.heading")),
      ...SANDBOX.map((s) => popItem(s.icon, s.label, sandboxDesc(s), state.settings.sandbox === s.id, () => { setSetting("sandbox", s.id); close(); }, s.disabled)),
    ]),
  }, icon(cur.icon, "sm"), h("span", { class: "v" }, cur.label), icon("chevDown", "caret"));
}

function modelTool() {
  const cur = MODELS.find((m) => m.id === state.settings.model) || MODELS[1];
  return h("button", {
    class: "tool",
    title: t("model.title"),
    onclick: (e) => openPop(e.currentTarget, (close) => [
      h("div", { class: "pop-title" }, t("model.heading")),
      ...MODELS.map((m) => popItem(m.icon, m.label, m.desc, state.settings.model === m.id, () => { setSetting("model", m.id); close(); })),
    ]),
  }, icon(cur.icon, "sm"), h("span", { class: "v" }, cur.label), icon("chevDown", "caret"));
}

function meter() {
  let cost = 0, input = 0, output = 0, cacheRead = 0, hasUsage = false;
  for (const t of state.tasks) {
    cost += taskCost(t);
    const d = state.detail.get(t.id);
    const u = (d && d.usage) || t.usage;
    if (u && typeof u === "object") {
      hasUsage = true;
      input += num(pick(u, "inputTokens", "input_tokens"));
      output += num(pick(u, "outputTokens", "output_tokens"));
      cacheRead += num(pick(u, "cacheReadTokens", "cache_read_tokens"));
    }
  }
  const cur = currentTask();
  return { cost, input, output, cacheRead, hasUsage, count: state.tasks.length, current: cur ? taskCost(cur) : null };
}

function meterText(m) {
  return m.hasUsage ? t("meter.text", { tokens: fmtTokens(m.input + m.output), cost: fmtUSD(m.cost) }) : fmtUSD(m.cost);
}

function meterTool(m) {
  return h("button", {
    class: "tool",
    title: t("meter.title"),
    onclick: (e) => openPop(e.currentTarget, () => {
      const mm = meter();
      const row = (k, v) => [h("span", { class: "k" }, k), h("span", { class: "v" }, v)];
      return [
        h("div", { class: "pop-title" }, t("meter.title")),
        h("div", { class: "meter-grid" },
          row(t("meter.tasks"), String(mm.count)),
          row(t("meter.cost"), fmtUSD(mm.cost)),
          mm.current !== null ? row(t("meter.current"), fmtUSD(mm.current)) : null,
          mm.hasUsage ? [row(t("meter.input"), fmtTokens(mm.input)), row(t("meter.output"), fmtTokens(mm.output)), row(t("meter.cache"), fmtTokens(mm.cacheRead))] : null),
      ];
    }),
  }, icon("gauge", "sm"), h("span", { class: "v keep" }, meterText(m)));
}

// submit 发出一条任务。草稿时先开会话（复制工作副本，标题取这条消息的第一行），再提交。
async function submit(text) {
  const message = (text ?? (ui.input ? ui.input.value : "")).trim();
  if (!message || state.sending || !state.project) return;
  state.sending = true;
  refreshSend();
  try {
    if (!state.session) {
      const p = state.project;
      const sess = await api("POST", `/api/projects/${p.id}/sessions`, { title: message });
      state.sessions.unshift(sess);
      state.session = sess;
      setHash(`#/w/${p.id}/s/${sess.id}`);
      renderApp();
      loadEnv();
    }
    const base = sbase();
    const t = await api("POST", `${base}/tasks`, { message, sandbox: state.settings.sandbox, model: state.settings.model });
    if (sbase() !== base) return;
    state.tasks.push(t);
    // 这个会话成了最近活动的：排到最前。
    const touched = { ...state.session, updated_at: t.created_at };
    state.sessions = [touched, ...state.sessions.filter((x) => !(x.project_id === touched.project_id && x.id === touched.id))];
    state.session = touched;
    if (text === undefined && ui.input) { ui.input.value = ""; autosize(); }
    renderThread(true);
    renderTopbar();
    loadRun(t);
    loadActive();
    scheduleChrome();
  } catch (ex) {
    report(ex);
    if (text === undefined && ui.input && !ui.input.value) setComposer(message);
  } finally {
    state.sending = false;
    refreshSend();
    if (ui.input) ui.input.focus();
  }
}

async function reply(task, ask, option) {
  try {
    await api("POST", `${sbase()}/tasks/${task.id}/reply`, { question_id: ask.qid, option, guidance: ui.input.value.trim() });
    state.replied.add(ask.qid);
    ui.input.value = "";
    autosize();
    renderTurn(task.id);
    updateComposer(true);
    toast(t("ask.sent"));
  } catch (ex) { report(ex); }
}

function setComposer(text) {
  if (!ui.input) return;
  ui.input.value = text;
  autosize();
  refreshSend();
  ui.input.focus();
  ui.input.setSelectionRange(text.length, text.length);
}

// ---------------------------------------------------------------- 右栏

function renderStatus() {
  if (!ui.statusBody || !inSession() || !state.statusOpen) return;
  ui.statusBody.replaceChildren(
    section("env", "server", t("sp.env"), envSummary(), envBody),
    section("context", "layers", t("sp.context"), contextSummary(), contextBody),
    section("output", "fileText", t("sp.output"), outputSummary(), outputBody),
    section("changes", "diff", t("sp.changes"), changesSummary(), changesBody),
    section("background", "activity", t("sp.background"), backgroundSummary(), backgroundBody),
  );
}

function section(key, ic, title, summary, body) {
  const open = state.sections.has(key);
  return h("section", { class: "sp" + (open ? " open" : "") },
    h("button", { class: "sp-head", onclick: () => {
      if (state.sections.has(key)) state.sections.delete(key); else state.sections.add(key);
      ls.set("cw_sections", [...state.sections].join(","));
      renderStatus();
    } }, icon(ic, "sm"), h("span", { class: "t" }, title), h("span", { class: "sum" }, summary), icon("chevron", "xs chev")),
    open ? h("div", { class: "sp-body" }, body()) : null,
  );
}

function kv(rows) {
  return h("div", { class: "kv" }, rows.filter(Boolean).map(([k, v]) => [h("div", { class: "k" }, k), h("div", { class: "v" }, v)]));
}

function envSummary() {
  const e = state.env;
  if (!e) return h("span", { class: "spinner" });
  return e.runtime && e.runtime.ok ? [h("span", { class: "dot ok" }), t("env.connected")] : [h("span", { class: "dot err" }), t("env.down")];
}

function envBody() {
  const e = state.env;
  if (!e) return h("div", { class: "empty-line" }, t("common.loading"));
  const p = state.project;
  const s = state.session;
  const sb = e.sandbox || {};
  const git = e.git || {};
  const se = e.session || {};
  const level = SANDBOX.find((x) => x.id === state.settings.sandbox) || SANDBOX[1];
  const sandboxText = sb.mode === "docker" ? t("env.docker", { image: sb.image || t("env.defaultImage") }) : sb.mode === "host" ? t("env.host") : t("env.unknown");
  const rsid = se.runtime_session_id || "";
  return [
    kv([
      ["Runtime", [h("span", { class: "dot " + (e.runtime && e.runtime.ok ? "ok" : "err") }), e.runtime && e.runtime.ok ? t("env.taskOk") : t("env.taskDown")]],
      [t("env.sandbox"), sandboxText],
      [t("env.permission"), [h("span", { class: "chip" + (level.id === "read" ? " info" : "") }, icon(level.icon, "xs"), level.label), h("span", { class: "muted small" }, t("env.nextTask"))]],
      [t("env.copy"), se.own_copy ? [h("span", {}, t("env.ownCopy")), h("span", { class: "mono small muted", title: se.workspace_id }, se.workspace_id)] : t("env.sharedCopy")],
      [t("env.branch"), git.branch ? [h("span", { class: "chip mono" }, icon("branch", "xs"), git.branch), git.head ? h("span", { class: "mono small muted" }, git.head.slice(0, 7)) : null] : "—"],
      git.subject ? [t("env.lastCommit"), h("span", { class: "muted" }, git.subject)] : null,
      [t("sp.changes"), git.dirty ? t("env.dirty", { n: git.dirty }) : t("env.clean")],
      [t("env.workspace"), [h("span", {}, p.name), p.repo ? null : h("span", { class: "muted small" }, sourceLabel(p))]],
      p.repo ? [t("env.repo"), [badge(p.git_provider, "xs"), h("a", { href: repoURL(p), target: "_blank", rel: "noopener" }, p.repo),
        connection(p.git_provider) ? null : h("span", { class: "muted small" }, t("env.notConnected", { name: providerInfo(p.git_provider).name }))]] : null,
      rsid ? ["Session", h("span", { class: "mono small", title: rsid }, rsid.slice(0, 16) + "…")] : null,
    ]),
    h("div", { class: "sp-actions" },
      h("button", { class: "btn xs", onclick: () => openDrawer("files") }, icon("folder", "xs"), t("env.browse")),
      s.branch ? h("button", { class: "btn xs", title: t("env.resetTitle", { base: p.base_branch }), onclick: resetBranch }, icon("refresh", "xs"), t("env.reset", { base: p.base_branch })) : null,
      h("button", { class: "btn ghost xs", title: t("common.reload"), onclick: loadEnv }, icon("refresh", "xs"))),
  ];
}

function contextSummary() {
  const task = focusTask();
  if (!task) return "—";
  const d = state.detail.get(task.id);
  const rounds = d && d.state && Array.isArray(d.state.rounds) ? d.state.rounds.length : task.rounds || 0;
  const cost = taskCost(task);
  return [rounds ? t("rounds", { n: rounds }) : null, cost ? fmtUSD(cost) : null].filter(Boolean).join(" · ") || "—";
}

function contextBody() {
  const task = focusTask();
  if (!task) return h("div", { class: "empty-line" }, t("ctx.empty"));
  const d = state.detail.get(task.id);
  const s = d && d.state;
  if (!s) return [h("div", { class: "goal" }, task.message), h("div", { class: "empty-line" }, t("ctx.loading"))];
  const c = s.constraints || {};
  const chips = [];
  if (c.effect_ceiling) {
    const lv = SANDBOX.find((x) => x.id === c.effect_ceiling);
    chips.push(h("span", { class: "chip" + (c.effect_ceiling === "read" ? " info" : "") }, lv ? lv.label : c.effect_ceiling));
  }
  if (c.budget_usd) chips.push(h("span", { class: "chip" }, t("ctx.budget", { cost: fmtUSD(num(c.budget_usd)) })));
  if (c.model_preference) chips.push(h("span", { class: "chip" }, t("ctx.model", { label: modelLabel(c.model_preference) })));
  for (const [k, v] of Object.entries(c)) {
    if (!["effect_ceiling", "budget_usd", "model_preference"].includes(k)) chips.push(h("span", { class: "chip mono" }, `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`));
  }
  const list = (title, items, max = 6) => {
    if (!Array.isArray(items) || !items.length) return null;
    const shown = items.slice(-max);
    return [h("div", { class: "sp-sub" }, `${title} · ${items.length}`), h("ul", { class: "sp-list" }, shown.map((x) => h("li", {}, String(x)))),
      items.length > max ? h("div", { class: "empty-line" }, t("ctx.older", { n: items.length - max })) : null];
  };
  const rounds = Array.isArray(s.rounds) ? s.rounds : [];
  return [
    h("div", { class: "goal" }, s.goal || task.message),
    h("div", { class: "sp-sub" }, t("ctx.doneWhen")),
    h("div", { class: "muted" }, s.done_when || "—"),
    chips.length ? [h("div", { class: "sp-sub" }, t("ctx.constraints")), h("div", { class: "chip-row" }, chips)] : null,
    list(t("ctx.milestones"), s.milestones),
    list(t("ctx.facts"), s.facts),
    list(t("ctx.assumptions"), s.assumptions),
    list(t("ctx.questions"), s.open_questions),
    rounds.length ? [h("div", { class: "sp-sub" }, t("ctx.rounds")), h("div", { class: "rounds" }, rounds.map((r) => h("div", { class: "round-row" },
      h("span", { class: "n" }, r.seq), h("span", { class: "g ellipsis", title: r.stage_goal }, r.stage_goal || "—"),
      r.verdict ? verdictChip(r.verdict) : r.status && r.status !== "completed" ? h("span", { class: "spinner" }) : null,
      r.cost_usd ? h("span", { class: "c" }, fmtUSD(num(r.cost_usd))) : null)))] : null,
  ];
}

function outputSummary() {
  const f = lastFinished();
  return f ? statusChip(f.result.status) : "—";
}

function outputBody() {
  const f = lastFinished();
  if (!f) return h("div", { class: "empty-line" }, t("output.empty"));
  const text = deliverableText(f.result);
  const d = state.detail.get(f.task.id);
  const artifacts = d && d.state && Array.isArray(d.state.artifacts) ? d.state.artifacts : [];
  return [
    h("div", { class: "muted small ellipsis" }, f.task.message),
    text ? h("div", { class: "output-preview" }, h("div", { class: "answer" }, md(text))) : h("div", { class: "empty-line" }, t("output.noText")),
    artifacts.length ? [h("div", { class: "sp-sub" }, t("output.artifacts", { n: artifacts.length })), h("ul", { class: "sp-list" }, artifacts.map((a) => h("li", { class: "mono small" }, a)))] : null,
    h("div", { class: "sp-actions" },
      h("button", { class: "btn xs", onclick: () => scrollToTurn(f.task.id) }, icon("chevron", "xs"), t("output.inThread")),
      text ? h("button", { class: "btn ghost xs", onclick: () => copy(text) }, icon("copy", "xs"), t("common.copy")) : null),
  ];
}

function changeStats() {
  const files = state.changes ? state.changes.files : [];
  return { files, added: files.reduce((n, f) => n + f.added, 0), deleted: files.reduce((n, f) => n + f.deleted, 0) };
}

function changesSummary() {
  if (!state.changes) return h("span", { class: "spinner" });
  const { files, added, deleted } = changeStats();
  if (!files.length) return t("env.clean");
  return [h("span", { class: "stat-add" }, `+${added}`), h("span", { class: "stat-del" }, `−${deleted}`), t("files.count", { n: files.length })];
}

function changesBody() {
  if (!state.changes) return h("div", { class: "empty-line" }, t("common.loading"));
  const { files } = changeStats();
  const p = state.project;
  const s = state.session;
  const max = 12;
  return [
    files.length
      ? h("div", {}, files.slice(0, max).map((f) => h("button", { class: "file-row", title: f.path, onclick: () => openDrawer("changes", { focus: f.path }) },
        icon("file", "xs"), h("span", { class: "p ellipsis" }, f.path),
        f.binary ? h("span", { class: "muted small" }, t("changes.binary")) : [h("span", { class: "stat-add" }, `+${f.added}`), h("span", { class: "stat-del" }, `−${f.deleted}`)])),
      files.length > max ? h("div", { class: "empty-line" }, t("changes.more", { n: files.length - max })) : null)
      : h("div", { class: "empty-line" }, t("changes.none")),
    h("div", { class: "sp-actions" },
      files.length ? h("button", { class: "btn primary xs", onclick: () => openDrawer("changes") }, icon("diff", "xs"), t("changes.reviewCommit")) : null,
      files.length && p.repo && connection(p.git_provider) ? h("button", { class: "btn xs", onclick: () => openDrawer("changes", { focus: "publish" }) },
        badge(p.git_provider, "xs"), s.pr_number ? t("publish.pushTo", { rn: requestName(p), n: s.pr_number }) : t("publish.pushOpen", { rn: requestName(p) })) : null,
      files.length ? h("a", { class: "btn xs", href: `${sbase()}/patch`, download: `${s.workspace_id}.patch` }, icon("download", "xs"), t("changes.patch")) : null,
      h("button", { class: "btn ghost xs", onclick: () => openDrawer("history") }, icon("history", "xs"), t("drawer.history"))),
  ];
}

function backgroundSummary() {
  const n = state.active.length;
  return n ? [h("span", { class: "spinner" }), t("bg.count", { n })] : t("bg.idle");
}

function backgroundBody() {
  if (!state.active.length) return h("div", { class: "empty-line" }, t("activity.empty"));
  return h("div", {}, state.active.map((a) => h("div", { class: "bg-row", title: t("bg.open"), onclick: () => jumpTo(a.project_id, a.session_id, a.task.id) },
    a.task.status === "TASK_STATUS_RUNNING" ? h("span", { class: "spinner" }) : icon("clock", "sm"),
    h("div", { class: "grow" }, h("div", { class: "t" }, a.task.message),
      h("div", { class: "m" }, `${a.project_name} / ${a.session_title || t("session.untitled")} · ${(STATUS[a.task.status] || [a.task.status])[0]} · ${fmtAgo(a.task.created_at)}`)),
    h("button", { class: "btn ghost xs icon-only", title: t("common.stop"), onclick: (e) => { e.stopPropagation(); cancelTask(a.task.id, sessionURL(a.project_id, a.session_id)); } }, icon("x", "xs")),
  )));
}

async function jumpTo(projectId, sessionId, taskId) {
  if (!inSession() || state.project.id !== projectId || state.session.id !== sessionId) await openSession(projectId, sessionId);
  requestAnimationFrame(() => scrollToTurn(taskId));
}

// ---------------------------------------------------------------- 详情抽屉

function openDrawer(tab, opts = {}) {
  if (!sbase()) return;
  closeDrawer();
  closePop();
  state.drawer = { tab, focus: opts.focus || null };
  const scrim = h("div", { class: "scrim", onclick: closeDrawer });
  const el = h("div", { class: "drawer", role: "dialog", "aria-label": t("drawer.title") });
  ui.drawer = { scrim, el };
  document.body.append(scrim, el);
  renderDrawer();
}

function closeDrawer() {
  if (!ui.drawer) return;
  ui.drawer.scrim.remove();
  ui.drawer.el.remove();
  ui.drawer = null;
  state.drawer = null;
}

function renderDrawer() {
  const d = state.drawer;
  if (!d || !ui.drawer) return;
  const n = state.changes ? state.changes.files.length : 0;
  const tab = (key, ic, label, extra) => h("button", { class: "dtab" + (d.tab === key ? " active" : ""), onclick: () => { d.tab = key; d.focus = null; renderDrawer(); } }, icon(ic, "sm"), label, extra);
  const body = h("div", { class: "drawer-body" });
  ui.drawer.el.replaceChildren(
    h("div", { class: "drawer-head" },
      h("span", { class: "h ellipsis", title: state.project.name }, state.session.title || t("session.untitled")),
      tab("changes", "diff", t("sp.changes"), n ? h("span", { class: "n" }, n) : null),
      tab("files", "folder", t("drawer.files")),
      tab("history", "history", t("drawer.history")),
      h("span", { class: "grow" }),
      h("button", { class: "btn ghost sm icon-only", title: t("drawer.close"), onclick: closeDrawer }, icon("x", "sm"))),
    body,
  );
  if (d.tab === "changes") drawChanges(body, d.focus);
  else if (d.tab === "files") loadFiles(body, state.filesPath);
  else loadHistory(body);
}

async function drawChanges(body, focus) {
  body.replaceChildren(h("div", { class: "row muted" }, h("span", { class: "spinner" }), t("changes.loading")));
  let data;
  try {
    data = await api("GET", `${sbase()}/changes`);
    state.changes = data;
    scheduleChrome();
  } catch (ex) {
    body.replaceChildren(h("div", { class: "error-box" }, ex.message));
    return;
  }
  const busy = !!currentTask();
  const parts = [];
  let commitInput = null;
  if (!data.files.length) {
    parts.push(h("div", { class: "card" },
      h("div", { class: "card-title" }, icon("check", "sm"), t("changes.cleanTitle")),
      h("div", { class: "muted" }, t("changes.branch"), h("span", { class: "mono" }, data.branch || "—"), " · HEAD ", h("span", { class: "mono" }, (data.head || "").slice(0, 10)))));
  } else {
    const added = data.files.reduce((n, f) => n + f.added, 0);
    const deleted = data.files.reduce((n, f) => n + f.deleted, 0);
    commitInput = h("input", { class: "field", placeholder: t("commit.ph"), value: lastMessage() });
    const commit = h("button", { class: "btn primary sm", disabled: busy, onclick: async () => {
      commit.disabled = true;
      try {
        const r = await api("POST", `${sbase()}/commit`, { message: commitInput.value });
        toast(t("commit.done", { hash: r.head.slice(0, 8) }));
        await loadChanges();
        loadEnv();
        renderDrawer();
      } catch (ex) { commit.disabled = false; report(ex); }
    } }, icon("commit", "sm"), t("commit.btn"));
    parts.push(h("div", { class: "card" },
      h("div", { class: "card-title" }, t("changes.title", { n: data.files.length }), h("span", { class: "stat-add" }, `+${added}`), h("span", { class: "stat-del" }, `−${deleted}`),
        busy ? h("span", { class: "chip run" }, h("span", { class: "spinner" }), t("changes.busy")) : null),
      h("div", { class: "actions" }, h("div", { class: "grow" }, commitInput), commit,
        h("a", { class: "btn sm", href: `${sbase()}/patch`, download: `${state.session.workspace_id}.patch` }, icon("download", "sm"), t("changes.patch")),
        h("button", { class: "btn sm danger", disabled: busy, onclick: discard }, icon("trash", "sm"), t("changes.discard"))),
    ));
  }
  const pub = publishCard(busy, data.files.length > 0);
  if (pub) parts.push(pub.card);
  parts.push(...renderDiff(data.diff));
  if (data.truncated) parts.push(h("div", { class: "muted" }, t("changes.truncated")));
  body.replaceChildren(...parts);
  if (focus === "commit" && commitInput) commitInput.focus();
  else if (focus === "publish" && pub) pub.title.focus();
  else if (focus) {
    const el = [...body.querySelectorAll(".diff-file")].find((x) => x.getAttribute("data-path") === focus);
    if (el) { el.scrollIntoView({ block: "start" }); el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 1600); }
  }
}

async function discard() {
  if (!confirm(t("changes.discardConfirm"))) return;
  try {
    await api("POST", `${sbase()}/discard`);
    toast(t("changes.discarded"));
    await loadChanges();
    loadEnv();
    renderDrawer();
  } catch (ex) { report(ex); }
}

function lastMessage() {
  // 默认提交说明：最近一条像样的任务描述（跳过“-”这类过短的）。
  const task = [...state.tasks].reverse().find((x) => x.message.trim().length > 3);
  return task ? task.message.trim().split("\n")[0].slice(0, 72) : "";
}

// publishCard 是“推送并开草稿 PR/MR”：一个会话一个 agent/* 分支、一个 PR（GitLab 叫 Merge Request）。
function publishCard(busy, dirty) {
  const p = state.project;
  const s = state.session;
  if (!p.repo) return null;
  const info = providerInfo(p.git_provider);
  const rn = requestName(p);
  if (!connection(p.git_provider)) {
    return {
      card: h("div", { class: "card" },
        h("div", { class: "card-title" }, badge(p.git_provider, "sm"), h("span", { class: "mono grow ellipsis" }, p.repo)),
        h("div", { class: "muted" }, t("publish.connectFirst", { name: info.name, rn })),
        info.configured ? h("div", { class: "actions" }, h("a", { class: "btn sm primary", href: `/api/auth/${p.git_provider}/login?link=1` }, t("account.connectTo", { name: info.name }))) : null),
      title: null,
    };
  }
  const title = h("input", { class: "field", placeholder: t("publish.titlePh", { rn }), value: s.title || lastMessage() });
  const out = h("div", {});
  const label = s.pr_number ? t("publish.pushTo", { rn, n: s.pr_number }) : t("publish.pushDraft", { rn });
  const push = h("button", { class: "btn primary sm", disabled: busy, onclick: async () => {
    push.disabled = true;
    push.lastChild.textContent = t("publish.pushing");
    try {
      const r = await api("POST", `${sbase()}/publish`, { title: title.value });
      setSession(r.session);
      toast(r.created ? t("publish.opened", { rn, n: r.pr_number }) : t("publish.pushed", { branch: r.branch }));
      renderTopbar();
      renderSide();
      await loadChanges();
      loadEnv();
      renderDrawer();
    } catch (ex) {
      push.disabled = false;
      push.lastChild.textContent = label;
      if (ex.message !== "unauthorized") out.replaceChildren(h("div", { class: "error-box" }, ex.message));
    }
  } }, badge(p.git_provider, "sm"), h("span", {}, label));
  const card = h("div", { class: "card" },
    h("div", { class: "card-title" }, badge(p.git_provider, "sm"), h("a", { class: "mono grow ellipsis", href: repoURL(p), target: "_blank", rel: "noopener" }, p.repo),
      s.pr_url ? h("a", { class: "chip ok", href: s.pr_url, target: "_blank", rel: "noopener" }, `${rn} #${s.pr_number}`, icon("external", "xs")) : null),
    h("div", { class: "muted" }, s.branch
      ? t(dirty ? "publish.updateDirty" : "publish.update", { branch: s.branch, rn })
      : t("publish.new", { base: p.base_branch || t("publish.baseBranch"), rn })),
    h("div", { class: "actions" }, h("div", { class: "grow" }, title), push),
    out,
  );
  return { card, title };
}

function renderDiff(text) {
  const files = [];
  let cur = null;
  let oldNo = 0, newNo = 0;
  for (const line of String(text || "").split("\n")) {
    if (line.startsWith("diff --git ")) {
      const m = line.match(/ b\/(.*)$/);
      cur = { path: m ? m[1] : line, rows: [], added: 0, deleted: 0 };
      files.push(cur);
      continue;
    }
    if (!cur) continue;
    if (/^(index |--- |\+\+\+ |new file mode|deleted file mode|similarity index|rename from|rename to|old mode|new mode|Binary files)/.test(line)) continue;
    const hunk = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (hunk) {
      oldNo = Number(hunk[1]); newNo = Number(hunk[2]);
      cur.rows.push(h("tr", { class: "hunk" }, h("td", { class: "ln" }), h("td", { class: "ln" }), h("td", { class: "code" }, line)));
    } else if (line.startsWith("+")) {
      cur.added++;
      cur.rows.push(h("tr", { class: "add" }, h("td", { class: "ln" }), h("td", { class: "ln" }, newNo++), h("td", { class: "code" }, line)));
    } else if (line.startsWith("-")) {
      cur.deleted++;
      cur.rows.push(h("tr", { class: "del" }, h("td", { class: "ln" }, oldNo++), h("td", { class: "ln" }), h("td", { class: "code" }, line)));
    } else if (line.startsWith(" ") || line === "") {
      if (line === "" && !cur.rows.length) continue;
      cur.rows.push(h("tr", {}, h("td", { class: "ln" }, oldNo++), h("td", { class: "ln" }, newNo++), h("td", { class: "code" }, line || " ")));
    }
  }
  return files.map((f) => {
    const box = h("div", { class: "diff-file", "data-path": f.path });
    box.append(
      h("button", { class: "diff-head", onclick: () => box.classList.toggle("collapsed") }, icon("file", "sm"), h("span", { class: "grow ellipsis" }, f.path),
        h("span", { class: "stat-add" }, `+${f.added}`), h("span", { class: "stat-del" }, `−${f.deleted}`)),
      h("div", { class: "diff-body" }, h("table", { class: "diff-table" }, h("tbody", {}, f.rows))),
    );
    return box;
  });
}

async function loadFiles(body, path) {
  state.filesPath = path;
  let entries;
  try {
    entries = await api("GET", `${sbase()}/tree?path=${encodeURIComponent(path)}`);
  } catch (ex) {
    body.replaceChildren(h("div", { class: "error-box" }, ex.message));
    return;
  }
  const parts = path ? path.split("/") : [];
  const viewer = h("div", {});
  body.replaceChildren(
    h("div", { class: "card" },
      h("div", { class: "crumbs" }, h("button", { onclick: () => loadFiles(body, "") }, state.session.workspace_id),
        parts.map((p, i) => [h("span", {}, "/"), h("button", { onclick: () => loadFiles(body, parts.slice(0, i + 1).join("/")) }, p)])),
      entries.length ? h("div", {}, entries.map((e) => h("button", {
        class: "tree-item",
        onclick: () => (e.dir ? loadFiles(body, join(path, e.name)) : showFile(viewer, join(path, e.name))),
      }, icon(e.dir ? "folder" : "file", "sm"), e.name, e.dir ? null : h("span", { class: "size" }, fmtSize(e.size))))) : h("div", { class: "muted" }, t("files.empty")),
    ),
    viewer,
  );
}

function join(a, b) {
  return a ? `${a}/${b}` : b;
}

async function showFile(viewer, path) {
  viewer.replaceChildren(h("div", { class: "row muted" }, h("span", { class: "spinner" }), t("common.loading")));
  try {
    const f = await api("GET", `${sbase()}/file?path=${encodeURIComponent(path)}`);
    const head = h("div", { class: "row" }, icon("file", "sm"), h("strong", { class: "mono grow ellipsis" }, path), h("span", { class: "muted" }, fmtSize(f.size || 0)));
    const content = f.binary ? h("div", { class: "muted" }, t("files.binary"))
      : f.too_large ? h("div", { class: "muted" }, t("files.tooLarge"))
        : h("pre", { class: "code-view" }, f.content.replace(/\n$/, "").split("\n").map((l) => h("span", {}, l || " ")));
    viewer.replaceChildren(h("div", { class: "col" }, head, content));
  } catch (ex) {
    viewer.replaceChildren(h("div", { class: "error-box" }, ex.message));
  }
}

async function loadHistory(body) {
  try {
    const commits = await api("GET", `${sbase()}/log`);
    body.replaceChildren(h("div", { class: "card" }, commits.length ? commits.map((c) => h("div", { class: "commit-row" },
      h("span", { class: "hash" }, c.hash.slice(0, 7)),
      h("div", { class: "grow" }, h("div", {}, c.subject), h("div", { class: "muted small" }, `${c.author} · ${fmtTime(c.date)}`)),
    )) : h("div", { class: "muted" }, t("history.none"))));
  } catch (ex) {
    body.replaceChildren(h("div", { class: "error-box" }, ex.message));
  }
}

async function resetBranch() {
  const p = state.project;
  const s = state.session;
  if (!confirm(t("env.resetConfirm", { base: p.base_branch, branch: s.branch }))) return;
  try {
    const r = await api("POST", `${sbase()}/reset-branch`);
    setSession(r.session);
    toast(t("env.resetDone", { base: p.base_branch }));
    renderTopbar();
    loadChanges();
    loadEnv();
  } catch (ex) { report(ex); }
}

// ---------------------------------------------------------------- 项目

function openNewProject(initial) {
  const conns = gitConnections();
  let mode = initial || (conns.length ? "git" : state.samples.length ? "sample" : "git");
  let sample = state.samples[0] ? state.samples[0].id : "";
  let provider = conns[0] ? conns[0].provider : "";
  const repoCache = {}; // 平台 → 仓库列表（"loading" 表示在读）
  let filter = "";
  const name = h("input", { class: "field", placeholder: t("new.name") });
  const gitUrl = h("input", { class: "field", placeholder: "https://github.com/owner/repo.git" });
  const branch = h("input", { class: "field", placeholder: t("new.branchPh") });
  const err = h("div", { class: "err-text" });
  const seg = h("div", { class: "seg" });
  const body = h("div", { class: "col" });
  const create = h("button", { class: "btn primary" }, t("new.create"));
  const cancel = h("button", { class: "btn ghost" }, t("common.cancel"));
  const dialog = h("dialog", {},
    h("div", { class: "d-head" }, h("h2", {}, t("new.title"))),
    h("div", { class: "d-body" }, seg, body, err),
    h("div", { class: "d-foot" }, cancel, create),
  );
  const repoList = h("div", { class: "pick-list" });
  const drawRepos = () => {
    const p = provider;
    if (!p) return repoList.replaceChildren();
    if (repoCache[p] === undefined) {
      repoCache[p] = "loading";
      api("GET", `/api/git/${p}/repos`).then((r) => { repoCache[p] = r; if (provider === p) drawRepos(); })
        .catch((ex) => { repoCache[p] = []; err.textContent = ex.message; if (provider === p) drawRepos(); });
    }
    const repos = repoCache[p];
    if (repos === "loading") return repoList.replaceChildren(h("div", { class: "row muted" }, h("span", { class: "spinner" }), t("new.loadingRepos", { name: providerInfo(p).name })));
    const q = filter.trim().toLowerCase();
    const shown = repos.filter((r) => !q || r.full_name.toLowerCase().includes(q)).slice(0, 40);
    repoList.replaceChildren(...(shown.length ? shown.map((r) => h("button", {
      class: "pick" + (gitUrl.value === r.clone_url ? " active" : ""),
      onclick: () => { gitUrl.value = r.clone_url; branch.value = r.default_branch || ""; name.value = r.full_name.split("/").pop(); drawRepos(); },
    }, badge(r.provider, "sm"), h("span", { class: "mono grow ellipsis" }, r.full_name),
      r.private ? h("span", { class: "chip" }, t("new.private")) : null,
      r.can_push ? null : h("span", { class: "chip warn" }, t("new.readOnly")))) : [h("div", { class: "muted" }, t("new.noRepos"))]));
  };
  const draw = () => {
    seg.replaceChildren(...[["git", "branch", conns.length ? t("new.myRepos") : t("new.gitUrl")], ["sample", "box", t("new.sample")], ["empty", "folder", t("source.empty")]].map(([k, ic, l]) =>
      h("button", { class: mode === k ? "active" : "", onclick: () => { mode = k; draw(); } }, icon(ic, "sm"), l)));
    const parts = [];
    if (mode === "git") {
      if (conns.length) {
        if (conns.length > 1) {
          parts.push(h("div", { class: "pv-tabs" }, conns.map((c) => h("button", {
            class: "pv-tab" + (c.provider === provider ? " active" : ""),
            onclick: () => { provider = c.provider; filter = ""; draw(); },
          }, badge(c.provider, "xs"), providerInfo(c.provider).name))));
        }
        const search = h("input", { class: "field", placeholder: t("new.searchPh", { name: providerInfo(provider).name }), value: filter });
        search.addEventListener("input", () => { filter = search.value; drawRepos(); });
        parts.push(h("div", { class: "col" }, search, repoList));
        drawRepos();
      }
      const connectable = providerList().filter((p) => p.git && p.configured && !connection(p.id));
      if (connectable.length) {
        parts.push(h("div", { class: "connect-row" }, h("span", { class: "muted small" }, conns.length ? t("new.alsoConnect") : t("new.connect")),
          connectable.map((p) => h("a", { class: "btn xs", href: `/api/auth/${p.id}/login?link=1` }, badge(p.id, "xs"), p.name))));
      }
      parts.push(h("div", {}, h("label", { class: "lbl" }, conns.length ? t("new.orUrl") : t("new.url")), gitUrl));
      parts.push(h("div", { class: "row" }, h("div", { class: "grow" }, h("label", { class: "lbl" }, t("new.name")), name), h("div", { class: "grow" }, h("label", { class: "lbl" }, t("env.branch")), branch)));
    } else if (mode === "sample") {
      parts.push(...state.samples.map((s) => h("div", {
        class: "sample-card" + (sample === s.id ? " active" : ""),
        onclick: () => { sample = s.id; name.value = s.name; draw(); },
      }, h("div", { class: "row" }, icon("box", "sm"), h("strong", {}, s.name)), h("div", { class: "muted" }, s.description))));
      if (!name.value && state.samples[0]) name.value = (state.samples.find((s) => s.id === sample) || state.samples[0]).name;
      parts.push(h("div", {}, h("label", { class: "lbl" }, t("new.name")), name));
    } else {
      parts.push(h("div", { class: "muted" }, t("new.emptyDesc")), h("div", {}, h("label", { class: "lbl" }, t("new.name")), name));
    }
    body.replaceChildren(...parts);
  };
  draw();
  cancel.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => dialog.remove());
  create.addEventListener("click", async () => {
    err.textContent = "";
    create.disabled = true;
    create.textContent = mode === "git" ? t("new.cloning") : t("new.creating");
    const req = { name: name.value || (mode === "git" ? (gitUrl.value.split("/").pop() || "").replace(/\.git$/, "") : "") };
    if (mode === "git") Object.assign(req, { git_url: gitUrl.value, branch: branch.value });
    if (mode === "sample") req.sample = sample;
    try {
      const p = await api("POST", "/api/projects", req);
      state.projects.unshift(p);
      dialog.close();
      openDraft(p.id);
      const s = mode === "sample" ? state.samples.find((x) => x.id === sample) : null;
      if (s) setComposer(s.prompt);
    } catch (ex) {
      if (ex.message !== "unauthorized") err.textContent = ex.message;
      create.disabled = false;
      create.textContent = t("new.create");
    }
  });
  document.body.append(dialog);
  dialog.showModal();
}

async function deleteProject(p) {
  const n = state.sessions.filter((s) => s.project_id === p.id).length;
  if (!confirm(t(n ? "ws.deleteConfirmSessions" : "ws.deleteConfirm", { name: p.name, n }))) return;
  try {
    await api("DELETE", `/api/projects/${p.id}`);
    state.projects = state.projects.filter((x) => x.id !== p.id);
    state.sessions = state.sessions.filter((s) => s.project_id !== p.id);
    toast(t("ws.deleted"));
    if (state.project && state.project.id === p.id) state.project = null;
    if (state.view === "workspaces") renderApp();
    else landing();
  } catch (ex) { report(ex); }
}

// ---------------------------------------------------------------- 计时、轮询与快捷键

let tick = 0;
setInterval(() => {
  tick++;
  // 进行中的任务：只改用时那段文字，不重画整轮（不打断正在输入的内容）。
  for (const t of state.tasks) {
    const run = state.runs.get(t.id);
    const el = document.getElementById("wl-" + t.id);
    if (!run || !el || !run.events.length) continue;
    el.textContent = workingText(workSteps(run.events).length, Date.now() - new Date(run.events[0].time));
  }
  const cur = inSession() && currentTask();
  const timer = document.getElementById("c-elapsed");
  if (timer && cur) {
    const run = state.runs.get(cur.id);
    if (run && run.events.length) timer.textContent = fmtTimer(Date.now() - new Date(run.events[0].time));
  }
  if (!state.me) return;
  if (cur && tick % 4 === 0) loadDetail(cur);
  if (tick % (state.active.length ? 5 : 30) === 0) loadActive();
}, 1000);

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "k" && state.me) {
    e.preventDefault();
    openSearch();
    return;
  }
  if (e.key === "Escape") {
    if (pop) { closePop(); return; }
    if (ui.drawer) { closeDrawer(); return; }
    if (document.querySelector("dialog[open]") || !state.me || !inSession()) return;
    const cur = currentTask();
    if (cur) { e.preventDefault(); cancelTask(cur.id); }
    return;
  }
  if (e.key === "/" && !isTyping(e.target) && ui.input && !ui.drawer && !document.querySelector("dialog[open]")) {
    e.preventDefault();
    ui.input.focus();
  }
});

document.addEventListener("mousedown", (e) => {
  if (pop && !pop.el.contains(e.target) && !pop.anchor.contains(e.target)) closePop();
});
window.addEventListener("resize", closePop);

// 浏览器的前进、后退：地址变了而且不是自己刚设的，就按地址重新打开。
window.addEventListener("hashchange", () => {
  if (!state.me || location.hash === routedHash) return;
  routedHash = location.hash;
  route();
});

async function boot() {
  document.documentElement.lang = locale;
  applyTheme();
  // OAuth 回调带回来的结果：连接成功（?linked=<平台>）或失败（?error=…）。进主界面会改地址，先记下来。
  const params = hashParams();
  const linked = params.get("linked");
  const failed = params.get("error");
  try { state.config = await api("GET", "/api/config"); } catch { /* 用默认 */ }
  try { state.me = await api("GET", "/api/me"); } catch { state.me = null; }
  if (state.me) {
    await enterApp().catch(report);
    if (linked) { toast(t("account.linked", { name: providerInfo(linked).name })); openAccount(); }
    if (failed) toast(failed, true);
  } else renderLogin();
}

boot();
