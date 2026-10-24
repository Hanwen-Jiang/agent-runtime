// Coding Agent 网页（原生 JS，无构建步骤）。DOM 一律用 textContent 构造，不拼 HTML。

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

const state = {
  config: { dev_login: false },
  me: null,
  projects: [],
  samples: [],
  project: null,
  tab: "tasks",
  tasks: [],
  taskId: null,
  events: [],
  stream: null,
  replied: new Set(),
  poll: null,
  filesPath: "",
  file: null,
};

// --- DOM ---

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v === null || v === undefined) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function mount(node) {
  const app = document.getElementById("app");
  app.className = "";
  app.replaceChildren(node);
}

function toast(text, isError = false) {
  const el = h("div", { class: "toast" + (isError ? " err" : "") }, text);
  document.body.append(el);
  setTimeout(() => el.remove(), isError ? 6000 : 3000);
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleString();
}

function ago(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return d.toLocaleDateString();
}

function statusBadge(status) {
  const [label, kind] = STATUS[status] || [status || "未知", ""];
  return h("span", { class: `badge ${kind}` }, label);
}

// --- API ---

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

// --- 登录 ---

function renderLogin(message = "") {
  stopStream();
  const tenant = h("input", { placeholder: "例如 acme", autocomplete: "organization", value: localStorage.getItem("cw_tenant") || "" });
  const user = h("input", { placeholder: "例如 alice", autocomplete: "username", value: localStorage.getItem("cw_user") || "" });
  const err = h("div", { class: "error-text" }, message);
  const submit = h("button", { class: "btn primary", type: "submit" }, "进入");
  const form = h("form", { class: "stack", onsubmit: async (e) => {
    e.preventDefault();
    submit.disabled = true;
    err.textContent = "";
    try {
      const me = await api("POST", "/api/login", { tenant: tenant.value, user: user.value });
      localStorage.setItem("cw_tenant", me.tenant);
      localStorage.setItem("cw_user", me.user);
      state.me = me;
      await enterApp();
    } catch (ex) {
      if (ex.message !== "unauthorized") err.textContent = ex.message;
    } finally {
      submit.disabled = false;
    }
  } },
    h("div", {}, h("label", {}, "租户（团队或组织）"), tenant),
    h("div", {}, h("label", {}, "用户名"), user),
    err,
    submit,
  );
  mount(h("div", { class: "login-wrap" },
    h("div", { class: "login stack" },
      h("div", { class: "row" }, h("span", { class: "brand-mark" }, "</>"), h("h1", {}, "Coding Agent")),
      h("div", { class: "muted" }, "在多租户 Agent Runtime 上运行的编码智能体：下任务、看进度、审改动。"),
      state.config.dev_login
        ? [h("div", { class: "notice" }, "测试版：开发登录，不校验密码，只用于试用。"), form]
        : h("div", { class: "notice" }, "服务端没有打开开发登录（CODING_DEV_LOGIN=1）。"),
    ),
  ));
  (tenant.value ? user : tenant).focus();
}

async function logout() {
  await api("POST", "/api/logout").catch(() => {});
  state.me = null;
  state.project = null;
  renderLogin();
}

// --- 主界面 ---

async function enterApp() {
  [state.projects, state.samples] = await Promise.all([api("GET", "/api/projects"), api("GET", "/api/samples")]);
  const wanted = decodeURIComponent(location.hash.replace(/^#\/?p\//, "").split("/")[0] || "");
  state.project = state.projects.find((p) => p.id === wanted) || state.projects[0] || null;
  if (state.project) await openProject(state.project.id, state.tab);
  else renderShell();
}

function renderShell() {
  const sidebar = h("aside", { class: "sidebar" },
    h("div", { class: "head" }, h("h2", {}, "项目"), h("span", { class: "spacer" }),
      h("button", { class: "btn small primary", onclick: openNewProject }, "+ 新建")),
    h("div", { class: "project-list" },
      state.projects.length === 0 ? h("div", { class: "muted", }, "还没有项目") : null,
      state.projects.map((p) => h("button", {
        class: "project-item" + (state.project && state.project.id === p.id ? " active" : ""),
        onclick: () => openProject(p.id, "tasks"),
      }, h("div", { class: "name" }, p.name), h("div", { class: "meta" }, sourceLabel(p.source)))),
    ),
  );
  const top = h("header", { class: "topbar" },
    h("span", { class: "brand-mark" }, "</>"),
    h("span", { class: "title" }, "Coding Agent"),
    h("span", { class: "badge test" }, "测试版"),
    h("span", { class: "spacer" }),
    h("span", { class: "muted" }, `${state.me.user} @ ${state.me.tenant}`),
    h("button", { class: "btn small ghost", onclick: logout }, "退出"),
  );
  const main = h("main", { class: "main" }, state.project ? projectView() : emptyMain());
  mount(h("div", { class: "shell" }, top, h("div", { class: "body" }, sidebar, main)));
}

function sourceLabel(source) {
  if (!source || source === "empty") return "空项目";
  if (source.startsWith("sample:")) return "示例：" + source.slice(7);
  return source.replace(/^https:\/\//, "");
}

function emptyMain() {
  return h("div", { class: "content" }, h("div", { class: "empty" },
    h("h2", {}, "先建一个项目"),
    h("p", {}, "可以从 Git 地址克隆，也可以用内置的示例仓库直接试一试。"),
    h("button", { class: "btn primary", onclick: openNewProject }, "新建项目"),
  ));
}

async function openProject(id, tab) {
  stopStream();
  state.project = state.projects.find((p) => p.id === id) || null;
  state.tab = tab || "tasks";
  state.taskId = null;
  state.events = [];
  state.filesPath = "";
  state.file = null;
  location.hash = state.project ? `#/p/${id}` : "";
  if (state.project) state.tasks = await api("GET", `/api/projects/${id}/tasks`);
  if (state.tasks.length) state.taskId = state.tasks[0].id;
  renderShell();
  if (state.taskId) startStream(state.taskId);
  schedulePoll();
}

function projectView() {
  const p = state.project;
  const tabs = [["tasks", "任务", state.tasks.length], ["changes", "改动"], ["files", "文件"], ["history", "提交历史"]];
  const content = h("div", { class: "content", id: "tab-content" });
  const view = h("div", { class: "main" },
    h("div", { class: "project-head" },
      h("div", { class: "row" },
        h("div", { class: "grow" }, h("h1", {}, p.name), h("div", { class: "muted mono" }, sourceLabel(p.source))),
        h("button", { class: "btn small danger", onclick: deleteProject }, "删除项目"),
      ),
      h("div", { class: "tabs" }, tabs.map(([key, label, count]) => h("button", {
        class: "tab" + (state.tab === key ? " active" : ""),
        onclick: () => { state.tab = key; renderShell(); },
      }, label, count ? h("span", { class: "count muted" }, count) : null))),
    ),
    content,
  );
  queueMicrotask(() => renderTab(content));
  return view;
}

function renderTab(content) {
  if (state.tab === "tasks") content.replaceChildren(tasksView());
  else if (state.tab === "changes") loadChanges(content);
  else if (state.tab === "files") loadFiles(content, state.filesPath);
  else if (state.tab === "history") loadHistory(content);
}

// --- 任务 ---

function tasksView() {
  const sample = state.samples.find((s) => state.project.source === "sample:" + s.id);
  const input = h("textarea", { placeholder: "描述要做的事，例如：修好失败的测试；给 /users 接口加分页……" });
  const send = h("button", { class: "btn primary", onclick: async () => {
    const message = input.value.trim();
    if (!message) return input.focus();
    send.disabled = true;
    try {
      const t = await api("POST", `/api/projects/${state.project.id}/tasks`, { message });
      state.tasks.unshift(t);
      state.taskId = t.id;
      state.events = [];
      input.value = "";
      renderShell();
      startStream(t.id);
      schedulePoll();
    } catch (ex) {
      if (ex.message !== "unauthorized") toast(ex.message, true);
    } finally {
      send.disabled = false;
    }
  } }, "发送任务");
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send.click(); });
  const composer = h("div", { class: "card card-pad composer stack" },
    input,
    h("div", { class: "row" },
      sample ? h("button", { class: "btn small", onclick: () => { input.value = sample.prompt; input.focus(); } }, "填入示例任务") : null,
      h("span", { class: "muted" }, "Ctrl + Enter 发送"),
      h("span", { class: "spacer" }), send),
  );
  const list = h("div", { class: "task-list" },
    state.tasks.map((t) => h("button", {
      class: "task-item" + (t.id === state.taskId ? " active" : ""),
      onclick: () => { state.taskId = t.id; state.events = []; renderShell(); startStream(t.id); },
    }, h("div", { class: "msg" }, t.message), h("div", { class: "meta" }, statusBadge(t.status), ago(t.created_at), t.created_by))),
  );
  const current = state.tasks.find((t) => t.id === state.taskId);
  return h("div", { class: "tasks-layout" },
    h("div", {}, composer, list),
    current ? taskDetail(current) : h("div", { class: "card card-pad empty" }, h("h2", {}, "还没有任务"), h("p", {}, "在左边描述要做的事，Agent 会读代码、改代码、在沙箱里跑测试。")),
  );
}

function taskDetail(t) {
  const finished = state.events.find((e) => e.type === "task_finished");
  const running = !TERMINAL.has(t.status) && !finished;
  const timeline = h("ol", { class: "timeline" }, state.events.filter((e) => !["heartbeat", "token_delta", "thinking_delta"].includes(e.type)).map(timelineItem));
  if (!state.events.length) timeline.append(h("li", { class: "tl-item run" }, h("span", { class: "tl-dot" }), h("div", { class: "tl-body" }, "正在连接进度流…")));
  return h("div", { class: "card card-pad task-detail", id: "task-detail" },
    h("div", { class: "head" },
      h("div", { class: "grow msg" }, t.message),
      statusBadge(finished ? (finished.result.status || t.status) : t.status),
      running ? h("button", { class: "btn small danger", onclick: () => cancelTask(t.id) }, "取消") : null,
    ),
    h("div", { class: "muted" }, `${t.created_by} · ${fmtTime(t.created_at)} · 基线 ${t.base_commit ? t.base_commit.slice(0, 8) : "—"}`),
    timeline,
    finished ? resultView(finished.result) : null,
  );
}

function payloadValue(ev) {
  return (ev && ev.payload && ev.payload.value) || {};
}

function pick(obj, ...keys) {
  for (const k of keys) {
    if (obj && obj[k] !== undefined && obj[k] !== null && obj[k] !== "") return obj[k];
  }
  return undefined;
}

function short(v, n = 400) {
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > n ? s.slice(0, n) + "…" : s;
}

function timelineItem(ev) {
  const v = ev.value;
  let title = ev.type, body = "", kind = "", extra = null;
  switch (ev.type) {
    case "task_accepted": title = "任务已受理"; kind = "ok"; break;
    case "round_started": title = `第 ${pick(v, "round_seq", "roundSeq", "seq") ?? ""} 轮开始`; body = pick(v, "triage_level", "triageLevel", "reason") || ""; kind = "run"; break;
    case "stage_started": title = "阶段：" + (pick(v, "goal", "stage_goal", "stageGoal") || "执行"); body = describeNodes(v); kind = "run"; break;
    case "node_started": title = "开始：" + (pick(v, "node_id", "nodeId", "id") || "节点"); break;
    case "node_finished": {
      const st = String(pick(v, "status", "state") || "");
      title = "完成：" + (pick(v, "node_id", "nodeId", "id") || "节点");
      body = [st.replace(/^NODE_STATUS_/, "").toLowerCase(), pick(v, "template", "tool")].filter(Boolean).join(" · ");
      kind = /fail|denied|error/i.test(st) ? "err" : "ok";
      break;
    }
    case "tool_status": title = "工具：" + (pick(v, "tool", "name") || ""); body = pick(v, "status", "message") || ""; break;
    case "verdict": {
      const verdict = String(pick(v, "verdict", "decision") || "").replace(/^VERDICT_/, "").toLowerCase();
      title = "Reviewer 判定：" + (verdict || "—");
      body = pick(v, "reason", "rationale_summary", "summary", "explanation") || "";
      kind = verdict === "done" ? "ok" : verdict === "abort" ? "err" : "warn";
      break;
    }
    case "human_input_required": title = "需要你确认"; kind = "warn"; extra = approvalCard(ev); break;
    case "task_finished": {
      const st = ev.result.status || "";
      title = "任务结束：" + ((STATUS[st] || [st])[0]);
      kind = st === "TASK_STATUS_DONE" ? "ok" : "err";
      break;
    }
  }
  return h("li", { class: `tl-item ${kind}` },
    h("span", { class: "tl-dot" }),
    h("div", { class: "tl-title" }, title, h("span", { class: "tl-time" }, ev.time ? new Date(ev.time).toLocaleTimeString() : "")),
    body ? h("div", { class: "tl-body" }, short(body)) : null,
    extra,
    h("details", { class: "raw" }, h("summary", {}, "原始数据"), h("pre", {}, JSON.stringify(v, null, 2))),
  );
}

function describeNodes(v) {
  const nodes = pick(v, "nodes", "node_ids", "nodeIds");
  if (!Array.isArray(nodes) || !nodes.length) return "";
  return nodes.map((n) => (typeof n === "string" ? n : pick(n, "id", "goal", "template") || "")).filter(Boolean).join("、");
}

function approvalCard(ev) {
  const v = ev.value;
  const qid = pick(v, "question_id", "questionId");
  const question = pick(v, "question", "prompt", "text", "summary", "message") || "Agent 请求确认后再继续。";
  const rawOptions = pick(v, "options", "choices") || ["approve", "reject"];
  const options = rawOptions.map((o) => (typeof o === "string" ? { id: o, label: o } : { id: pick(o, "id", "value", "option"), label: pick(o, "label", "text", "id", "value") }));
  const answered = state.replied.has(qid) || state.events.some((e) => e.event_id > ev.event_id && e.type !== "heartbeat");
  const guidance = h("textarea", { placeholder: "可选：给 Agent 的补充说明" });
  const card = h("div", { class: "approval" },
    h("div", { class: "q" }, question),
    pick(v, "kind") ? h("div", { class: "muted" }, "类型：" + pick(v, "kind")) : null,
    answered ? h("div", { class: "muted" }, "已回复") : [
      guidance,
      h("div", { class: "opts" }, options.map((o) => h("button", {
        class: "btn small" + (/approve|yes|continue|同意|批准/i.test(o.id) ? " primary" : ""),
        onclick: async (e) => {
          e.target.disabled = true;
          try {
            await api("POST", `/api/projects/${state.project.id}/tasks/${state.taskId}/reply`, { question_id: qid, option: o.id, guidance: guidance.value });
            state.replied.add(qid);
            toast("已回复：" + o.label);
            rerenderTask();
          } catch (ex) {
            e.target.disabled = false;
            if (ex.message !== "unauthorized") toast(ex.message, true);
          }
        },
      }, labelOption(o.label)))),
    ],
  );
  return card;
}

function labelOption(label) {
  const map = { approve: "批准", reject: "拒绝", continue: "原样继续", replan: "带指导重规划", stop: "停止并收尾" };
  return map[label] || label;
}

function resultView(result) {
  const deliverable = result.deliverable && result.deliverable.value !== undefined ? result.deliverable.value : result.deliverable;
  let text = "";
  if (typeof deliverable === "string") text = deliverable;
  else if (deliverable && typeof deliverable === "object") {
    const t = pick(deliverable, "summary", "answer", "text", "content", "result", "report", "message");
    text = typeof t === "string" ? t : JSON.stringify(deliverable, null, 2);
  }
  const usage = result.usage || {};
  const usageItems = Object.entries(usage).filter(([, v]) => typeof v !== "object").map(([k, v]) => h("span", { class: "badge" }, `${k}: ${v}`));
  const ok = result.status === "TASK_STATUS_DONE";
  return h("div", { class: "result" },
    h("div", { class: "bar" }, h("strong", {}, ok ? "交付结果" : "结束情况"), statusBadge(result.status), h("span", { class: "spacer" }),
      ok ? h("button", { class: "btn small", onclick: () => { state.tab = "changes"; renderShell(); } }, "查看改动 →") : null),
    text ? h("div", { class: "text" }, text) : null,
    result.error ? h("div", { class: "text error-text" }, short(result.error, 2000)) : null,
    usageItems.length ? h("div", { class: "usage" }, usageItems) : null,
  );
}

function rerenderTask() {
  const el = document.getElementById("task-detail");
  const t = state.tasks.find((x) => x.id === state.taskId);
  if (el && t && state.tab === "tasks") el.replaceWith(taskDetail(t));
}

function stopStream() {
  if (state.stream) state.stream.close();
  state.stream = null;
}

function startStream(taskId) {
  stopStream();
  state.events = [];
  const project = state.project.id;
  const es = new EventSource(`/api/projects/${project}/tasks/${taskId}/events`);
  state.stream = es;
  const seen = new Set();
  const onEvent = (msg) => {
    if (state.taskId !== taskId) return;
    let data;
    try { data = JSON.parse(msg.data); } catch { return; }
    const id = Number(data.event_id || msg.lastEventId || 0);
    if (id && seen.has(id)) return;
    if (id) seen.add(id);
    const type = msg.type === "message" ? String(data.type || "").replace(/^TASK_EVENT_TYPE_/, "").toLowerCase() : msg.type;
    const ev = { event_id: id, type, time: data.emitted_at, payload: data.payload, value: payloadValue(data) };
    if (type === "task_finished") {
      ev.result = ev.value;
      es.close();
      const t = state.tasks.find((x) => x.id === taskId);
      if (t) t.status = ev.result.status || t.status;
    }
    state.events.push(ev);
    rerenderTask();
  };
  for (const t of EVENT_TYPES) es.addEventListener(t, onEvent);
  es.onmessage = onEvent;
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED && state.stream === es) state.stream = null;
  };
}

async function cancelTask(taskId) {
  if (!confirm("取消这个任务？已经做的改动会留在工作区里。")) return;
  try {
    await api("POST", `/api/projects/${state.project.id}/tasks/${taskId}/cancel`);
    toast("已请求取消");
  } catch (ex) {
    if (ex.message !== "unauthorized") toast(ex.message, true);
  }
}

function schedulePoll() {
  clearInterval(state.poll);
  state.poll = setInterval(async () => {
    if (!state.project || !state.tasks.some((t) => !TERMINAL.has(t.status))) return;
    try {
      const tasks = await api("GET", `/api/projects/${state.project.id}/tasks`);
      const changed = tasks.some((t) => (state.tasks.find((x) => x.id === t.id) || {}).status !== t.status);
      state.tasks = tasks;
      if (changed && state.tab === "tasks") {
        document.querySelectorAll(".task-list").forEach((el) => el.replaceWith(tasksView().querySelector(".task-list")));
        rerenderTask();
      }
    } catch { /* 下次再试 */ }
  }, 5000);
}

// --- 改动 ---

async function loadChanges(content) {
  content.replaceChildren(h("div", { class: "muted" }, "正在读取改动…"));
  let data;
  try {
    data = await api("GET", `/api/projects/${state.project.id}/changes`);
  } catch (ex) {
    content.replaceChildren(h("div", { class: "error-text" }, ex.message));
    return;
  }
  const running = state.tasks.some((t) => !TERMINAL.has(t.status));
  if (!data.files.length) {
    content.replaceChildren(h("div", { class: "empty" }, h("h2", {}, "没有未提交的改动"), h("p", {}, "当前 HEAD：", h("span", { class: "mono" }, (data.head || "").slice(0, 10)))));
    return;
  }
  const added = data.files.reduce((n, f) => n + f.added, 0);
  const deleted = data.files.reduce((n, f) => n + f.deleted, 0);
  const message = h("input", { placeholder: "提交说明，例如：修复金额舍入", value: lastTaskMessage() });
  const commit = h("button", { class: "btn primary", disabled: running, onclick: async () => {
    commit.disabled = true;
    try {
      const r = await api("POST", `/api/projects/${state.project.id}/commit`, { message: message.value });
      toast("已提交 " + r.head.slice(0, 8));
      loadChanges(content);
    } catch (ex) {
      commit.disabled = false;
      if (ex.message !== "unauthorized") toast(ex.message, true);
    }
  } }, "提交改动");
  const discard = h("button", { class: "btn danger", disabled: running, onclick: async () => {
    if (!confirm("丢弃全部未提交的改动？这一步不能撤销。")) return;
    try {
      await api("POST", `/api/projects/${state.project.id}/discard`);
      toast("已丢弃改动");
      loadChanges(content);
    } catch (ex) {
      if (ex.message !== "unauthorized") toast(ex.message, true);
    }
  } }, "丢弃");
  const download = h("a", { class: "btn", href: `/api/projects/${state.project.id}/patch`, download: `${state.project.id}.patch` }, "下载补丁");
  content.replaceChildren(...[
    h("div", { class: "card card-pad stack" },
      h("div", { class: "row" },
        h("strong", {}, `${data.files.length} 个文件`),
        h("span", { class: "stat-add" }, `+${added}`), h("span", { class: "stat-del" }, `−${deleted}`),
        running ? h("span", { class: "badge run" }, "有任务在跑，跑完再提交") : null,
      ),
      h("div", { class: "toolbar" }, h("div", { class: "grow" }, message), commit, download, discard),
    ),
    renderDiff(data.diff),
    data.truncated ? h("div", { class: "muted" }, "改动太大，只显示了前 2 MB；完整内容请下载补丁。") : null,
  ].filter(Boolean));
}

function lastTaskMessage() {
  const done = state.tasks.find((t) => t.status === "TASK_STATUS_DONE");
  return done ? done.message.split("\n")[0].slice(0, 72) : "";
}

function renderDiff(text) {
  const files = [];
  let current = null;
  for (const line of text.split("\n")) {
    if (line.startsWith("diff --git ")) {
      const m = line.match(/ b\/(.*)$/);
      current = { path: m ? m[1] : line, lines: [], added: 0, deleted: 0 };
      files.push(current);
      continue;
    }
    if (!current) continue;
    if (/^(index |--- |\+\+\+ |new file mode|deleted file mode|similarity index|rename from|rename to|old mode|new mode)/.test(line)) continue;
    let cls = "diff-line";
    if (line.startsWith("@@")) cls += " hunk";
    else if (line.startsWith("+")) { cls += " add"; current.added++; }
    else if (line.startsWith("-")) { cls += " del"; current.deleted++; }
    current.lines.push(h("span", { class: cls }, line || " "));
  }
  return h("div", {}, files.map((f) => h("div", { class: "diff-file" },
    h("div", { class: "fhead" }, h("span", { class: "grow" }, f.path), h("span", { class: "stat-add" }, `+${f.added}`), h("span", { class: "stat-del" }, `−${f.deleted}`)),
    h("pre", { class: "diff-lines" }, f.lines),
  )));
}

// --- 文件 ---

async function loadFiles(content, path) {
  state.filesPath = path;
  let entries;
  try {
    entries = await api("GET", `/api/projects/${state.project.id}/tree?path=${encodeURIComponent(path)}`);
  } catch (ex) {
    content.replaceChildren(h("div", { class: "error-text" }, ex.message));
    return;
  }
  const parts = path ? path.split("/") : [];
  const crumbs = h("div", { class: "crumbs" },
    h("button", { onclick: () => loadFiles(content, "") }, state.project.id),
    parts.map((p, i) => [h("span", { class: "muted" }, "/"), h("button", { onclick: () => loadFiles(content, parts.slice(0, i + 1).join("/")) }, p)]),
  );
  const viewer = h("div", { class: "card card-pad" }, h("div", { class: "muted" }, "选择一个文件查看"));
  const list = h("div", { class: "card card-pad" }, crumbs,
    entries.length === 0 ? h("div", { class: "muted" }, "空目录") : null,
    entries.map((e) => h("button", {
      class: "entry",
      onclick: () => (e.dir ? loadFiles(content, join(path, e.name)) : showFile(viewer, join(path, e.name))),
    }, e.dir ? "📁" : "📄", e.name, e.dir ? null : h("span", { class: "size" }, fmtSize(e.size)))),
  );
  content.replaceChildren(h("div", { class: "files-layout" }, list, viewer));
}

function join(a, b) {
  return a ? `${a}/${b}` : b;
}

function fmtSize(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

async function showFile(viewer, path) {
  viewer.replaceChildren(h("div", { class: "muted" }, "正在读取…"));
  try {
    const f = await api("GET", `/api/projects/${state.project.id}/file?path=${encodeURIComponent(path)}`);
    const head = h("div", { class: "row" }, h("strong", { class: "mono grow" }, path), h("span", { class: "muted" }, fmtSize(f.size || 0)));
    if (f.binary) viewer.replaceChildren(head, h("div", { class: "muted" }, "二进制文件，不显示内容"));
    else if (f.too_large) viewer.replaceChildren(head, h("div", { class: "muted" }, "文件太大，不显示内容"));
    else viewer.replaceChildren(head, h("pre", { class: "code" }, f.content.replace(/\n$/, "").split("\n").map((l) => h("span", {}, l || " "))));
  } catch (ex) {
    viewer.replaceChildren(h("div", { class: "error-text" }, ex.message));
  }
}

// --- 提交历史 ---

async function loadHistory(content) {
  try {
    const commits = await api("GET", `/api/projects/${state.project.id}/log`);
    content.replaceChildren(h("div", { class: "card card-pad" }, commits.map((c) => h("div", { class: "commit" },
      h("span", { class: "hash" }, c.hash.slice(0, 8)),
      h("div", { class: "grow" }, h("div", {}, c.subject), h("div", { class: "muted" }, `${c.author} · ${fmtTime(c.date)}`)),
    ))));
  } catch (ex) {
    content.replaceChildren(h("div", { class: "error-text" }, ex.message));
  }
}

// --- 项目 ---

function openNewProject() {
  let mode = state.samples.length ? "sample" : "git";
  let sample = state.samples[0] ? state.samples[0].id : "";
  const name = h("input", { placeholder: "项目名" });
  const gitUrl = h("input", { placeholder: "https://github.com/owner/repo.git" });
  const branch = h("input", { placeholder: "分支（可选）" });
  const err = h("div", { class: "error-text" });
  const body = h("div", { class: "stack" });
  const seg = h("div", { class: "seg" });
  const create = h("button", { class: "btn primary" }, "创建");
  const cancel = h("button", { class: "btn" }, "取消");
  const dialog = h("dialog", {},
    h("div", { class: "dhead" }, h("h2", {}, "新建项目")),
    h("div", { class: "dbody stack" }, seg, body, err),
    h("div", { class: "dfoot" }, cancel, create),
  );
  const draw = () => {
    seg.replaceChildren(...[["sample", "示例仓库"], ["git", "Git 地址"], ["empty", "空项目"]].map(([k, l]) =>
      h("button", { class: mode === k ? "active" : "", onclick: () => { mode = k; draw(); } }, l)));
    const parts = [h("div", {}, h("label", {}, "项目名"), name)];
    if (mode === "sample") {
      parts.push(h("div", { class: "stack" }, state.samples.map((s) => h("div", {
        class: "sample" + (sample === s.id ? " active" : ""),
        onclick: () => { sample = s.id; if (!name.value) name.value = s.name; draw(); },
      }, h("strong", {}, s.name), h("div", { class: "muted" }, s.description)))));
      if (!name.value && state.samples[0]) name.value = state.samples.find((s) => s.id === sample).name;
    } else if (mode === "git") {
      parts.push(h("div", {}, h("label", {}, "Git 地址（https，公开仓库）"), gitUrl), h("div", {}, h("label", {}, "分支"), branch));
    } else {
      parts.push(h("div", { class: "muted" }, "建一个只有 README 的空仓库，让 Agent 从零开始写。"));
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
    const req = { name: name.value };
    if (mode === "git") Object.assign(req, { git_url: gitUrl.value, branch: branch.value });
    if (mode === "sample") req.sample = sample;
    try {
      const p = await api("POST", "/api/projects", req);
      state.projects.unshift(p);
      dialog.close();
      await openProject(p.id, "tasks");
    } catch (ex) {
      if (ex.message !== "unauthorized") err.textContent = ex.message;
      create.disabled = false;
      create.textContent = "创建";
    }
  });
  document.body.append(dialog);
  dialog.showModal();
  name.focus();
}

async function deleteProject() {
  const p = state.project;
  if (!confirm(`删除项目“${p.name}”？工作区里的文件会一起删掉，不能撤销。`)) return;
  try {
    await api("DELETE", `/api/projects/${p.id}`);
    state.projects = state.projects.filter((x) => x.id !== p.id);
    state.project = state.projects[0] || null;
    toast("已删除");
    if (state.project) await openProject(state.project.id, "tasks");
    else { stopStream(); renderShell(); }
  } catch (ex) {
    if (ex.message !== "unauthorized") toast(ex.message, true);
  }
}

// --- 启动 ---

async function boot() {
  try {
    state.config = await api("GET", "/api/config");
  } catch { /* 用默认 */ }
  try {
    state.me = await api("GET", "/api/me");
  } catch {
    state.me = null;
  }
  if (state.me) await enterApp().catch((ex) => toast(ex.message, true));
  else renderLogin();
}

boot();
