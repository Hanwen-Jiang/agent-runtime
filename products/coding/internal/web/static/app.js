// Coding Agent 网页（原生 JS，无构建步骤）。版式参照 Claude：纸面底色、衬线体的回答、卡片式输入框（docs/plan/coding-agent-mvp.md 第 7 节）。
// 三栏：左栏项目；中间对话（每个任务一轮：你的消息、工作过程、回答），输入框随阶段变化（空闲、运行中、等你确认、结束）；
// 右栏是环境、上下文、输出、改动与后台任务的状态；diff、文件与提交历史在宽抽屉里打开。DOM 一律用 textContent 构造，不拼 HTML。

const EVENT_TYPES = [
  "task_accepted", "round_started", "stage_started", "node_started", "node_finished",
  "thinking_delta", "tool_status", "token_delta", "human_input_required", "verdict", "task_finished",
];
const STATUS = {
  TASK_STATUS_QUEUED: ["排队中", "run"],
  TASK_STATUS_RUNNING: ["进行中", "run"],
  TASK_STATUS_DONE: ["已完成", "ok"],
  TASK_STATUS_ABORTED: ["已终止", "err"],
  TASK_STATUS_CANCELLED: ["已取消", "warn"],
  TASK_STATUS_FAILED: ["失败", "err"],
};
const TERMINAL = new Set(["TASK_STATUS_DONE", "TASK_STATUS_ABORTED", "TASK_STATUS_CANCELLED", "TASK_STATUS_FAILED"]);
const QUIET = new Set(["heartbeat", "token_delta", "thinking_delta"]);
const REPLAY_LIMIT = 12;

// 沙箱等级就是 Task 的 effect 上限；模型倾向交给 Planner 选 Tier（服务端见 runsettings.go）。
const SANDBOX = [
  { id: "read", icon: "eye", label: "只读", desc: "只读代码、回答问题；不改文件，也不跑命令" },
  { id: "write_reversible", icon: "edit", label: "工作区可写", desc: "改文件；命令在断网的一次性容器里跑" },
  { id: "full", icon: "globe", label: "完全访问", desc: "联网与对外的写操作 —— 暂不开放", disabled: true },
];
const MODELS = [
  { id: "economy", icon: "leaf", label: "省钱", desc: "Planner 尽量选便宜的模型档位" },
  { id: "balanced", icon: "gauge", label: "均衡", desc: "按任务难度选档位" },
  { id: "best", icon: "zap", label: "最强", desc: "关键步骤用最强的档位，花费更高" },
];
const VERDICTS = {
  done: ["完成", "ok"], continue: ["继续", "info"], repair: ["修复", "warn"], replan: ["重新规划", "warn"],
  rollback: ["回滚", "warn"], reroute: ["换路线", "warn"], escalate: ["升级", "warn"], abort: ["终止", "err"],
};
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
  projects: [],
  samples: [],
  project: null,
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
  navigator.clipboard.writeText(text).then(() => toast("已复制"), () => toast("复制失败", true));
}

function fmtTime(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleString([], { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function fmtClock(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function fmtDuration(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  return s % 60 ? `${m} 分 ${s % 60} 秒` : `${m} 分`;
}

function fmtTimer(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function fmtAgo(iso) {
  const ms = Date.now() - new Date(iso);
  if (isNaN(ms)) return "";
  if (ms < 60e3) return "刚刚";
  if (ms < 3600e3) return `${Math.floor(ms / 60e3)} 分钟前`;
  if (ms < 86400e3) return `${Math.floor(ms / 3600e3)} 小时前`;
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
  const [label, kind] = STATUS[status] || [status || "未知", ""];
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
    h("div", { class: "cb-head" }, h("span", { class: "grow" }, lang || "代码"),
      h("button", { class: "btn ghost xs", onclick: () => copy(code) }, icon("copy", "xs"), "复制")),
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
    renderLogin("登录已过期，请重新登录");
    throw new Error("unauthorized");
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) throw new Error((data && data.error) || `请求失败（${res.status}）`);
  return data;
}

function report(ex) {
  if (ex && ex.message !== "unauthorized") toast(ex.message, true);
}

// ---------------------------------------------------------------- 外观

function applyTheme() {
  const t = ls.get("cw_theme");
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
}

// ---------------------------------------------------------------- 弹出菜单

let pop = null;

function openPop(anchor, build) {
  if (pop && pop.anchor === anchor) return closePop();
  closePop();
  const el = h("div", { class: "pop", role: "menu" });
  append(el, build(closePop));
  document.body.append(el);
  const r = anchor.getBoundingClientRect();
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
  const tenant = h("input", { class: "field", placeholder: "例如 acme", autocomplete: "organization", value: ls.get("cw_tenant") || "" });
  const user = h("input", { class: "field", placeholder: "例如 alice", autocomplete: "username", value: ls.get("cw_user") || "" });
  const submit = h("button", { class: "btn primary block", type: "submit" }, "用开发账号进入");
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
    h("div", {}, h("label", { class: "lbl" }, "租户"), tenant),
    h("div", {}, h("label", { class: "lbl" }, "用户名"), user),
    submit,
    h("div", { class: "note" }, "开发登录不校验密码，只用于本机测试。"),
  );
  const github = state.config.github_login
    ? h("a", { class: "btn block github-btn", href: "/api/auth/github/login" }, icon("github"), "使用 GitHub 登录")
    : null;
  mount(h("div", { class: "login-page" },
    h("div", { class: "login-card" },
      h("div", { class: "hero" },
        h("div", { class: "mark big" }, logo("lg")),
        h("h1", {}, "Coding Agent"),
        h("p", {}, "读代码、改代码、跑测试；改动由你审阅后再推送。"),
      ),
      h("div", { class: "login-box" },
        github,
        github && state.config.dev_login ? h("div", { class: "sep" }, "或") : null,
        state.config.dev_login ? form : null,
        !github && !state.config.dev_login ? h("div", { class: "note" }, "服务端既没有配置 GitHub 登录，也没有打开开发登录。") : null,
        err,
      ),
    ),
  ));
}

async function logout() {
  await api("POST", "/api/logout").catch(() => {});
  state.me = null;
  state.project = null;
  renderLogin();
}

// ---------------------------------------------------------------- 主界面

async function enterApp() {
  [state.projects, state.samples] = await Promise.all([api("GET", "/api/projects"), api("GET", "/api/samples")]);
  loadActive();
  const wanted = decodeURIComponent((location.hash.match(/^#\/p\/([^/?]+)/) || [])[1] || "");
  const p = state.projects.find((x) => x.id === wanted) || state.projects[0];
  if (p) await openProject(p.id);
  else { state.project = null; renderApp(); }
}

function appClass() {
  return ["app",
    state.sideCollapsed && "side-collapsed",
    state.sideOpen && "side-open",
    !(state.statusOpen && state.project) && "status-closed",
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
  ui.dock = state.project ? dock() : null;
  if (!state.project) ui.composer = null;
  ui.statusBody = h("div", { class: "status-body" });
  ui.root = h("div", { class: appClass() },
    ui.side,
    h("main", { class: "main" }, ui.topbar, ui.scroll, ui.dock),
    h("aside", { class: "status" },
      h("div", { class: "status-head" }, h("span", { class: "h grow" }, "状态"),
        h("button", { class: "btn ghost sm icon-only", title: "收起状态栏", onclick: toggleStatus }, icon("x", "sm"))),
      ui.statusBody),
  );
  mount(ui.root);
  renderSide();
  renderTopbar();
  renderThread(true);
  if (state.project) { updateComposer(true); renderStatus(); }
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
  const live = state.project && currentTask();
  document.title = `${live ? "● " : ""}${state.project ? state.project.name + " · " : ""}Coding Agent`;
}

// 事件来得密：同一帧里只重画一次输入框、右栏与左栏。
let chromePending = false;
function scheduleChrome() {
  if (chromePending) return;
  chromePending = true;
  requestAnimationFrame(() => {
    chromePending = false;
    if (!state.me || !state.project) return;
    updateComposer();
    renderStatus();
    renderSide();
    updateTitle();
  });
}

// ---------------------------------------------------------------- 左栏

function projectIcon(p) {
  return p.github_repo ? "github" : p.source && p.source.startsWith("sample:") ? "box" : "folder";
}

function sourceLabel(p) {
  if (p.github_repo) return p.github_repo;
  if (!p.source || p.source === "empty") return "空项目";
  if (p.source.startsWith("sample:")) return "示例仓库";
  return p.source.replace(/^https:\/\//, "");
}

function renderSide() {
  if (!ui.side || !state.me) return;
  const me = state.me;
  const name = me.github ? me.github.login : me.user;
  const live = new Set(state.active.map((a) => a.project_id));
  if (state.project && currentTask()) live.add(state.project.id);
  ui.side.replaceChildren(
    h("div", { class: "side-head" },
      h("div", { class: "brand grow" }, h("span", { class: "mark" }, logo()), "Coding Agent"),
      h("button", { class: "btn ghost sm icon-only only-wide", title: "收起侧栏", onclick: () => { state.sideCollapsed = true; ls.set("cw_side", "0"); applyLayout(); renderTopbar(); } }, icon("panelLeft", "sm")),
      h("button", { class: "btn ghost sm icon-only only-narrow", title: "关闭", onclick: () => { state.sideOpen = false; applyLayout(); } }, icon("x", "sm"))),
    h("button", { class: "btn new-btn", onclick: () => openNewProject() }, h("span", { class: "plus" }, icon("plus", "xs")), "新建项目"),
    h("div", { class: "side-label" }, "项目"),
    h("nav", { class: "side-list" },
      state.projects.length ? state.projects.map((p) => h("button", {
        class: "side-item" + (state.project && state.project.id === p.id ? " active" : ""),
        title: p.name,
        onclick: () => { state.sideOpen = false; if (!state.project || state.project.id !== p.id) openProject(p.id); else applyLayout(); },
      }, icon(projectIcon(p), "sm"),
        h("div", { class: "grow" }, h("div", { class: "t ellipsis" }, p.name), h("div", { class: "m ellipsis" }, sourceLabel(p))),
        live.has(p.id) ? h("span", { class: "spinner", title: "有任务在跑" }) : null)) : h("div", { class: "side-empty" }, "还没有项目")),
    h("div", { class: "side-foot" },
      h("div", { class: "avatar" }, me.github && me.github.avatar_url ? h("img", { src: me.github.avatar_url, alt: "" }) : initials(name)),
      h("div", { class: "grow who" }, h("div", { class: "ellipsis" }, name), h("div", { class: "m ellipsis" }, `租户 ${me.tenant}`)),
      h("button", { class: "btn ghost sm icon-only", title: "外观与账号", onclick: (e) => openPop(e.currentTarget, userMenu) }, icon("more", "sm"))),
  );
}

function userMenu(close) {
  const theme = ls.get("cw_theme") || "system";
  const set = (t) => { ls.set("cw_theme", t); applyTheme(); close(); };
  return [
    h("div", { class: "pop-title" }, "外观"),
    popItem("monitor", "跟随系统", null, theme === "system", () => set("system")),
    popItem("sun", "浅色", null, theme === "light", () => set("light")),
    popItem("moon", "深色", null, theme === "dark", () => set("dark")),
    h("div", { class: "pop-sep" }),
    popItem("logout", "退出登录", null, false, () => { close(); logout(); }),
  ];
}

// ---------------------------------------------------------------- 顶栏

function renderTopbar() {
  if (!ui.topbar) return;
  const p = state.project;
  const parts = [
    h("button", { class: "btn ghost sm icon-only only-narrow", title: "项目", onclick: () => { state.sideOpen = true; applyLayout(); } }, icon("menu", "sm")),
    state.sideCollapsed ? h("button", { class: "btn ghost sm icon-only only-wide", title: "展开侧栏", onclick: () => { state.sideCollapsed = false; ls.set("cw_side", "1"); applyLayout(); renderTopbar(); } }, icon("panelLeft", "sm")) : null,
  ];
  if (p) {
    parts.push(h("span", { class: "title ellipsis" }, p.name));
    parts.push(h("div", { class: "chips" },
      p.github_repo ? h("a", { class: "chip", href: `https://github.com/${p.github_repo}`, target: "_blank", rel: "noopener" }, icon("github", "xs"), p.github_repo) : null,
      p.branch ? h("span", { class: "chip run mono" }, icon("branch", "xs"), p.branch) : p.base_branch ? h("span", { class: "chip mono" }, icon("branch", "xs"), p.base_branch) : null,
      p.pr_url ? h("a", { class: "chip ok", href: p.pr_url, target: "_blank", rel: "noopener" }, `PR #${p.pr_number}`, icon("external", "xs")) : null,
    ));
  }
  parts.push(h("span", { class: "grow" }));
  if (p) {
    parts.push(h("button", { class: "btn ghost sm icon-only", title: "改动、文件与提交历史", onclick: () => openDrawer("changes") }, icon("diff", "sm")));
    parts.push(h("button", { class: "btn ghost sm icon-only", title: state.statusOpen ? "收起状态栏" : "展开状态栏", onclick: toggleStatus }, icon("panelRight", "sm")));
  }
  ui.topbar.replaceChildren(...parts.filter(Boolean));
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

function pendingAsk(t) {
  const run = state.runs.get(t.id);
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
    return { qid, question: pick(v, "question", "prompt", "text", "summary", "message") || "Agent 请求确认后再继续。", options };
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

function liveInfo(t) {
  const run = state.runs.get(t.id);
  const events = run ? run.events : [];
  let round = 0;
  for (const e of events) if (e.type === "round_started") round = num(pick(e.value, "round_seq")) || round;
  const steps = workSteps(events);
  const last = steps[steps.length - 1];
  const now = last ? stepInfo(last).now : t.status === "TASK_STATUS_QUEUED" ? "排队中" : "正在连接…";
  const elapsed = events.length ? Date.now() - new Date(events[0].time) : 0;
  return { round, now, elapsed, steps: steps.length };
}

// ---------------------------------------------------------------- 对话

async function openProject(id) {
  closeStreams();
  closeDrawer();
  state.project = state.projects.find((p) => p.id === id) || null;
  state.tasks = [];
  state.runs = new Map();
  state.detail = new Map();
  state.changes = null;
  state.env = null;
  state.filesPath = "";
  location.hash = state.project ? `#/p/${id}` : "";
  if (state.project) {
    const tasks = await api("GET", `/api/projects/${id}/tasks`);
    state.tasks = tasks.slice().reverse();
  }
  renderApp();
  if (state.project) {
    loadChanges();
    loadEnv();
    const focus = focusTask();
    if (focus) loadDetail(focus);
    replayTasks();
  }
}

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
  return h("div", { class: "hero-empty" },
    h("div", { class: "mark big" }, logo("lg")),
    h("h2", {}, "从一个项目开始"),
    h("p", {}, "Agent 在项目的工作区里读代码、改代码，在隔离的沙箱里跑测试；改动由你审阅后提交或推送。"),
    h("div", { class: "suggestions" },
      sample ? suggestion("box", "试试示例仓库", sample.description, () => quickSample(sample)) : null,
      suggestion("github", state.me.github ? "从我的 GitHub 仓库开始" : "从 Git 地址克隆", state.me.github ? "选一个仓库，改完推到 agent/* 分支开草稿 PR" : "https 公开仓库", () => openNewProject("git")),
      suggestion("folder", "空项目", "从零开始让 Agent 写", () => openNewProject("empty")),
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
    await openProject(p.id);
    setComposer(sample.prompt);
  } catch (ex) { report(ex); }
}

function projectEmpty() {
  const sample = state.samples.find((s) => state.project.source === "sample:" + s.id);
  const ideas = [
    sample ? ["sparkles", "修好失败的测试", sample.prompt] : null,
    ["eye", "讲讲这个仓库", "先通读仓库，说明它的结构、入口和主要模块，不要改任何文件。"],
    ["check", "找出并修复失败的测试", "运行项目的测试，找出失败的用例，定位原因并修好；改完再跑一遍确认。"],
    ["terminal", "补单元测试", "为核心模块补充单元测试，覆盖主要分支与边界情况，并确认全部通过。"],
  ].filter(Boolean);
  return h("div", { class: "hero-empty" },
    h("div", { class: "mark big" }, logo("lg")),
    h("h2", {}, "要 Agent 做什么？"),
    h("p", {}, "用一句话描述任务。它会规划、动手、自我检查；进度在右侧，改动由你审阅。"),
    h("div", { class: "suggestions" }, ideas.map(([ic, t, prompt]) => suggestion(ic, t, prompt.length > 60 ? prompt.slice(0, 58) + "…" : prompt, () => setComposer(prompt)))),
  );
}

function modelLabel(id) {
  return (MODELS.find((m) => m.id === id) || {}).label || id;
}

function turn(t) {
  const run = state.runs.get(t.id);
  const events = run ? run.events : [];
  const finished = events.find((e) => e.type === "task_finished");
  const status = statusOf(t);
  const running = !TERMINAL.has(status);
  let tail = null;
  if (finished) tail = result(t, finished.value);
  else if (running) tail = waitingLine(t);
  else if (!run) tail = h("div", { class: "result-foot" }, statusChip(status), h("button", { class: "btn ghost sm", onclick: () => loadRun(t, true) }, "加载详情"));
  return h("section", { class: "turn", id: "turn-" + t.id },
    h("div", { class: "msg-user" },
      h("div", { class: "bubble" }, t.message),
      h("div", { class: "meta" }, fmtTime(t.created_at),
        t.sandbox === "read" ? h("span", { class: "chip info" }, icon("eye", "xs"), "只读") : null,
        t.model && t.model !== "balanced" ? h("span", { class: "chip" }, modelLabel(t.model)) : null),
    ),
    h("div", { class: "msg-agent" },
      h("div", { class: "agent-mark" + (running ? " live" : ""), title: running ? "工作中" : null }, logo()),
      h("div", { class: "agent-body" }, worklog(t, events, running, finished), asks(events), tail),
    ),
  );
}

function waitingLine(t) {
  const live = liveTasks();
  const ahead = live.indexOf(t);
  if (ahead > 0) {
    return h("div", { class: "waiting-line" }, icon("clock", "sm"), `排队中：前面还有 ${ahead} 个任务`,
      h("button", { class: "btn ghost xs", onclick: () => cancelTask(t.id) }, "取消排队"));
  }
  const run = state.runs.get(t.id);
  if (run && run.events.length) return null; // 进度在工作过程与输入框上方
  return h("div", { class: "waiting-line" }, h("span", { class: "spinner" }), run ? "正在开始…" : "正在连接…");
}

function asks(events) {
  return events.filter((e) => e.type === "human_input_required").map((ev) => {
    const v = ev.value || {};
    const qid = pick(v, "question_id", "questionId");
    const q = pick(v, "question", "prompt", "text", "summary", "message") || "确认";
    const answered = state.replied.has(qid) || events.some((e) => e.event_id > ev.event_id && !QUIET.has(e.type));
    return answered
      ? h("div", { class: "ask-inline done" }, icon("check", "sm"), h("span", {}, "已回复：" + q))
      : h("div", { class: "ask-inline" }, icon("alert", "sm"), h("span", {}, h("strong", {}, "需要你确认："), q, h("span", { class: "muted" }, " —— 在下方输入框里回复")));
  });
}

function worklog(t, events, running, finished) {
  const steps = workSteps(events);
  if (!steps.length) return null;
  const isOpen = state.open.has(t.id) || (running && !state.closed.has(t.id));
  const first = events[0];
  const elapsed = first ? (finished ? new Date(finished.time) : Date.now()) - new Date(first.time) : 0;
  const head = running
    ? [h("span", { class: "spinner" }), h("span", { class: "grow", id: "wl-" + t.id }, workingText(steps.length, elapsed))]
    : [icon(finished && finished.value.status === "TASK_STATUS_DONE" ? "check" : "clock", "sm lead"), h("span", { class: "grow" }, `工作了 ${fmtDuration(elapsed)} · ${steps.length} 步`)];
  return h("div", { class: "worklog" + (isOpen ? " open" : "") },
    h("button", { class: "worklog-head", onclick: () => {
      if (isOpen) { state.open.delete(t.id); state.closed.add(t.id); } else { state.open.add(t.id); state.closed.delete(t.id); }
      renderTurn(t.id);
    } }, head, icon("chevron", "sm chev")),
    h("div", { class: "worklog-steps" }, steps.map(step)),
  );
}

function workingText(steps, elapsed) {
  return `工作中 · ${steps} 步${elapsed ? " · " + fmtDuration(elapsed) : ""}`;
}

function workSteps(events) {
  return events.filter((e) => !QUIET.has(e.type) && e.type !== "task_finished" && e.type !== "human_input_required");
}

function stepInfo(ev) {
  const v = ev.value || {};
  let ic = "box", kind = "", title = ev.type, detail = "", chips = null, now = "";
  switch (ev.type) {
    case "task_accepted": ic = "check"; kind = "ok"; title = "任务已受理"; now = "已受理，正在分诊"; break;
    case "round_started":
      ic = "play"; kind = "run"; title = `开始第 ${pick(v, "round_seq") ?? ""} 轮`; now = "正在规划这一轮";
      detail = v.rerun ? "重新规划" : "";
      break;
    case "stage_started": {
      ic = "layers"; kind = "run";
      const goal = pick(v, "goal") || "执行";
      title = "计划：" + goal;
      now = "按计划执行：" + goal;
      detail = pick(v, "exit_when") ? "完成标准：" + v.exit_when : "";
      if (v.triage_level) detail = (detail ? detail + "\n" : "") + "Triage：" + v.triage_level;
      const nodes = Array.isArray(v.nodes) ? v.nodes : [];
      chips = nodes.length ? h("div", { class: "step-chips" }, nodes.map((n) => h("span", { class: "chip mono" }, typeof n === "string" ? n : pick(n, "id") || "节点"))) : null;
      break;
    }
    case "node_started": ic = "terminal"; title = "开始 " + (pick(v, "node_id") || "节点"); now = "正在执行 " + (pick(v, "node_id") || "节点"); break;
    case "node_finished": {
      const st = String(pick(v, "status") || "");
      const failed = /fail|denied|error/i.test(st);
      const id = pick(v, "node_id") || "节点";
      ic = failed ? "x" : "check"; kind = failed ? "err" : "ok";
      title = (failed ? "失败：" : "完成：") + id;
      now = failed ? `${id} 失败，Reviewer 会决定怎么办` : `完成 ${id}`;
      detail = [pick(v, "role"), st && st !== "succeeded" ? st : ""].filter(Boolean).join(" · ");
      break;
    }
    case "tool_status": ic = "terminal"; title = "工具 " + (pick(v, "tool", "name") || ""); detail = pick(v, "status", "message") || ""; break;
    case "verdict": {
      const key = String(pick(v, "verdict") || "").replace(/^VERDICT_/, "").toLowerCase();
      const [label, k] = VERDICTS[key] || [key || "—", ""];
      ic = "shield"; kind = k === "info" ? "run" : k;
      title = `Reviewer 判定：${label}（${key}）`;
      now = `Reviewer 判定：${label}`;
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
      h("details", { class: "raw" }, h("summary", {}, "原始事件"), h("pre", {}, JSON.stringify(ev.value || {}, null, 2))),
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
    else if (v !== null && typeof v === "object") out.push(Object.entries(v).map(([kk, vv]) => `- **${kk}**：${inlineValue(vv)}`).join("\n"));
    else out.push(String(v));
    out.push("");
  }
  return out.join("\n");
}

function taskCost(t) {
  const d = state.detail.get(t.id);
  return d && d.cost !== undefined ? d.cost : t.cost_usd || 0;
}

function result(t, r) {
  const text = deliverableText(r);
  const ok = r.status === "TASK_STATUS_DONE";
  const cost = taskCost(t);
  const rounds = r.rounds || t.rounds;
  const n = state.changes ? state.changes.files.length : 0;
  return h("div", { class: "col" },
    text ? h("div", { class: "answer" }, md(text)) : null,
    r.error ? h("div", { class: "error-box" }, typeof r.error === "string" ? r.error : pick(r.error, "message") || JSON.stringify(r.error)) : null,
    h("div", { class: "result-foot" },
      statusChip(r.status),
      rounds ? h("span", { class: "chip" }, `${rounds} 轮`) : null,
      cost ? h("span", { class: "chip" }, fmtUSD(cost)) : null,
      h("span", { class: "grow" }),
      text ? h("button", { class: "btn ghost xs", onclick: () => copy(text) }, icon("copy", "xs"), "复制") : null,
      ok && n && t === state.tasks[state.tasks.length - 1] ? h("button", { class: "btn ghost xs", onclick: () => openDrawer("changes") }, icon("diff", "xs"), "审阅改动") : null,
    ),
  );
}

// ---------------------------------------------------------------- 事件流

function closeStreams() {
  for (const run of state.runs.values()) if (run.es) run.es.close();
}

function loadRun(t, force = false) {
  if (state.runs.has(t.id) && !force) return state.runs.get(t.id).promise;
  const projectId = state.project.id;
  const run = { events: [], done: false, es: null };
  state.runs.set(t.id, run);
  run.promise = new Promise((resolve) => {
    const es = new EventSource(`/api/projects/${projectId}/tasks/${t.id}/events`);
    run.es = es;
    const seen = new Set();
    let timer = null;
    const finish = () => { es.close(); run.done = true; clearTimeout(timer); resolve(); };
    const onEvent = (msg) => {
      if (!state.project || state.project.id !== projectId) return finish();
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
  const projectId = state.project.id;
  const recent = state.tasks.slice(-REPLAY_LIMIT);
  // 先接上进行中的（实时），再按顺序重放已结束的（浏览器对同一主机的连接数有限）。
  for (const t of recent) if (!TERMINAL.has(t.status)) loadRun(t);
  for (const t of recent) {
    if (!state.project || state.project.id !== projectId) return;
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

async function cancelTask(taskId, projectId) {
  const pid = projectId || state.project.id;
  if (!confirm("停止这个任务？已经做的改动会留在工作区里。")) return;
  try {
    await api("POST", `/api/projects/${pid}/tasks/${taskId}/cancel`);
    toast("已请求停止");
    loadActive();
  } catch (ex) { report(ex); }
}

// ---------------------------------------------------------------- 数据

async function loadChanges() {
  if (!state.project) return;
  const pid = state.project.id;
  try {
    const c = await api("GET", `/api/projects/${pid}/changes`);
    if (state.project && state.project.id === pid) { state.changes = c; scheduleChrome(); }
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
  if (!state.project) return;
  const pid = state.project.id;
  try {
    const e = await api("GET", `/api/projects/${pid}/env`);
    if (state.project && state.project.id === pid) { state.env = e; scheduleChrome(); }
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
  if (!state.project) return;
  const pid = state.project.id;
  try {
    const data = await api("GET", `/api/projects/${pid}/tasks/${t.id}`);
    if (!state.project || state.project.id !== pid) return;
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
  if (!state.project) return;
  const pid = state.project.id;
  try {
    const list = await api("GET", `/api/projects/${pid}/tasks`);
    if (!state.project || state.project.id !== pid) return;
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
  ui.input = h("textarea", { class: "c-input", rows: 1, id: "composer", "aria-label": "给 Agent 的消息" });
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
      h("span", { class: "grow ellipsis" }, info.round ? `第 ${info.round} 轮 · ` : "", h("span", { class: "what" }, ph.mode === "waiting" ? "等你确认后继续" : info.now)),
      ph.queued.length ? h("span", { class: "chip" }, `排队 ${ph.queued.length}`) : null,
      h("span", { class: "when", id: "c-elapsed" }, info.elapsed ? fmtTimer(info.elapsed) : ""),
      h("button", { class: "btn xs", title: "停止当前任务（Esc）", onclick: () => cancelTask(ph.current.id) }, icon("stop", "xs"), "停止"),
    ].filter(Boolean));
  } else {
    ui.cStatus.className = "hidden";
    ui.cStatus.replaceChildren();
  }

  if (ph.mode === "waiting") {
    ui.cAsk.className = "c-ask";
    ui.cAsk.replaceChildren(h("div", { class: "h" }, icon("alert", "xs"), "需要你确认"), h("div", { class: "q" }, ph.ask.question));
  } else {
    ui.cAsk.className = "hidden";
    ui.cAsk.replaceChildren();
  }

  ui.input.placeholder = {
    empty: "描述要做的改动，例如：修好失败的测试",
    done: "接着说，或者开始新的改动",
    failed: "换个说法再试，或者补充更多信息",
    running: "继续输入：会排在当前任务之后执行",
    waiting: "补充说明（可选），然后选一个选项",
  }[ph.mode] || "";

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
  ui.send = h("button", { class: "send" + (queue ? " queue" : ""), title: queue ? "排在当前任务之后（Enter）" : "发送（Enter）", onclick: () => submit() },
    icon(queue ? "queue" : "send", "sm"), queue ? "排队" : null);
  return ui.send;
}

function hint(ph) {
  const k = (key, text) => h("span", {}, h("span", { class: "kbd" }, key), " ", text);
  if (ph.mode === "running") return [k("Esc", "停止当前任务"), k("Enter", "排队"), k("Shift+Enter", "换行")];
  if (ph.mode === "waiting") return [h("span", {}, "选项会作为人工回复送回 Runtime；补充说明随选项一起发送")];
  return [k("Enter", "发送"), k("Shift+Enter", "换行"),
    h("span", {}, state.settings.sandbox === "read" ? "只读：这个任务不会改文件" : "改动留在工作区，由你审阅后再提交")];
}

function optionLabel(label) {
  return {
    approve: "批准", reject: "拒绝", continue_as_is: "原样继续", replan_with_guidance: "按补充说明重新规划", stop_and_wrap_up: "停下并收尾",
    continue: "原样继续", replan: "带指导重规划", stop: "停止并收尾",
  }[label] || label;
}

function nextSteps(ph) {
  const chip = (ic, label, onclick, primary) => h("button", { class: "next-step" + (primary ? " primary" : ""), onclick }, icon(ic, "xs"), label);
  if (ph.mode === "done") {
    const n = state.changes ? state.changes.files.length : 0;
    const p = state.project;
    const out = [];
    if (n) out.push(chip("diff", `审阅改动 · ${n} 个文件`, () => openDrawer("changes"), true));
    if (n && p.github_repo && state.me.github) out.push(chip("github", p.pr_number ? `推送到 PR #${p.pr_number}` : "推送并开草稿 PR", () => openDrawer("changes", { focus: "publish" })));
    else if (n) out.push(chip("commit", "提交这些改动", () => openDrawer("changes", { focus: "commit" })));
    out.push(chip("edit", "继续完善", () => setComposer("在刚才的基础上继续：")));
    out.push(chip("fileText", "解释改了什么", () => setComposer("解释一下刚才改了哪些地方、为什么这样改，不要再改文件。")));
    return out;
  }
  if (ph.mode === "failed") {
    const t = ph.last;
    return [
      chip("refresh", "重试", () => submit(t.message), true),
      chip("edit", "改一改再试", () => setComposer(t.message)),
      chip("list", "看看卡在哪里", () => { state.open.add(t.id); state.closed.delete(t.id); renderTurn(t.id); scrollToTurn(t.id); }),
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
  return mode === "host" ? "改文件；命令直接在本机跑（开发环境，不隔离）" : s.desc;
}

function sandboxTool() {
  const cur = SANDBOX.find((s) => s.id === state.settings.sandbox) || SANDBOX[1];
  const can = state.config.sandbox_control;
  return h("button", {
    class: "tool" + (cur.id === "read" ? " read-only" : ""),
    disabled: !can,
    title: can ? "沙箱等级：对下一个任务生效" : "这个部署的沙箱等级由租户策略决定",
    onclick: (e) => openPop(e.currentTarget, (close) => [
      h("div", { class: "pop-title" }, "沙箱等级"),
      ...SANDBOX.map((s) => popItem(s.icon, s.label, sandboxDesc(s), state.settings.sandbox === s.id, () => { setSetting("sandbox", s.id); close(); }, s.disabled)),
      h("div", { class: "pop-sep" }),
      h("div", { class: "pop-note" }, "对下一个任务生效：提交时固定成这个任务的 effect 上限，运行中改不了。"),
    ]),
  }, icon(cur.icon, "sm"), h("span", { class: "v" }, cur.label), icon("chevDown", "caret"));
}

function modelTool() {
  const cur = MODELS.find((m) => m.id === state.settings.model) || MODELS[1];
  return h("button", {
    class: "tool",
    title: "模型倾向：对下一个任务生效",
    onclick: (e) => openPop(e.currentTarget, (close) => [
      h("div", { class: "pop-title" }, "模型倾向"),
      ...MODELS.map((m) => popItem(m.icon, m.label, m.desc, state.settings.model === m.id, () => { setSetting("model", m.id); close(); })),
      h("div", { class: "pop-sep" }),
      h("div", { class: "pop-note" }, "作为约束交给 Planner，它规划模型节点时据此选档位；工具循环与 Reviewer 的档位是固定配置。"),
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
  return m.hasUsage ? `${fmtTokens(m.input + m.output)} token · ${fmtUSD(m.cost)}` : fmtUSD(m.cost);
}

function meterTool(m) {
  return h("button", {
    class: "tool",
    title: "本项目的用量",
    onclick: (e) => openPop(e.currentTarget, () => {
      const mm = meter();
      const row = (k, v) => [h("span", { class: "k" }, k), h("span", { class: "v" }, v)];
      return [
        h("div", { class: "pop-title" }, "本项目的用量"),
        h("div", { class: "meter-grid" },
          row("任务", String(mm.count)),
          row("花费合计", fmtUSD(mm.cost)),
          mm.current !== null ? row("当前任务", fmtUSD(mm.current)) : null,
          mm.hasUsage ? [row("输入 token", fmtTokens(mm.input)), row("输出 token", fmtTokens(mm.output)), row("缓存读", fmtTokens(mm.cacheRead))] : null),
        h("div", { class: "pop-sep" }),
        h("div", { class: "pop-note" }, mm.hasUsage
          ? "花费按 Task State 里每一轮的 cost_usd 相加；token 来自 Runtime 的计量。"
          : "花费按 Task State 里每一轮的 cost_usd 相加。Token 数要等 Runtime 在 QueryTask 里回报 usage，现在还是空的。"),
      ];
    }),
  }, icon("gauge", "sm"), h("span", { class: "v keep" }, meterText(m)));
}

async function submit(text) {
  const message = (text ?? ui.input.value).trim();
  if (!message || state.sending || !state.project) return;
  state.sending = true;
  refreshSend();
  try {
    const t = await api("POST", `/api/projects/${state.project.id}/tasks`, { message, sandbox: state.settings.sandbox, model: state.settings.model });
    state.tasks.push(t);
    if (text === undefined) { ui.input.value = ""; autosize(); }
    renderThread(true);
    loadRun(t);
    loadActive();
    scheduleChrome();
  } catch (ex) { report(ex); } finally {
    state.sending = false;
    refreshSend();
    ui.input.focus();
  }
}

async function reply(t, ask, option) {
  try {
    await api("POST", `/api/projects/${state.project.id}/tasks/${t.id}/reply`, { question_id: ask.qid, option, guidance: ui.input.value.trim() });
    state.replied.add(ask.qid);
    ui.input.value = "";
    autosize();
    renderTurn(t.id);
    updateComposer(true);
    toast("已回复");
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
  if (!ui.statusBody || !state.project || !state.statusOpen) return;
  ui.statusBody.replaceChildren(
    section("env", "server", "环境", envSummary(), envBody),
    section("context", "layers", "上下文", contextSummary(), contextBody),
    section("output", "fileText", "输出", outputSummary(), outputBody),
    section("changes", "diff", "改动", changesSummary(), changesBody),
    section("background", "activity", "后台任务", backgroundSummary(), backgroundBody),
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
  return e.runtime && e.runtime.ok ? [h("span", { class: "dot ok" }), "已连接"] : [h("span", { class: "dot err" }), "Runtime 连不上"];
}

function envBody() {
  const e = state.env;
  if (!e) return h("div", { class: "empty-line" }, "正在读取…");
  const p = state.project;
  const sb = e.sandbox || {};
  const git = e.git || {};
  const level = SANDBOX.find((s) => s.id === state.settings.sandbox) || SANDBOX[1];
  const sandboxText = sb.mode === "docker" ? `Docker · ${sb.image || "默认镜像"} · 断网` : sb.mode === "host" ? "本机（不隔离，仅开发）" : "没有说明";
  return [
    kv([
      ["Runtime", [h("span", { class: "dot " + (e.runtime && e.runtime.ok ? "ok" : "err") }), e.runtime && e.runtime.ok ? "task-service 已连接" : "连不上 task-service"]],
      ["沙箱", sandboxText],
      ["权限", [h("span", { class: "chip" + (level.id === "read" ? " info" : "") }, icon(level.icon, "xs"), level.label), h("span", { class: "muted small" }, "下一个任务")]],
      ["分支", git.branch ? [h("span", { class: "chip mono" }, icon("branch", "xs"), git.branch), git.head ? h("span", { class: "mono small muted" }, git.head.slice(0, 7)) : null] : "—"],
      git.subject ? ["最近提交", h("span", { class: "muted" }, git.subject)] : null,
      ["工作区", git.dirty ? `${git.dirty} 个文件未提交` : "干净"],
      ["来源", p.github_repo ? h("a", { href: `https://github.com/${p.github_repo}`, target: "_blank", rel: "noopener" }, p.github_repo) : sourceLabel(p)],
      p.session_id ? ["Session", h("span", { class: "mono small", title: p.session_id }, p.session_id.slice(0, 16) + "…")] : null,
    ]),
    h("div", { class: "sp-actions" },
      h("button", { class: "btn xs", onclick: () => openDrawer("files") }, icon("folder", "xs"), "浏览文件"),
      p.branch ? h("button", { class: "btn xs", title: `回到 ${p.base_branch} 并拉取最新`, onclick: resetBranch }, icon("refresh", "xs"), `回到 ${p.base_branch}`) : null,
      h("button", { class: "btn ghost xs", title: "重新读取", onclick: loadEnv }, icon("refresh", "xs"))),
  ];
}

function contextSummary() {
  const t = focusTask();
  if (!t) return "—";
  const d = state.detail.get(t.id);
  const rounds = d && d.state && Array.isArray(d.state.rounds) ? d.state.rounds.length : t.rounds || 0;
  const cost = taskCost(t);
  return [rounds ? `${rounds} 轮` : null, cost ? fmtUSD(cost) : null].filter(Boolean).join(" · ") || "—";
}

function contextBody() {
  const t = focusTask();
  if (!t) return h("div", { class: "empty-line" }, "还没有任务。提交一个任务后，这里显示它的目标、约束、已知的事实与每一轮的判定。");
  const d = state.detail.get(t.id);
  const s = d && d.state;
  if (!s) return [h("div", { class: "goal" }, t.message), h("div", { class: "empty-line" }, "正在读取 Task State…")];
  const c = s.constraints || {};
  const chips = [];
  if (c.effect_ceiling) {
    const lv = SANDBOX.find((x) => x.id === c.effect_ceiling);
    chips.push(h("span", { class: "chip" + (c.effect_ceiling === "read" ? " info" : "") }, lv ? lv.label : c.effect_ceiling));
  }
  if (c.budget_usd) chips.push(h("span", { class: "chip" }, `预算 ${fmtUSD(num(c.budget_usd))}`));
  if (c.model_preference) chips.push(h("span", { class: "chip" }, `模型 ${modelLabel(c.model_preference)}`));
  for (const [k, v] of Object.entries(c)) {
    if (!["effect_ceiling", "budget_usd", "model_preference"].includes(k)) chips.push(h("span", { class: "chip mono" }, `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`));
  }
  const list = (title, items, max = 6) => {
    if (!Array.isArray(items) || !items.length) return null;
    const shown = items.slice(-max);
    return [h("div", { class: "sp-sub" }, `${title} · ${items.length}`), h("ul", { class: "sp-list" }, shown.map((x) => h("li", {}, String(x)))),
      items.length > max ? h("div", { class: "empty-line" }, `还有更早的 ${items.length - max} 条`) : null];
  };
  const rounds = Array.isArray(s.rounds) ? s.rounds : [];
  return [
    h("div", { class: "goal" }, s.goal || t.message),
    h("div", { class: "sp-sub" }, "完成条件"),
    h("div", { class: "muted" }, s.done_when || "—"),
    chips.length ? [h("div", { class: "sp-sub" }, "约束"), h("div", { class: "chip-row" }, chips)] : null,
    list("里程碑", s.milestones),
    list("已验证的事实", s.facts),
    list("待查的假设", s.assumptions),
    list("待问的问题", s.open_questions),
    rounds.length ? [h("div", { class: "sp-sub" }, "各轮"), h("div", { class: "rounds" }, rounds.map((r) => h("div", { class: "round-row" },
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
  if (!f) return h("div", { class: "empty-line" }, "任务结束后，这里显示最近一次的交付。");
  const text = deliverableText(f.result);
  const d = state.detail.get(f.task.id);
  const artifacts = d && d.state && Array.isArray(d.state.artifacts) ? d.state.artifacts : [];
  return [
    h("div", { class: "muted small ellipsis" }, f.task.message),
    text ? h("div", { class: "output-preview" }, h("div", { class: "answer" }, md(text))) : h("div", { class: "empty-line" }, "这个任务没有文字交付。"),
    artifacts.length ? [h("div", { class: "sp-sub" }, `产物 · ${artifacts.length}`), h("ul", { class: "sp-list" }, artifacts.map((a) => h("li", { class: "mono small" }, a)))] : null,
    h("div", { class: "sp-actions" },
      h("button", { class: "btn xs", onclick: () => scrollToTurn(f.task.id) }, icon("chevron", "xs"), "在对话里看"),
      text ? h("button", { class: "btn ghost xs", onclick: () => copy(text) }, icon("copy", "xs"), "复制") : null),
  ];
}

function changeStats() {
  const files = state.changes ? state.changes.files : [];
  return { files, added: files.reduce((n, f) => n + f.added, 0), deleted: files.reduce((n, f) => n + f.deleted, 0) };
}

function changesSummary() {
  if (!state.changes) return h("span", { class: "spinner" });
  const { files, added, deleted } = changeStats();
  if (!files.length) return "干净";
  return [h("span", { class: "stat-add" }, `+${added}`), h("span", { class: "stat-del" }, `−${deleted}`), `${files.length} 个文件`];
}

function changesBody() {
  if (!state.changes) return h("div", { class: "empty-line" }, "正在读取…");
  const { files } = changeStats();
  const p = state.project;
  const max = 12;
  return [
    files.length
      ? h("div", {}, files.slice(0, max).map((f) => h("button", { class: "file-row", title: f.path, onclick: () => openDrawer("changes", { focus: f.path }) },
        icon("file", "xs"), h("span", { class: "p ellipsis" }, f.path),
        f.binary ? h("span", { class: "muted small" }, "二进制") : [h("span", { class: "stat-add" }, `+${f.added}`), h("span", { class: "stat-del" }, `−${f.deleted}`)])),
      files.length > max ? h("div", { class: "empty-line" }, `还有 ${files.length - max} 个文件`) : null)
      : h("div", { class: "empty-line" }, "工作区相对 HEAD 没有改动。"),
    h("div", { class: "sp-actions" },
      files.length ? h("button", { class: "btn primary xs", onclick: () => openDrawer("changes") }, icon("diff", "xs"), "审阅并提交") : null,
      files.length && p.github_repo && state.me.github ? h("button", { class: "btn xs", onclick: () => openDrawer("changes", { focus: "publish" }) }, icon("github", "xs"), p.pr_number ? `推送到 PR #${p.pr_number}` : "推送并开 PR") : null,
      files.length ? h("a", { class: "btn xs", href: `/api/projects/${p.id}/patch`, download: `${p.id}.patch` }, icon("download", "xs"), "补丁") : null,
      h("button", { class: "btn ghost xs", onclick: () => openDrawer("history") }, icon("history", "xs"), "提交历史")),
  ];
}

function backgroundSummary() {
  const n = state.active.length;
  return n ? [h("span", { class: "spinner" }), `${n} 个进行中`] : "空闲";
}

function backgroundBody() {
  if (!state.active.length) return h("div", { class: "empty-line" }, "没有进行中的任务。同一项目的任务按顺序执行，不同项目可以同时跑。");
  return [
    h("div", {}, state.active.map((a) => h("div", { class: "bg-row", title: "打开这个任务", onclick: () => jumpTo(a.project_id, a.task.id) },
      a.task.status === "TASK_STATUS_RUNNING" ? h("span", { class: "spinner" }) : icon("clock", "sm"),
      h("div", { class: "grow" }, h("div", { class: "t" }, a.task.message),
        h("div", { class: "m" }, `${a.project_name} · ${(STATUS[a.task.status] || [a.task.status])[0]} · ${fmtAgo(a.task.created_at)}`)),
      h("button", { class: "btn ghost xs icon-only", title: "停止", onclick: (e) => { e.stopPropagation(); cancelTask(a.task.id, a.project_id); } }, icon("x", "xs")),
    ))),
    h("div", { class: "empty-line" }, "同一项目的任务按顺序执行，不同项目可以同时跑。"),
  ];
}

async function jumpTo(projectId, taskId) {
  if (!state.project || state.project.id !== projectId) await openProject(projectId);
  requestAnimationFrame(() => scrollToTurn(taskId));
}

// ---------------------------------------------------------------- 详情抽屉

function openDrawer(tab, opts = {}) {
  if (!state.project) return;
  closeDrawer();
  closePop();
  state.drawer = { tab, focus: opts.focus || null };
  const scrim = h("div", { class: "scrim", onclick: closeDrawer });
  const el = h("div", { class: "drawer", role: "dialog", "aria-label": "改动、文件与提交历史" });
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
      h("span", { class: "h ellipsis" }, state.project.name),
      tab("changes", "diff", "改动", n ? h("span", { class: "n" }, n) : null),
      tab("files", "folder", "文件"),
      tab("history", "history", "提交历史"),
      h("span", { class: "grow" }),
      h("button", { class: "btn ghost sm icon-only", title: "关闭（Esc）", onclick: closeDrawer }, icon("x", "sm"))),
    body,
  );
  if (d.tab === "changes") drawChanges(body, d.focus);
  else if (d.tab === "files") loadFiles(body, state.filesPath);
  else loadHistory(body);
}

async function drawChanges(body, focus) {
  body.replaceChildren(h("div", { class: "row muted" }, h("span", { class: "spinner" }), "正在读取改动…"));
  let data;
  try {
    data = await api("GET", `/api/projects/${state.project.id}/changes`);
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
      h("div", { class: "card-title" }, icon("check", "sm"), "工作区是干净的"),
      h("div", { class: "muted" }, "当前分支 ", h("span", { class: "mono" }, data.branch || "—"), " · HEAD ", h("span", { class: "mono" }, (data.head || "").slice(0, 10)))));
  } else {
    const added = data.files.reduce((n, f) => n + f.added, 0);
    const deleted = data.files.reduce((n, f) => n + f.deleted, 0);
    commitInput = h("input", { class: "field", placeholder: "提交说明", value: lastMessage() });
    const commit = h("button", { class: "btn primary sm", disabled: busy, onclick: async () => {
      commit.disabled = true;
      try {
        const r = await api("POST", `/api/projects/${state.project.id}/commit`, { message: commitInput.value });
        toast("已提交 " + r.head.slice(0, 8));
        await loadChanges();
        loadEnv();
        renderDrawer();
      } catch (ex) { commit.disabled = false; report(ex); }
    } }, icon("commit", "sm"), "提交");
    parts.push(h("div", { class: "card" },
      h("div", { class: "card-title" }, `${data.files.length} 个文件有改动`, h("span", { class: "stat-add" }, `+${added}`), h("span", { class: "stat-del" }, `−${deleted}`),
        busy ? h("span", { class: "chip run" }, h("span", { class: "spinner" }), "任务进行中，结束后才能提交") : null),
      h("div", { class: "actions" }, h("div", { class: "grow" }, commitInput), commit,
        h("a", { class: "btn sm", href: `/api/projects/${state.project.id}/patch`, download: `${state.project.id}.patch` }, icon("download", "sm"), "补丁"),
        h("button", { class: "btn sm danger", disabled: busy, onclick: discard }, icon("trash", "sm"), "丢弃")),
    ));
  }
  const pub = publishCard(busy, data.files.length > 0);
  if (pub) parts.push(pub.card);
  parts.push(...renderDiff(data.diff));
  if (data.truncated) parts.push(h("div", { class: "muted" }, "改动太大，只显示了前 2 MB；完整内容请下载补丁。"));
  body.replaceChildren(...parts);
  if (focus === "commit" && commitInput) commitInput.focus();
  else if (focus === "publish" && pub) pub.title.focus();
  else if (focus) {
    const el = [...body.querySelectorAll(".diff-file")].find((x) => x.getAttribute("data-path") === focus);
    if (el) { el.scrollIntoView({ block: "start" }); el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 1600); }
  }
}

async function discard() {
  if (!confirm("丢弃全部未提交的改动？这一步不能撤销。")) return;
  try {
    await api("POST", `/api/projects/${state.project.id}/discard`);
    toast("已丢弃改动");
    await loadChanges();
    loadEnv();
    renderDrawer();
  } catch (ex) { report(ex); }
}

function lastMessage() {
  // 默认提交说明：最近一条像样的任务描述（跳过“-”这类过短的）。
  const t = [...state.tasks].reverse().find((x) => x.message.trim().length > 3);
  return t ? t.message.trim().split("\n")[0].slice(0, 72) : "";
}

function setProject(p) {
  state.project = p;
  state.projects = state.projects.map((x) => (x.id === p.id ? p : x));
}

function publishCard(busy, dirty) {
  const p = state.project;
  if (!p.github_repo) return null;
  if (!state.me.github) {
    return { card: h("div", { class: "card" }, h("div", { class: "card-title" }, icon("github", "sm"), p.github_repo), h("div", { class: "muted" }, "用 GitHub 登录后，可以把改动推到 agent/* 分支并开草稿 PR。")), title: null };
  }
  const title = h("input", { class: "field", placeholder: "PR 标题，也用作提交说明", value: lastMessage() });
  const out = h("div", {});
  const label = p.pr_number ? `推送到 PR #${p.pr_number}` : "推送并开草稿 PR";
  const push = h("button", { class: "btn primary sm", disabled: busy, onclick: async () => {
    push.disabled = true;
    push.lastChild.textContent = "正在推送…";
    try {
      const r = await api("POST", `/api/projects/${p.id}/publish`, { title: title.value });
      setProject(r.project);
      toast(r.created ? `已开草稿 PR #${r.pr_number}` : `已推送到 ${r.branch}`);
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
  } }, icon("github", "sm"), h("span", {}, label));
  const card = h("div", { class: "card" },
    h("div", { class: "card-title" }, icon("github", "sm"), h("span", { class: "mono grow ellipsis" }, p.github_repo),
      p.pr_url ? h("a", { class: "chip ok", href: p.pr_url, target: "_blank", rel: "noopener" }, `PR #${p.pr_number}`, icon("external", "xs")) : null),
    h("div", { class: "muted" }, p.branch
      ? `${dirty ? "提交未提交的改动后" : ""}推送到 ${p.branch}，PR 自动更新。`
      : `新建 agent/… 分支，提交后推送，再向 ${p.base_branch || "基线分支"} 开草稿 PR；不会推到别的分支，也不强推。`),
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
    entries = await api("GET", `/api/projects/${state.project.id}/tree?path=${encodeURIComponent(path)}`);
  } catch (ex) {
    body.replaceChildren(h("div", { class: "error-box" }, ex.message));
    return;
  }
  const parts = path ? path.split("/") : [];
  const viewer = h("div", {});
  body.replaceChildren(
    h("div", { class: "card" },
      h("div", { class: "crumbs" }, h("button", { onclick: () => loadFiles(body, "") }, state.project.id),
        parts.map((p, i) => [h("span", {}, "/"), h("button", { onclick: () => loadFiles(body, parts.slice(0, i + 1).join("/")) }, p)])),
      entries.length ? h("div", {}, entries.map((e) => h("button", {
        class: "tree-item",
        onclick: () => (e.dir ? loadFiles(body, join(path, e.name)) : showFile(viewer, join(path, e.name))),
      }, icon(e.dir ? "folder" : "file", "sm"), e.name, e.dir ? null : h("span", { class: "size" }, fmtSize(e.size))))) : h("div", { class: "muted" }, "空目录"),
    ),
    viewer,
  );
}

function join(a, b) {
  return a ? `${a}/${b}` : b;
}

async function showFile(viewer, path) {
  viewer.replaceChildren(h("div", { class: "row muted" }, h("span", { class: "spinner" }), "正在读取…"));
  try {
    const f = await api("GET", `/api/projects/${state.project.id}/file?path=${encodeURIComponent(path)}`);
    const head = h("div", { class: "row" }, icon("file", "sm"), h("strong", { class: "mono grow ellipsis" }, path), h("span", { class: "muted" }, fmtSize(f.size || 0)));
    const content = f.binary ? h("div", { class: "muted" }, "二进制文件，不显示内容")
      : f.too_large ? h("div", { class: "muted" }, "文件太大，不显示内容")
        : h("pre", { class: "code-view" }, f.content.replace(/\n$/, "").split("\n").map((l) => h("span", {}, l || " ")));
    viewer.replaceChildren(h("div", { class: "col" }, head, content));
  } catch (ex) {
    viewer.replaceChildren(h("div", { class: "error-box" }, ex.message));
  }
}

async function loadHistory(body) {
  try {
    const commits = await api("GET", `/api/projects/${state.project.id}/log`);
    body.replaceChildren(h("div", { class: "card" }, commits.length ? commits.map((c) => h("div", { class: "commit-row" },
      h("span", { class: "hash" }, c.hash.slice(0, 7)),
      h("div", { class: "grow" }, h("div", {}, c.subject), h("div", { class: "muted small" }, `${c.author} · ${fmtTime(c.date)}`)),
    )) : h("div", { class: "muted" }, "还没有提交")));
  } catch (ex) {
    body.replaceChildren(h("div", { class: "error-box" }, ex.message));
  }
}

async function resetBranch() {
  const p = state.project;
  if (!confirm(`回到 ${p.base_branch} 并拉取最新？${p.branch} 上已推送的提交不受影响；未提交的改动要先处理。`)) return;
  try {
    const r = await api("POST", `/api/projects/${p.id}/reset-branch`);
    setProject(r.project);
    toast(`已回到 ${p.base_branch}`);
    renderTopbar();
    loadChanges();
    loadEnv();
  } catch (ex) { report(ex); }
}

// ---------------------------------------------------------------- 项目

function openNewProject(initial) {
  let mode = initial || (state.me.github ? "git" : state.samples.length ? "sample" : "git");
  let sample = state.samples[0] ? state.samples[0].id : "";
  let repos = null;
  let filter = "";
  const name = h("input", { class: "field", placeholder: "项目名" });
  const gitUrl = h("input", { class: "field", placeholder: "https://github.com/owner/repo.git" });
  const branch = h("input", { class: "field", placeholder: "默认分支" });
  const err = h("div", { class: "err-text" });
  const seg = h("div", { class: "seg" });
  const body = h("div", { class: "col" });
  const create = h("button", { class: "btn primary" }, "创建");
  const cancel = h("button", { class: "btn ghost" }, "取消");
  const dialog = h("dialog", {},
    h("div", { class: "d-head" }, h("h2", {}, "新建项目")),
    h("div", { class: "d-body" }, seg, body, err),
    h("div", { class: "d-foot" }, cancel, create),
  );
  const repoList = h("div", { class: "pick-list" });
  const drawRepos = () => {
    if (repos === null) {
      repos = "loading";
      api("GET", "/api/github/repos").then((r) => { repos = r; drawRepos(); }).catch((ex) => { repos = []; err.textContent = ex.message; drawRepos(); });
    }
    if (repos === "loading") return repoList.replaceChildren(h("div", { class: "row muted" }, h("span", { class: "spinner" }), "正在读取你的仓库…"));
    const q = filter.trim().toLowerCase();
    const shown = repos.filter((r) => !q || r.full_name.toLowerCase().includes(q)).slice(0, 40);
    repoList.replaceChildren(...(shown.length ? shown.map((r) => h("button", {
      class: "pick" + (gitUrl.value === r.clone_url ? " active" : ""),
      onclick: () => { gitUrl.value = r.clone_url; branch.value = r.default_branch || ""; name.value = r.full_name.split("/")[1]; drawRepos(); },
    }, icon("github", "sm"), h("span", { class: "mono grow ellipsis" }, r.full_name),
      r.private ? h("span", { class: "chip" }, "私有") : null,
      r.permissions && !r.permissions.push ? h("span", { class: "chip warn" }, "只读") : null)) : [h("div", { class: "muted" }, "没有匹配的仓库")]));
  };
  const draw = () => {
    seg.replaceChildren(...[["git", "github", state.me.github ? "GitHub 仓库" : "Git 地址"], ["sample", "box", "示例"], ["empty", "folder", "空项目"]].map(([k, ic, l]) =>
      h("button", { class: mode === k ? "active" : "", onclick: () => { mode = k; draw(); } }, icon(ic, "sm"), l)));
    const parts = [];
    if (mode === "git") {
      if (state.me.github) {
        const search = h("input", { class: "field", placeholder: "搜索我的仓库", value: filter });
        search.addEventListener("input", () => { filter = search.value; drawRepos(); });
        parts.push(h("div", { class: "col" }, search, repoList));
        drawRepos();
      }
      parts.push(h("div", {}, h("label", { class: "lbl" }, state.me.github ? "或者填 Git 地址（https）" : "Git 地址（https，公开仓库）"), gitUrl));
      parts.push(h("div", { class: "row" }, h("div", { class: "grow" }, h("label", { class: "lbl" }, "项目名"), name), h("div", { class: "grow" }, h("label", { class: "lbl" }, "分支"), branch)));
    } else if (mode === "sample") {
      parts.push(...state.samples.map((s) => h("div", {
        class: "sample-card" + (sample === s.id ? " active" : ""),
        onclick: () => { sample = s.id; name.value = s.name; draw(); },
      }, h("div", { class: "row" }, icon("box", "sm"), h("strong", {}, s.name)), h("div", { class: "muted" }, s.description))));
      if (!name.value && state.samples[0]) name.value = (state.samples.find((s) => s.id === sample) || state.samples[0]).name;
      parts.push(h("div", {}, h("label", { class: "lbl" }, "项目名"), name));
    } else {
      parts.push(h("div", { class: "muted" }, "一个只有 README 的空仓库，让 Agent 从零开始写。"), h("div", {}, h("label", { class: "lbl" }, "项目名"), name));
    }
    body.replaceChildren(...parts);
  };
  draw();
  cancel.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => dialog.remove());
  create.addEventListener("click", async () => {
    err.textContent = "";
    create.disabled = true;
    create.textContent = mode === "git" ? "正在克隆…" : "正在创建…";
    const req = { name: name.value || (mode === "git" ? (gitUrl.value.split("/").pop() || "").replace(/\.git$/, "") : "") };
    if (mode === "git") Object.assign(req, { git_url: gitUrl.value, branch: branch.value });
    if (mode === "sample") req.sample = sample;
    try {
      const p = await api("POST", "/api/projects", req);
      state.projects.unshift(p);
      dialog.close();
      await openProject(p.id);
      const s = mode === "sample" ? state.samples.find((x) => x.id === sample) : null;
      if (s) setComposer(s.prompt);
    } catch (ex) {
      if (ex.message !== "unauthorized") err.textContent = ex.message;
      create.disabled = false;
      create.textContent = "创建";
    }
  });
  document.body.append(dialog);
  dialog.showModal();
}

async function deleteProject() {
  const p = state.project;
  if (!confirm(`删除项目「${p.name}」？工作区里的文件会一起删掉，不能撤销。`)) return;
  try {
    await api("DELETE", `/api/projects/${p.id}`);
    closeStreams();
    state.projects = state.projects.filter((x) => x.id !== p.id);
    toast("已删除");
    if (state.projects[0]) await openProject(state.projects[0].id);
    else { state.project = null; renderApp(); }
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
  const cur = state.project && currentTask();
  const timer = document.getElementById("c-elapsed");
  if (timer && cur) {
    const run = state.runs.get(cur.id);
    if (run && run.events.length) timer.textContent = fmtTimer(Date.now() - new Date(run.events[0].time));
  }
  if (!state.me || !state.project) return;
  if (cur && tick % 4 === 0) loadDetail(cur);
  if (tick % (state.active.length ? 5 : 30) === 0) loadActive();
}, 1000);

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (pop) { closePop(); return; }
    if (ui.drawer) { closeDrawer(); return; }
    if (document.querySelector("dialog[open]") || !state.me || !state.project) return;
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

window.addEventListener("hashchange", () => {
  const wanted = decodeURIComponent((location.hash.match(/^#\/p\/([^/?]+)/) || [])[1] || "");
  if (state.me && wanted && (!state.project || state.project.id !== wanted) && state.projects.some((p) => p.id === wanted)) openProject(wanted);
});

async function boot() {
  applyTheme();
  try { state.config = await api("GET", "/api/config"); } catch { /* 用默认 */ }
  try { state.me = await api("GET", "/api/me"); } catch { state.me = null; }
  if (state.me) await enterApp().catch(report);
  else renderLogin();
}

boot();
