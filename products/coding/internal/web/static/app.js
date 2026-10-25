// Coding Agent 网页：对话式布局（原生 JS，无构建步骤）。
// 项目 = 一段对话（Runtime 的一个 Session），每个任务 = 一轮：你的消息、Agent 的工作过程（可折叠）、最终回答。
// 右侧面板放改动、文件与提交历史。DOM 一律用 textContent 构造，不拼 HTML。

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
const REPLAY_LIMIT = 12;

const state = {
  config: { dev_login: false, github_login: false },
  me: null,
  projects: [],
  samples: [],
  project: null,
  tasks: [], // 旧 → 新
  runs: new Map(), // taskId → { events, done, es, loading }
  open: new Set(), // 展开的工作过程
  closed: new Set(), // 用户手动收起的
  replied: new Set(),
  panelOpen: false,
  panelTab: "changes",
  changes: null,
  filesPath: "",
  sidebarOpen: false,
  sending: false,
};

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
  panel: "M3 5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM15 3v18",
  file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6",
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
  stop: "M6 6h12v12H6z",
  clock: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2",
  code: "M16 18l6-6-6-6M8 6l-6 6 6 6",
  box: "M21 8 12 3 3 8v8l9 5 9-5zM3 8l9 5 9-5M12 13v8",
  copy: "M9 9h11v11H9zM5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1",
  history: "M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5M12 7v5l3 2",
  diff: "M12 3v14M5 10h14M5 21h14",
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

function fmtTime(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleString();
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

function fmtSize(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function statusChip(status) {
  const [label, kind] = STATUS[status] || [status || "未知", ""];
  return h("span", { class: `chip ${kind}` }, kind === "run" ? h("span", { class: "spinner" }) : null, label);
}

function initials(name) {
  return (name || "?").slice(0, 2).toUpperCase();
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
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^\s*```(\S*)\s*$/);
    if (fence) {
      flush();
      const body = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) body.push(lines[i++]);
      i++;
      frag.append(h("pre", {}, h("code", {}, body.join("\n"))));
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
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const list = h(ordered ? "ol" : "ul");
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

// ---------------------------------------------------------------- 登录

function renderLogin(message = "") {
  closeStreams();
  const hashError = new URLSearchParams(location.hash.split("?")[1] || "").get("error");
  const err = h("div", { class: "err-text" }, message || hashError || "");
  const tenant = h("input", { class: "field", placeholder: "例如 acme", autocomplete: "organization", value: localStorage.getItem("cw_tenant") || "" });
  const user = h("input", { class: "field", placeholder: "例如 alice", autocomplete: "username", value: localStorage.getItem("cw_user") || "" });
  const submit = h("button", { class: "btn block", type: "submit" }, "用开发账号进入");
  const form = h("form", { class: "col", onsubmit: async (e) => {
    e.preventDefault();
    submit.disabled = true;
    err.textContent = "";
    try {
      state.me = await api("POST", "/api/login", { tenant: tenant.value, user: user.value });
      localStorage.setItem("cw_tenant", state.me.tenant);
      localStorage.setItem("cw_user", state.me.user);
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
        h("div", { class: "logo" }, icon("code", "lg")),
        h("h1", {}, "Coding Agent"),
        h("p", {}, "在你的仓库上读代码、改代码、跑测试，改动由你审阅后推送。"),
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
  const wanted = decodeURIComponent((location.hash.match(/^#\/p\/([^/?]+)/) || [])[1] || "");
  const p = state.projects.find((x) => x.id === wanted) || state.projects[0];
  if (p) await openProject(p.id);
  else { state.project = null; renderApp(); }
}

function renderApp() {
  const app = h("div", { class: "app" + (state.sidebarOpen ? " sidebar-open" : "") },
    sidebar(),
    h("main", { class: "chat" }, chatHead(), h("div", { class: "thread", id: "thread" }), state.project ? composer() : null),
    panel(),
  );
  mount(app);
  renderThread(true);
  if (state.panelOpen) renderPanel();
}

function sidebar() {
  const me = state.me;
  const name = me.github ? me.github.login : me.user;
  return h("aside", { class: "sidebar" },
    h("div", { class: "sb-head" }, h("div", { class: "logo sm" }, icon("code", "sm")), h("span", { class: "name grow" }, "Coding Agent"),
      h("button", { class: "btn ghost sm icon-only only-mobile", title: "收起", onclick: () => { state.sidebarOpen = false; renderApp(); } }, icon("x"))),
    h("button", { class: "btn sb-new", onclick: () => openNewProject() }, icon("plus"), "新建项目"),
    h("div", { class: "sb-label" }, "项目"),
    h("div", { class: "sb-list" },
      state.projects.length === 0 ? h("div", { class: "muted", style: null }, h("div", { class: "sb-item" }, "还没有项目")) : null,
      state.projects.map((p) => h("button", {
        class: "sb-item" + (state.project && state.project.id === p.id ? " active" : ""),
        title: p.name,
        onclick: () => { state.sidebarOpen = false; openProject(p.id); },
      }, icon(p.github_repo ? "github" : p.source && p.source.startsWith("sample:") ? "box" : "folder", "sm"),
        h("div", { class: "grow" }, h("div", { class: "ellipsis" }, p.name), h("div", { class: "meta ellipsis" }, sourceLabel(p))))),
    ),
    h("div", { class: "sb-foot" },
      h("div", { class: "avatar" }, me.github && me.github.avatar_url ? h("img", { src: me.github.avatar_url, alt: "" }) : initials(name)),
      h("div", { class: "grow" }, h("div", { class: "ellipsis" }, name), h("div", { class: "meta muted ellipsis" }, me.tenant)),
      h("button", { class: "btn ghost sm icon-only", title: "退出", onclick: logout }, icon("logout", "sm")),
    ),
  );
}

function sourceLabel(p) {
  if (p.github_repo) return p.github_repo;
  if (!p.source || p.source === "empty") return "空项目";
  if (p.source.startsWith("sample:")) return "示例仓库";
  return p.source.replace(/^https:\/\//, "");
}

function chatHead() {
  const p = state.project;
  const changed = state.changes ? state.changes.files.length : 0;
  return h("header", { class: "chat-head" },
    h("button", { class: "btn ghost icon-only only-mobile", title: "项目", onclick: () => { state.sidebarOpen = true; renderApp(); } }, icon("menu")),
    p ? h("div", { class: "grow row" },
      h("span", { class: "title ellipsis" }, p.name),
      p.branch ? h("span", { class: "chip run mono" }, icon("branch", "sm"), p.branch) : p.base_branch ? h("span", { class: "chip mono" }, icon("branch", "sm"), p.base_branch) : null,
      p.pr_url ? h("a", { class: "chip ok", href: p.pr_url, target: "_blank", rel: "noopener" }, icon("github", "sm"), `PR #${p.pr_number}`) : null,
    ) : h("div", { class: "grow" }),
    p && p.branch ? h("button", { class: "btn ghost sm", title: `回到 ${p.base_branch}`, onclick: resetBranch }, icon("refresh", "sm"), p.base_branch) : null,
    p ? h("button", { class: "btn sm" + (state.panelOpen && state.panelTab === "changes" ? " primary" : ""), onclick: () => togglePanel("changes") },
      icon("diff", "sm"), "改动", changed ? h("span", { class: "chip run" }, changed) : null) : null,
    p ? h("button", { class: "btn ghost sm icon-only", title: "文件与历史", onclick: () => togglePanel(state.panelTab === "changes" ? "files" : state.panelTab) }, icon("panel", "sm")) : null,
    p ? h("button", { class: "btn ghost sm icon-only danger", title: "删除项目", onclick: deleteProject }, icon("trash", "sm")) : null,
  );
}

function refreshHead() {
  const head = document.querySelector(".chat-head");
  if (head) head.replaceWith(chatHead());
}

// ---------------------------------------------------------------- 对话

async function openProject(id) {
  closeStreams();
  state.project = state.projects.find((p) => p.id === id) || null;
  state.tasks = [];
  state.runs = new Map();
  state.changes = null;
  state.filesPath = "";
  location.hash = state.project ? `#/p/${id}` : "";
  if (state.project) {
    const tasks = await api("GET", `/api/projects/${id}/tasks`);
    state.tasks = tasks.slice().reverse();
  }
  renderApp();
  if (state.project) {
    loadChangesCount();
    replayTasks();
  }
}

function renderThread(stick = false) {
  const thread = document.getElementById("thread");
  if (!thread) return;
  const nearBottom = stick || thread.scrollHeight - thread.scrollTop - thread.clientHeight < 120;
  let content;
  if (!state.project) content = welcome();
  else if (!state.tasks.length) content = projectEmpty();
  else content = h("div", { class: "thread-inner" }, state.tasks.map(turn));
  thread.replaceChildren(content);
  if (nearBottom) thread.scrollTop = thread.scrollHeight;
}

function renderTurn(taskId) {
  const old = document.getElementById("turn-" + taskId);
  const t = state.tasks.find((x) => x.id === taskId);
  if (!old || !t) return renderThread();
  const thread = document.getElementById("thread");
  const nearBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 120;
  old.replaceWith(turn(t));
  if (nearBottom) thread.scrollTop = thread.scrollHeight;
}

function welcome() {
  const sample = state.samples[0];
  return h("div", { class: "hero-empty" },
    h("div", { class: "logo" }, icon("sparkles", "lg")),
    h("h2", {}, "从一个项目开始"),
    h("p", {}, "Agent 会在项目的工作区里读代码、改代码，在隔离的沙箱里跑测试；改动由你审阅后提交或推送。"),
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
    ["code", "讲讲这个仓库", "先通读仓库，说明它的结构、入口和主要模块，不要改任何文件。"],
    ["check", "找出并修复失败的测试", "运行项目的测试，找出失败的用例，定位原因并修好；改完再跑一遍确认。"],
    ["terminal", "补单元测试", "为核心模块补充单元测试，覆盖主要分支与边界情况，并确认全部通过。"],
  ].filter(Boolean);
  return h("div", { class: "hero-empty" },
    h("div", { class: "logo" }, icon("sparkles", "lg")),
    h("h2", {}, "要 Agent 做什么？"),
    h("p", {}, "用一句话描述任务。它会规划、动手、自我检查，你在右侧审阅改动。"),
    h("div", { class: "suggestions" }, ideas.map(([ic, t, prompt]) => suggestion(ic, t, prompt.length > 60 ? prompt.slice(0, 58) + "…" : prompt, () => setComposer(prompt)))),
  );
}

function turn(t) {
  const run = state.runs.get(t.id);
  const events = run ? run.events : [];
  const finished = events.find((e) => e.type === "task_finished");
  const status = finished ? finished.value.status || t.status : t.status;
  const running = !TERMINAL.has(status);
  return h("section", { class: "turn", id: "turn-" + t.id },
    h("div", { class: "msg-user" }, t.message, h("div", { class: "when" }, `${t.created_by} · ${fmtTime(t.created_at)}`)),
    h("div", { class: "msg-agent" },
      h("div", { class: "agent-avatar" }, icon("sparkles", "sm")),
      h("div", { class: "agent-body" },
        worklog(t, events, running, finished),
        approvals(t, events),
        finished ? result(finished.value) : running ? h("div", { class: "row muted" }, h("span", { class: "spinner" }), run ? "正在工作…" : "正在连接…",
          h("button", { class: "btn ghost sm", onclick: () => cancelTask(t.id) }, icon("stop", "sm"), "停止")) :
          !run ? h("div", { class: "row muted" }, statusChip(status), h("button", { class: "btn ghost sm", onclick: () => loadRun(t, true) }, "加载详情")) : null,
      ),
    ),
  );
}

function worklog(t, events, running, finished) {
  const steps = workSteps(events);
  if (!steps.length && !running) return null;
  const isOpen = state.open.has(t.id) || (running && !state.closed.has(t.id));
  const first = events[0], last = events[events.length - 1];
  const elapsed = first && last ? new Date(finished ? finished.time : Date.now()) - new Date(first.time) : 0;
  const head = running
    ? [h("span", { class: "spinner" }), h("span", { class: "grow", id: "wl-" + t.id }, workingText(steps.length, elapsed))]
    : [icon(finished && finished.value.status === "TASK_STATUS_DONE" ? "check" : "clock", "sm"), h("span", { class: "grow" }, `已工作 ${fmtDuration(elapsed)} · ${steps.length} 步`)];
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
  return events.filter((e) => !["heartbeat", "token_delta", "thinking_delta", "task_finished", "human_input_required"].includes(e.type));
}

function pick(obj, ...keys) {
  for (const k of keys) if (obj && obj[k] !== undefined && obj[k] !== null && obj[k] !== "") return obj[k];
  return undefined;
}

function step(ev) {
  const v = ev.value || {};
  let ic = "box", kind = "", title = ev.type, detail = "", chips = null;
  switch (ev.type) {
    case "task_accepted": ic = "check"; kind = "ok"; title = "任务已受理"; break;
    case "round_started": ic = "play"; kind = "run"; title = `第 ${pick(v, "round_seq") ?? ""} 轮`; detail = v.rerun ? "重新规划" : ""; break;
    case "stage_started": {
      ic = "layers"; kind = "run";
      title = "计划：" + (pick(v, "goal") || "执行");
      detail = pick(v, "exit_when") ? "完成标准：" + v.exit_when : "";
      const nodes = Array.isArray(v.nodes) ? v.nodes : [];
      chips = nodes.length ? h("div", { class: "step-chips" }, nodes.map((n) => h("span", { class: "chip mono" }, typeof n === "string" ? n : pick(n, "id") || "节点"))) : null;
      if (v.triage_level) detail = (detail ? detail + "\n" : "") + "分诊：" + v.triage_level;
      break;
    }
    case "node_started": ic = "terminal"; title = "开始 " + (pick(v, "node_id") || "节点"); break;
    case "node_finished": {
      const st = String(pick(v, "status") || "");
      const failed = /fail|denied|error/i.test(st);
      ic = failed ? "x" : "check"; kind = failed ? "err" : "ok";
      title = (failed ? "失败：" : "完成：") + (pick(v, "node_id") || "节点");
      detail = [pick(v, "role"), st && st !== "succeeded" ? st : ""].filter(Boolean).join(" · ");
      break;
    }
    case "tool_status": ic = "terminal"; title = "工具 " + (pick(v, "tool", "name") || ""); detail = pick(v, "status", "message") || ""; break;
    case "verdict": {
      const verdict = String(pick(v, "verdict") || "").replace(/^VERDICT_/, "").toLowerCase();
      ic = "shield"; kind = verdict === "done" ? "ok" : verdict === "abort" ? "err" : "warn";
      title = "Reviewer 判定：" + (verdict || "—");
      detail = pick(v, "reason") || "";
      break;
    }
  }
  return h("div", { class: `step ${kind}` },
    h("div", { class: "step-icon" }, icon(ic, "sm")),
    h("div", { class: "step-main" },
      h("div", { class: "step-title" }, title),
      detail ? h("div", { class: "step-detail" }, detail) : null,
      chips,
      h("details", { class: "raw" }, h("summary", {}, "原始事件"), h("pre", {}, JSON.stringify(v, null, 2))),
    ),
    h("span", { class: "step-time" }, fmtClock(ev.time)),
  );
}

function approvals(t, events) {
  const asks = events.filter((e) => e.type === "human_input_required");
  return asks.map((ev) => {
    const v = ev.value || {};
    const qid = pick(v, "question_id", "questionId");
    const answered = state.replied.has(qid) || events.some((e) => e.event_id > ev.event_id && !["heartbeat", "token_delta", "thinking_delta"].includes(e.type));
    if (answered) return h("div", { class: "row muted" }, icon("check", "sm"), "已回复：" + (pick(v, "question", "prompt", "text") || "确认"));
    const raw = pick(v, "options", "choices") || ["approve", "reject"];
    const options = raw.map((o) => (typeof o === "string" ? { id: o, label: o } : { id: pick(o, "id", "value", "option"), label: pick(o, "label", "text", "id", "value") }));
    const guidance = h("textarea", { class: "field", rows: 2, placeholder: "可选：给 Agent 的补充说明" });
    return h("div", { class: "approval" },
      h("div", { class: "head" }, icon("alert", "sm"), "需要你确认"),
      h("div", { class: "q" }, pick(v, "question", "prompt", "text", "summary", "message") || "Agent 请求确认后再继续。"),
      guidance,
      h("div", { class: "actions" }, options.map((o) => h("button", {
        class: "btn sm" + (/approve|yes|continue/i.test(o.id) ? " primary" : ""),
        onclick: async (e) => {
          e.currentTarget.disabled = true;
          try {
            await api("POST", `/api/projects/${state.project.id}/tasks/${t.id}/reply`, { question_id: qid, option: o.id, guidance: guidance.value });
            state.replied.add(qid);
            renderTurn(t.id);
          } catch (ex) { report(ex); }
        },
      }, optionLabel(o.label)))),
    );
  });
}

function optionLabel(label) {
  return { approve: "批准", reject: "拒绝", continue: "原样继续", replan: "带指导重规划", stop: "停止并收尾" }[label] || label;
}

function deliverableText(result) {
  const d = result.deliverable && result.deliverable.value !== undefined ? result.deliverable.value : result.deliverable;
  if (typeof d === "string") return d;
  if (!d || typeof d !== "object") return "";
  let t = pick(d, "answer", "summary", "text", "content", "result", "report", "message");
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
  return "```\n" + JSON.stringify(d, null, 2) + "\n```";
}

function result(r) {
  const text = deliverableText(r);
  const ok = r.status === "TASK_STATUS_DONE";
  const usage = r.usage || {};
  const usageChips = Object.entries(usage).filter(([, v]) => typeof v !== "object" && v).slice(0, 4).map(([k, v]) => h("span", { class: "chip" }, `${k}: ${v}`));
  return h("div", { class: "col" },
    text ? h("div", { class: "answer" }, md(text)) : null,
    r.error ? h("div", { class: "error-box" }, typeof r.error === "string" ? r.error : pick(r.error, "message") || JSON.stringify(r.error)) : null,
    h("div", { class: "result-foot" },
      statusChip(r.status),
      r.rounds ? h("span", { class: "chip" }, `${r.rounds} 轮`) : null,
      usageChips,
      h("span", { class: "grow" }),
      text ? h("button", { class: "btn ghost sm", onclick: () => navigator.clipboard.writeText(text).then(() => toast("已复制")) }, icon("copy", "sm"), "复制") : null,
      ok ? h("button", { class: "btn sm", onclick: () => openPanel("changes") }, icon("diff", "sm"), "审阅改动") : null,
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
      if (type === "task_finished") {
        t.status = ev.value.status || t.status;
        if (!state.closed.has(t.id)) state.open.delete(t.id);
        finish();
        loadChangesCount();
      }
      scheduleTurn(t.id);
    };
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

async function cancelTask(taskId) {
  if (!confirm("停止这个任务？已经做的改动会留在工作区里。")) return;
  try {
    await api("POST", `/api/projects/${state.project.id}/tasks/${taskId}/cancel`);
    toast("已请求停止");
  } catch (ex) { report(ex); }
}

// ---------------------------------------------------------------- 输入框

function composer() {
  const box = h("textarea", { id: "composer", rows: 1, placeholder: "让 Agent 做点什么…（例如：修好失败的测试）" });
  const send = h("button", { class: "send", title: "发送", disabled: true }, icon("send"));
  const resize = () => { box.style.height = "auto"; box.style.height = Math.min(box.scrollHeight, 220) + "px"; send.disabled = !box.value.trim() || state.sending; };
  box.addEventListener("input", resize);
  box.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  });
  const submit = async () => {
    const message = box.value.trim();
    if (!message || state.sending) return;
    state.sending = true;
    send.disabled = true;
    try {
      const t = await api("POST", `/api/projects/${state.project.id}/tasks`, { message });
      state.tasks.push(t);
      box.value = "";
      resize();
      renderThread(true);
      loadRun(t);
    } catch (ex) { report(ex); } finally {
      state.sending = false;
      resize();
      box.focus();
    }
  };
  send.addEventListener("click", submit);
  return h("div", { class: "composer-wrap" },
    h("div", { class: "composer" }, box, send),
    h("div", { class: "composer-hint" }, h("span", {}, "Enter 发送 · Shift+Enter 换行"), h("span", { class: "grow" }), h("span", {}, "命令在隔离沙箱里运行，改动在「改动」里审阅")),
  );
}

function setComposer(text) {
  const box = document.getElementById("composer");
  if (!box) return;
  box.value = text;
  box.dispatchEvent(new Event("input"));
  box.focus();
}

// ---------------------------------------------------------------- 右侧面板

function panel() {
  return h("aside", { class: "panel" + (state.panelOpen && state.project ? "" : " closed"), id: "panel" });
}

function togglePanel(tab) {
  if (state.panelOpen && state.panelTab === tab) state.panelOpen = false;
  else { state.panelOpen = true; state.panelTab = tab; }
  renderPanel();
  refreshHead();
}

function openPanel(tab) {
  state.panelOpen = true;
  state.panelTab = tab;
  renderPanel();
  refreshHead();
}

function renderPanel() {
  const el = document.getElementById("panel");
  if (!el) return;
  el.className = "panel" + (state.panelOpen && state.project ? "" : " closed");
  if (!state.panelOpen || !state.project) return el.replaceChildren();
  const body = h("div", { class: "panel-body" });
  const n = state.changes ? state.changes.files.length : 0;
  const tab = (key, ic, label, extra) => h("button", { class: "ptab" + (state.panelTab === key ? " active" : ""), onclick: () => { state.panelTab = key; renderPanel(); refreshHead(); } }, icon(ic, "sm"), label, extra);
  el.replaceChildren(
    h("div", { class: "panel-head" },
      tab("changes", "diff", "改动", n ? h("span", { class: "n" }, n) : null),
      tab("files", "file", "文件"),
      tab("history", "history", "历史"),
      h("span", { class: "grow" }),
      h("button", { class: "btn ghost sm icon-only", title: "关闭", onclick: () => { state.panelOpen = false; renderPanel(); refreshHead(); } }, icon("x", "sm")),
    ),
    body,
  );
  if (state.panelTab === "changes") loadChanges(body);
  else if (state.panelTab === "files") loadFiles(body, state.filesPath);
  else loadHistory(body);
}

async function loadChangesCount() {
  if (!state.project) return;
  try {
    state.changes = await api("GET", `/api/projects/${state.project.id}/changes`);
    refreshHead();
    if (state.panelOpen && state.panelTab === "changes") renderPanel();
  } catch { /* 下次再取 */ }
}

function running() {
  return state.tasks.some((t) => !TERMINAL.has(t.status));
}

async function loadChanges(body) {
  body.replaceChildren(h("div", { class: "row muted" }, h("span", { class: "spinner" }), "正在读取改动…"));
  let data;
  try {
    data = await api("GET", `/api/projects/${state.project.id}/changes`);
    state.changes = data;
  } catch (ex) {
    body.replaceChildren(h("div", { class: "error-box" }, ex.message));
    return;
  }
  const busy = running();
  const parts = [];
  if (!data.files.length) {
    parts.push(h("div", { class: "card" },
      h("div", { class: "card-title" }, icon("check", "sm"), "工作区是干净的"),
      h("div", { class: "muted" }, "当前分支 ", h("span", { class: "mono" }, data.branch || "—"), " · HEAD ", h("span", { class: "mono" }, (data.head || "").slice(0, 10)))));
  } else {
    const added = data.files.reduce((n, f) => n + f.added, 0);
    const deleted = data.files.reduce((n, f) => n + f.deleted, 0);
    const msg = h("input", { class: "field", placeholder: "提交说明", value: lastDoneMessage() });
    const commit = h("button", { class: "btn primary sm", disabled: busy, onclick: async () => {
      commit.disabled = true;
      try {
        const r = await api("POST", `/api/projects/${state.project.id}/commit`, { message: msg.value });
        toast("已提交 " + r.head.slice(0, 8));
        loadChangesCount();
        renderPanel();
      } catch (ex) { commit.disabled = false; report(ex); }
    } }, icon("commit", "sm"), "提交");
    parts.push(h("div", { class: "card" },
      h("div", { class: "card-title" }, `${data.files.length} 个文件有改动`, h("span", { class: "stat-add" }, `+${added}`), h("span", { class: "stat-del" }, `−${deleted}`),
        busy ? h("span", { class: "chip run" }, h("span", { class: "spinner" }), "任务进行中") : null),
      h("div", { class: "actions" }, h("div", { class: "grow" }, msg), commit,
        h("a", { class: "btn sm", href: `/api/projects/${state.project.id}/patch`, download: `${state.project.id}.patch` }, icon("download", "sm"), "补丁"),
        h("button", { class: "btn sm danger", disabled: busy, onclick: discard }, icon("trash", "sm"), "丢弃")),
    ));
  }
  const pub = publishCard(busy, data.files.length > 0);
  if (pub) parts.push(pub);
  parts.push(...renderDiff(data.diff));
  if (data.truncated) parts.push(h("div", { class: "muted" }, "改动太大，只显示了前 2 MB；完整内容请下载补丁。"));
  body.replaceChildren(...parts);
}

async function discard() {
  if (!confirm("丢弃全部未提交的改动？这一步不能撤销。")) return;
  try {
    await api("POST", `/api/projects/${state.project.id}/discard`);
    toast("已丢弃改动");
    loadChangesCount();
    renderPanel();
  } catch (ex) { report(ex); }
}

function lastDoneMessage() {
  // 默认提交说明：最近一条像样的任务描述（不管结局，跳过“-”这类过短的）。
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
  if (!state.me.github) return h("div", { class: "card" }, h("div", { class: "card-title" }, icon("github", "sm"), p.github_repo), h("div", { class: "muted" }, "用 GitHub 登录后，可以把改动推到 agent/* 分支并开草稿 PR。"));
  const title = h("input", { class: "field", placeholder: "PR 标题，也用作提交说明", value: lastDoneMessage() });
  const out = h("div", {});
  const label = p.pr_number ? `推送到 PR #${p.pr_number}` : "推送并开草稿 PR";
  const push = h("button", { class: "btn primary sm", disabled: busy, onclick: async () => {
    push.disabled = true;
    push.lastChild.textContent = "正在推送…";
    try {
      const r = await api("POST", `/api/projects/${p.id}/publish`, { title: title.value });
      setProject(r.project);
      toast(r.created ? `已开草稿 PR #${r.pr_number}` : `已推送到 ${r.branch}`);
      refreshHead();
      loadChangesCount();
      renderPanel();
    } catch (ex) {
      push.disabled = false;
      push.lastChild.textContent = label;
      if (ex.message !== "unauthorized") out.replaceChildren(h("div", { class: "error-box" }, ex.message));
    }
  } }, icon("github", "sm"), h("span", {}, label));
  return h("div", { class: "card" },
    h("div", { class: "card-title" }, icon("github", "sm"), h("span", { class: "mono grow ellipsis" }, p.github_repo),
      p.pr_url ? h("a", { class: "chip ok", href: p.pr_url, target: "_blank", rel: "noopener" }, `PR #${p.pr_number}`, icon("external", "sm")) : null),
    h("div", { class: "muted" }, p.branch
      ? `${dirty ? "提交未提交的改动后" : ""}推送到 ${p.branch}，PR 自动更新。`
      : `新建 agent/… 分支，提交后推送，再向 ${p.base_branch || "基线分支"} 开草稿 PR；不会推到别的分支，也不强推。`),
    h("div", { class: "actions" }, h("div", { class: "grow" }, title), push),
    out,
  );
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
    const box = h("div", { class: "diff-file" });
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
    body.replaceChildren(h("div", { class: "card" }, commits.map((c) => h("div", { class: "commit-row" },
      h("span", { class: "hash" }, c.hash.slice(0, 7)),
      h("div", { class: "grow" }, h("div", {}, c.subject), h("div", { class: "muted" }, `${c.author} · ${fmtTime(c.date)}`)),
    ))));
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
    refreshHead();
    loadChangesCount();
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

// ---------------------------------------------------------------- 启动

setInterval(() => {
  // 进行中的任务：只更新工作时长那段文字，不重画整轮（不打断正在输入的审批说明）。
  for (const t of state.tasks) {
    const run = state.runs.get(t.id);
    const el = document.getElementById("wl-" + t.id);
    if (!run || !el || !run.events.length) continue;
    el.textContent = workingText(workSteps(run.events).length, Date.now() - new Date(run.events[0].time));
  }
}, 1000);

window.addEventListener("hashchange", () => {
  const wanted = decodeURIComponent((location.hash.match(/^#\/p\/([^/?]+)/) || [])[1] || "");
  if (state.me && wanted && (!state.project || state.project.id !== wanted) && state.projects.some((p) => p.id === wanted)) openProject(wanted);
});

async function boot() {
  try { state.config = await api("GET", "/api/config"); } catch { /* 用默认 */ }
  try { state.me = await api("GET", "/api/me"); } catch { state.me = null; }
  if (state.me) await enterApp().catch(report);
  else renderLogin();
}

boot();
