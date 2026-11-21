import React, { useMemo, useState, useEffect } from "react";
import { Button, Card, Input, Table } from "@heroui/react";
import { NativeSelect } from "@heroui-pro/react/native-select";
import { LineChart } from "@heroui-pro/react/line-chart";
import { SelectionInfo } from "./selection-info.jsx";
import { PUBLIC_BATCHES } from "./selection-info.js";
import { buildCategoryCharts } from "./result-series.js";
import { readTrendLocation, trendLocation } from "./result-location.js";

const finite = (value) => typeof value === "number" && Number.isFinite(value);
const words = (entry, lang, fallback = "") => entry?.[`label_${lang}`] || entry?.label || fallback;
const numeric = (value, digits = 3) => finite(value) ? value.toLocaleString(undefined, { maximumFractionDigits: digits }) : "—";
const percent = (value) => finite(value) ? `${(value * 100).toFixed(1)}%` : "—";
const valueText = (value, unit) => unit === "ratio" ? percent(value) : unit === "usd" ? `$${numeric(value)}` : `${numeric(value, unit === "score" ? 4 : 3)}${unit === "seconds" ? " s" : ""}`;
const dateText = (value, lang, short = false) => {
  const time = typeof value === "number" ? value : Date.parse(value);
  if (!Number.isFinite(time)) return "—";
  return new Date(time).toLocaleString(lang === "zh" ? "zh-CN" : "en-GB", { month: "short", day: "numeric", ...(short ? {} : { hour: "2-digit", minute: "2-digit" }) });
};
const categoryName = (suite) => ({ "terminal-bench": "Terminal-Bench", swebench: "SWE-bench", "task-loop": "Task Loop", deepeval: "DeepEval", ragas: "Ragas", context: "Context", longmemeval: "LongMemEval", "memory-rerank": "LongMemEval", "longbench-context": "LongBench v2 · Runtime" }[suite] || suite);

function Filter({ label, value, onChange, options }) {
  return <NativeSelect className="analytics-select" variant="secondary">
    <NativeSelect.Trigger aria-label={label} value={value} onChange={(event) => onChange(event.target.value)}>
      {options.map(([id, name]) => <NativeSelect.Option key={id} value={id}>{name}</NativeSelect.Option>)}
      <NativeSelect.Indicator />
    </NativeSelect.Trigger>
  </NativeSelect>;
}

function PointDetails({ point, group, lang, t, className = "" }) {
  if (!point) return null;
  return <div className={`analytics-point ${className}`}>
    <strong>{valueText(point.value, group.unit)}</strong>
    <span>{words(point, lang, point.label || point.id)}</span>
    {group.axis === "sequence" && <small>{group[`subject_${lang}`] || group.subject}</small>}
    {group.comparison_basis === "historical_shared_cohort" && <small>{t(`答对 ${point.correct_count}/${point.sample_count} · 未判定 ${point.unscored_count}`, `Correct ${point.correct_count}/${point.sample_count} · Unscored ${point.unscored_count}`)}{finite(point.correct_delta) && ` · ${point.correct_delta > 0 ? "+" : ""}${point.correct_delta} ${t("题", "answers")}`}{finite(point.delta_points) && ` / ${point.delta_points > 0 ? "+" : ""}${numeric(point.delta_points,1)} ${t("个百分点", "pp")}`}</small>}
    <small>{[point.at ? dateText(point.at, lang, point.date_precision === "day") : group.completed_at ? dateText(group.completed_at,lang) : null, point.sample_count ? t(`${point.sample_count} 个样本`, `${point.sample_count} samples`) : null, group.split].filter(Boolean).join(" · ")}</small>
  </div>;
}

const SERIES_COLORS = ["var(--accent)", "#8b5cf6", "#16836c", "#c17421", "#d15381", "#477e9d", "#8b733c", "#6d7c43"];
function seriesLabel(group, lang) {
  const sample = group.sample_count ? `N=${group.sample_count}` : (lang === "zh" ? "样本数未记录" : "N unrecorded");
  if (group.axis === "sequence") return [group.batch_id, group.completed_at?dateText(group.completed_at,lang,true):null,sample,`${group.points[0].label} → ${group.points.at(-1).label}`].filter(Boolean).join(" · ");
  const model = group.comparison_basis === "verified_cohort" ? "" : (group.model_family || "").split("|").at(-1).replace(/^label:/, "").replace(/a6api\//g, "").replace(/@\d{4}-\d{2}/g, "").replace(/(?:archive-)?model-unrecorded/, "");
  return [group.batch_id || sample, group.batch_id ? null : group.split, model || (group.comparison_basis === "verified_cohort" ? "" : group.subject)].filter(Boolean).join(" · ");
}

function TrendCard({ category, lang, t, batch = "", pinnedMetric = "", onMetricChange }) {
  const [chartHeight,setChartHeight]=useState(()=>window.matchMedia("(max-width:620px)").matches?250:330);
  useEffect(()=>{const media=window.matchMedia("(max-width:620px)");const resize=()=>setChartHeight(media.matches?250:330);media.addEventListener("change",resize);return()=>media.removeEventListener("change",resize);},[]);
  const [metricID, setMetricID] = useState(null);
  const [axis, setAxis] = useState(null);
  const [focused, setFocused] = useState(null);
  const requestedMetric = pinnedMetric || metricID;
  const metric = category.metrics.find((entry) => entry.id === (requestedMetric || category.default_metric)) || (!requestedMetric ? category.metrics[0] : null);
  const changeMetric = (value) => { setMetricID(value); setAxis(null); setFocused(null); onMetricChange?.(value); };
  if (!metric) return <Card id={`trend-${category.suite}`} className="trend-card" data-trend-category={category.suite}>
    <Card.Header className="trend-card-heading"><Card.Title>{categoryName(category.suite)}</Card.Title>
      <Filter label={t(`${categoryName(category.suite)} 指标`, `${categoryName(category.suite)} metric`)} value={requestedMetric} onChange={changeMetric}
        options={[[requestedMetric, requestedMetric], ...category.metrics.map((entry) => [entry.id, words(entry, lang, entry.id)])]} />
    </Card.Header><Card.Content><p className="analytics-empty" role="status">{t("指定指标的数据暂不可用，数据更新后将自动显示。", "The requested metric is unavailable; it will appear when data updates.")}</p></Card.Content>
  </Card>;
  const view = metric.views.find((entry) => entry.axis === axis) || metric.views[0];
  const sequence = view.axis === "sequence";
  const runAxis = view.axis === "time" && view.x_axis === "run";
  const series = batch ? view.series.map((group) => ({ ...group, points: group.points.filter((point) => point.batch_id === batch) })).filter((group) => group.points.length) : view.series;
  const historical = series.some((group) => group.comparison_basis === "historical_shared_cohort");
  const count = series.reduce((sum, group) => sum + group.points.length, 0);
  const runDates = runAxis ? [...new Set(series.flatMap((group) => group.points.map((point) => point.at)).filter(Boolean))]
    .sort((a, b) => Date.parse(a) - Date.parse(b)) : [];
  const runIndex = new Map(runDates.map((at, index) => [at, index]));
  const data = series.flatMap((group, seriesIndex) => group.points.map((point, order) => ({
    at: sequence ? order : runAxis ? runIndex.get(point.at) : Date.parse(point.at), [`series${seriesIndex}`]: point.value,
    point, group, seriesIndex,
  }))).sort((a, b) => a.at - b.at || a.seriesIndex - b.seriesIndex || a.point.id.localeCompare(b.point.id));
  const firstAt = data[0]?.at, lastAt = data.at(-1)?.at;
  const bounded = metric.unit === "ratio" || metric.unit === "score";
  const intraday = !sequence && !runAxis && lastAt - firstAt < 86400000 && data.every((entry) => entry.point.date_precision !== "day");
  const timeTick = (value) => intraday ? new Date(value).toLocaleTimeString(lang === "zh" ? "zh-CN" : "en-GB", { hour: "2-digit", minute: "2-digit" }) : dateText(value, lang, true);
  const sequenceTick = (value) => {
    if (series.length === 1) { const point = series[0].points[value]; return point?.[`short_label_${lang}`] || words(point, lang, "").slice(0, 18); }
    return value === 0 ? t("基线", "Baseline") : value === 1 && series.every((group) => group.points.length === 2) ? t("候选方案", "Candidate") : t(`阶段 ${value + 1}`, `Stage ${value + 1}`);
  };
  const runTick = (value) => {
    const at = runDates[Math.round(value)];
    return at ? dateText(at, lang, false) : "";
  };
  const domain = runAxis ? [0, Math.max(0, runDates.length - 1)] :
    !sequence && firstAt === lastAt ? [firstAt - 3600000, lastAt + 3600000] : ["dataMin", "dataMax"];
  const xTicks = runAxis ? runDates.map((_, index) => index) :
    sequence ? Array.from({length:Math.max(...series.map((group) => group.points.length))}, (_, index) => index) : undefined;
  const dot = (seriesIndex) => ({ cx, cy, payload }) => {
    if (!finite(cx) || !finite(cy) || payload?.seriesIndex !== seriesIndex) return null;
    const {point, group} = payload;
    const cohort = group.comparison_basis === "historical_shared_cohort";
    return <g><circle cx={cx} cy={cy} r={4} stroke="var(--surface)" strokeWidth={2} fill={SERIES_COLORS[seriesIndex % SERIES_COLORS.length]} tabIndex={0} role="img"
      aria-label={`${words(point, lang, point.id)}, ${valueText(point.value, metric.unit)}, N=${point.sample_count ?? "—"}`}
      onFocus={() => setFocused({point, group})} onBlur={() => setFocused(null)} />
      {cohort && <text x={cx} y={cy - 23} textAnchor="middle" fill="var(--foreground)" fontSize={11} pointerEvents="none">{percent(point.value)}{finite(point.delta_points) && point.correct_delta !== 0 && <tspan x={cx} dy={13} fill="var(--muted)" fontSize={9}>{point.delta_points > 0 ? "+" : ""}{numeric(point.delta_points,1)} pp</tspan>}</text>}
    </g>;
  };
  return <Card id={`trend-${category.suite}`} className="trend-card" data-trend-category={category.suite} data-trend-group={category.suite}>
    <Card.Header className="trend-card-heading">
      <div><Card.Title>{categoryName(category.suite)}</Card.Title><span className="analytics-run-count">{t(`${count} 次记录 · ${series.length} 个系列`, `${count} observations · ${series.length} series`)}</span></div>
      <Filter label={t(`${categoryName(category.suite)} 指标`, `${categoryName(category.suite)} metric`)} value={metric.id}
        onChange={changeMetric} options={category.metrics.map((entry) => [entry.id, words(entry, lang, entry.id)])} />
    </Card.Header>
    <Card.Content className="trend-card-body">
      {metric.views.length > 1 && <div className="trend-view-switch" role="group" aria-label={t("图表视图", "Chart view")}>{metric.views.map((entry) => <Button key={entry.axis} size="sm" variant={view.axis === entry.axis ? "secondary" : "ghost"} onPress={() => {setAxis(entry.axis);setFocused(null);}}>{entry.axis === "time" ? t("运行历史", "Run history") : t("方案对照", "Comparison")}</Button>)}</div>}
      {batch && !series.length ? <p className="analytics-empty" role="status">{t(`没有 ${batch} 批次的有效记录。`, `No valid observations found for ${batch}.`)}</p> : null}
      <div className="trend-legend" aria-label={t("图例与最近结果", "Legend and latest results")}>
        {series.map((group, index) => <div className="trend-legend-item" key={group.id} data-series-samples={group.sample_count ?? "unknown"} title={`${seriesLabel(group, lang)} · ${words(group.points.at(-1),lang,group.points.at(-1).id)}`}>
          <span className={group.points.length === 1 ? "trend-legend-dot" : "trend-legend-line"} style={{backgroundColor:SERIES_COLORS[index % SERIES_COLORS.length]}} />
          <span>{seriesLabel(group, lang)}</span><strong>{valueText(group.points.at(-1).value, metric.unit)}</strong>
        </div>)}
      </div>
      <LineChart data={data} height={chartHeight} margin={{ top: 12, right: 15, left: 0, bottom: 8 }} aria-label={`${categoryName(category.suite)} · ${words(metric, lang, metric.id)}`}>
        <LineChart.Grid vertical={false} stroke="var(--separator)" strokeDasharray="3 4" />
        <LineChart.XAxis dataKey="at" type="number" scale="linear" domain={domain}
          ticks={xTicks}
          padding={historical ? {left:22,right:28} : undefined} interval={historical ? 0 : undefined}
          tickFormatter={runAxis ? runTick : sequence ? sequenceTick : timeTick} axisLine={false} tickLine={false} tickMargin={10} minTickGap={28} tick={{ fontSize: 11, fill: "var(--muted)" }} />
        <LineChart.YAxis domain={bounded ? [0, 1] : [0, "auto"]} ticks={bounded ? [0, .25, .5, .75, 1] : undefined} width={45}
          tickFormatter={(value) => metric.unit === "ratio" ? `${Math.round(value * 100)}%` : numeric(value, 2)} axisLine={false} tickLine={false} tick={{ fontSize: 11, fill: "var(--muted)" }} />
        {series.map((group, index) => <LineChart.Line key={group.id} dataKey={`series${index}`} name={seriesLabel(group, lang)} type="linear"
          stroke={SERIES_COLORS[index % SERIES_COLORS.length]} strokeWidth={group.points.length > 1 ? 2 : 0}
          dot={dot(index)} activeDot={{r:5,strokeWidth:2}} isAnimationActive={false} connectNulls />)}
        <LineChart.Tooltip cursor={{ stroke: "var(--border)", strokeDasharray: "3 3" }} content={({active,payload}) => {
          const entry=payload?.find((item)=>finite(item.value));
          return active&&entry ? <PointDetails point={entry.payload.point} group={entry.payload.group} lang={lang} t={t} className="analytics-tooltip" /> : null;
        }} />
      </LineChart>
      {focused && <PointDetails point={focused.point} group={focused.group} lang={lang} t={t} className="analytics-focused-point" />}
      <details className="trend-observations"><summary>{t("查看每次结果", "View individual results")}</summary><p className="analytics-note">{historical ? t("同一40题；横轴为策略，差值相对原基线，未判定单列。各组调用协议有所不同。", "Same 40 questions; strategies on the x-axis, deltas versus the original baseline. Unscored calls stay separate; call protocols differ.") : sequence ? t("横轴为实验阶段；每条线保留其对应方案。", "The x-axis shows stages; each line retains its compared configurations.") : runAxis ? t("横轴按运行次数等距排列，日期保留在点的详情和明细表中。", "Runs are spaced evenly on the x-axis; timestamps remain in point details and the table.") : t("不同样本规模与配置用不同系列展示。", "Sample sizes and configurations are kept as separate series.")}</p><div className="analytics-observation-scroll"><table>
        <thead><tr><th>{t("运行 / 模型", "Run / model")}</th><th>{t("时间", "Time")}</th><th>{t("样本", "Samples")}</th><th>{words(metric,lang,metric.id)}</th></tr></thead>
        <tbody>{data.map(({point,group,seriesIndex}) => <tr key={`${group.id}:${point.id}`} data-observation={point.id}><td>{point.href ? <a href={point.href}>{words(point, lang, point.label || point.id)}</a> : words(point, lang, point.label || point.id)}<small style={{color:SERIES_COLORS[seriesIndex % SERIES_COLORS.length]}}>{seriesLabel(group,lang)}</small></td><td>{point.at ? dateText(point.at, lang, point.date_precision === "day") : "—"}</td><td>{numeric(point.sample_count,0)} <SelectionInfo row={point.selection_record || point} t={t} /></td><td>{valueText(point.value,metric.unit)}</td></tr>)}</tbody>
      </table></div></details>
    </Card.Content>
  </Card>;
}

function Trends({ live, archive, benchmarks, t, lang }) {
  const [{suite, metric, batch}, setSelection] = useState(() => readTrendLocation(window.location.href));
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    const navigate = () => { setSelection(readTrendLocation(window.location.href)); setExpanded(false); };
    window.addEventListener("popstate", navigate);
    window.addEventListener("hashchange", navigate);
    return () => { window.removeEventListener("popstate", navigate); window.removeEventListener("hashchange", navigate); };
  }, []);
  const changeSelection = (next) => {
    const selection = { suite, metric, batch, ...next };
    const url = trendLocation(window.location.href, selection);
    if (url.href !== window.location.href) window.history.pushState(null, "", url);
    setSelection(selection); setExpanded(false);
    // Reuse the result page's existing fragment navigation and back/forward flow.
    window.dispatchEvent(new PopStateEvent("popstate"));
  };
  const categories = useMemo(() => buildCategoryCharts(live,archive,benchmarks), [live,archive,benchmarks]);
  const selected = suite;
  const batchCategories = selected ? categories.filter((category) => category.suite === selected) : categories;
  const batchIDs = [...new Set(batchCategories.flatMap((category) => category.metrics.flatMap((entry) => entry.views.flatMap((view) => view.series.flatMap((group) => group.points.map((point) => point.batch_id).filter(Boolean))))))].sort();
  const batchOptions = [["", t("全部批次", "All batches")], ...batchIDs.map((id) => [id, PUBLIC_BATCHES[id]?.[lang === "zh" ? "label_zh" : "label_en"] || id])];
  if (batch && !batchIDs.includes(batch)) batchOptions.push([batch, batch]);
  const filtered = categories.filter((entry) => !selected || entry.suite === selected);
  const visible = selected || expanded ? filtered : filtered.slice(0,4);
  const options = [["",t("全部品类","All categories")], ...categories.map((entry)=>[entry.suite,categoryName(entry.suite)])];
  if (selected && !filtered.length) options.push([selected,categoryName(selected)]);
  return <section className="result-trends analytics-section" id="trends" aria-labelledby="trend-heading">
    <div className="analytics-section-heading"><div><h2 id="trend-heading">{t("评测趋势", "Evaluation trends")}</h2><p>{t("每个品类一张图，查看不同批次与配置的结果。", "One chart per category, with results from different batches and configurations.")}</p></div><div className="analytics-controls">
      <Filter label={t("趋势品类", "Trend category")} value={selected} onChange={(value) => changeSelection({suite:value,metric:"",batch:""})} options={options} />
      {batchOptions.length > 1 && <Filter label={t("趋势批次", "Trend batch")} value={batch} onChange={(value) => changeSelection({batch:value})} options={batchOptions} />}
    </div></div>
    {visible.length ? <div className="trend-grid">{visible.map((category) => <TrendCard key={`${category.id}:${selected ? metric : ""}:${batch}`} category={category} lang={lang} t={t}
      batch={batch} pinnedMetric={selected ? metric : ""} onMetricChange={selected ? (value) => changeSelection({suite:selected,metric:value}) : undefined} />)}</div>
      : <div id={selected ? `trend-${selected}` : undefined} className="analytics-empty" role="status">{selected
        ? `${categoryName(selected)} · ${archive ? t("该品类数据暂不可用，更新后将自动显示。", "Data is unavailable; the chart will appear when it updates.") : t("正在读取评测记录…", "Loading evaluation history…")}`
        : live || archive ? t("积累多次结果后自动展示。", "Charts appear as repeated results become available.") : t("正在读取评测记录…", "Loading evaluation history…")}</div>}
    {!selected && categories.length>4 && <div className="analytics-more"><Button variant="ghost" size="sm" onPress={()=>setExpanded(!expanded)}>{expanded?t("收起品类","Show fewer categories"):t(`查看全部 ${categories.length} 个品类`,`Show all ${categories.length} categories`)}</Button></div>}
  </section>;
}

function CellDetail({ detail, scoreKey, t, lang }) {
  if (!detail) return <div className="matrix-detail-hint">{t("点击分数查看样本、置信区间与评测来源。", "Select a score to inspect its sample size, confidence interval and source.")}</div>;
  const { model, column, cell, run } = detail;
  return <div className="matrix-detail" aria-live="polite">
    <div className="matrix-detail-heading"><strong>{model.label || model.id}</strong><span>{words(column, lang, column.role)} · {percent(cell[scoreKey])}</span></div>
    <dl><div><dt>{t("能力分", "Capability score")}</dt><dd>{percent(cell.score)}</dd></div><div><dt>{t("原始通过率", "Raw pass rate")}</dt><dd>{percent(cell.raw_score)}</dd></div><div><dt>{t("样本数", "Samples")} <SelectionInfo row={{ id: run.id + " · " + model.id + " · " + column.id, sample_count: cell.samples, selection_info: cell.selection_info || column.selection_info }} t={t} /></dt><dd>{numeric(cell.samples, 0)}{finite(cell.repeats) ? ` × ${cell.repeats}` : ""}</dd></div><div><dt>{t("原始分 95% 区间", "Raw score 95% interval")}</dt><dd>{Array.isArray(cell.ci95) && cell.ci95.length === 2 && cell.ci95.every(finite) ? `${percent(cell.ci95[0])} – ${percent(cell.ci95[1])}` : "—"}</dd></div><div><dt>{t("模型切换 / 调用异常占比", "Model mismatch / call-error share")}</dt><dd>{percent(cell.contamination_rate)}</dd></div><div><dt>{t("测量时间", "Measured at")}</dt><dd>{dateText(cell.at || run.at, lang)}</dd></div><div><dt>{t("发布状态", "Publication status")}</dt><dd>{cell.publication_state === "eligible" ? t("达到发布条件", "Eligible") : cell.publication_state === "not_eligible" ? t("尚未达到发布条件", "Not eligible") : t("发布状态未核验", "Not verified")}</dd></div></dl>
    <p>{[cell[`reason_${lang}`], (column.evals || []).map((item) => `${item.name}${item.version ? ` · ${item.version}` : ""}`).join(" / ")].filter(Boolean).join(" · ")}</p>
    <small>{t("能力分根据样本量向 0.5 收缩；区间对应原始通过率。", "Capability scores are shrunk toward 0.5 according to sample size; intervals describe the raw pass rate.")}</small>
  </div>;
}

function CapabilityMatrix({ capability, t, lang }) {
  const [runId, setRunId] = useState("");
  const [query, setQuery] = useState("");
  const [scoreKey, setScoreKey] = useState("score");
  const [sortColumn, setSortColumn] = useState("");
  const [selected, setSelected] = useState(null);
  const runs = capability?.runs || [];
  const run = runs.find((entry) => entry.id === runId) || runs.find((entry) => entry.id === capability?.latest_run_id) || runs[0];
  const columns = run?.columns || [];
  const sorting = columns.find((column) => column.id === sortColumn) || columns.find((column) => column.role === "act") || columns[0];
  const models = (run?.models || []).filter((model) => `${model.id} ${model.label || ""}`.toLowerCase().includes(query.toLowerCase())).sort((a, b) => {
    const av = a.cells?.[sorting?.id]?.[scoreKey], bv = b.cells?.[sorting?.id]?.[scoreKey];
    return (finite(bv) ? bv : -Infinity) - (finite(av) ? av : -Infinity) || String(a.label || a.id).localeCompare(String(b.label || b.id));
  });
  const selectedModel = selected?.run === run?.id ? (run?.models || []).find((model) => model.id === selected.model) : null;
  const selectedColumn = columns.find((column) => column.id === selected?.column);
  const selectedCell = selectedModel?.cells?.[selectedColumn?.id];
  const detail = selectedCell ? { model: selectedModel, column: selectedColumn, cell: selectedCell, run } : null;
  return <section className="capability-matrix analytics-section" id="capability-matrix" aria-labelledby="matrix-heading">
    <div className="analytics-section-heading"><div><h2 id="matrix-heading">{t("模型能力矩阵", "Model capability matrix")}</h2><p>{t("按角色与推理档位比较模型，点击分数查看测量详情。", "Compare models by role and reasoning tier. Select a score for measurement details.")}</p></div>{runs.length > 0 && <Filter label={t("矩阵评测批次", "Matrix evaluation run")} value={run?.id || ""} onChange={(value) => { setRunId(value); setSelected(null); }} options={runs.map((entry) => [entry.id, entry.label || entry.id])} />}</div>
    {run && <div className="matrix-toolbar"><Input aria-label={t("筛选矩阵模型", "Filter matrix models")} placeholder={t("搜索模型…", "Search models…")} value={query} onChange={(event) => setQuery(event.target.value)} />
      <Filter label={t("矩阵分数", "Matrix score type")} value={scoreKey} onChange={setScoreKey} options={[["score", t("能力分", "Capability score")], ["raw_score", t("原始通过率", "Raw pass rate")]]} />
      <Filter label={t("矩阵排序依据", "Matrix sort metric")} value={sorting?.id || ""} onChange={setSortColumn} options={columns.map((column) => [column.id, `${t("按", "By")} ${words(column, lang, column.role)}`])} />
    </div>}
    {run ? <><div className="matrix-meta"><span><SelectionInfo row={run} t={t} /> {t(`${models.length} 个模型 · ${columns.length} 个维度`, `${models.length} models · ${columns.length} dimensions`)}</span><span>{dateText(run.at, lang)}{capability.stale ? ` · ${t("显示上次数据", "Last available data")}` : ""}</span></div>
      <Table variant="secondary" className="matrix-table"><Table.ScrollContainer className="matrix-table-scroll"><Table.Content aria-label={t("模型能力分数", "Model capability scores")}>
        <Table.Header><Table.Column id="model" isRowHeader>{t("模型", "Model")}</Table.Column>{columns.map((column) => <Table.Column id={column.id} key={column.id}><span>{words(column, lang, column.role)} <SelectionInfo row={{ id: run.id + " · " + column.id, selection_info: column.selection_info }} t={t} /></span></Table.Column>)}</Table.Header>
        <Table.Body renderEmptyState={() => t("没有匹配的模型", "No matching models")}>{models.map((model) => <Table.Row id={model.id} key={model.id}>
          <Table.Cell><strong title={model.id}>{model.label || model.id}</strong></Table.Cell>
          <Table.Collection items={columns}>{(column) => { const cell = model.cells?.[column.id]; const value = cell?.[scoreKey]; return <Table.Cell key={column.id}>{finite(value) ? <Button className="matrix-score" variant="ghost" size="sm" aria-pressed={selected?.run === run.id && selected?.model === model.id && selected?.column === column.id} aria-label={`${model.label || model.id}, ${words(column, lang, column.role)}, ${percent(value)}; ${t("查看详情", "View details")}`} style={{ "--score-tint": `${Math.round(Math.max(0, Math.min(1, value)) * 21 + 3)}%` }} onPress={() => setSelected({ run: run.id, model: model.id, column: column.id })}>{percent(value)}</Button> : <span className="matrix-missing" aria-label={t("暂无测量", "Not measured")}>—</span>}</Table.Cell>; }}</Table.Collection>
        </Table.Row>)}</Table.Body>
      </Table.Content></Table.ScrollContainer></Table>
      <CellDetail detail={detail} scoreKey={scoreKey} t={t} lang={lang} />
      <p className="analytics-note">{t("每列独立比较；— 表示未测或没有有效数值，不能按 0 分理解。", "Compare within each column. — means unmeasured or unavailable, not a score of zero.")}</p>
    </> : <div className="analytics-empty" role="status">{capability ? t("暂无可展示的能力矩阵数据。", "No capability matrix data is available yet.") : t("正在读取能力矩阵…", "Loading the capability matrix…")}</div>}
  </section>;
}

export function ResultAnalytics({ live, archive, capability, benchmarks, t, lang, showCapability = true }) {
  return <div className="result-analytics"><nav className="analytics-navigation" aria-label={t("评测图表导航", "Evaluation chart navigation")}><a href="#trends">{t("评测趋势", "Trends")}</a>{showCapability && <a href="#capability-matrix">{t("模型能力矩阵", "Capability matrix")}</a>}<a href="#run-records">{t("运行记录", "Run records")}</a></nav>
    <Trends live={live} archive={archive} benchmarks={benchmarks} t={t} lang={lang} />{showCapability && <CapabilityMatrix capability={capability} t={t} lang={lang} />}
  </div>;
}
