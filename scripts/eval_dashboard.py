"""评测与运行指标的收集与可视化页面（docs/plan/eval-observability-and-runtime-fixes.md 第 9 节第 1 期）。只用标准库。

收三类数据，生成一个自包含的 HTML 页面（数据嵌在页面里，图用内联 SVG 画，不连网）：
- 评测报告：services/py-learning/.eval-reports 下的 task-loop--*.json（S-09、S-10）与 context-*.json（S-11），子目录名当作分组
  （例如 exp-2026-10-28/e2-opus）；
- Model Gateway 的计量记录（每行一个 JSON，网关的标准输出）：按模型的调用次数、花费、失败与错误码；
- OpenTelemetry 的 span（OTLP JSON，每行一个 ExportTraceServiceRequest，Collector 的 file 或 kafka 导出器写的）：按模型的 chat 延迟
  （p50、p95）与错误，工具调用，Task 的结局、任务族、花费，Round 的 Verdict 分布。

用法：
  uv run --no-sync python scripts/eval_dashboard.py [--reports DIR] [--metering 名字=文件 …] [--spans 文件 …] [--out 文件] [--title 标题]
不给 --out 时写到 <reports>/dashboard.html（.eval-reports 不提交）。

--public 生成对外开放的评测看板（Eval Dashboard，同一计划第 11.12 节）：只有评测集上的汇总，中英文都有。网关计量与 span 带租户、Task id 与
每个 Task 的花费，给了这两种输入就拒绝；数据里也不带报告的文件名。发布见 docs/plan/cloud-deploy.md。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DEFAULT_REPORTS = REPO / "services" / "py-learning" / ".eval-reports"
# 一次运行里回退成规则（模型调用失败）的决策超过这个比例，这次运行不代表模型（真模型实验第 1.2 条）。
INVALID_FALLBACK_RATIO = 0.05
# 评测 S-10 的被测对象（Planner 与 Reviewer 都是模型）；其余被测对象的 Reviewer 是规则的基线。
REVIEWER_SUBJECT = "worker"
STRUCTURED_FAILURE = "bad_structured_output"
# 打 chat span 的网关（service.name）；Worker 的 model 节点也打一个同名的 span，不重复算。
GATEWAY_SERVICE = "model-gateway"
# 公开版放在 ConfigMap 里（Kubernetes 的上限是 1 MiB），留一点余量给键名与元数据。
MAX_PUBLIC_BYTES = 1_000_000
PUBLIC_TITLE = "Agent Runtime Eval Dashboard · 评测看板"
PUBLIC_ABOUT = (
    "Results on our internal eval sets, not on user data. Model calls went through a third-party relay that may alter requests, so treat "
    "the numbers as indicative; costs are computed from the token counts it reports at list prices and may differ from vendor billing. "
    "Runs where more than 5% of decisions fell back to rules (hollow markers) do not represent the model. "
    "这些是内部评测集上的结果，不含用户数据。模型调用经第三方中转，中转可能改动请求，数字仅供参考；成本按它报回的用量与标价计算，可能与供应商账单不同。"
    "回退成规则超过 5% 的运行（空心点）不代表模型。"
)
# 各节的标题：（中文，公开版另加的英文）。
HEADINGS = {
    "trend": ("门槛指标随运行变化（S-09 Triage、S-10 Reviewer）", "Gate metrics across runs"),
    "scatter": ("模型对比：Reviewer 准确率对每例成本（开发集）", "Models: Reviewer accuracy vs cost per case (dev set)"),
    "runs": ("各次运行", "All runs"),
    "failures": ("失败原因（开发集，按分组合计）", "Failure reasons (dev set, summed across groups)"),
    "context": ("上下文压缩（S-11）：完整阶梯对只遮蔽", "Context compression (S-11): full ladder vs masking only"),
}


def _group(path: Path, root: Path) -> str:
    rel = path.parent.relative_to(root)
    return "history" if str(rel) == "." else rel.as_posix()


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def collect_reports(root: Path) -> dict[str, Any]:
    runs, contexts = [], []
    failures: dict[str, Counter[str]] = defaultdict(Counter)
    for path in sorted(root.rglob("task-loop--*.json")):
        try:
            d = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        m = d.get("metrics") or {}
        planner, reviewer, rounds, cost = m.get("planner") or {}, m.get("reviewer") or {}, m.get("rounds") or {}, m.get("cost") or {}
        tasks = [c for c in d.get("cases") or [] if c.get("kind") == "task"]
        codes = d.get("model_call_failure_codes") or {}
        # 输出不合 schema 的调用多半重问一次就好了（Planner 问两次、Reviewer 两次都不合格才回退）；其余（models_exhausted、超时、不可达）才是回退。
        structured = int(codes.get(STRUCTURED_FAILURE) or 0)
        fallbacks = sum(int(v) for k, v in codes.items() if k != STRUCTURED_FAILURE)
        decisions = sum(int(x.get("decisions") or 0) for x in (planner, reviewer, rounds))
        group = _group(path, root)
        # 只有被测对象是 worker（Planner 加 Reviewer）时 Reviewer 的指标才是模型的；S-09 的被测对象用规则的基线 Reviewer。
        model_reviewer = d.get("subject") == REVIEWER_SUBJECT
        runs.append({
            "group": group, "file": path.name, "subject": d.get("subject"), "split": d.get("split"), "created_at": d.get("created_at"),
            "triage": _num(planner.get("triage_accuracy")), "shape": _num(planner.get("shape_match_rate")),
            "reviewer": _num(reviewer.get("verdict_accuracy")) if model_reviewer else None,
            "rounds": _num(rounds.get("verdict_accuracy")) if model_reviewer else None,
            "stuck": _num(m.get("stuck_retry_errors")), "total_usd": _num(cost.get("total_usd")), "per_case_usd": _num(cost.get("per_case_usd")),
            "task_pass": sum(1 for c in tasks if c.get("passed")), "task_total": len(tasks),
            "fallbacks": fallbacks, "structured_failures": structured, "decisions": decisions,
            "valid": not decisions or fallbacks <= INVALID_FALLBACK_RATIO * decisions,
        })
        if d.get("split") == "dev":
            for r in d.get("failure_reasons") or []:
                failures[group][f"{r.get('dimension')}/{r.get('code')}"] += int(r.get("count") or 0)
    for path in sorted(root.rglob("context-*.json")):
        try:
            d = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        m = d.get("metrics") or {}
        full, mask = m.get("full") or {}, m.get("mask_only") or {}
        contexts.append({
            "group": _group(path, root), "file": path.name, "split": d.get("split"), "gate": d.get("gate_passed"),
            "full_probe": _num(full.get("probe_pass_rate")), "mask_probe": _num(mask.get("probe_pass_rate")),
            "full_retention": _num(full.get("retention_rate")), "repeat_rate": _num(full.get("repeat_rate")),
        })
    runs.sort(key=lambda r: str(r["created_at"] or ""))
    return {"runs": runs, "contexts": contexts, "failures": {g: c.most_common(10) for g, c in sorted(failures.items())}}


def collect_metering(sources: list[tuple[str, Path]]) -> list[dict[str, Any]]:
    by: dict[str, dict[str, Any]] = {}
    for name, path in sources:
        for line in path.read_text("utf-8", errors="replace").splitlines():
            if not line.startswith("{"):
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "model_profile_id" not in r:
                continue
            model = str(r["model_profile_id"]).split("/")[-1]
            e = by.setdefault(model, {"model": model, "calls": 0, "usd": 0.0, "failed": 0, "errors": Counter(), "sources": Counter()})
            e["calls"] += 1
            e["usd"] += int(r.get("cost_usd_micros") or 0) / 1e6
            e["sources"][name] += 1
            if r.get("status") != "ok":
                e["failed"] += 1
                e["errors"][str(r.get("error_code") or "unknown")] += 1
    out = []
    for e in sorted(by.values(), key=lambda x: -x["usd"]):
        out.append({**e, "usd": round(e["usd"], 4), "errors": dict(e["errors"]), "sources": dict(e["sources"])})
    return out


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for kv in span.get("attributes") or []:
        v = kv.get("value") or {}
        out[kv["key"]] = next(iter(v.values()), None) if v else None
    return out


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(q * len(values)))], 1)


def collect_spans(paths: list[Path]) -> dict[str, Any]:
    """chat 只取网关打的 span（Worker 的 model 节点另打一个同一次调用的 chat，会重复），按请求的模型分组：网关同档切换后
    gen_ai.response.model 是最后试的那个，用尽了（models_exhausted）也记在它名下，按它分组会把失败算到备选模型头上。"""
    chat: dict[str, list[float]] = defaultdict(list)
    chat_err: Counter[str] = Counter()
    chat_err_types: dict[str, Counter[str]] = defaultdict(Counter)
    chat_cost: Counter[str] = Counter()
    tools: dict[str, list[float]] = defaultdict(list)
    tool_err: Counter[str] = Counter()
    tasks: dict[str, dict[str, Any]] = {}
    verdicts: Counter[str] = Counter()
    for path in paths:
        for line in path.read_text("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                continue
            for rs in doc.get("resourceSpans") or []:
                service = _attrs(rs.get("resource") or {}).get("service.name")
                for ss in rs.get("scopeSpans") or []:
                    for s in ss.get("spans") or []:
                        name = str(s.get("name") or "")
                        a = _attrs(s)
                        ms = (int(s.get("endTimeUnixNano") or 0) - int(s.get("startTimeUnixNano") or 0)) / 1e6
                        failed = (s.get("status") or {}).get("code") == 2 or bool(a.get("error.type"))
                        if name.startswith("chat"):
                            if service != GATEWAY_SERVICE:
                                continue
                            model = str(a.get("gen_ai.request.model") or name.partition(" ")[2] or "?")
                            chat[model].append(ms)
                            chat_cost[model] += float(a.get("runtime.cost_usd") or 0)
                            chat_err[model] += failed
                            if failed:
                                chat_err_types[model][str(a.get("error.type") or "error")] += 1
                        elif name.startswith("execute_tool"):
                            tool = str(a.get("gen_ai.tool.name") or name.partition(" ")[2] or "?")
                            tools[tool].append(ms)
                            tool_err[tool] += failed
                        elif name == "invoke_workflow" and a.get("task_id"):
                            tasks.setdefault(str(a["task_id"]), {
                                "status": str(a.get("runtime.task_status") or "?"), "family": str(a.get("runtime.task_family") or "(空)"),
                                "usd": float(a.get("runtime.cost_usd") or 0), "rounds": int(a.get("runtime.rounds") or 0), "tenant": str(a.get("tenant_id") or ""),
                            })
                        elif name == "runtime.round":
                            verdicts[str(a.get("verdict") or "?")] += 1
    models = [{"model": m, "calls": len(v), "p50_ms": _pct(v, 0.5), "p95_ms": _pct(v, 0.95), "errors": chat_err[m],
               "error_types": dict(chat_err_types[m]), "usd": round(chat_cost[m], 4)}
              for m, v in sorted(chat.items(), key=lambda kv: -len(kv[1]))]
    tool_rows = [{"tool": t, "calls": len(v), "p50_ms": _pct(v, 0.5), "errors": tool_err[t]} for t, v in sorted(tools.items(), key=lambda kv: -len(kv[1]))]
    by_status = Counter(t["status"] for t in tasks.values())
    by_family = Counter(t["family"] for t in tasks.values())
    return {
        "chat": models, "tools": tool_rows, "verdicts": dict(verdicts.most_common()),
        "tasks": {"count": len(tasks), "by_status": dict(by_status), "by_family": dict(by_family.most_common()),
                  "usd": round(sum(t["usd"] for t in tasks.values()), 4),
                  "rounds_median": statistics.median([t["rounds"] for t in tasks.values()]) if tasks else None},
    }


PAGE = """<!doctype html>
<html lang="__LANG__"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#fbfbfa;--fg:#1f2328;--muted:#656d76;--line:#d0d7de;--card:#fff;--c1:#2563eb;--c2:#d97706;--c3:#059669;--c4:#db2777;--c5:#7c3aed;--c6:#0891b2;--bad:#dc2626;--ok:#16a34a}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;--line:#30363d;--card:#161b22;--c1:#60a5fa;--c2:#fbbf24;--c3:#34d399;--c4:#f472b6;--c5:#a78bfa;--c6:#22d3ee;--bad:#f87171;--ok:#4ade80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:32px 0 8px}
.muted{color:var(--muted)}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(170px,100%),1fr));gap:12px;margin:16px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}.tile b{display:block;font-size:22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;overflow-x:auto}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(360px,100%),1fr));gap:12px}.grid2>div{min-width:0}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:4px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}.bad{color:var(--bad)}.ok{color:var(--ok)}
svg text{fill:var(--fg);font-size:11px}svg .axis{stroke:var(--line)}svg .thr{stroke:var(--bad);stroke-dasharray:4 3}
</style></head><body><main>
<h1>__TITLE__</h1>__ABOUT__<div class="muted" id="meta"></div>
<div class="tiles" id="tiles"></div>
<h2>__H_trend__</h2><div class="card"><svg id="trend" width="100%" height="280"></svg>
<div class="muted">__NOTE_trend__</div></div>
<h2>__H_scatter__</h2><div class="card"><svg id="scatter" width="100%" height="300"></svg></div>
<h2>__H_runs__</h2><div class="card"><table id="runs"></table></div>
<div class="grid2"><div><h2>__H_failures__</h2><div class="card" id="failures"></div></div>
<div><h2>__H_context__</h2><div class="card"><svg id="context" width="100%" height="240"></svg></div></div></div>
__RUNTIME_HTML__</main><script>
const D=__DATA__;const C=['--c1','--c2','--c3','--c4','--c5','--c6'].map(v=>getComputedStyle(document.documentElement).getPropertyValue(v).trim());
const pct=v=>v==null?'—':(v*100).toFixed(1)+'%',usd=v=>v==null?'—':'$'+v.toFixed(v<1?3:2);
const BI=__BILINGUAL__,t=(zh,en)=>BI?en+' · '+zh:zh;
const el=(t,a={},h='')=>{const e=document.createElementNS('http://www.w3.org/2000/svg',t);for(const k in a)e.setAttribute(k,a[k]);if(h)e.textContent=h;return e};
__META_JS__
const groups=[...new Set(D.reports.runs.map(r=>r.group))];const color=g=>C[groups.indexOf(g)%C.length];
const valid=D.reports.runs.filter(r=>r.valid).length;
const tiles=[[t('评测运行','Eval runs'),D.reports.runs.length,BI?`${valid} valid · ${valid} 份有效`:`${valid} 份有效`]__RUNTIME_TILES__];
document.getElementById('tiles').innerHTML=tiles.map(([k,v,s])=>`<div class="tile"><span class="muted">${k}</span><b>${v}</b><span class="muted">${s}</span></div>`).join('');
function frame(svg,h){const w=svg.clientWidth||900;svg.setAttribute('viewBox',`0 0 ${w} ${h}`);return w}
(function trend(){const svg=document.getElementById('trend'),h=280,w=frame(svg,h),runs=D.reports.runs.filter(r=>r.triage!=null||r.reviewer!=null);if(!runs.length)return;
 const x=i=>60+i*(w-90)/Math.max(1,runs.length-1),y=v=>h-30-v*(h-50);svg.append(el('line',{x1:50,x2:w-20,y1:y(0),y2:y(0),class:'axis'}));
 for(const v of [0.25,0.5,0.75,1])svg.append(el('text',{x:10,y:y(v)+4},(v*100)+'%'));
 [[0.9,t('Triage 门槛','Triage gate')],[0.85,t('Reviewer 门槛','Reviewer gate')]].forEach(([v,label])=>{svg.append(el('line',{x1:50,x2:w-20,y1:y(v),y2:y(v),class:'thr'}));svg.append(el('text',{x:w-(BI?230:110),y:y(v)-4},label))});
 [['triage','○ Triage',0],['reviewer','◆ Reviewer',1]].forEach(([k,label,s])=>{runs.forEach((r,i)=>{if(r[k]==null)return;const c=color(r.group),cx=x(i),cy=y(r[k]);
  const m=s?el('rect',{x:cx-4,y:cy-4,width:8,height:8,transform:`rotate(45 ${cx} ${cy})`}):el('circle',{cx,cy,r:4});m.setAttribute(r.valid?'fill':'stroke',c);if(!r.valid)m.setAttribute('fill','none');
  m.append(el('title',{},`${r.group} ${r.split} ${r.created_at}\\n${k} ${pct(r[k])}`));svg.append(m)})});
 svg.append(el('text',{x:60,y:14},t('○ Triage 准确率  ◆ Reviewer 准确率  （颜色 = 分组）','○ Triage accuracy  ◆ Reviewer accuracy  (colour = group)')))})();
(function scatter(){const svg=document.getElementById('scatter'),h=300,w=frame(svg,h),runs=D.reports.runs.filter(r=>r.split==='dev'&&r.reviewer!=null&&r.per_case_usd!=null);if(!runs.length)return;
 const mx=Math.max(...runs.map(r=>r.per_case_usd))*1.15,x=v=>60+v/mx*(w-100),y=v=>h-30-v*(h-50);
 svg.append(el('line',{x1:55,x2:w-20,y1:y(0),y2:y(0),class:'axis'}));svg.append(el('line',{x1:55,x2:55,y1:10,y2:y(0),class:'axis'}));
 svg.append(el('line',{x1:55,x2:w-20,y1:y(0.85),y2:y(0.85),class:'thr'}));svg.append(el('text',{x:w-(BI?290:160),y:h-8},t('每例成本（美元）→','Cost per case (USD) →')));svg.append(el('text',{x:4,y:16},t('Reviewer 准确率','Reviewer accuracy')));
 for(const v of [0.25,0.5,0.75,1])svg.append(el('text',{x:14,y:y(v)+4},(v*100)+'%'));
 runs.forEach(r=>{const cx=x(r.per_case_usd),cy=y(r.reviewer),c=color(r.group),dot=el('circle',{cx,cy,r:6,fill:r.valid?c:'none',stroke:c});dot.append(el('title',{},`${r.group}\\nReviewer ${pct(r.reviewer)} · ${usd(r.per_case_usd)}/${t('例','case')}`));svg.append(dot);
  svg.append(el('text',{x:cx+8,y:cy+4},r.group.split('/').pop()))})})();
document.getElementById('runs').innerHTML='<tr>'+[t('分组','Group'),t('集','Split'),t('时间','Time'),'Triage',t('形状','Shape'),'Reviewer','Round',t('原地重试','Stuck retries'),t('Task 样本','Task samples'),t('花费','Cost'),t('回退','Fallbacks'),t('不合 schema','Schema misses')].map(h=>`<th>${h}</th>`).join('')+'</tr>'+
 D.reports.runs.map(r=>`<tr${r.valid?'':' class="muted"'}><td>${r.group}</td><td>${r.split}</td><td>${(r.created_at||'').slice(0,16)}</td><td class="${r.triage>=0.9?'ok':'bad'}">${pct(r.triage)}</td><td>${pct(r.shape)}</td><td class="${r.reviewer>=0.85?'ok':'bad'}">${pct(r.reviewer)}</td><td>${pct(r.rounds)}</td><td>${r.stuck??'—'}</td><td>${r.task_total?r.task_pass+'/'+r.task_total:'—'}</td><td>${usd(r.total_usd)}</td><td>${r.fallbacks}${r.valid?'':t('（无效）',' (invalid)')}</td><td>${r.structured_failures}</td></tr>`).join('');
(function failures(){const box=document.getElementById('failures'),all={};Object.values(D.reports.failures).forEach(l=>l.forEach(([k,v])=>all[k]=(all[k]||0)+v));
 const top=Object.entries(all).sort((a,b)=>b[1]-a[1]).slice(0,10),mx=Math.max(1,...top.map(t=>t[1]));
 box.innerHTML=top.map(([k,v])=>`<div style="display:flex;gap:8px;align-items:center;margin:3px 0"><span style="flex:0 0 190px;font-size:12px">${k}</span><span style="background:${C[0]};height:12px;width:${v/mx*60}%"></span><span class="muted">${v}</span></div>`).join('')||`<span class="muted">${t('没有数据','No data')}</span>`})();
(function context(){const svg=document.getElementById('context'),h=240,w=frame(svg,h),rows=D.reports.contexts;if(!rows.length)return;const bw=(w-80)/rows.length,y=v=>h-40-v*(h-70);
 svg.append(el('line',{x1:50,x2:w-10,y1:y(0),y2:y(0),class:'axis'}));rows.forEach((r,i)=>{const x0=60+i*bw;[[r.full_probe,C[2]],[r.mask_probe,C[1]]].forEach(([v,c],j)=>{if(v==null)return;
  svg.append(el('rect',{x:x0+j*bw*0.35,y:y(v),width:bw*0.3,height:y(0)-y(v),fill:c}));svg.append(el('text',{x:x0+j*bw*0.35,y:y(v)-3},pct(v)))});
  svg.append(el('text',{x:x0,y:h-22},`${r.group.split('/').pop()}·${r.split==='holdout'?(BI?'holdout':'留出'):(BI?'dev':'开发')}`));svg.append(el('text',{x:x0,y:h-8,class:r.gate?'':'bad'},r.gate?(BI?'pass':'过'):(BI?'fail':'没过')))});
 svg.append(el('text',{x:60,y:14},t('■ 完整阶梯 Probe 通过率   ■ 只遮蔽','■ Full ladder probe pass rate   ■ Masking only')));})();
__RUNTIME_JS__
</script></body></html>
"""


# 运行指标的几节（网关计量、span）：只有内部版有，公开版连同引用它们的脚本一起不出现。
RUNTIME_HTML = """<div class="grid2"><div><h2>网关计量：按模型的花费与失败</h2><div class="card"><table id="metering"></table></div></div>
<div><h2>chat 延迟（OpenTelemetry span）</h2><div class="card"><svg id="latency" width="100%" height="260"></svg></div></div></div>
<div class="grid2"><div><h2>真 Task（invoke_workflow span）</h2><div class="card" id="tasks"></div></div>
<div><h2>工具调用（execute_tool span）</h2><div class="card"><table id="tools"></table></div></div></div>
"""
RUNTIME_TILES = """,['模型（计量）',D.metering.length,''],['模型花费',usd(D.metering.reduce((s,m)=>s+m.usd,0)),'按网关标价'],['真 Task',D.spans.tasks.count,Object.entries(D.spans.tasks.by_status).map(([k,v])=>k+' '+v).join('，')]"""
RUNTIME_JS = """document.getElementById('metering').innerHTML='<tr><th>模型</th><th>来源</th><th>调用</th><th>花费</th><th>失败</th><th>失败率</th></tr>'+D.metering.map(m=>`<tr><td>${m.model}</td><td>${Object.keys(m.sources).length}</td><td>${m.calls}</td><td>${usd(m.usd)}</td><td title="${Object.entries(m.errors).map(([k,v])=>k+' '+v).join('，')}">${m.failed}</td><td class="${m.failed/m.calls>0.05?'bad':''}">${pct(m.failed/m.calls)}</td></tr>`).join('');
(function latency(){const svg=document.getElementById('latency'),h=260,w=frame(svg,h),rows=D.spans.chat;if(!rows.length)return;const mx=Math.max(...rows.map(r=>r.p95_ms||0))*1.1,bh=(h-40)/rows.length;
 rows.forEach((r,i)=>{const y0=20+i*bh,x=v=>170+v/mx*(w-240);svg.append(el('text',{x:4,y:y0+bh*0.6},r.model));
  svg.append(el('rect',{x:170,y:y0+2,width:x(r.p95_ms)-170,height:bh*0.35,fill:C[1]}));svg.append(el('rect',{x:170,y:y0+bh*0.4,width:x(r.p50_ms)-170,height:bh*0.35,fill:C[0]}));
  const t=el('text',{x:x(r.p95_ms)+4,y:y0+bh*0.35},`p95 ${(r.p95_ms/1000).toFixed(1)}s · ${r.calls} 次${r.errors?' · 失败 '+r.errors:''}`);t.append(el('title',{},Object.entries(r.error_types||{}).map(([k,v])=>k+' '+v).join('，')));svg.append(t)});
 svg.append(el('text',{x:170,y:12},'■ p50   ■ p95（按请求的模型；失败含同档切换后用尽的调用）'))})();
(function tasks(){const t=D.spans.tasks,box=document.getElementById('tasks');if(!t.count){box.innerHTML='<span class="muted">没有 span 数据</span>';return}
 box.innerHTML=`<p>${t.count} 个 Task，花费 ${usd(t.usd)}，Round 数中位数 ${t.rounds_median}</p><p>结局：${Object.entries(t.by_status).map(([k,v])=>k+' '+v).join('，')}</p>`+
  `<p>任务族：${Object.entries(t.by_family).map(([k,v])=>`<span class="${k==='(空)'?'bad':''}">${k} ${v}</span>`).join('，')}</p><p>Round 的 Verdict：${Object.entries(D.spans.verdicts).map(([k,v])=>k+' '+v).join('，')}</p>`})();
document.getElementById('tools').innerHTML='<tr><th>工具</th><th>调用</th><th>p50</th><th>失败</th></tr>'+D.spans.tools.map(r=>`<tr><td>${r.tool}</td><td>${r.calls}</td><td>${r.p50_ms==null?'—':r.p50_ms+' ms'}</td><td class="${r.errors?'bad':''}">${r.errors}</td></tr>`).join('');"""
META_INTERNAL = """document.getElementById('meta').textContent=`生成于 ${D.generated_at} · 评测报告 ${D.reports.runs.length} 份 · 计量来源 ${D.metering_sources.join('、')||'无'} · span 文件 ${D.span_sources.join('、')||'无'}`;"""
META_PUBLIC = """document.getElementById('meta').textContent=`Generated ${D.generated_at} · ${D.reports.runs.length} runs · 评测报告 ${D.reports.runs.length} 份`;"""
NOTE_TREND = "虚线是门槛：Triage ≥ 90%，Reviewer ≥ 85%。空心点是回退成规则超过 5% 的运行（不代表模型）。"
NOTE_TREND_EN = "Dashed lines are the gates: Triage ≥ 90%, Reviewer ≥ 85%. Hollow markers are runs where more than 5% of decisions fell back to rules. "


def render(data: dict[str, Any], title: str, *, public: bool = False) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")
    page = PAGE
    for key, (zh, en) in HEADINGS.items():
        page = page.replace(f"__H_{key}__", f"{en} · {zh}" if public else zh)
    # __DATA__ 最后换：数据里的文字不再被当成占位符。
    replacements = {
        "__LANG__": "en" if public else "zh-CN",
        "__ABOUT__": f'<p class="muted" id="about">{PUBLIC_ABOUT}</p>' if public else "",
        "__NOTE_trend__": NOTE_TREND_EN + NOTE_TREND if public else NOTE_TREND,
        "__RUNTIME_HTML__": "" if public else RUNTIME_HTML,
        "__RUNTIME_TILES__": "" if public else RUNTIME_TILES,
        "__RUNTIME_JS__": "" if public else RUNTIME_JS,
        "__META_JS__": META_PUBLIC if public else META_INTERNAL,
        "__BILINGUAL__": "true" if public else "false",
        "__TITLE__": title,
        "__DATA__": payload,
    }
    for key, value in replacements.items():
        page = page.replace(key, value)
    return page


def public_data(reports: dict[str, Any], generated_at: str) -> dict[str, Any]:
    """公开版的数据：只有生成时间与评测的汇总，运行行里去掉报告的文件名。"""

    def without_file(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: v for k, v in row.items() if k != "file"} for row in rows]

    return {
        "generated_at": generated_at,
        "reports": {"runs": without_file(reports["runs"]), "contexts": without_file(reports["contexts"]), "failures": reports["failures"]},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="评测与运行指标的收集与可视化页面")
    parser.add_argument("--reports", type=Path, default=DEFAULT_REPORTS, help="评测报告的目录（递归）")
    parser.add_argument("--metering", action="append", default=[], help="名字=网关计量记录的文件，可以给多个")
    parser.add_argument("--spans", action="append", default=[], type=Path, help="OTLP JSON 的 span 文件，可以给多个")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--title", default=None)
    parser.add_argument("--public", action="store_true", help="生成对外开放的评测看板：只有评测集上的汇总，不收网关计量与 span")
    args = parser.parse_args(argv)
    if args.public and (args.metering or args.spans):
        parser.error("--public 不收 --metering、--spans：它们带租户、Task id 与每个 Task 的花费，不能公开")
    title = args.title or (PUBLIC_TITLE if args.public else "Agent Runtime 评测与运行指标")
    metering = []
    for item in args.metering:
        name, sep, path = item.partition("=")
        if not sep:
            parser.error(f"--metering 要写成 名字=文件：{item}")
        metering.append((name, Path(path)))
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    reports = collect_reports(args.reports) if args.reports.is_dir() else {"runs": [], "contexts": [], "failures": {}}
    out = args.out or (args.reports / "dashboard.html")
    if args.public:
        page = render(public_data(reports, generated_at), title, public=True)
        if len(page.encode("utf-8")) > MAX_PUBLIC_BYTES:
            parser.error(f"公开版超过 {MAX_PUBLIC_BYTES} 字节，放不进 ConfigMap：先减少评测报告")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page, "utf-8", newline="\n")
        sys.stdout.write(f"{out}（公开版，运行 {len(reports['runs'])}）\n")
        return 0
    data = {
        "generated_at": generated_at,
        "reports": reports,
        "metering": collect_metering(metering), "metering_sources": [n for n, _ in metering],
        "spans": collect_spans(list(args.spans)), "span_sources": [p.name for p in args.spans],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data, title), "utf-8")
    sys.stdout.write(f"{out}（运行 {len(data['reports']['runs'])}、模型 {len(data['metering'])}、Task {data['spans']['tasks']['count']}）\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
