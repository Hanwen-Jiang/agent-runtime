import React from "react";
import { EVALUATION_TYPES, evaluationType } from "./evaluation-info.js";
import { mergeEngineeringExperiments } from "./engineering-cards.js";
import { RuntimeSubject } from "./runtime-subject.jsx";
import { runtimeEvidence } from "./runtime-identity.js";
import { SelectionInfo } from "./selection-info.jsx";

function formatDelta(metric, lang) {
  const b = metric.baseline;
  const c = metric.candidate;
  if (typeof b !== "number" || typeof c !== "number" || !Number.isFinite(b) || !Number.isFinite(c) || b === 0) {
    return null;
  }
  const lower = metric.lower_is_better || metric.unit === "seconds" || metric.key === "peak_queries";
  if (["ratio", "fraction", "rate"].includes(metric.unit)) {
    const pp = (c - b) * 100;
    const sign = pp > 0 ? "+" : "";
    return { text: `${sign}${pp.toFixed(1)} pp`, good: lower ? pp < 0 : pp > 0 };
  }
  if (lower) {
    const pctDrop = ((b - c) / Math.abs(b)) * 100;
    if (pctDrop > 0) return { text: `-${pctDrop.toFixed(1)}%`, good: true };
    return { text: `+${Math.abs(pctDrop).toFixed(1)}%`, good: false };
  }
  const ratio = c / b;
  if (ratio >= 1.15) return { text: `${ratio.toFixed(2)}×`, good: true };
  const pctGain = ((c - b) / Math.abs(b)) * 100;
  return { text: `${pctGain >= 0 ? "+" : ""}${pctGain.toFixed(1)}%`, good: pctGain >= 0 };
}

function ComparisonBars({ experiment, metricValue, t, lang }) {
  const metrics = (experiment.metrics || []).filter(
    (m) => typeof m.baseline === "number" && typeof m.candidate === "number" && Number.isFinite(m.baseline) && Number.isFinite(m.candidate),
  );
  if (!metrics.length) return null;
  return (
    <div className="exp-bars" aria-label={t("指标前后对比图", "Metric comparison chart")}>
      {metrics.map((m) => {
        const maxVal = ["ratio", "fraction", "rate"].includes(m.unit)
          ? 1
          : Math.max(Math.abs(m.baseline), Math.abs(m.candidate), 1e-9);
        const baseW = Math.max(3, Math.min(100, (Math.abs(m.baseline) / maxVal) * 100));
        const candW = Math.max(3, Math.min(100, (Math.abs(m.candidate) / maxVal) * 100));
        const delta = formatDelta(m, lang);
        const lower = m.lower_is_better || m.unit === "seconds" || m.key === "peak_queries";
        return (
          <div key={m.key} className="exp-bar-item">
            <div className="exp-bar-head">
              <span className="exp-bar-label">{m[`label_${lang}`] || m.key}</span>
              <span className="exp-bar-values">
                <span className="muted">{metricValue(m.baseline, m.unit)}</span>
                <span className="exp-bar-arrow">→</span>
                <strong>{metricValue(m.candidate, m.unit)}</strong>
                {delta && <em className={`exp-delta ${delta.good ? "good" : "warn"}`}>{delta.text}</em>}
              </span>
            </div>
            <div className="exp-bar-tracks">
              <div className="exp-track">
                <div className="exp-fill base" style={{ width: `${baseW}%` }} title={experiment.baseline_label || t("基线", "Baseline")} />
              </div>
              <div className="exp-track">
                <div className={`exp-fill ${lower ? "fast" : "best"}`} style={{ width: `${candW}%` }} title={experiment.candidate_label || t("候选方案", "Candidate")} />
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function ExperimentsView({ archive, t, lang, compact = false, date, number, pct, dollars, Badge, EvaluationIdentity, includePublished = true }) {
  const rawList = includePublished
    ? mergeEngineeringExperiments(archive?.reports?.experiments || [])
    : archive?.reports?.experiments || [];
  const experiments = [...rawList].sort(
    (a, b) =>
      (Date.parse(b.completed_at || b.updated_at || b.evaluated_on) || 0) -
      (Date.parse(a.completed_at || a.updated_at || a.evaluated_on) || 0),
  );
  if (!experiments.length) return null;

  const metricValue = (value, unit) => {
    if (typeof value !== "number" || !Number.isFinite(value)) return "—";
    if (["ratio", "fraction", "rate"].includes(unit)) return pct(value);
    if (unit === "usd") return dollars(value);
    if (unit === "score") {
      return value.toLocaleString(lang === "zh" ? "zh-CN" : "en-GB", {
        minimumFractionDigits: 4,
        maximumFractionDigits: 4,
      });
    }
    if (unit === "seconds" && value > 0 && value < 0.01) {
      return `${(value * 1000).toFixed(3)} ms`;
    }
    return `${number(value)}${unit === "seconds" ? " s" : ""}`;
  };

  return (
    <section className="experiments" aria-label={t("工程性能与消融对照", "Engineering & ablation experiments")}>
      <div className="section-heading">
        <h2>{t("工程性能与消融对照", "Engineering & ablation comparisons")}</h2>
        {compact && <a href="/results?type=ablation#experiments">{t("查看全部对照", "All comparisons")} →</a>}
      </div>
      <div className="experiment-grid" id={compact ? undefined : "experiments"}>
        {experiments.map((experiment) => {
          const limitations = experiment[`limitations_${lang}`] || [];
          const columns = experiment.kind === "context-cohort-comparison" && Array.isArray(experiment.variants) ? experiment.variants : null;
          const incomplete = experiment.kind === "context-ablation" && experiment.collection_complete !== true;
          const hasPairedBars = !columns && (experiment.metrics || []).some((m) => typeof m.baseline === "number" && typeof m.candidate === "number");
          return (
            <article key={experiment.id} id={`experiment-${experiment.id}`} className="experiment-card">
              <div className="experiment-heading">
                <h3>
                  {experiment[`title_${lang}`] || experiment.id} <SelectionInfo row={experiment} t={t} />
                </h3>
                <Badge>{t(...EVALUATION_TYPES[evaluationType(experiment)])}</Badge>
                {incomplete && <Badge color="warning">{t("实验进行中", "Running")}</Badge>}
              </div>
              <div className="experiment-meta">
                <span>{experiment.evaluated_on || (experiment.completed_at ? date(experiment.completed_at, lang) : "—")}</span>
                <span>
                  {number(experiment.completed_count)} / {number(experiment.sample_count)} {t("样本完成", "completed")}
                </span>
                {!columns && (
                  <span>
                    {experiment.baseline_label || t("基线", "Baseline")} vs {experiment.candidate_label || t("候选方案", "Candidate")}
                  </span>
                )}
              </div>

              {hasPairedBars ? (
                <ComparisonBars experiment={experiment} metricValue={metricValue} t={t} lang={lang} />
              ) : (
                (experiment.metrics || []).length > 0 && (
                  <div className="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th>{t("指标", "Metric")}</th>
                          {columns ? (
                            columns.map((col) => <th key={col.id}>{col[`label_${lang}`] || col.id}</th>)
                          ) : (
                            <>
                              <th>{experiment.baseline_label || t("基线", "Baseline")}</th>
                              <th>{experiment.candidate_label || t("候选方案", "Candidate")}</th>
                            </>
                          )}
                        </tr>
                      </thead>
                      <tbody>
                        {experiment.metrics.map((metric) => (
                          <tr key={metric.key}>
                            <td>{metric[`label_${lang}`] || metric.key}</td>
                            {columns ? (
                              columns.map((col, idx) => <td key={col.id}>{metricValue(metric.values?.[idx], metric.unit)}</td>)
                            ) : (
                              <>
                                <td>{metricValue(metric.baseline, metric.unit)}</td>
                                <td>{metricValue(metric.candidate, metric.unit)}</td>
                              </>
                            )}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )
              )}

              {limitations.length > 0 && (
                <p className="experiment-note">{limitations[0]}</p>
              )}

              {!compact && (
                <details className="audit-drawer">
                  <summary>{t("方法、表格明细与审计记录", "Methodology, exact table & audit details")}</summary>
                  {(experiment.metrics || []).length > 0 && hasPairedBars && (
                    <div className="table-scroll">
                      <table>
                        <thead>
                          <tr>
                            <th>{t("指标", "Metric")}</th>
                            <th>{experiment.baseline_label || t("基线", "Baseline")}</th>
                            <th>{experiment.candidate_label || t("候选方案", "Candidate")}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {experiment.metrics.map((metric) => (
                            <tr key={metric.key}>
                              <td>{metric[`label_${lang}`] || metric.key}</td>
                              <td>{metricValue(metric.baseline, metric.unit)}</td>
                              <td>{metricValue(metric.candidate, metric.unit)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                  <dl>
                    <EvaluationIdentity row={experiment} t={t} />
                    {runtimeEvidence(experiment) && (
                      <>
                        <dt>{t("被测对象", "Subject")}</dt>
                        <dd><RuntimeSubject row={experiment} t={t} /></dd>
                      </>
                    )}
                    {experiment.model_profile && (
                      <>
                        <dt>{t("固定模型", "Pinned model")}</dt>
                        <dd>{experiment.model_profile}</dd>
                      </>
                    )}
                    {experiment.protocol && (
                      <>
                        <dt>{t("评测协议", "Protocol")}</dt>
                        <dd>{experiment.protocol}</dd>
                      </>
                    )}
                    <dt>{t("模型费用", "Model cost")}</dt>
                    <dd>{experiment.cost_known ? dollars(experiment.actual_cost_usd) : t("待结算或未知", "Pending / unknown")}</dd>
                  </dl>
                  {limitations.length > 1 && (
                    <ul className="experiment-limitations">
                      {limitations.slice(1).map((item, idx) => <li key={idx}>{item}</li>)}
                    </ul>
                  )}
                </details>
              )}
            </article>
          );
        })}
      </div>
    </section>
  );
}
