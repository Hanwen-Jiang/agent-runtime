import React from "react";
import { RuntimeSubject, RuntimeRouting } from "./runtime-subject.jsx";
import { SelectionInfo } from "./selection-info.jsx";
import { campaignHref, exactCampaignRunId } from "./swe-campaigns.js";

const armName = (arm, t) => arm.arm === "a" ? t("旧版 A", "Baseline A") : t("候选 B", "Candidate B");
const fraction = (n, d) => Number.isInteger(n) && Number.isInteger(d) ? `${n}/${d}` : "—";
const diagnosticNames = {
  setup_attempts:["环境与路由排查记录","Environment and routing checks"],
  sphinx_workspace:["Sphinx 工作区检查","Sphinx workspace checks"],
  routing_quota:["路由对照 · 额度中断","Routing pilot · quota interrupted"],
  routing_funded:["固定模型与动态路由","Fixed vs. native routing"],
};
const verdictNames = {
  resolved:["通过","Resolved"], unresolved:["未通过","Unresolved"], empty_patch:["空补丁","Empty patch"],
  run_error:["执行错误","Run error"], error:["判分异常","Grader error"], infra_error:["环境错误","Environment error"],
  not_run:["未运行","Not run"], not_graded:["未判分","Ungraded"],
};
const verdict = (v,t) => v ? t(...(verdictNames[v] || [v,v])) : "—";

export function SweCampaign({campaign: c, rows, search="", t, renderRun, compact=false}) {
  const exactRun = exactCampaignRunId(c, search);
  const armRow = arm => ({suite:"swebench", runtime_tree:arm.runtime_tree,
    ...rows?.get(arm.initial_run_id), run_id:arm.initial_run_id});
  const selectionRow = {suite:c.suite, split:c.split, selected:c.selected, run_id:c.campaign_id,
    cohort_sha256:c.selection_info?.cohort_sha256, selection_info:c.selection_info};
  function sourceRun(id, title) {
    if (!id) return null;
    const row = rows.get(id);
    return <div className="swe-campaign-source" key={id}>
      <strong>{title}</strong>
      {row && renderRun ? renderRun(row,id === exactRun) : <code>{id}</code>}
    </div>;
  }
  return <article className="swe-campaign" data-campaign-id={c.campaign_id}>
    <header><div><h2>{compact ? <a href={campaignHref(c)}>{c.title}</a> : c.title}
      {" "}<SelectionInfo row={selectionRow} t={t}/></h2>
      </div>
      {!compact && <a className="small" href={campaignHref(c)}>{t("实验链接","Permalink")}</a>}
    </header>
    <div className="table-scroll"><table><thead><tr>
      <th>{t("Runtime 版本","Runtime version")}</th><th>{t("解决题数","Resolved")}</th>
      <th>{t("解决率","Resolved rate")}</th><th>{t("官方判分覆盖","Official grading coverage")}</th>
    </tr></thead><tbody>{c.arms.map(a=><tr key={a.arm}><th scope="row"><RuntimeSubject row={armRow(a)} t={t}/><small>{armName(a,t)}</small></th>
      <td>{fraction(a.cumulative_resolved,c.selected)}</td>
      <td><strong>{Number.isInteger(a.cumulative_resolved) && c.selected > 0 ? `${Number((100*a.cumulative_resolved/c.selected).toFixed(1))}%` : "—"}</strong></td>
      <td>{fraction(a.graded_count,c.selected)}{Number.isInteger(a.graded_count) && a.graded_count < c.selected && <small> · {t(`待补判 ${c.selected-a.graded_count} 题`,`${c.selected-a.graded_count} awaiting grades`)}</small>}</td>
    </tr>)}</tbody></table></div>
    {!compact && <>
      <details className="swe-campaign-sources" open={exactRun ? true : undefined}>
        <summary>{t("运行记录与独立诊断","Run records and separate diagnostics")}</summary>
        {c.arms.map(a=><section key={a.arm}>
          <h3><RuntimeSubject row={armRow(a)} t={t}/> · {armName(a,t)}</h3><dl><RuntimeRouting row={armRow(a)} t={t}/></dl>
          <p>{t(`首轮解决 ${fraction(a.initial_resolved,c.selected)} · 原补丁补判新增 ${a.regraded_resolved ?? "—"} 题 · 额度补跑新增 ${fraction(a.retry_resolved,a.retry_selected)} · 环境补跑新增 ${fraction(a.infra_resolved,a.infra_selected)}`,`Initial ${fraction(a.initial_resolved,c.selected)} resolved · Patch regrade added ${a.regraded_resolved ?? "—"} · Quota retry added ${fraction(a.retry_resolved,a.retry_selected)} · Environment retry added ${fraction(a.infra_resolved,a.infra_selected)}`)}</p>
          {sourceRun(a.initial_run_id,t("首轮 · 原始判分","First run · original grading"))}
          {a.regrades?.length>0 && <p>{t("原补丁重新判分","Original patch regrades")} · {a.regrades.map(r=>`${r.instance_id} ${verdict(r.grade,t)}`).join(" · ")}</p>}
          {sourceRun(a.recovery_run_id,t("供应商额度恢复后补跑","Retries after quota restoration"))}
          {sourceRun(a.infra_run_id,t("环境恢复后补跑","Retries after environment recovery"))}
        </section>)}
        {c.diagnostics?.length>0 && <section><h3>{t("独立诊断 · 单独判分","Separate diagnostic evaluations")}</h3>
          {c.diagnostics.map(d=><details key={d.kind} open={d.run_ids.some(id=>id === exactRun) ? true : undefined}>
            <summary>{t(...(diagnosticNames[d.kind] || [d.kind,d.kind]))}{Number.isInteger(d.selected) && ` · ${t(`${d.selected} 题`,`${d.selected} cases`)}`}</summary>
            {d.run_ids.map(id=>sourceRun(id, <RuntimeSubject row={{suite:"swebench", ...rows.get(id), run_id:id}} t={t}/>))}
          </details>)}</section>}
      </details>
      <details className="swe-campaign-cases"><summary>{t("查看原题单的逐题判分","Grading for every original case")} ({c.selected})</summary>
        {c.paired && <p>{t(`同题对照 · 两版均通过 ${c.paired.both_resolved} 题，仅 A 通过 ${c.paired.only_a_ids.length} 题，仅 B 通过 ${c.paired.only_b_ids.length} 题，两版均无通过记录 ${c.paired.neither_resolved} 题。`,`Matched cases · Both resolved ${c.paired.both_resolved}, only A ${c.paired.only_a_ids.length}, only B ${c.paired.only_b_ids.length}, neither has a passing result ${c.paired.neither_resolved}.`)}</p>}
        <div className="table-scroll"><table><thead><tr><th>{t("题目","Case")}</th>{["A","B"].map(label=><th key={label}>{label} · {t("判分过程","Grading history")}</th>)}</tr></thead>
          <tbody>{(c.cases || []).map(row=><tr key={row.instance_id}><th scope="row">{row.instance_id}</th>
            {["a","b"].map(key=>{
              const stages=row[key] || {};
              return <td key={key}>{[
                [t("首轮","Initial"),stages.initial], [t("补判","Regrade"),stages.regrade],
                [t("额度补跑","Quota retry"),stages.retry], [t("环境补跑","Environment retry"),stages.infra],
              ].filter(([,g])=>g).map(([label,g])=>`${label} ${verdict(g,t)}`).join(" → ") || "—"}</td>;
            })}</tr>)}</tbody></table></div>
      </details></>}
  </article>;
}
