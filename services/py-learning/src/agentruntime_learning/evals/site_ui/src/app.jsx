import { RuntimeSubject, RuntimeRouting } from "./runtime-subject.jsx";
import { matchesRunSearch, runtimeEvidence, subjectLabel } from "./runtime-identity.js";
import React, { useEffect, useLayoutEffect, useState, useRef } from "react";
import { createRoot } from "react-dom/client";
import { Button, Card, Chip, Input, Skeleton } from "@heroui/react";
import { NativeSelect } from "@heroui-pro/react/native-select";
import { bestResults, scoreForRun } from "./best-results.js";
import { SelectionInfo } from "./selection-info.jsx";
import { archivedSelections } from "./selection-info.js";
import { ResultAnalytics } from "./result-analytics.jsx";
import { reportRuns } from "./report-runs.js";
import { SweCampaign } from "./swe-campaign.jsx";
import { campaignMatches, campaignMemberIds, separateCampaignRuns } from "./swe-campaigns.js";
import { EVALUATION_TYPES, evaluationType, evaluationScope, runtimeRevision, matchesEvaluationType, filterEvaluationArchive } from "./evaluation-info.js";
import { resultFragmentTarget, afterResultLayout } from "./fragment-navigation.js";
import { readResultLocation, resultLocation } from "./result-location.js";
import { OverviewShowcase } from "./overview.jsx";
import { ExperimentsView } from "./experiments-view.jsx";

const text = (v) =>
  v == null ? "" : typeof v === "string" ? v : JSON.stringify(v, null, 2);
const date = (v, lang) =>
  v
    ? new Date(v).toLocaleString(lang === "zh" ? "zh-CN" : "en-GB", {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";
const number = (v) =>
  typeof v === "number" && Number.isFinite(v)
    ? v.toLocaleString(undefined, { maximumFractionDigits: 3 })
    : "—";
const pct = (v) =>
  typeof v === "number" && Number.isFinite(v)
    ? `${(v * 100).toFixed(1)}%`
    : "—";
const dollars = (v) =>
  typeof v === "number" && Number.isFinite(v) ? `$${v.toFixed(2)}` : "—";
const metricName = {
  resolved_rate: ["解决率", "Resolved"],
  triage_accuracy: ["分诊准确率", "Triage accuracy"],
  verdict_accuracy: ["判定准确率", "Verdict accuracy"],
  shape_match_rate: ["规划匹配率", "Plan match"],
  accuracy: ["准确率", "Accuracy"],
  pass_rate: ["通过率", "Pass rate"],
  success_rate: ["任务成功率", "Task success rate"],
  "act.medium": ["工具执行 · 中等", "Tool use · Medium"],
  "judge.medium": ["判断能力 · 中等", "Judgment · Medium"],
  "plan.medium": ["规划能力 · 中等", "Planning · Medium"],
  "route.low": ["分诊能力 · 基础", "Routing · Basic"],
  "full.probe_pass_rate": ["上下文探针通过率", "Context probe pass rate"],
  mrr: ["平均倒数排名", "MRR"],
  resolved: ["已解决", "Resolved count"],
  passed: ["已通过", "Passed count"],
  selected: ["样本数", "Sample size"],
  score: ["分数", "Score"],
};
const isRatio = (k) =>
  /(?:_rate|_accuracy|_precision|_recall|_retention)$/.test(k) ||
  [
    "accuracy",
    "precision",
    "recall",
    "f1",
    "mrr",
    "act.medium",
    "judge.medium",
    "plan.medium",
    "route.low",
  ].includes(k);
const scoreValue = (k, v) => (isRatio(k) ? pct(v) : number(v));
const sortRuns = (rows) =>
  [...rows].sort((a, b) =>
    String(b.updated_at || b.created_at || "").localeCompare(
      String(a.updated_at || a.created_at || ""),
    ),
  );
function useFeed(url, interval = 20000) {
  const [state, setState] = useState({
    data: null,
    error: false,
    loaded: false,
    at: null,
  });
  useEffect(() => {
    let active = true;
    let controller;
    const read = async () => {
      controller?.abort();
      controller = new AbortController();
      const requestController = controller;
      let timedOut = false;
      const deadline = setTimeout(() => {
        timedOut = true;
        requestController.abort();
      }, 15000);
      try {
        const r = await fetch(url, {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!r.ok) throw Error(r.status);
        const data = await r.json();
        if (active)
          setState({ data, error: false, loaded: true, at: new Date() });
      } catch (e) {
        if (active && (e.name !== "AbortError" || timedOut))
          setState((s) => ({
            ...s,
            data: s.data || {
              unavailable: true,
              runs: [],
              datasets: [],
              frameworks: [],
              reports: {},
            },
            error: true,
            loaded: true,
          }));
      } finally {
        clearTimeout(deadline);
      }
    };
    read();
    const timer = setInterval(() => {
      if (!document.hidden) read();
    }, interval);
    const resume = () => {
      if (!document.hidden) read();
    };
    document.addEventListener("visibilitychange", resume);
    return () => {
      active = false;
      controller?.abort();
      clearInterval(timer);
      document.removeEventListener("visibilitychange", resume);
    };
  }, [url, interval]);
  return state;
}
function Mark() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="23"
      height="23"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      aria-hidden="true"
    >
      <path d="M4 3v17h17M7 14l5-6 4 3 5-7" />
    </svg>
  );
}
function Badge({ children, color = "default" }) {
  return (
    <Chip size="sm" variant="soft" color={color}>
      <Chip.Label>{children}</Chip.Label>
    </Chip>
  );
}
function Stat({ value, label }) {
  return (
    <div className="stat">
      <b>{value}</b>
      <span>{label}</span>
    </div>
  );
}
function Empty({ children }) {
  return <div className="empty">{children}</div>;
}
function Loading() {
  return (
    <div aria-label="Loading" className="loading">
      <Skeleton className="h-8 w-1/3" />
      <Skeleton className="h-24 w-full" />
      <Skeleton className="h-24 w-full" />
    </div>
  );
}
function Select({ id, label, value, onChange, options }) {
  return (
    <NativeSelect className="filter-select" variant="secondary">
      <NativeSelect.Trigger
        id={id}
        aria-label={label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map(([k, v]) => (
          <NativeSelect.Option key={k} value={k}>
            {v}
          </NativeSelect.Option>
        ))}
        <NativeSelect.Indicator />
      </NativeSelect.Trigger>
    </NativeSelect>
  );
}
function App() {
  const [lang, setLang] = useState(() => {
    try {
      return localStorage.getItem("evals-lang") === "en" ? "en" : "zh";
    } catch {
      return "zh";
    }
  });
  const t = (zh, en) => (lang === "zh" ? zh : en);
  const tr = (v) =>
    typeof v === "object" && v ? v[lang] || v.zh || v.en || "" : v || "";
  const site = useFeed("/api/site.json", 60000),
    live = useFeed("/api/live.json"),
    archive = useFeed("/api/reports.json", 10000),
    benchmarks = useFeed("/api/benchmarks.json");
  const page =
    location.pathname === "/datasets"
      ? "datasets"
      : location.pathname === "/results"
        ? "results"
        : "home";
  useEffect(() => {
    document.documentElement.lang = lang;
    document.title = `${t("评测", "Evaluations")} · Agent Runtime`;
    try {
      localStorage.setItem("evals-lang", lang);
    } catch {}
  }, [lang]);
  const props = {
    lang,
    t,
    tr,
    site: site.data,
    live: live.data,
    archive: React.useMemo(() => archivedSelections(archive.data, benchmarks.data), [archive.data, benchmarks.data]),
    benchmarks: benchmarks.data,
  };
  return (
    <>
      <header className="topbar">
        <div className="nav-inner">
          <a className="brand" href="/" aria-label="Agent Runtime Evals">
            <Mark />
            <span>
              Agent Runtime{" "}
              <span className="brand-muted">/ {t("评测", "Evals")}</span>
            </span>
          </a>
          <nav aria-label={t("主导航", "Main navigation")}>
            {[
              ["home", "/", t("总览", "Overview")],
              ["results", "/results", t("专项评测", "Evaluations")],
              ["datasets", "/datasets", t("数据集与框架", "Datasets")],
            ].map(([key, url, label]) => (
              <a
                key={key}
                href={url}
                aria-current={page === key ? "page" : undefined}
              >
                {label}
              </a>
            ))}
          </nav>
          <div className="language" aria-label="Language">
            <Button
              size="sm"
              variant={lang === "zh" ? "primary" : "ghost"}
              onPress={() => setLang("zh")}
              aria-pressed={lang === "zh"}
              data-set-lang="zh"
            >
              中文
            </Button>
            <Button
              id="lang"
              size="sm"
              variant={lang === "en" ? "primary" : "ghost"}
              onPress={() => setLang("en")}
              aria-pressed={lang === "en"}
              data-set-lang="en"
            >
              English
            </Button>
          </div>
        </div>
      </header>
      <main>
        <section className="hero">
          <div className="eyebrow">
            Agent Runtime /{" "}
            {t(
              page === "home"
                ? "评测概览"
                : page === "results"
                  ? "评测结果"
                  : "数据集与框架",
              page === "home"
                ? "Evaluation overview"
                : page === "results"
                  ? "Evaluation results"
                  : "Datasets & frameworks",
            )}
          </div>
          <h1>
            {page === "home"
              ? t("Agent Runtime 评测", "Agent Runtime evaluations")
              : page === "results"
                ? t("每次评测，结果可查。", "Every evaluation, in detail.")
                : t("从题目到评分，公开可查。", "From questions to scores.")}
          </h1>
          <p>
            {page === "home"
              ? t(
                  "查看智能体在规划、代码、工具调用等任务上的表现，追踪评测进度，了解数据来源与评分方法。",
                  "Explore performance across planning, coding and tool use. Follow evaluation progress and inspect the datasets and scoring methods.",
                )
              : page === "results"
                ? t(
                    "实时进度、评分与费用汇总。按评测任务、模型和数据切分查看结果。",
                    "Live progress, scores and cost. Explore results by task, model and evaluation split.",
                  )
                : t(
                    "浏览公开题目、参考答案及来源；留出集仅展示数量，用于独立验证。",
                    "Browse public questions, reference answers and sources. Held-out questions are counted but kept private for independent validation.",
                  )}
          </p>
        </section>
        {(site.error ||
          live.error ||
          archive.error ||
          site.data?.sync?.stale ||
          live.data?.stale ||
          archive.data?.stale) && (
          <div role="status" className="notice">
            {t(
              "部分数据暂时无法更新，已保留最近可用的结果。",
              "Some data could not be refreshed. The latest available results remain visible.",
            )}{" "}
            <Button size="sm" variant="ghost" onPress={() => location.reload()}>
              {t("重试", "Retry")}
            </Button>
          </div>
        )}
        {page === "home" ? (
          <Home {...props} />
        ) : page === "results" ? (
          <Results {...props} />
        ) : (
          <Datasets {...props} />
        )}
        <footer>
          <span>Agent Runtime · {t("评测", "Evaluations")}</span>
          <span>
            {t("自动更新", "Updates automatically")}
            {live.at
              ? ` · ${t("上次检查", "Last checked")} ${date(live.at, lang)}`
              : ""}
          </span>
          <a href="/datasets">{t("数据与方法", "Data & methodology")}</a>
        </footer>
      </main>
    </>
  );
}
function Totals(site) {
  const all = {
    total: 0,
    shown: 0,
    holdout_hidden: 0,
    confirmed: 0,
    draft: 0,
    original: 0,
    adapted: 0,
    authored: 0,
  };
  for (const d of site?.datasets || []) {
    for (const k of ["total", "shown", "holdout_hidden", "confirmed", "draft"])
      all[k] += d.counts[k] || 0;
    all[d.source_type] += d.counts.total || 0;
  }
  return all;
}
function Home(p) {
  const { t, tr, site, lang } = p;
  const total = Totals(site);
  return (
    <OverviewShowcase
      t={t}
      lang={lang}
      site={site}
      total={total}
      number={number}
      tr={tr}
      Badge={Badge}
      Frameworks={Frameworks}
      props={p}
    />
  );
}
const fwKinds = [
  ["authored", "自研框架", "In-house"],
  ["standard", "标准框架", "Standard"],
  ["adapted", "适配与扩展", "Adapted"],
];
const frameworkCopy = {
  "task-loop": [
    "按固定规则评估分诊、规划结构与 Reviewer 判定。支持规则基线、参考答案及真实模型，留出集仅发布汇总结果。",
    "Rule-based evaluation of triage, plan structure and Reviewer decisions. Supports rule baselines, reference answers and live models; held-out results are aggregate-only.",
  ],
  "capability-matrix": [
    "按模型与角色评估组件能力，使用 bootstrap 与收缩估计分数和区间。结果经人审后用于模型能力档案。",
    "Evaluates model capabilities by role, with bootstrap and shrinkage estimates of scores and intervals. Human-reviewed results inform model profiles.",
  ],
  deepeval: [
    "采用 DeepEval 的 G-Eval 与 AnswerRelevancy 评估正确性、相关性和诚实性。裁判调用留有记录，并标记裁判与被测模型同厂商的情况。",
    "Uses DeepEval G-Eval and AnswerRelevancy for correctness, relevance and honesty. Judge calls are recorded and same-vendor pairs are identified.",
  ],
  ragas: [
    "评估记忆检索的命中率、召回率与 MRR，并采用 Ragas 评估上下文精确率和召回率。需要模型裁判的指标单独计算。",
    "Measures retrieval hit rate, recall and MRR, plus context precision and recall with Ragas. Model-judged metrics are computed separately.",
  ],
  "swebench-harness": [
    "在 SWE-bench 官方 Docker 环境运行隐藏测试，按官方口径判定代码问题是否解决。",
    "Runs hidden tests in the official SWE-bench Docker environments and reports resolution using the official grading criteria.",
  ],
  "terminalbench-adapted": [
    "提供工作目录回传后的离线判分，以及 Harbor 原容器判分两条路径。离线路径无法保留工作目录外的状态，结果需结合环境限制解读。",
    "Supports offline grading of returned workspace files and Harbor grading in the original task container. Offline grading cannot preserve state outside the working directory.",
  ],
  "official-templates": [
    "采用公开数据集的官方提示或判分规则。BFCL 适配参数类型并仅覆盖单轮；SimpleQA 同时报告官方口径和“不知道”给予部分分数的口径。",
    "Uses official prompts or scoring rules. BFCL adapts argument types and covers single-turn tasks; SimpleQA reports both official and partial-credit abstention scores.",
  ],
};
function Frameworks({ site, t, tr, compact = false }) {
  return (
    <div id="frameworks" className="frameworks">
      {fwKinds.map(([kind, zh, en]) => {
        const frameworks = (site?.frameworks || []).filter(
          (f) => f.kind === kind,
        );
        return (
          <section key={kind} className="framework-group">
            <h3>
              {t(zh, en)}{" "}
              <span className="muted mono">{frameworks.length}</span>
            </h3>
            {compact ? (
              <p>
                {frameworks
                  .map((f) =>
                    tr(f.title)
                      .replace(/（.*?）|\(.*?\)/g, "")
                      .trim(),
                  )
                  .join(" · ") || "—"}
              </p>
            ) : (
              frameworks.map((f) => (
                <details key={f.id}>
                  <summary>
                    {tr(f.title)
                      .replace(/（.*?）|\(.*?\)/g, "")
                      .trim()}
                  </summary>
                  <div className="framework-detail">
                    <p>
                      {frameworkCopy[f.id]
                        ? t(...frameworkCopy[f.id])
                        : tr(f.what)}
                    </p>
                    <small>
                      {t("适用数据集", "Datasets")}:{" "}
                      {(f.used_by || [])
                        .map(
                          (id) =>
                            tr(
                              site.datasets.find((d) => d.name === id)?.title,
                            ) || id,
                        )
                        .join(" · ")}
                    </small>
                  </div>
                </details>
              ))
            )}
          </section>
        );
      })}
    </div>
  );
}
function EvaluationIdentity({ row, t }) {
  const type = evaluationType(row), scope = evaluationScope(row);
  const revision = runtimeRevision(row);
  const identity = runtimeEvidence(row);
  return <>
    <dt>{t("评测类型", "Evaluation type")}</dt><dd>{t(...EVALUATION_TYPES[type])}</dd>
    <dt>{t("执行范围", "Execution scope")}</dt><dd>{scope === "component" ? t("组件评测", "Component evaluation") : scope === "end_to_end" ? t("端到端评测", "End-to-end evaluation") : t("未记录", "Unrecorded")}</dd>
    {identity && <><dt>{t("被测系统","Evaluated system")}</dt><dd><RuntimeSubject row={row} t={t}/></dd></>}
    <RuntimeRouting row={row} t={t}/>
    {type !== "model" && (row.runtime_tree && !revision
      ? <><dt>{t("Runtime 源码树", "Runtime Git tree")}</dt><dd>{row.runtime_tree}</dd></>
      : <><dt>{t("Runtime 版本", "Runtime revision")}</dt><dd>{identity?.version || revision || t("未记录", "Unrecorded")}</dd></>)}
    {row.comparison_id && <><dt>{t("对照实验", "Comparison")}</dt><dd>{row.comparison_id}</dd></>}
  </>;
}

function Experiments(props) {
  return (
    <ExperimentsView
      {...props}
      date={date}
      number={number}
      pct={pct}
      dollars={dollars}
      Badge={Badge}
      EvaluationIdentity={EvaluationIdentity}
      includePublished={!props.type || props.type === "ablation"}
    />
  );
}

function Status({ r, t }) {
  if (r.source === "swe_progress" && r.status === "paused") return <Badge color="warning">{t("供应商额度中断 · 已暂停", "Provider quota interruption · Paused")}</Badge>;
  if (r.source === "swe_progress" && r.status === "awaiting_grade") return <Badge>{t("等待官方判分归档", "Awaiting official grading report")}</Badge>;
  if (r.source === "swe_progress" && r.status === "running") return <Badge>{t("补跑中", "Recovery running")}</Badge>;
  if (r.status === "incomplete") return <Badge color="warning">{t("尚未完成","Incomplete")}</Badge>;
  if (r.provider_quota_interrupted_count > 0)
    return <Badge color="warning">{r.graded_count === 0 ? t("供应商额度中断","Provider quota interruption") : t("供应商额度中断 · 部分评分", "Provider quota interruption · Partially graded")}</Badge>;
  if (r.source === "benchmark_artifacts" && r.suite === "swebench" && r.graded_count === 0 && r.finished)
    return <Badge color="warning">{t("运行结束 · 缺少判分","Run ended · Grading missing")}</Badge>;
  return (
    <Badge
      color={
        r.status === "running"
          ? "accent"
          : r.status === "stalled"
            ? "warning"
            : r.valid === false
              ? "danger"
              : "default"
      }
    >
      {r.valid === false
        ? t("无效", "Invalid")
        : r.status === "running"
          ? t("运行中", "Running")
          : r.status === "stalled"
            ? t("长时间未更新", "Stalled")
            : r.status === "partial" ? t("部分判分", "Partially graded") : t("已完成", "Completed")}
    </Badge>
  );
}
function Results(p) {
  const { t, tr, lang } = p;
  const capability = useFeed("/api/capability.json");
  const pendingFragment = useRef(location.hash);
  const cancelScroll = useRef(() => {});
  const searchHistory = useRef(false);
  const [fragmentNavigation, setFragmentNavigation] = useState(0);
  const [{ view, search }, setRecordLocation] = useState(() => readResultLocation(location.href));
  const [status, setStatus] = useState(""), [split, setSplit] = useState("");
  const [type, setType] = useState(() => {
    const initial = new URLSearchParams(location.search).get("type");
    return Object.hasOwn(EVALUATION_TYPES, initial) ? initial : "";
  });
  const live = p.live ? { ...p.live, runs: (p.live.runs || []).filter((row) => matchesEvaluationType(row, type)) } : p.live;
  const archive = filterEvaluationArchive(p.archive, type);
  const benchmarks = p.benchmarks ? { ...p.benchmarks, campaigns: (!type || type === "runtime") ? p.benchmarks.campaigns : [], runs: (p.benchmarks.runs || []).filter((row) => matchesEvaluationType(row, type)),
    exclusions: (p.benchmarks.exclusions || []).filter((row) => matchesEvaluationType(row, type)) } : p.benchmarks;
  const filteredProps = { ...p, live, archive, benchmarks };
  useLayoutEffect(() => {
    const navigate = () => {
      cancelScroll.current();
      searchHistory.current = false;
      setRecordLocation(readResultLocation(location.href));
      const nextType = new URLSearchParams(location.search).get("type");
      setType(Object.hasOwn(EVALUATION_TYPES, nextType) ? nextType : "");
      setStatus(""); setSplit("");
      pendingFragment.current = location.hash;
      setFragmentNavigation((value) => value + 1);
    };
    const cancelPending = () => { pendingFragment.current = null; cancelScroll.current(); };
    const cancelForKey = (event) => {
      if (event.target.closest?.("input,textarea,select,[contenteditable=true]")) return;
      if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) cancelPending();
    };
    window.addEventListener("popstate", navigate);
    window.addEventListener("hashchange", navigate);
    window.addEventListener("wheel", cancelPending, { passive: true });
    window.addEventListener("touchstart", cancelPending, { passive: true });
    window.addEventListener("pointerdown", cancelPending, { passive: true });
    window.addEventListener("keydown", cancelForKey);
    return () => {
      window.removeEventListener("popstate", navigate);
      window.removeEventListener("hashchange", navigate);
      window.removeEventListener("wheel", cancelPending);
      window.removeEventListener("touchstart", cancelPending);
      window.removeEventListener("pointerdown", cancelPending);
      window.removeEventListener("keydown", cancelForKey);
      cancelScroll.current();
    };
  }, []);
  const fragmentReady = Boolean(p.site && p.live && p.archive && p.benchmarks &&
    ((type && type !== "model") || capability.loaded));
  useEffect(() => {
    const hash = pendingFragment.current;
    if (!resultFragmentTarget(hash) || !fragmentReady) return;
    // Wait for sections above the target to finish their first load. Once a
    // requested navigation succeeds, feed polling must never scroll again.
    cancelScroll.current = afterResultLayout(hash, () => {
      if (pendingFragment.current === hash) pendingFragment.current = null;
    });
    return () => cancelScroll.current();
  }, [fragmentNavigation, fragmentReady, type, view]);
  function changeType(value) {
    setType(value);
    const url = new URL(location.href);
    if (value) url.searchParams.set("type", value); else url.searchParams.delete("type");
    window.history.replaceState(null, "", url);
  }
  function changeView(nextView) {
    searchHistory.current = false;
    const next = { view: nextView, search };
    const url = resultLocation(location.href, next);
    if (url.href !== location.href) window.history.pushState(null, "", url);
    setRecordLocation(next);
    cancelScroll.current();
    pendingFragment.current = url.hash;
    setFragmentNavigation((value) => value + 1);
  }
  function changeSearch(nextSearch) {
    cancelScroll.current(); pendingFragment.current = null;
    const next = { view, search: nextSearch };
    const url = resultLocation(location.href, next);
    if (url.href !== location.href) {
      // One history entry per editing session, not one per keystroke. Switching
      // views or leaving the input starts a new entry for the next edit.
      window.history[searchHistory.current ? "replaceState" : "pushState"](null, "", url);
      searchHistory.current = true;
    }
    setRecordLocation(next);
  }
  const rows = reportRuns(live, archive, benchmarks);
  const campaigns = !type || type === "runtime" ? benchmarks?.campaigns || [] : [];
  const standalone = separateCampaignRuns(live, archive, benchmarks);
  const memberIds = campaignMemberIds(campaigns);
  const standaloneRows = rows.filter(r => r.suite !== "swebench" || !memberIds.has(r.run_id));
  const memberRows = new Map(rows.filter(r=>r.suite === "swebench").map(r=>[r.run_id,r]));
  const matchingCampaigns = campaigns.filter(c => campaignMatches(c, memberRows, {search, status: view === "recent" ? status : "", split}));
  const campaignCards = matchingCampaigns.map(c=><SweCampaign key={c.campaign_id} campaign={c} rows={memberRows} search={search} t={t}
    renderRun={(r,opened)=><Run key={r.run_id} r={r} {...p} opened={opened} />} />);
  const filtered = standaloneRows.filter(
    (r) =>
      (!status ||
        (status === "invalid" ? r.valid === false : r.status === status)) &&
      (!split || r.split === split) &&
      matchesRunSearch(r, search),
  );
  const reports = standalone.archive?.reports || {};
  const archiveBenchmarks = (reports.benchmarks || []).map(row => {
    const official = memberRows.get(row.run_id);
    return {suite:"swebench", ...row, ...(official?.source === "benchmark_artifacts" ? official : {})};
  }).filter(row=>matchesRunSearch(row,search) && (!split || !row.split || row.split === split));
  const history = sortRuns(reports.runs || []).filter(
    (r) =>
      (!split || r.split === split) &&
      matchesRunSearch({suite:"task-loop", ...r}, search),
  );
  const running = rows.filter((r) => r.status === "running");
  const standaloneRunning = standaloneRows.filter((r) => r.status === "running");
  return (
    <>
      <div className="track-tabs" role="tablist" aria-label={t("评测专题切换", "Evaluation tracks")}>
        {[
          ["", t("全部专题", "All tracks")],
          ["runtime", t("代码与运行时基准", "Runtime & Coding")],
          ["ablation", t("工程性能与消融对照", "Engineering & Ablations")],
          ["model", t("模型能力矩阵", "Model Capability Matrix")],
        ].map(([key, label]) => (
          <Button
            key={key || "all"}
            size="sm"
            variant={type === key ? "primary" : "ghost"}
            onPress={() => changeType(key)}
            role="tab"
            aria-selected={type === key}
          >
            {label}
          </Button>
        ))}
      </div>
      <ResultAnalytics {...filteredProps} capability={capability.data} benchmarks={benchmarks} showCapability={!type || type === "model"} capabilityError={capability.error} />
      <Experiments {...filteredProps} type={type} />
      <div className="stats result-stats">
        <Stat
          value={
            live && !live.unavailable && live.source !== "none"
              ? rows.filter((r) => r.status === "finished").length
              : "—"
          }
          label={t("最近完成", "Recently completed")}
        />
        <Stat
          value={
            live && !live.unavailable && live.source !== "none"
              ? running.length
              : "—"
          }
          label={t("正在运行", "Running")}
        />
        <Stat
          value={
            live && !live.unavailable && live.source !== "none"
              ? rows.filter((r) => r.status === "stalled").length
              : "—"
          }
          label={t("长时间未更新", "Stalled")}
        />
        <Stat
          value={
            live && !live.unavailable && live.source !== "none"
              ? `${rows.some((r) => r.cost_known === false) ? t("已知 ", "Known ") : ""}${dollars(rows.reduce((s, r) => s + (r.cost_known === false ? r.known_cost_usd || 0 : r.cost_usd || 0), 0))}`
              : "—"
          }
          label={t("最近运行费用", "Recent run cost")}
        />
      </div>
      {standaloneRunning.length > 0 && (
        <section className="active-runs">
          <div className="section-heading">
            <h2>{t("运行中", "Running now")}</h2>
            <Badge color="accent">{standaloneRunning.length}</Badge>
          </div>
          {standaloneRunning.map((r) => (
            <div className="live-progress" key={r.run_id}>
              <div>
                <strong>{r.suite} <SelectionInfo row={r} t={t} /></strong>
                <span>
                  {r.done} / {r.planned}
                </span>
              </div>
              <progress max={r.planned || 1} value={r.done} />
              <small>
                <RuntimeSubject row={r} t={t} fallback={r.subject}/> · {dollars(r.cost_usd)}
              </small>
            </div>
          ))}
        </section>
      )}
      <div
        className="results-switch"
        id="run-records"
        role="group"
        aria-label={t("结果范围", "Result scope")}
      >
        <Button
          data-view="recent"
          aria-pressed={view === "recent"}
          variant={view === "recent" ? "primary" : "ghost"}
          onPress={() => changeView("recent")}
        >
          {t("最近记录", "Recent records")} {standaloneRows.length + campaigns.length}
        </Button>
        <Button
          data-view="archive"
          aria-pressed={view === "archive"}
          variant={view === "archive" ? "primary" : "ghost"}
          onPress={() => changeView("archive")}
        >
          {t("全部报告", "All reports")} {(reports.runs || []).length + campaigns.length}
        </Button>
      </div>
      <div className="filterbar">
        <Input
          data-run-records-search
          aria-describedby="record-search-scope"
          aria-label={t("搜索评测、模型", "Search evaluations and models")}
          placeholder={t("搜索评测、模型…", "Search evaluations, models…")}
          value={search}
          onChange={(e) => changeSearch(e.target.value)}
          onBlur={() => { searchHistory.current = false; }}
        />
        <Select
          label={t("数据切分", "Split")}
          value={split}
          onChange={setSplit}
          options={[
            ["", t("全部切分", "All splits")],
            ...Array.from(
              new Set(
                [...rows, ...(reports.runs || [])]
                  .map((r) => r.split)
                  .filter(Boolean),
              ),
            ).map((k) => [k, k]),
          ]}
        />
        {view === "recent" && (
          <Select
            label={t("运行状态", "Run status")}
            value={status}
            onChange={setStatus}
            options={[
              ["", t("全部状态", "All statuses")],
              ["finished", t("已完成", "Completed")],
              ["partial", t("部分判分", "Partially graded")],
              ["paused", t("额度中断", "Quota interruption")],
              ["awaiting_grade", t("待判分归档", "Awaiting grading report")],
              ["running", t("运行中", "Running")],
              ["stalled", t("长时间未更新", "Stalled")],
              ["invalid", t("无效", "Invalid")],
            ]}
          />
        )}
      </div>
      <p className="muted" id="record-search-scope" data-record-search-scope>
        {t("搜索词仅筛选下方运行记录，不影响上方图表和能力矩阵。", "Search filters only the run records below, not the charts or capability matrix above.")}
      </p>
      <div className="section-caption">
        <span>
          {view === "recent"
            ? t(
                `${filtered.length + matchingCampaigns.length} 条记录 · 自动汇集进度与报告`,
                `${filtered.length + matchingCampaigns.length} records · Live progress and reports`,
              )
            : t(
                `${history.length + matchingCampaigns.length} 份报告 · 每 10 秒自动更新`,
                `${history.length + matchingCampaigns.length} reports · Refreshes every 10 seconds`,
              )}
        </span>
        <span>
          {view === "recent" && rows[0]?.updated_at
            ? `${t("最近结果", "Latest result")} ${date(rows[0].updated_at, lang)}`
            : archive?.task_loop_source?.checked_at
              ? `${t("报告检查", "Reports checked")} ${date(archive.task_loop_source.checked_at, lang)}`
            : archive?.generated_at
              ? `${t("归档更新", "Archive updated")} ${archive.generated_at}`
              : ""}
        </span>
      </div>
      {view === "archive" && archive?.status === "unavailable" && (
        <div role="status" className="notice">
          {archive.error === "archive_missing"
            ? t("暂无历史报告", "No archived reports yet")
            : t(
                "历史报告暂不可用",
                "The report archive is temporarily unavailable",
              )}
        </div>
      )}
      {view === "archive" && archive?.stale && (
        <div role="status" className="notice">
          {t("显示上次获取的报告", "Showing the last available reports")}
        </div>
      )}
      {view === "recent" ? (
        !live ? (
          <Loading />
        ) : filtered.length || campaignCards.length ? (
          <div className="run-list">
            {campaignCards}
            {filtered.map((r) => (
              <Run key={r.run_id} r={r} {...p} audit={benchmarks?.exclusions?.find((a) => a.suite === r.suite && a.run_id === r.run_id)} />
            ))}
          </div>
        ) : (
          <Empty>{t("没有符合条件的运行", "No matching runs")}</Empty>
        )
      ) : !archive ? (
        <Loading />
      ) : (
        <>
          {campaignCards}
          <div className="table-scroll">
            <table id="runs">
              <thead>
                <tr>
                  {[
                    t("评测报告", "Report"),
                    t("切分", "Split"),
                    t("任务成功率", "Task success"),
                    t("分诊", "Triage"),
                    t("规划", "Plan"),
                    t("判定", "Verdict"),
                    t("费用", "Cost"),
                  ].map((x) => (
                    <th key={x}>{x}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {history.map((r, i) => (
                  <tr key={i}>
                    <td>
                      <details>
                        <summary><RuntimeSubject row={{suite:"task-loop",...r}} t={t} fallback={r[`label_${lang}`] || r.group}/> · {r[`label_${lang}`] || r.group}{r.variant ? ` · ${r.variant === "candidate" ? t("候选版", "Candidate") : t("基线版", "Baseline")}` : ""}</summary>
                        <dl>
                          <EvaluationIdentity row={{ suite: "task-loop", ...r }} t={t} />
                          <dt>{t("任务通过", "Tasks passed")}</dt>
                          <dd>
                            {r.task_pass} / {r.task_total}
                          </dd>
                          <dt>{t("回退次数", "Fallbacks")}</dt>
                          <dd>{r.fallbacks}</dd>
                          <dt>{t("结构错误", "Structure errors")}</dt>
                          <dd>{r.structured_failures}</dd>
                          <dt>{t("轮次判定", "Round verdicts")}</dt>
                          <dd>{pct(r.rounds)}</dd>
                          <dt>{t("Reviewer 样本", "Reviewer samples")}</dt>
                          <dd>{r.reviewer_samples ?? "—"}</dd>
                          {r.run_id && <><dt>{t("运行标识","Run ID")}</dt><dd>{r.run_id}</dd></>}
                          {r.labels_draft > 0 && <><dt>{t("标注状态", "Label status")}</dt><dd>{t(`${r.labels_draft} 道草稿回归题`, `${r.labels_draft} draft regression cases`)}</dd></>}
                        </dl>
                      </details>
                      <small>
                        {date(r.created_at, lang)}
                        {r.valid === false ? ` · ${t("无效", "Invalid")}` : ""}
                      </small>
                    </td>
                    <td>{r.split} <SelectionInfo row={r} t={t} /></td>
                    <td>
                      {r.task_total > 0 && Number.isFinite(r.task_pass)
                        ? pct(r.task_pass / r.task_total)
                        : "—"}
                    </td>
                    <td>{pct(r.triage)}</td>
                    <td>{pct(r.shape)}</td>
                    <td>{pct(r.reviewer)}</td>
                    <td>{dollars(r.total_usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!history.length && !campaignCards.length && (
            <Empty>
              {t("没有符合条件的归档报告", "No matching archived reports")}
            </Empty>
          )}
          {archiveBenchmarks.length > 0 && (
            <>
              <div className="section-heading">
                <h2>{t("代码任务归档", "Coding benchmarks")}</h2>
              </div>
              <div className="archive-grid">
                {archiveBenchmarks.map((r, i) => (
                  <Card key={r.run_id || i}>
                    <Card.Header>
                      <Card.Title><RuntimeSubject row={r} t={t}/> <SelectionInfo row={r} t={t} /></Card.Title>
                    </Card.Header>
                    <Card.Content>
                      <b className="large-score">{r.graded_count === 0 ? "—" : pct(r.rate)}</b>
                      <p>
                        {r.graded_count === 0 ? t("暂无官方判定","No official verdicts") : `${r.resolved} / ${r.selected} ${t("题已解决","resolved")}`}{" "}
                        {Number.isInteger(r.graded_count) && ` · ${t("官方判分覆盖","Official grading coverage")} ${r.graded_count}/${r.selected} `}
                        · {dollars(r.cost_usd)}
                      </p>
                      <small className="muted">{r.run_id}</small>
                      <details className="benchmark-detail">
                        <summary>
                          {t("查看判分详情", "Grading details")}
                        </summary>
                        <dl>
                          <EvaluationIdentity row={r} t={t}/>
                          <dt>{t("完整度", "Completeness")}</dt>
                          <dd>
                            {r.complete
                              ? t("全部完成", "Complete")
                              : t("部分完成", "Partial")}
                          </dd>
                          <dt>{t("环境", "Environment")}</dt>
                          <dd>{r.environment || "—"}</dd>
                          <dt>{t("轮次中位数", "Median rounds")}</dt>
                          <dd>{number(r.rounds_median)}</dd>
                          <dt>{t("单题费用", "Cost / case")}</dt>
                          <dd>{dollars(r.per_problem_usd)}</dd>
                        </dl>
                        {[
                          ["by_repo", t("按仓库", "By repository")],
                          ["by_difficulty", t("按难度", "By difficulty")],
                          ["gradings", t("判分状态", "Grading status")],
                        ].map(([key, label]) => (
                          <section key={key}>
                            <h4>{label}</h4>
                            <dl>
                              {Object.entries(r[key] || {}).map(
                                ([name, value]) => (
                                  <React.Fragment key={name}>
                                    <dt>{name}</dt>
                                    <dd>
                                      {typeof value === "object"
                                        ? Object.entries(value)
                                            .map(
                                              ([k, v]) => `${k}: ${number(v)}`,
                                            )
                                            .join(" · ")
                                        : number(value)}
                                    </dd>
                                  </React.Fragment>
                                ),
                              )}
                            </dl>
                          </section>
                        ))}
                      </details>
                    </Card.Content>
                  </Card>
                ))}
              </div>
            </>
          )}
          <ArchiveExtras reports={reports} {...p} />
          {(reports.contexts || []).length > 0 && (
            <>
              <div className="section-heading">
                <h2>{t("上下文评测", "Context evaluations")}</h2>
              </div>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>{t("报告", "Report")}</th>
                      <th>{t("切分", "Split")}</th>
                      <th>{t("完整上下文", "Full context")}</th>
                      <th>{t("仅遮罩", "Mask only")}</th>
                      <th>{t("保留率", "Retention")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {reports.contexts.map((r, i) => (
                      <tr key={i}>
                        <td>
                          <details>
                            <summary><RuntimeSubject row={{suite:"context",...r}} t={t}/> · {r.group}</summary>
                            <dl>
                              <EvaluationIdentity row={{suite:"context",...r}} t={t}/>
                              <dt>{t("日期", "Date")}</dt>
                              <dd>{r.date || "—"}</dd>
                              <dt>{t("验收", "Gate")}</dt>
                              <dd>
                                {r.gate == null
                                  ? "—"
                                  : r.gate
                                    ? t("通过", "Passed")
                                    : t("未通过", "Failed")}
                              </dd>
                              <dt>{t("重复率", "Repeat rate")}</dt>
                              <dd>{pct(r.repeat_rate)}</dd>
                            </dl>
                          </details>
                        </td>
                        <td>{r.split} <SelectionInfo row={r} t={t} /></td>
                        <td>{pct(r.full_probe)}</td>
                        <td>{pct(r.mask_probe)}</td>
                        <td>{pct(r.full_retention)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </>
      )}
      <p className="method-note">
        {t(
          "不同任务的指标口径不同，分数不跨任务直接比较。评测模型通过第三方服务调用，运行费用按报告记录。",
          "Metrics differ across tasks and should not be compared directly. Evaluation models are accessed through third-party services; costs are reported per run.",
        )}
      </p>
    </>
  );
}
function ArchiveExtras({ reports, t, lang }) {
  return (
    <>
      {(reports.tuning || []).length > 0 && (
        <details className="archive-extra">
          <summary>
            {t(
              "调优对比与置信区间",
              "Tuning comparison & confidence intervals",
            )}
          </summary>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  {[
                    t("运行", "Run"),
                    t("解决率", "Resolve rate"),
                    t("95% 区间", "95% interval"),
                    t("单题费用", "Cost / case"),
                    t("新增 / 回退", "Gained / lost"),
                  ].map((x) => (
                    <th key={x}>{x}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {reports.tuning.map((r) => (
                  <tr key={r.run_id}>
                    <td>
                      <strong><RuntimeSubject row={{suite:"swebench",...r}} t={t}/> · {r[`short_${lang}`] || r.run_id} <SelectionInfo row={r} t={t} /></strong>
                      <small>
                        {r.same_problems
                          ? t("相同题目", "Same problems")
                          : t("不同题目，谨慎比较", "Different problems")}
                      </small>
                    </td>
                    <td>
                      {pct(r.rate)}
                      <small>
                        {r.resolved} / {r.selected}
                      </small>
                    </td>
                    <td>
                      {pct(r.low)}–{pct(r.high)}
                    </td>
                    <td>{dollars(r.per_problem_usd)}</td>
                    <td>
                      {r.gained ?? "—"} / {r.lost ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
      {Object.keys(reports.failures || {}).length > 0 && (
        <details className="archive-extra">
          <summary>{t("失败原因汇总", "Failure breakdown")}</summary>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>{t("评测报告", "Report")}</th>
                  <th>{t("原因", "Reason")}</th>
                  <th>{t("次数", "Count")}</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(reports.failures).flatMap(([group, rows]) =>
                  rows.map(([reason, count]) => (
                    <tr key={`${group}/${reason}`}>
                      <td>{group}</td>
                      <td>{reason}</td>
                      <td>{number(count)}</td>
                    </tr>
                  )),
                )}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </>
  );
}

function TerminalBenchDetails({ r, t, lang }) {
  const costScope = r.cost_scope === "campaign" ? t("实验累计", "Campaign") : t("本批任务", "Cohort tasks");
  const outcomes = {
    resolved: ["通过", "Resolved"], unresolved: ["未通过", "Unresolved"], not_run: ["未运行", "Not run"],
    infra_error: ["执行环境异常", "Infrastructure error"], grading_error: ["判分异常", "Grading error"],
  };
  const short = (value) => typeof value === "string" ? value.slice(0, 12) : "—";
  return <section className="terminalbench-report" aria-label={t("Terminal-Bench 判分详情", "Terminal-Bench grading details")}>
    <div className="metrics">
      <Stat value={`${r.resolved} / ${r.rate_denominator}`} label={t("通过 / 已判分", "Resolved / Graded")} />
      <Stat value={`${r.graded_count} / ${r.selected} · ${pct(r.coverage)}`} label={t("判分覆盖", "Grading coverage")} />
      <Stat value={number(r.not_run)} label={t("未运行", "Not run")} />
      <Stat value={number(r.infra_failed)} label={t("执行环境异常", "Infrastructure errors")} />
      <Stat value={number(r.grading_errors)} label={t("判分异常", "Grading errors")} />
    </div>
    <p className="muted small">{t("解决率仅以获得官方判分的题目为分母；未运行和未成功判分的题目单独列出。", "Resolved rate uses officially graded cases only; unstarted and ungraded cases are shown separately.")}</p>
    <dl>
      <dt>{t("费用范围", "Cost scope")}</dt><dd>{costScope}</dd>
      <dt>{t("已结算费用", "Settled cost")}</dt><dd>{dollars(r.known_cost_usd)}</dd>
      <dt>{t("未结算预占", "Pending reservation")}</dt><dd>{dollars(r.pending_reserved_usd)} · {t("非已计费用", "not a settled charge")}</dd>
      <dt>{t("实际总费用", "Actual total cost")}</dt><dd>{r.cost_known === true ? dollars(r.actual_cost_usd) : t("待结算", "Unsettled")}</dd>
      {r.campaign_cost && <><dt>{t("实验累计已结算", "Campaign settled cost")}</dt><dd>{dollars(r.campaign_cost.known_cost_usd)}</dd><dt>{t("实验累计未结算预占", "Campaign pending reservation")}</dt><dd>{dollars(r.campaign_cost.pending_reserved_usd)}</dd></>}
      <dt>{t("题集版本", "Dataset revision")}</dt><dd>{r.dataset_version || "—"}</dd>
      <dt>{t("沿用已判分结果", "Carried graded results")}</dt><dd>{number(r.carried_count)}</dd>
      <dt>{t("汇总来源", "Summary digest")}</dt><dd className="mono" title={r.source_sha256}>{short(r.source_sha256)}</dd>
    </dl>
    <details><summary>{t("逐题判分与版本来源", "Per-case grading and source revisions")}</summary><div className="table-scroll"><table>
      <thead><tr>{[t("题目", "Case"), t("结果", "Outcome"), t("官方评分", "Official reward"), t("来源运行", "Source run"), "Runtime", t("沙箱 / 收集器", "Sandbox / collector")].map((label)=><th key={label}>{label}</th>)}</tr></thead>
      <tbody>{(r.cases || []).map((row)=><tr key={row.instance_id}><td>{row.instance_id}</td><td>{t(...(outcomes[row.outcome] || [row.outcome, row.outcome]))}</td><td>{typeof row.reward === "number" ? number(row.reward) : "—"}</td><td>{row.source_attempt?.run_id || "—"}{row.source_attempt?.carried ? ` · ${t("沿用", "Carried")}` : ""}</td><td className="mono" title={row.source_attempt?.runtime_commit}>{short(row.source_attempt?.runtime_commit)}</td><td className="mono" title={[row.source_attempt?.sandbox_commit,row.source_attempt?.collector_commit].filter(Boolean).join(" / ")}>{short(row.source_attempt?.sandbox_commit)} / {short(row.source_attempt?.collector_commit)}</td></tr>)}</tbody>
    </table></div></details>
  </section>;
}

function Run({ r, t, lang, audit, opened }) {
  const terminalSummary = r.source === "terminalbench_artifacts";
  const sweSummary = r.suite === "swebench" && r.source === "benchmark_artifacts";
  const runAudit = audit || (r.execution_valid === false ? r : null);
  const scores = sweSummary && r.graded_count === 0 ? {} : r.scores || {};
  const score = scoreForRun(scores, r.suite);
  return (
    <details className="run-item" data-run-id={r.run_id} open={opened || undefined}>
      <summary>
        <div className="run-identity">
          <strong>{r.suite} <SelectionInfo row={r} t={t} /></strong>
          <span><RuntimeSubject row={r} t={t}/>{r.variant ? ` · ${r.variant === "candidate" ? t("候选版", "Candidate") : t("基线版", "Baseline")}` : ""}</span>
        </div>
        <div className="run-meta">
          <Badge>{t(...EVALUATION_TYPES[evaluationType(r)])}</Badge>
          {runAudit ? <Badge>{t("异常批次", "Affected run")}</Badge> : <Status r={r} t={t} />}
          <span className="mono">
            {r.source === "swe_progress" && Number.isInteger(r.graded_count) ? t(`已提交 ${r.submitted} / ${r.selected} · 已收回非空补丁 ${r.patches} · 已归档判分 ${r.graded_count} / ${r.selected}，其中 ${r.resolved} 题通过`,
              `Submitted ${r.submitted} / ${r.selected} · ${r.patches} nonempty patches · ${r.graded_count} / ${r.selected} officially graded, ${r.resolved} resolved`) :
              r.source === "swe_progress" ? t(`已提交 ${r.submitted} / ${r.selected} · 已收回非空补丁 ${r.patches} · 官方评分待归档`,
              `Submitted ${r.submitted} / ${r.selected} · ${r.patches} nonempty patches collected · Awaiting official grading report`) :
              sweSummary && r.graded_count === 0 ? t("暂无官方判定","No official verdicts") :
              sweSummary ? `${r.resolved} / ${r.selected} ${t("题已解决", "resolved")}` :
              terminalSummary ? `${r.graded_count} / ${r.selected} ${t("已判分", "graded")}` :
              `${r.done} / ${r.planned} ${t("已处理", "processed")}`}
            {sweSummary && Number.isInteger(r.graded_count) &&
              ` · ${t("判分覆盖", "Grading coverage")} ${r.graded_count} / ${r.selected}`}
          </span>
          <span className="run-date">{date(sweSummary ? r.created_at : r.updated_at, lang)}</span>
        </div>
        <div className="run-score">
          <strong>{score ? scoreValue(...score) : "—"}</strong>
          <small>
            {score
              ? (terminalSummary && score[0] === "resolved_rate" ? t("已判分解决率", "Graded resolved rate") : metricName[score[0]]?.[lang === "zh" ? 0 : 1] || score[0])
              : t("暂无评分", "Unscored")}
          </small>
        </div>
        <span className="run-cost mono">{terminalSummary && r.cost_known !== true ? `${t("已知 ", "Known ")}${dollars(r.known_cost_usd)}` : dollars(r.cost_usd)}</span>
      </summary>
      <div className="run-detail">
        {runAudit && <p className="muted">{runAudit[`reason_${lang}`] || runAudit.invalid_reason}</p>}
        <dl>
          <EvaluationIdentity row={r} t={t} />
          <dt>{t("运行标识", "Run ID")}</dt>
          <dd>{r.run_id}</dd>
          {sweSummary && <><dt>{t("成绩来源", "Score source")}</dt><dd>{t("官方评分器归档", "Official grader report")}</dd></>}
          {sweSummary && Number.isInteger(r.empty_patch_count) && <>
            <dt>{t("有通过或未通过判定", "Graded resolved or unresolved")}</dt><dd>{r.graded_count} / {r.selected}</dd>
            <dt>{t("空补丁", "Empty patches")}</dt><dd>{r.empty_patch_count}</dd>
            <dt>{t("执行错误 / 判分异常", "Run errors / grading exceptions")}</dt><dd>{r.errors} / {r.grading_errors}</dd>
          </>}
          {r.provider_quota_interrupted_count > 0 && <><dt>{t("供应商额度中断且无补丁", "No patch after provider quota interruption")}</dt><dd>{r.provider_quota_interrupted_count}</dd></>}
          <dt>{t("切分", "Split")}</dt>
          <dd>{r.split || "—"}</dd>
          <dt>{t("开始时间", "Started")}</dt>
          <dd>{date(r.started_at, lang)}</dd>
          {r.eval_set_version && <><dt>{t("题集版本", "Dataset version")}</dt><dd>{r.eval_set_version}</dd></>}
          {r.protocol && <><dt>{t("评测范围", "Evaluation protocol")}</dt><dd>{r.protocol}</dd></>}
          {r.variant && <><dt>{t("对照版本", "Comparison arm")}</dt><dd>{r.variant}</dd></>}
          {r.eval_set_name && <><dt>{t("题集", "Dataset")}</dt><dd>{r.eval_set_name}</dd></>}
          {r.labels_draft > 0 && <><dt>{t("标注状态", "Label status")}</dt><dd>{t(`${r.labels_draft} 道草稿回归题，尚未经人审`, `${r.labels_draft} draft regression cases, not yet human-reviewed`)}</dd></>}
        </dl>
        {terminalSummary ? <TerminalBenchDetails r={r} t={t} lang={lang} /> : <div className="metrics">
          {Object.entries(scores).map(([k, v]) => (
            <Stat
              key={k}
              value={scoreValue(k, v)}
              label={metricName[k]?.[lang === "zh" ? 0 : 1] || k}
            />
          ))}
          {Object.entries(r.outcomes || {}).map(([k, v]) => (
            <Stat key={`outcome-${k}`} value={number(v)} label={k} />
          ))}
        </div>}
      </div>
    </details>
  );
}
function Datasets(p) {
  const { t, tr, site, lang } = p;
  const [items, setItems] = useState(null),
    [failed, setFailed] = useState([]),
    [query, setQuery] = useState(""),
    [grouping, setGrouping] = useState(
      new URLSearchParams(location.search).get("group") || "eval_category",
    ),
    [status, setStatus] = useState(""),
    [category, setCategory] = useState(
      new URLSearchParams(location.search).get("category") || "",
    );
  const datasetCache = useRef(new Map());
  useEffect(() => {
    if (!site) return;
    let active = true;
    const controller = new AbortController();
    const parts = new Array(site.datasets.length);
    let next = 0;
    const worker = async () => {
      while (next < site.datasets.length && active) {
        const index = next++;
        const ds = site.datasets[index];
        try {
          const response = await fetch(
            `/api/datasets/${encodeURIComponent(ds.name)}.json`,
            { signal: controller.signal },
          );
          if (!response.ok) throw Error(response.status);
          const data = await response.json();
          datasetCache.current.set(ds.name, data.items || []);
          parts[index] = { items: data.items || [] };
        } catch {
          parts[index] = {
            failed: ds.name,
            items: datasetCache.current.get(ds.name) || [],
          };
        }
      }
    };
    Promise.all(
      Array.from({ length: Math.min(4, site.datasets.length) }, worker),
    ).then(() => {
      if (active) {
        setItems(parts.flatMap((p) => p.items));
        setFailed(parts.filter((p) => p.failed).map((p) => p.failed));
      }
    });
    return () => {
      active = false;
      controller.abort();
    };
  }, [site]);
  const total = Totals(site);
  const review = (i) =>
    ["confirmed", "original"].includes(i.review?.status)
      ? i.review.status
      : "draft";
  const reviewLabel = (k) =>
    ({
      confirmed: t("已人审", "Reviewed"),
      original: t("官方原题", "Official"),
      draft: t("待人审", "Awaiting review"),
    })[k] || k;
  const groupLabel = (kind, key) =>
    kind === "review"
      ? reviewLabel(key)
      : kind === "dataset"
        ? tr(site?.datasets.find((d) => d.name === key)?.title) || key
        : kind === "framework"
          ? tr(site?.frameworks.find((f) => f.id === key)?.title) || key
          : tr(
              (site?.[
                kind === "eval_category" ? "categories" : "source_types"
              ] || {})[key],
            ) || key;
  const filtered = (items || []).filter(
    (i) =>
      (!status || review(i) === status) &&
      (!category || i.eval_category === category) &&
      `${i.id} ${i.question} ${text(i.answer)} ${text(i.source)}`
        .toLowerCase()
        .includes(query.toLowerCase()),
  );
  const groups = new Map();
  for (const i of filtered) {
    const key = grouping === "review" ? review(i) : i[grouping] || "—";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(i);
  }
  return (
    <>
      <div className="stats dataset-stats">
        <Stat
          value={site && !site.unavailable ? total.total : "—"}
          label={t("全部题目", "Total questions")}
        />
        <Stat
          value={site && !site.unavailable ? total.shown : "—"}
          label={t("公开题目", "Public questions")}
        />
        <Stat
          value={site && !site.unavailable ? total.holdout_hidden : "—"}
          label={t("留出集", "Held out")}
        />
        <Stat
          value={site && !site.unavailable ? total.draft : "—"}
          label={t("待人审", "Awaiting review")}
        />
      </div>
      <div className="section-heading">
        <h2>{t("测试框架", "Evaluation frameworks")}</h2>
        <span className="muted">
          {site?.frameworks.length || "—"} {t("套框架", "frameworks")}
        </span>
      </div>
      <Frameworks {...p} />
      <div className="section-heading question-heading">
        <h2>{t("公开题库", "Question explorer")}</h2>
        <a href="/api/site.json">{t("数据目录 JSON", "Catalog JSON")} ↗</a>
      </div>
      <div
        className="category-tabs"
        aria-label={t("评测类别", "Evaluation categories")}
      >
        <Button
          size="sm"
          variant={!category ? "primary" : "secondary"}
          onPress={() => setCategory("")}
        >
          {t("全部", "All")}
        </Button>
        {Object.entries(site?.categories || {}).map(([key, label]) => (
          <Button
            key={key}
            size="sm"
            variant={category === key ? "primary" : "ghost"}
            onPress={() => setCategory(key)}
          >
            {tr(label)}
          </Button>
        ))}
      </div>
      <div className="filterbar dataset-filters">
        <Input
          id="search"
          type="search"
          aria-label={t(
            "搜索题目、答案、来源",
            "Search questions, answers, sources",
          )}
          placeholder={t(
            "搜索题目、答案、来源…",
            "Search questions, answers, sources…",
          )}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <Select
          id="grouping"
          label={t("分组方式", "Group by")}
          value={grouping}
          onChange={setGrouping}
          options={[
            ["eval_category", t("按评测类别", "By category")],
            ["dataset", t("按数据集", "By dataset")],
            ["source_type", t("按来源", "By source")],
            ["framework", t("按框架", "By framework")],
            ["review", t("按人审状态", "By review status")],
          ]}
        />
        <Select
          id="status"
          label={t("人审状态", "Review status")}
          value={status}
          onChange={setStatus}
          options={[
            ["", t("全部人审状态", "All review statuses")],
            ...["confirmed", "original", "draft"].map((k) => [
              k,
              reviewLabel(k),
            ]),
          ]}
        />
      </div>
      <div id="shown" className="section-caption">
        <span>
          {t(
            `显示 ${filtered.length} 道公开题目`,
            `${filtered.length} public questions shown`,
          )}
        </span>
        <span>
          {t(
            `${total.holdout_hidden} 道留出题不公开`,
            `${total.holdout_hidden} held-out questions remain private`,
          )}
        </span>
      </div>
      {failed.length > 0 && (
        <div role="status" className="notice">
          {t(
            `${failed.length} 个数据集暂时无法加载，稍后将自动重试。`,
            `${failed.length} datasets could not be loaded. Retrying automatically.`,
          )}
        </div>
      )}
      {!items ? (
        <Loading />
      ) : (
        <div id="groups">
          {[...groups]
            .sort((a, b) =>
              groupLabel(grouping, a[0]).localeCompare(
                groupLabel(grouping, b[0]),
              ),
            )
            .map(([k, rows]) => (
              <section className="group" key={k}>
                <h3>
                  {groupLabel(grouping, k)}{" "}
                  <span className="count">{rows.length}</span>
                </h3>
                {rows.map((i) => (
                  <Question
                    key={`${i.dataset}/${i.id}`}
                    item={i}
                    {...p}
                    reviewLabel={reviewLabel}
                    status={review(i)}
                    groupLabel={groupLabel}
                  />
                ))}
              </section>
            ))}
          {!filtered.length && (
            <Empty>
              {t(
                "没有找到匹配的题目，请调整筛选条件。",
                "No matching questions. Try changing your filters.",
              )}
            </Empty>
          )}
        </div>
      )}
    </>
  );
}
function SafeLink({ href, children }) {
  return /^https:\/\//.test(href || "") ? (
    <a href={href} rel="noopener noreferrer" target="_blank">
      {children}
    </a>
  ) : (
    <>{children}</>
  );
}
function Question({ item: i, t, tr, status, reviewLabel, groupLabel }) {
  const [open, setOpen] = useState(false);
  const sum = i.view?.summary;
  return (
    <details className="row" onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>
        <div className="question-copy">
          <span className="question-id mono">{i.id}</span>
          <div className="q">{sum ? tr(sum.question) : i.question || i.id}</div>
          <span className="answer-preview">
            {sum ? tr(sum.answer) : text(i.answer)}
          </span>
        </div>
        <div className="tags">
          <Badge color={status === "draft" ? "warning" : "default"}>
            {reviewLabel(status)}
          </Badge>
          <Badge>{groupLabel("source_type", i.source_type)}</Badge>
          <span className="source-label">
            {i.source?.dataset ||
              i.source?.work_item ||
              groupLabel("dataset", i.dataset)}
          </span>
        </div>
      </summary>
      {open && (
        <div className="body">
          <dl>
            <dt>{t("数据集", "Dataset")}</dt>
            <dd>{groupLabel("dataset", i.dataset)}</dd>
            <dt>{t("框架", "Framework")}</dt>
            <dd>{groupLabel("framework", i.framework)}</dd>
            <dt>{t("来源", "Source")}</dt>
            <dd>
              <SafeLink href={i.source?.url}>
                {[
                  i.source?.dataset,
                  i.source?.source_id || i.source?.instance_id,
                  i.source?.revision,
                  i.source?.work_item,
                ]
                  .filter(Boolean)
                  .join(" · ") ||
                  i.source?.url ||
                  "—"}
              </SafeLink>
            </dd>
            {i.source?.license && (
              <>
                <dt>{t("许可证", "License")}</dt>
                <dd>{i.source.license}</dd>
              </>
            )}
            {i.review?.confirmed_by && (
              <>
                <dt>{t("人审", "Review")}</dt>
                <dd>
                  {i.review.confirmed_by} · {i.review.confirmed_at}
                </dd>
              </>
            )}
          </dl>
          {i.view ? (
            (i.view.blocks || []).map((b, n) => (
              <Block key={n} b={b} t={t} tr={tr} />
            ))
          ) : (
            <>
              <h4>{t("题目", "Question")}</h4>
              <pre>{i.question}</pre>
              <h4>{t("参考答案与标注", "Reference answer & labels")}</h4>
              <pre>{text(i.answer)}</pre>
              {i.detail?.rubric && (
                <>
                  <h4>{t("评分标准", "Scoring rubric")}</h4>
                  <pre>{text(i.detail.rubric)}</pre>
                </>
              )}
              <details>
                <summary>{t("完整用例", "Full case")}</summary>
                <pre>{text(i.detail)}</pre>
              </details>
            </>
          )}
          <details className="provenance">
            <summary>{t("完整来源信息", "Full provenance")}</summary>
            <pre>{text(i.source)}</pre>
          </details>
        </div>
      )}
    </details>
  );
}
function Block({ b, t, tr }) {
  const items = b.items || [];
  return (
    <section className={`blk blk-${b.type}`}>
      {b.label && <h4>{tr(b.label)}</h4>}
      {b.type === "text" ? (
        b.mono ? (
          <pre>{b.text}</pre>
        ) : (
          <div className="prose">{b.text}</div>
        )
      ) : b.type === "candidates" ? (
        <div className={b.columns ? "candidates columns" : "candidates"}>
          {items.map((c, i) => (
            <div className="candidate" key={i}>
              <strong>{tr(c.label)}</strong>
              {c.mark && (
                <Badge color="success">
                  {c.mark === "better"
                    ? t("更优回答", "Preferred")
                    : t("正确答案", "Correct answer")}
                </Badge>
              )}
              <div className="prose">{c.text}</div>
            </div>
          ))}
        </div>
      ) : b.type === "fields" ? (
        <dl>
          {items.map((f, i) => (
            <React.Fragment key={i}>
              <dt>{tr(f.key)}</dt>
              <dd>
                <SafeLink href={f.href}>{tr(f.value)}</SafeLink>
              </dd>
            </React.Fragment>
          ))}
        </dl>
      ) : b.type === "messages" ? (
        items.map((m, i) => (
          <div className="message" key={i}>
            <Badge>{m.role}</Badge>
            <div className="prose">{m.content}</div>
          </div>
        ))
      ) : b.type === "tools" ? (
        items.map((x, i) => (
          <details key={i}>
            <summary>
              <code>{x.name}</code> {x.description}
            </summary>
            <pre>{text(x.parameters)}</pre>
          </details>
        ))
      ) : b.type === "calls" ? (
        items.map((c, i) => (
          <div key={i}>
            <h4>{c.name}</h4>
            <pre>{text(c)}</pre>
          </div>
        ))
      ) : b.type === "list" ? (
        <ul>
          {items.map((s, i) => (
            <li key={i}>
              <SafeLink href={s}>{s}</SafeLink>
            </li>
          ))}
        </ul>
      ) : (
        <pre>{text(b.value)}</pre>
      )}
    </section>
  );
}
const colorScheme = matchMedia("(prefers-color-scheme: dark)");
const applyColorScheme = () => {
  document.documentElement.classList.toggle("dark", colorScheme.matches);
  document.documentElement.dataset.theme = colorScheme.matches
    ? "dark"
    : "light";
};
applyColorScheme();
colorScheme.addEventListener("change", applyColorScheme);
createRoot(document.getElementById("root")).render(<App />);
