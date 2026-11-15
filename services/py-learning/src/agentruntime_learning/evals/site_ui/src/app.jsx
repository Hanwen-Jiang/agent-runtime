import React, { useEffect, useState, useRef } from "react";
import { createRoot } from "react-dom/client";
import { Button, Card, Chip, Input, Skeleton } from "@heroui/react";
import { NativeSelect } from "@heroui-pro/react/native-select";
import { bestResults, scoreForRun } from "./best-results.js";

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
    archive = useFeed("/api/reports.json", 60000);
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
    archive: archive.data,
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
              ["home", "/", t("主页", "Overview")],
              ["results", "/results", t("评测结果", "Results")],
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
  const { t, tr, site, live, archive, lang } = p;
  const total = Totals(site);
  const rows = sortRuns(live?.runs || []);
  const best = bestResults(rows);
  const cats = {};
  for (const d of site?.datasets || [])
    cats[d.eval_category] = (cats[d.eval_category] || 0) + d.counts.total;
  const last = [
    rows[0]?.updated_at,
    ...(archive?.reports?.experiments || []).map((r) => r.completed_at),
  ]
    .filter((value) => value && Number.isFinite(Date.parse(value)))
    .sort((a, b) => Date.parse(b) - Date.parse(a))[0];
  return (
    <>
      <div className="update-line">
        <span className="status-dot" />
        {last
          ? `${t("最近评测", "Latest evaluation")} ${date(last, lang)}`
          : live?.unavailable || live?.source === "none"
            ? t("评测数据暂不可用", "Evaluation data unavailable")
            : live
              ? t("暂无评测运行", "No evaluation runs yet")
              : t("评测结果加载中", "Loading evaluation results")}
        {site?.sync?.synced_at && (
          <span>
            {t("题库更新", "Catalog updated")} {date(site.sync.synced_at, lang)}
          </span>
        )}
      </div>
      <Experiments {...p} compact />
      <div className="overview-grid">
        <Card className="overview-panel">
          <Card.Header className="panel-head">
            <Card.Title>
              {t("最佳评测结果", "Best evaluation results")}
            </Card.Title>
            <a href="/results">{t("全部结果", "All results")} →</a>
          </Card.Header>
          <Card.Content>
            {!live ? (
              <Loading />
            ) : (
              <>
                <div id="run-stats" className="stats three">
                  <Stat
                    value={
                      live?.unavailable || live?.source === "none"
                        ? "—"
                        : rows.filter((r) => r.status === "running").length
                    }
                    label={t("正在运行", "Running")}
                  />
                  <Stat
                    value={
                      live?.unavailable || live?.source === "none"
                        ? "—"
                        : rows.filter((r) => r.status === "finished").length
                    }
                    label={t("最近完成", "Recently completed")}
                  />
                  <Stat
                    value={
                      archive?.source !== "none" &&
                      Array.isArray(archive?.reports?.runs)
                        ? archive.reports.runs.length
                        : "—"
                    }
                    label={t("归档报告", "Archived reports")}
                  />
                </div>
                <div className="suite-list">
                  {best.length ? (
                    best.map(({ run: r, score }) => {
                      return (
                        <a
                          href={`/results?q=${encodeURIComponent(r.run_id)}`}
                          className="suite-row"
                          key={`${r.suite}/${r.split}`}
                        >
                          <span>
                            <strong>{r.suite}</strong>
                            <small
                              title={`${r.model || r.subject} · ${r.split}`}
                            >
                              {r.model || r.subject} · {r.split}
                            </small>
                          </span>
                          <span className="score">
                            {score
                              ? scoreValue(...score)
                              : `${r.done} / ${r.planned}`}
                            <small>
                              {score
                                ? metricName[score[0]]?.[
                                    lang === "zh" ? 0 : 1
                                  ] || score[0]
                                : t("已完成", "Completed")}
                            </small>
                          </span>
                        </a>
                      );
                    })
                  ) : (
                    <Empty>{t("暂无评测运行", "No evaluation runs yet")}</Empty>
                  )}
                </div>
              </>
            )}
          </Card.Content>
          <Card.Footer className="muted small">
            {t(
              "按任务与数据切分选主指标最高的一次已结束运行，不含标记无效的结果。",
              "Highest primary metric per task and split among finished runs; explicitly invalid results are excluded.",
            )}
          </Card.Footer>
        </Card>
        <Card className="overview-panel">
          <Card.Header className="panel-head">
            <Card.Title>
              {t("数据集与框架", "Datasets & frameworks")}
            </Card.Title>
            <a href="/datasets">{t("全部题目", "All questions")} →</a>
          </Card.Header>
          <Card.Content>
            {!site ? (
              <Loading />
            ) : (
              <>
                <div className="catalog-total">
                  <b>{site?.unavailable ? "—" : number(total.total)}</b>
                  <span>
                    {t("道题目", "questions")} · {site.datasets.length}{" "}
                    {t("个数据集", "datasets")} · {site.frameworks.length}{" "}
                    {t("套框架", "frameworks")}
                  </span>
                </div>
                <div
                  className="source-bar"
                  role="img"
                  aria-label={["original", "authored", "adapted"]
                    .map((k) => `${tr(site.source_types?.[k])} ${total[k]}`)
                    .join(", ")}
                >
                  {["original", "authored", "adapted"].map((k) => (
                    <span
                      key={k}
                      className={k}
                      style={{
                        width: `${total.total ? (total[k] / total.total) * 100 : 0}%`,
                      }}
                    />
                  ))}
                </div>
                <div className="legend">
                  {["original", "authored", "adapted"].map((k) => (
                    <span key={k}>
                      <i className={k} />
                      {tr(site.source_types?.[k])} {total[k]}
                    </span>
                  ))}
                </div>
                <div id="set-stats" className="stats three">
                  <Stat
                    value={site?.unavailable ? "—" : total.shown}
                    label={t("公开题目", "Public questions")}
                  />
                  <Stat
                    value={site?.unavailable ? "—" : total.holdout_hidden}
                    label={t("留出集", "Held out")}
                  />
                  <Stat
                    value={site?.unavailable ? "—" : total.draft}
                    label={t("待人审", "Awaiting review")}
                  />
                </div>
                <div className="category-chips">
                  {Object.entries(cats)
                    .sort((a, b) => b[1] - a[1])
                    .map(([k, v]) => (
                      <a
                        href={`/datasets?category=${encodeURIComponent(k)}`}
                        key={k}
                      >
                        <Badge>
                          {tr(site.categories?.[k])}{" "}
                          <span className="mono">{v}</span>
                        </Badge>
                      </a>
                    ))}
                </div>
              </>
            )}
          </Card.Content>
        </Card>
      </div>
      <div className="section-heading">
        <h2>{t("评测方法", "Evaluation methods")}</h2>
        <a href="/datasets">
          {t("查看框架与适用范围", "Frameworks and scope")} →
        </a>
      </div>
      <Frameworks {...p} compact />
    </>
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
function Experiments({ archive, t, lang, compact = false }) {
  const experiments = [...(archive?.reports?.experiments || [])].sort(
    (a, b) =>
      (Date.parse(b.completed_at || b.evaluated_on) || 0) -
      (Date.parse(a.completed_at || a.evaluated_on) || 0),
  );
  if (!experiments.length) return null;
  const metricValue = (value, unit) => {
    if (typeof value !== "number" || !Number.isFinite(value)) return "—";
    if (["ratio", "fraction", "rate"].includes(unit)) return pct(value);
    return `${number(value)}${unit === "seconds" ? " s" : ""}`;
  };
  return (
    <section
      className="experiments"
      aria-label={t("最新实验", "Latest experiments")}
    >
      <div className="section-heading">
        <h2>{t("最新实验", "Latest experiments")}</h2>
        {compact && (
          <a href="/results#experiments">
            {t("查看结果与限制", "Results and limitations")} →
          </a>
        )}
      </div>
      <div className="experiment-grid" id={compact ? undefined : "experiments"}>
        {experiments.map((experiment) => {
          const failed =
            experiment.acceptance_passed === false ||
            experiment.quality_valid === false;
          const passed =
            experiment.acceptance_passed === true &&
            experiment.quality_valid === true;
          const limitations = experiment[`limitations_${lang}`] || [];
          return (
            <article key={experiment.id} className="experiment-card">
              <div className="experiment-heading">
                <h3>{experiment[`title_${lang}`] || experiment.id}</h3>
                <Badge
                  color={failed ? "danger" : passed ? "success" : "warning"}
                >
                  {failed
                    ? t("验收未通过", "Acceptance failed")
                    : passed
                      ? t("验收通过", "Accepted")
                      : t("待验证", "Unverified")}
                </Badge>
              </div>
              <div className="experiment-meta">
                <span>
                  {experiment.completed_at
                    ? date(experiment.completed_at, lang)
                    : experiment.evaluated_on ||
                      t("实验日期未记录", "Evaluation date not recorded")}
                </span>
                <span>
                  {number(experiment.completed_count)} /{" "}
                  {number(experiment.sample_count)}{" "}
                  {t("样本完成", "samples completed")}
                </span>
                <span>
                  {experiment.cost_known
                    ? dollars(experiment.actual_cost_usd)
                    : t("完整费用未知", "Total cost unknown")}
                </span>
              </div>
              {compact ? (
                <p className="experiment-summary">
                  {limitations[0] ||
                    t(
                      "查看完整指标、对照设置与验收状态。",
                      "Inspect metrics, comparison settings and acceptance status.",
                    )}
                </p>
              ) : (
                <>
                  {(experiment.metrics || []).length > 0 && (
                    <div className="table-scroll">
                      <table>
                        <thead>
                          <tr>
                            <th>{t("指标", "Metric")}</th>
                            <th>
                              {experiment.baseline_label ||
                                t("基线", "Baseline")}
                            </th>
                            <th>
                              {experiment.candidate_label ||
                                t("候选方案", "Candidate")}
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          {experiment.metrics.map((metric) => (
                            <tr key={metric.key}>
                              <td>{metric[`label_${lang}`] || metric.key}</td>
                              <td>
                                {metricValue(metric.baseline, metric.unit)}
                              </td>
                              <td>
                                {metricValue(metric.candidate, metric.unit)}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                  {limitations.length > 0 && (
                    <ul className="experiment-limitations">
                      {limitations.map((limitation, index) => (
                        <li key={index}>{limitation}</li>
                      ))}
                    </ul>
                  )}
                  {!experiment.cost_known &&
                    typeof experiment.known_cost_usd === "number" && (
                      <p className="small muted">
                        {t("已知费用下界", "Known cost lower bound")}:{" "}
                        {dollars(experiment.known_cost_usd)}
                      </p>
                    )}
                  {experiment.reported_on && (
                    <small>
                      {t("汇总日期", "Report date")}: {experiment.reported_on}
                    </small>
                  )}
                </>
              )}
            </article>
          );
        })}
      </div>
    </section>
  );
}

function Status({ r, t }) {
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
            : t("已完成", "Completed")}
    </Badge>
  );
}
function Results(p) {
  const { t, tr, lang, live, archive } = p;
  const [view, setView] = useState("recent"),
    [search, setSearch] = useState(
      new URLSearchParams(location.search).get("q") || "",
    ),
    [status, setStatus] = useState(""),
    [split, setSplit] = useState("");
  const rows = sortRuns(live?.runs || []);
  const filtered = rows.filter(
    (r) =>
      (!status ||
        (status === "invalid" ? r.valid === false : r.status === status)) &&
      (!split || r.split === split) &&
      `${r.suite} ${r.model} ${r.subject} ${r.run_id}`
        .toLowerCase()
        .includes(search.toLowerCase()),
  );
  const reports = archive?.reports || {};
  const history = (reports.runs || []).filter(
    (r) =>
      (!split || r.split === split) &&
      `${r.label_zh} ${r.label_en} ${r.group} ${r.subject}`
        .toLowerCase()
        .includes(search.toLowerCase()),
  );
  const running = rows.filter((r) => r.status === "running");
  return (
    <>
      <Experiments {...p} />
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
              ? dollars(rows.reduce((s, r) => s + (r.cost_usd || 0), 0))
              : "—"
          }
          label={t("最近运行费用", "Recent run cost")}
        />
      </div>
      {running.length > 0 && (
        <section className="active-runs">
          <div className="section-heading">
            <h2>{t("运行中", "Running now")}</h2>
            <Badge color="accent">{running.length}</Badge>
          </div>
          {running.map((r) => (
            <div className="live-progress" key={r.run_id}>
              <div>
                <strong>{r.suite}</strong>
                <span>
                  {r.done} / {r.planned}
                </span>
              </div>
              <progress max={r.planned || 1} value={r.done} />
              <small>
                {r.subject} · {dollars(r.cost_usd)}
              </small>
            </div>
          ))}
        </section>
      )}
      <div
        className="results-switch"
        role="group"
        aria-label={t("结果范围", "Result scope")}
      >
        <Button
          variant={view === "recent" ? "primary" : "ghost"}
          onPress={() => setView("recent")}
        >
          {t("最近运行", "Recent runs")} {rows.length}
        </Button>
        <Button
          data-view="archive"
          variant={view === "archive" ? "primary" : "ghost"}
          onPress={() => setView("archive")}
        >
          {t("历史报告", "Report archive")} {(reports.runs || []).length}
        </Button>
      </div>
      <div className="filterbar">
        <Input
          aria-label={t("搜索评测、模型", "Search evaluations and models")}
          placeholder={t("搜索评测、模型…", "Search evaluations, models…")}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
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
              ["running", t("运行中", "Running")],
              ["stalled", t("长时间未更新", "Stalled")],
              ["invalid", t("无效", "Invalid")],
            ]}
          />
        )}
      </div>
      <div className="section-caption">
        <span>
          {view === "recent"
            ? t(
                `${filtered.length} 次运行 · 最近 31 天`,
                `${filtered.length} runs · Last 31 days`,
              )
            : t(
                `${history.length} 份归档报告`,
                `${history.length} archived reports`,
              )}
        </span>
        <span>
          {view === "recent" && rows[0]?.updated_at
            ? `${t("最近结果", "Latest result")} ${date(rows[0].updated_at, lang)}`
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
        ) : filtered.length ? (
          <div className="run-list">
            {filtered.map((r) => (
              <Run key={r.run_id} r={r} {...p} />
            ))}
          </div>
        ) : (
          <Empty>{t("没有符合条件的运行", "No matching runs")}</Empty>
        )
      ) : !archive ? (
        <Loading />
      ) : (
        <>
          <div className="table-scroll">
            <table id="runs">
              <thead>
                <tr>
                  {[
                    t("评测报告", "Report"),
                    t("切分", "Split"),
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
                        <summary>{r[`label_${lang}`] || r.group}</summary>
                        <dl>
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
                        </dl>
                      </details>
                      <small>
                        {date(r.created_at, lang)}
                        {r.valid === false ? ` · ${t("无效", "Invalid")}` : ""}
                      </small>
                    </td>
                    <td>{r.split}</td>
                    <td>{pct(r.triage)}</td>
                    <td>{pct(r.shape)}</td>
                    <td>{pct(r.reviewer)}</td>
                    <td>{dollars(r.total_usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!history.length && (
            <Empty>
              {t("没有符合条件的归档报告", "No matching archived reports")}
            </Empty>
          )}
          {(reports.benchmarks || []).length > 0 && (
            <>
              <div className="section-heading">
                <h2>{t("代码任务归档", "Coding benchmarks")}</h2>
              </div>
              <div className="archive-grid">
                {reports.benchmarks.map((r, i) => (
                  <Card key={r.run_id || i}>
                    <Card.Header>
                      <Card.Title>{r.model || r.run_id}</Card.Title>
                    </Card.Header>
                    <Card.Content>
                      <b className="large-score">{pct(r.rate)}</b>
                      <p>
                        {r.resolved} / {r.selected} {t("题已解决", "resolved")}{" "}
                        · {dollars(r.cost_usd)}
                      </p>
                      <small className="muted">{r.run_id}</small>
                      <details className="benchmark-detail">
                        <summary>
                          {t("查看判分详情", "Grading details")}
                        </summary>
                        <dl>
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
                            <summary>{r.group}</summary>
                            <dl>
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
                        <td>{r.split}</td>
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
                      <strong>{r[`short_${lang}`] || r.run_id}</strong>
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

function Run({ r, t, lang }) {
  const score = scoreForRun(r.scores, r.suite);
  return (
    <details className="run-item">
      <summary>
        <div className="run-identity">
          <strong>{r.suite}</strong>
          <span>{r.model || r.subject || r.run_id}</span>
        </div>
        <div className="run-meta">
          <Status r={r} t={t} />
          <span className="mono">
            {r.done} / {r.planned}
          </span>
          <span className="run-date">{date(r.updated_at, lang)}</span>
        </div>
        <div className="run-score">
          <strong>{score ? scoreValue(...score) : "—"}</strong>
          <small>
            {score
              ? metricName[score[0]]?.[lang === "zh" ? 0 : 1] || score[0]
              : t("暂无评分", "Unscored")}
          </small>
        </div>
        <span className="run-cost mono">{dollars(r.cost_usd)}</span>
      </summary>
      <div className="run-detail">
        <dl>
          <dt>{t("运行标识", "Run ID")}</dt>
          <dd>{r.run_id}</dd>
          <dt>{t("切分", "Split")}</dt>
          <dd>{r.split || "—"}</dd>
          <dt>{t("开始时间", "Started")}</dt>
          <dd>{date(r.started_at, lang)}</dd>
        </dl>
        <div className="metrics">
          {Object.entries(r.scores || {}).map(([k, v]) => (
            <Stat
              key={k}
              value={scoreValue(k, v)}
              label={metricName[k]?.[lang === "zh" ? 0 : 1] || k}
            />
          ))}
          {Object.entries(r.outcomes || {}).map(([k, v]) => (
            <Stat key={`outcome-${k}`} value={number(v)} label={k} />
          ))}
        </div>
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
