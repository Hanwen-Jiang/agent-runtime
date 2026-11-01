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

--public 生成对外开放的评测看板（Eval Dashboard，同一计划第 11.12 节）：只有评测集上的汇总，中英文都有，读者不懂这个项目也要看得懂。网关计量与
span 带租户、Task id 与每个 Task 的花费，给了这两种输入就拒绝；数据里也不带报告的文件名。发布见 docs/plan/cloud-deploy.md。
"""

from __future__ import annotations

import argparse
import json
import math
import re
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
# 各节的标题。
HEADINGS = {
    "trend": "门槛指标随运行变化（S-09 Triage、S-10 Reviewer）",
    "scatter": "模型对比：Reviewer 准确率对每例成本（开发集）",
    "runs": "各次运行",
    "failures": "失败原因（开发集，按分组合计）",
    "context": "上下文压缩（S-11）：完整阶梯对只遮蔽",
}


def _group(path: Path, root: Path) -> str:
    rel = path.parent.relative_to(root)
    return "history" if str(rel) == "." else rel.as_posix()


def _date_of(name: str) -> str | None:
    """报告文件名里的日期（上下文评测的报告正文没有时间）。"""
    found = re.search(r"(\d{4})(\d{2})(\d{2})T", name)
    return "-".join(found.groups()) if found else None


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
            "group": _group(path, root), "file": path.name, "split": d.get("split"), "gate": d.get("gate_passed"), "date": _date_of(path.name),
            "full_probe": _num(full.get("probe_pass_rate")), "mask_probe": _num(mask.get("probe_pass_rate")),
            "full_retention": _num(full.get("retention_rate")), "repeat_rate": _num(full.get("repeat_rate")),
        })
    runs.sort(key=lambda r: str(r["created_at"] or ""))
    return {"runs": runs, "contexts": contexts, "failures": {g: c.most_common(10) for g, c in sorted(failures.items())}, "benchmarks": collect_benchmarks(root / "benchmarks")}


def collect_benchmarks(root: Path) -> list[dict[str, Any]]:
    """公开题库的运行报告（<目录>/*.json，benchmarks swebench report 写的 report.json）：只留汇总，不带路径与补丁。没判分的不算。"""
    out = []
    for path in sorted(root.glob("*.json")) if root.is_dir() else []:
        try:
            d = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not d.get("graded") or d.get("resolved") is None or not d.get("selected"):
            continue
        out.append({
            "run_id": d.get("run_id"), "model": d.get("model_name"), "selected": d["selected"], "resolved": d["resolved"], "rate": _num(d.get("resolve_rate")),
            "cost_usd": _num(d.get("cost_usd")), "per_problem_usd": _num(d.get("cost_per_instance_usd")), "complete": bool(d.get("graded_complete")),
            "rounds_median": (d.get("rounds") or {}).get("median"), "environment": (d.get("settings") or {}).get("environment"),
            "by_repo": d.get("by_repo") or {}, "by_difficulty": d.get("by_difficulty") or {},
            "gradings": dict(Counter(str(i.get("grading")) for i in d.get("instances") or [])),
            "problems": sorted(str(i.get("instance_id")) for i in d.get("instances") or []),
            "solved": sorted(str(i.get("instance_id")) for i in d.get("instances") or [] if i.get("grading") == "resolved"),
        })
    return out


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
NOTE_TREND = "虚线是门槛：Triage ≥ 90%，Reviewer ≥ 85%。空心点是回退成规则超过 5% 的运行（不代表模型）。"


def render(data: dict[str, Any], title: str) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")
    page = PAGE
    for key, heading in HEADINGS.items():
        page = page.replace(f"__H_{key}__", heading)
    # __DATA__ 最后换：数据里的文字不再被当成占位符。
    replacements = {
        "__LANG__": "zh-CN",
        "__ABOUT__": "",
        "__NOTE_trend__": NOTE_TREND,
        "__RUNTIME_HTML__": RUNTIME_HTML,
        "__RUNTIME_TILES__": RUNTIME_TILES,
        "__RUNTIME_JS__": RUNTIME_JS,
        "__META_JS__": META_INTERNAL,
        "__BILINGUAL__": "false",
        "__TITLE__": title,
        "__DATA__": payload,
    }
    for key, value in replacements.items():
        page = page.replace(key, value)
    return page


# 对外开放的评测看板（--public）：读者不一定懂这个项目（例如 HR），所以说清在做什么、怎么考、发现了什么，每张图说明怎么看，术语有解释。
# 结论与数字来自报告与评测集；“发现了什么”与“还没做完的”是写好的文字，带日期，有新进展时改这里再发布。
PUBLIC_TITLE = "Agent Runtime Eval Dashboard · 评测看板"
TRIAGE_GATE, REVIEWER_GATE = 0.9, 0.85
EVALSET = REPO / "services" / "py-learning" / "evalsets" / "task-loop"
_E = "exp-2026-10-28/"
# 分组（报告的子目录）给读者看的名字：（中文，英文）。没列出的显示子目录的最后一段。
GROUP_LABELS = {
    "history": ("早期运行", "Early runs"),
    _E + "e1": ("Sonnet 基线", "Sonnet baseline"), _E + "e1b": ("Sonnet 基线", "Sonnet baseline"), _E + "e1c": ("Sonnet 基线", "Sonnet baseline"),
    _E + "e1d": ("Sonnet 基线", "Sonnet baseline"), _E + "e1-holdout": ("Sonnet 基线", "Sonnet baseline"),
    _E + "e2-gpt55": ("GPT-5.5", "GPT-5.5"), _E + "e2-gpt55-rerun": ("GPT-5.5", "GPT-5.5"),
    _E + "e2-geminiflash": ("Gemini 3.5 Flash", "Gemini 3.5 Flash"), _E + "e2-opus": ("Claude Opus", "Claude Opus"),
    _E + "e5-fixed-v1": ("修复之后（Sonnet）", "After the fixes (Sonnet)"), _E + "e5-fixed-v2": ("修复之后（Sonnet）", "After the fixes (Sonnet)"),
}
# 失败原因（维度/代码）的白话：（中文，英文）。
FAILURE_TEXT = {
    "verdict/verdict": ("验收结论判错了", "Reached the wrong review verdict"),
    "shape/modes": ("干活的方式选错了（比如该用工具的步骤没给工具）", "Chose the wrong way of working (for example no tools where tools were needed)"),
    "shape/deliver": ("计划里漏了“交付结果”这一步", "Left the “deliver the result” step out of the plan"),
    "escalation/stuck_retry": ("该换方法或找人时，还在原地重试", "Kept retrying when it should have changed approach or asked someone"),
    "shape/quality": ("质量把关的方式选错了", "Chose the wrong kind of quality check"),
    "shape/nodes": ("计划的步骤数量不对", "Planned the wrong number of steps"),
    "shape/gate": ("危险操作前的审批关卡不对", "Got the approval step before risky actions wrong"),
    "shape/fan_out": ("同时做几件事的安排不对", "Got the amount of parallel work wrong"),
    "triage/triage": ("任务复杂程度判断错了", "Misjudged how complex the task was"),
    "shape/forbidden_roles": ("这一轮安排了不该有的步骤", "Planned a step that does not belong in this round"),
    "shape/human": ("该不该问人判断错了", "Misjudged whether to ask a person"),
}
# 题库题目自带的难度（预估修复时间）。
DIFFICULTY_TEXT = {
    "<15 min fix": ("不到 15 分钟", "Under 15 minutes"),
    "15 min - 1 hour": ("15 分钟到 1 小时", "15 minutes to 1 hour"),
    "1-4 hours": ("1 到 4 小时", "1 to 4 hours"),
    ">4 hours": ("超过 4 小时", "Over 4 hours"),
}
# 公开题库的各次运行，按时间排：（中文，英文）。
BENCHMARK_LABELS = {
    "gpt55-smoke5": ("5 题试跑：项目环境没建成", "5-problem trial: project environment not built"),
    "gpt55-smoke5-official": ("同样 5 题：用上官方环境", "Same 5 problems: official environment"),
    "gpt55-base50": ("50 题基线", "50-problem baseline"),
    "gpt55-smoke5-again": ("同样 5 题再跑一次（检查稳定性）", "Same 5 problems again (stability check)"),
    "gpt55-tune1": ("调优 1：同样 50 题", "Tuning 1: same 50 problems"),
    "gpt55-tune2": ("调优 2：同样 50 题", "Tuning 2: same 50 problems"),
    "gpt55-valid50": ("验证：没用过的 50 题", "Validation: 50 unseen problems"),
}
# 调优的折线：同样的题按题配对比较，起点是基线；验证用没用过的题，单独一个点，不和前面连线（TMP 计划第 11.15 节）。
TUNING_RUNS = ("gpt55-base50", "gpt55-tune1", "gpt55-tune2")
VALIDATION_RUN = "gpt55-valid50"
TUNING_SHORT = {"gpt55-base50": ("基线", "Baseline"), "gpt55-tune1": ("调优 1", "Tuning 1"), "gpt55-tune2": ("调优 2", "Tuning 2"), VALIDATION_RUN: ("验证", "Validation")}
FINDINGS = (
    ("10-22", "分数会抖，考一次不算数", "Scores jump around, so one exam proves nothing",
     "第一次用真模型考，验收判断的分数在 37.5% 到 75% 之间跳。于是改成同一张卷考多次、看中位数，差一两题不下结论。",
     "In the first exams with real models, the review score moved between 37.5% and 75%. So we now sit the same paper several times and read the median, and do not draw conclusions from a difference of one or two questions."),
    ("10-28", "成绩下降的原因不在代码，在“选最便宜的模型”", "The score dropped because of “pick the cheapest model”, not because of the code",
     "验收成绩从 75% 掉到 50%。我们只换模型、其他不变，做了对照实验：之前用的是 Claude Opus，后来上线的模型网关在同一档里自动选了最便宜的 Sonnet。换回 Opus 是 81%，每次运行多花约 30%；Gemini 3.5 Flash 是 62.5%，花费约为 Sonnet 的七分之一；GPT-5.5 是 50%。现在每份报告都记下每个角色用了哪个模型。",
     "The review score fell from 75% to 50%. We ran a controlled comparison, changing only the model. The earlier run used Claude Opus; the model gateway launched later picked the cheapest model in the same class, Sonnet. Opus scored 81% at about 30% more per run, Gemini 3.5 Flash 62.5% at about a seventh of Sonnet's cost, and GPT-5.5 50%. Every report now records which model each role used."),
    ("10-28", "考试和实际运行指向同一个问题", "The exams and real runs pointed at the same problem",
     "考试里最常见的错误是计划里缺少交付步骤和能用工具的步骤。实验任务的运行记录也一样：规划把读文件、跑测试拆成了不能用工具的步骤，11 个任务里有 6 个这样，17% 的花费浪费在这上面。现在这种计划会被校验直接拒绝。",
     "The most common mistake in the exams was a plan with no delivery step and no step that could use tools. The run records of our experimental tasks showed the same thing: reading files and running tests were planned as steps that could not use tools. That hit 6 of 11 tasks and wasted 17% of the spend. Plans like this are now rejected by validation."),
    ("10-28", "修好之后有进步，但还没及格", "The fixes helped, but it has not passed yet",
     "能由结构判断的四种情况（比如同一个错误重试后又出现）写成了代码底线，模型怎么说都改不了。验收成绩从 37.5%～50% 升到 56.3%，“该升级却原地重试”从 3～4 次降到 2 次。离 85% 还很远。",
     "Four situations that can be judged from structure, such as the same error coming back after a retry, became hard rules in code that no model answer can override. The review score rose from 37.5–50% to 56.3%, and retries where it should have escalated fell from 3–4 to 2. That is still far from 85%."),
    ("10-30", "第一次跑公开题库，先查出了别的问题", "The first run on a public benchmark uncovered other problems first",
     "SWE-bench 的 5 题试跑超了预算（12.16 美元对 10 美元），没有得到分数，我们中途停了。查下来有两个原因：系统的花费统计没有接通（已修好）；第三方中转的 Claude 通道会塞进我们没给的工具，这样测的就不是我们的系统了。所以接下来换一个不受影响的模型重跑。",
     "A 5-problem trial of SWE-bench went over budget (12.16 USD against 10 USD) and produced no score, so we stopped it. There were two causes: the system's cost tracking was not connected (now fixed), and the Claude channel of the third-party relay added tools we had not offered, which meant the test was no longer measuring our system. We will rerun it on a model that is not affected."),
    ("10-31", "题目的运行环境一直没建起来", "The problems' environments were never being built",
     "下载镜像的地址没放行；Task 不等构建，构建要 25 分钟，一题 15 分钟就做完了；构建在最后一步失败，而且失败会被永远记住。所以 Agent 一直没在题目自己的环境里工作。现在先在服务器上把镜像建好，每题约 3 分钟。这些产品问题已记录，还没有修。",
     "A download host was blocked. Tasks did not wait for builds, which took 25 minutes while a problem took 15. Builds failed at the last step, and a failure was remembered forever. So the Agent never worked in the problem's own environment. Images are now built on the server first, in about 3 minutes each. The product problems are recorded but not yet fixed."),
    ("10-31", "第一次拿到公开题库的基线：50 题解决 32 题", "First public benchmark baseline: 32 of 50 solved",
     "题目越难差距越明显：预估 15 分钟内的 18 题解决 17 题，15 分钟到 1 小时的 29 题解决 15 题，1 到 4 小时的 3 题一题没解决。判定“做完了”的 46 题里有 15 题其实没解决，说明验收偏松，这和自己考卷上验收判断不及格的结果一致。这是下一步优化的重点。",
     "The gap widens with difficulty: 17 of 18 problems estimated under 15 minutes were solved, 15 of 29 between 15 minutes and an hour, and none of 3 between one and four hours. Of the 46 problems the system declared done, 15 were not actually solved. Review is too lenient, which matches its failing score on our own papers. This is the next thing to improve."),
    ("10-31", "配置没变，成绩也会变", "Results move even when nothing in the system changes",
     "同样的 5 题、同样的配置，隔几小时重跑，从解决 0 题变成解决 2 题，失败的模型调用从 74 次降到 1 次。最可能的原因是第三方中转的状态变了，目前还不能完全证实。所以以后比较优化前后，要在同一时段跑，并且多跑几次。",
     "The same 5 problems with the same setup, rerun a few hours later, went from 0 solved to 2, and failed model calls fell from 74 to 1. The most likely cause is a change in the state of the third-party relay, though that is not proven. From now on, before-and-after comparisons will run in the same period and more than once."),
)
GLOSSARY = (
    ("Agent（智能体）", "Agent", "能自己决定下一步、动手使用工具的 AI 程序，不只是回答问题。", "An AI program that decides its next step and uses tools, rather than only answering questions."),
    ("Task（任务）", "Task", "交给平台的一件工作，比如“修好这个失败的测试”。", "One piece of work handed to the platform, such as “fix this failing test”."),
    ("Round（轮）", "Round", "规划、执行、验收走完一遍叫一轮。一件工作可能要好几轮。", "One pass through planning, doing and reviewing. A piece of work may take several."),
    ("Planner（规划）", "Planner", "每轮开始时决定接下来做什么的部分。", "The part that decides what to do at the start of each round."),
    ("Stage（执行阶段）", "Stage", "一轮里要执行的一批步骤，有的可以同时做。", "The set of steps carried out in a round, some of them in parallel."),
    ("Reviewer（验收）", "Reviewer", "每轮结束时检查结果、决定下一步的部分。", "The part that checks the result at the end of each round and decides what happens next."),
    ("Triage（分诊）", "Triage", "先判断任务有多复杂，决定走快速、轻量还是完整的流程。", "Judging first how complex a task is, to choose a fast, light or full process."),
    ("Verdict（判定）", "Verdict", "Reviewer 给出的结论，比如做完了、继续、小修、重新规划、回滚、问人、放弃。", "The Reviewer's conclusion, such as done, continue, repair, replan, roll back, ask a person or give up."),
    ("开发集（练习卷）", "Dev set", "可以反复用来改进系统的题。", "Questions we may use again and again to improve the system."),
    ("留出集（正式卷）", "Holdout set", "只用来验证、不拿来调的题，防止系统“背答案”。", "Questions used only to verify, never to tune, so the system cannot learn the answers by heart."),
    ("及格线", "Pass line (gate)", "考试之前定好的最低分：规划 90%，验收 85%。", "The minimum score set before the exam: 90% for planning and 85% for review."),
    ("回退", "Fallback", "模型服务出故障时，系统改用简单规则做判断。回退太多的考试不代表 AI 的水平，会作废。", "When the model service fails, the system decides with simple rules instead. An exam with too many fallbacks does not reflect the AI, so it is thrown out."),
    ("原地重试", "Stuck retry", "该换方法或找人的时候，还在重复同一个做法。", "Repeating the same approach when it should change course or ask a person."),
    ("形状", "Shape", "规划出来的步骤结构和标准答案是否一致。", "Whether the structure of the planned steps matches the reference answer."),
    ("上下文", "Context", "模型一次能读到的内容，有上限，所以长任务要压缩旧内容。", "What a model can read at one time. It has a limit, so long tasks must compress older content."),
    ("Token", "Token", "模型读写文字的计量单位，花费按它计算。", "The unit models use to measure text, and what they charge by."),
    ("模型网关", "Model gateway", "统一调用各家模型的服务，负责选模型和记花费。", "The service that calls the various models, chooses among them and records the cost."),
    ("中转", "Relay", "第三方的转发服务。我们经它调用模型，它可能改动请求。", "A third-party forwarding service. We call models through it, and it may alter requests."),
)
ROADMAP = (
    ("验收判断过线", "Getting review past the pass line", "让验收判断在正式卷上达到及格线。", "Bring the review score up to the pass line on the holdout set.", "还没过线，是现在最大的短板。", "Not passed yet. This is the biggest gap today."),
    ("SWE-bench Verified", "SWE-bench Verified", "500 道来自真实开源项目的修 bug 题，业内常用来衡量写代码的能力。", "500 bug-fixing problems from real open-source projects, widely used to measure coding ability.",
     "基线已测：50 题解决 32 题。接下来修问题，再用同样的题和没用过的题各测一次。", "Baseline measured: 32 of 50 solved. Next: fix problems, then measure on the same problems and on unseen ones."),
    ("Terminal-Bench", "Terminal-Bench", "在命令行里完成多步骤的任务。", "Multi-step tasks carried out on the command line.", "还没开始。", "Not started."),
    ("DeepEval", "DeepEval", "开源评测工具，衡量任务是否完成、工具用得对不对等。", "An open-source evaluation tool that measures things like task completion and correct tool use.", "还没开始。", "Not started."),
    ("Ragas", "Ragas", "开源评测工具，专门衡量“先查资料再回答”（RAG）的质量。", "An open-source evaluation tool for the quality of answering from retrieved documents (RAG).",
     "还没开始。要先把资料检索接进主流程。", "Not started. Retrieval must first be connected to the main flow."),
)


def _both(zh: str, en: str) -> str:
    return f'<span class="zh">{zh}</span><span class="en">{en}</span>'


def _para(zh: str, en: str, cls: str = "") -> str:
    extra = f"{cls} " if cls else ""
    return f'<p class="{extra}zh">{zh}</p><p class="{extra}en">{en}</p>'


def _heading(zh: str, en: str) -> str:
    return f"<h2>{_both(zh, en)}</h2>"


def _percent(value: float | None) -> str:
    # 四舍五入到一位，与页面脚本里的 toFixed 显示一致（56.25 都是 56.3）。
    return "—" if value is None else f"{math.floor(value * 1000 + 0.5) / 10:.1f}%"


def evalset_counts(root: Path = EVALSET) -> dict[str, int]:
    """考卷有多大：按题型、按集计数，标注经人确认的有多少。"""
    counts = {"task": 0, "reviewer": 0, "context": 0, "dev": 0, "holdout": 0, "confirmed": 0}
    for sub, key in (("task_cases", "task"), ("reviewer_samples", "reviewer"), ("context_samples", "context")):
        for path in sorted((root / sub).glob("*.json")):
            doc = json.loads(path.read_text("utf-8"))
            counts[key] += 1
            counts[str(doc.get("split"))] = counts.get(str(doc.get("split")), 0) + 1
            counts["confirmed"] += (doc.get("review") or {}).get("status") == "confirmed"
    counts["total"] = counts["task"] + counts["reviewer"] + counts["context"]
    return counts


def public_summary(reports: dict[str, Any]) -> dict[str, Any]:
    """开头的结论：最新一次有效的开发集成绩，加开发集上最好的一次。作废的运行不算。"""
    runs = reports["runs"]
    valid = [r for r in runs if r["valid"]]
    scored = [r for r in valid if r["split"] == "dev" and r["reviewer"] is not None and r["triage"] is not None]
    return {
        "runs": len(runs), "valid": len(valid), "total_usd": sum(r["total_usd"] or 0 for r in runs),
        "latest": scored[-1] if scored else None,
        "best": max(scored, key=lambda r: r["reviewer"], default=None),
    }


def _tile(label_zh: str, label_en: str, value: str, note_zh: str, note_en: str, state: str = "") -> str:
    cls = f' class="{state}"' if state else ""
    return f'<div class="tile"><span class="muted">{_both(label_zh, label_en)}</span><b{cls}>{value}</b><span class="muted">{_both(note_zh, note_en)}</span></div>'


def _gate_tile(label_zh: str, label_en: str, score: float | None, gate: float) -> str:
    passed = score is not None and score >= gate
    state = ("已过线", "Passed") if passed else ("还没过线", "Not passed yet")
    return _tile(label_zh, label_en, _percent(score), f"及格线 {gate * 100:.0f}% · {state[0]}", f"Pass line {gate * 100:.0f}% · {state[1]}", "ok" if passed else "bad")


def wilson(hits: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """比例的 95% 区间（Wilson）：题少时误差很大，要写出来。"""
    if not total:
        return 0.0, 1.0
    share = hits / total
    centre = (share + z * z / (2 * total)) / (1 + z * z / total)
    spread = z * math.sqrt(share * (1 - share) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return max(0.0, centre - spread), min(1.0, centre + spread)


def _main_benchmark(benchmarks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """开头的结论用规模最大的运行里最新的一次；验证跑完就是它：没用过的题最说明问题。"""
    order = list(BENCHMARK_LABELS)
    return max(benchmarks, key=lambda b: (b["selected"], order.index(b["run_id"]) if b["run_id"] in order else -1), default=None)


def tuning_series(benchmarks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """折线图的点。同样的题才按题比较：比基线多解决了几题、丢了几题，只看总数会把这两件事抵消掉。"""
    by_id = {b["run_id"]: b for b in benchmarks}
    base = by_id.get(TUNING_RUNS[0])
    points = []
    for run_id in (*TUNING_RUNS, VALIDATION_RUN):
        run = by_id.get(run_id)
        if run is None:
            continue
        low, high = wilson(run["resolved"], run["selected"])
        zh, en = TUNING_SHORT[run_id]
        point = {
            "run_id": run_id, "short_zh": zh, "short_en": en, "resolved": run["resolved"], "selected": run["selected"], "rate": run["rate"], "low": low, "high": high,
            "per_problem_usd": run["per_problem_usd"], "same_problems": run_id != VALIDATION_RUN,
        }
        if base is not None and run is not base and run_id != VALIDATION_RUN and set(run.get("problems") or []) == set(base.get("problems") or []):
            point["gained"] = len(set(run.get("solved") or []) - set(base.get("solved") or []))
            point["lost"] = len(set(base.get("solved") or []) - set(run.get("solved") or []))
        points.append(point)
    return points


def _summary_section(summary: dict[str, Any], benchmarks: list[dict[str, Any]]) -> str:
    latest, best = summary["latest"], summary["best"]
    triage, reviewer = (latest["triage"], latest["reviewer"]) if latest else (None, None)
    tiles = "".join((
        _tile("考了多少次", "Exams so far", str(summary["runs"]), f"{summary['valid']} 次有效", f"{summary['valid']} valid"),
        _gate_tile("规划判断（Triage）最新成绩", "Planning (Triage), latest", triage, TRIAGE_GATE),
        _gate_tile("验收判断（Reviewer）最新成绩", "Review (Reviewer), latest", reviewer, REVIEWER_GATE),
        _tile("累计花费", "Total spend", f"${summary['total_usd']:.2f}", "按模型标价估算", "Estimated at list prices"),
        *([_tile("公开题库 SWE-bench", "Public benchmark SWE-bench", f"{main['resolved']}/{main['selected']}", f"解决率 {_percent(main['rate'])}，见下面的说明", f"{_percent(main['rate'])} solved, see the notes below")] if (main := _main_benchmark(benchmarks)) else []),
    ))
    zh, en = [], []
    if latest:
        zh.append("规划这一步已经过线。" if triage >= TRIAGE_GATE else "规划这一步还没过线。")
        en.append("Planning has reached its pass line." if triage >= TRIAGE_GATE else "Planning has not reached its pass line.")
        if reviewer >= REVIEWER_GATE:
            zh.append("验收这一步也过线了。")
            en.append("Review has reached its pass line too.")
        else:
            gap = (REVIEWER_GATE - reviewer) * 100
            zh.append(f"验收这一步还差约 {gap:.0f} 个百分点，是现在最大的短板。")
            en.append(f"Review is about {gap:.0f} points short, which is the biggest gap today.")
            if best is not None and best["reviewer"] > reviewer:
                zh.append(f"练习卷上最好的一次是“{best['label_zh']}”的 {_percent(best['reviewer'])}，也还没到及格线。" if best["reviewer"] < REVIEWER_GATE else f"练习卷上最好的一次是“{best['label_zh']}”的 {_percent(best['reviewer'])}。")
                en.append(f"The best result on the dev set is {_percent(best['reviewer'])} with {best['label_en']}" + (", which is still below the line." if best["reviewer"] < REVIEWER_GATE else "."))
    return f'<section id="summary">{_heading("一分钟看懂", "The one-minute version")}<div class="tiles">{tiles}</div>{_para("".join(zh), " ".join(en), "conclusion") if latest else ""}</section>'


def _what_section() -> str:
    steps = (
        ("1", "Planner 规划", "Planner: plan", "像项目经理。先判断这件事有多复杂，再决定这一轮做哪几步。", "Like a project manager. It judges how complex the work is, then decides which steps to take this round."),
        ("2", "Stage 执行", "Stage: do", "像执行小组。按计划读文件、写代码、跑测试，有的步骤可以同时做。", "Like the delivery team. It reads files, writes code and runs tests as planned, some of it in parallel."),
        ("3", "Reviewer 验收", "Reviewer: check", "像质检员。只看做出来的结果和验收标准，不看规划时的想法，避免自己给自己打高分。然后判定：做完了、继续、返工，还是找人。", "Like a quality inspector. It looks only at the result and the acceptance criteria, not at the planning, so the system cannot mark its own homework. Then it decides: done, continue, redo or ask a person."),
    )
    cards = "".join(f'<div class="step"><span class="num">{n}</span><b>{_both(zh, en)}</b>{_para(dz, de)}</div>' for n, zh, en, dz, de in steps)
    return (
        f'<section id="what">{_heading("我们在做什么", "What we are building")}'
        + _para("每件工作都按同样的三步循环。三步走完算一轮，没做完就进入下一轮。", "Every piece of work follows the same three-step loop. Three steps make a round, and if the work is not finished another round begins.")
        + f'<div class="steps">{cards}</div>'
        + _para("为什么要分开考：任何一步出错，都可能变成“AI 说做完了，其实没做完”。所以规划和验收各有自己的考题和及格线，长任务还要单独考会不会忘事。",
                "Why each part has its own exam: a mistake at any step can end with the AI saying it finished when it did not. So planning and review each have their own questions and pass line, and long tasks are tested separately for forgetting.")
        + "</section>"
    )


def _how_section(counts: dict[str, int], summary: dict[str, Any]) -> str:
    confirmed = ("标准答案都由人确认过", "and people have confirmed all the reference answers") if counts["confirmed"] == counts["total"] else (f"其中 {counts['confirmed']} 道的标准答案已由人确认", f"and people have confirmed the reference answers for {counts['confirmed']} of them")
    voided = summary["runs"] - summary["valid"]
    items = (
        ("考卷", "The paper", f"一共 {counts['total']} 道题：{counts['task']} 个完整任务、{counts['reviewer']} 个验收判断、{counts['context']} 个长任务，{confirmed[0]}。题目是我们自己写的，不含用户数据。",
         f"{counts['total']} questions in all: {counts['task']} full tasks, {counts['reviewer']} review decisions and {counts['context']} long tasks, {confirmed[1]}. We wrote the questions ourselves and they contain no user data."),
        ("练习卷和正式卷", "Practice and final papers", f"{counts['dev']} 道是练习卷（开发集），可以反复用来改进；{counts['holdout']} 道是正式卷（留出集），只用来验证，不拿来调，防止系统“背答案”。",
         f"{counts['dev']} questions are the practice paper (dev set), which we may use repeatedly to improve. {counts['holdout']} are the final paper (holdout set), used only to verify, so the system cannot learn the answers by heart."),
        ("及格线先定好", "Pass lines set in advance", "规划判断至少 90%，验收判断至少 85%，写进评测程序，自动判定过没过。", "At least 90% for planning and 85% for review. They are written into the evaluation program, which decides pass or fail automatically."),
        ("同一张卷考多次", "The same paper, several times", "AI 有随机性，同一张卷每次分数都不同。所以看多次的中位数，差一两题不下结论。", "AI has randomness, so the same paper gives a different score each time. We read the median of several runs and do not draw conclusions from one or two questions."),
        ("不合格的考试作废", "Invalid exams are thrown out", f"模型服务出故障时，系统会退回简单规则。超过 5% 的判断退回规则，这次考试就不代表 AI 的水平，作废重考。目前作废了 {voided} 次。",
         f"When the model service fails, the system falls back to simple rules. If more than 5% of decisions fall back, the exam does not reflect the AI, so it is thrown out and repeated. {voided} have been thrown out so far."),
    )
    cards = "".join(f'<div class="fact"><b>{_both(zh, en)}</b>{_para(dz, de)}</div>' for zh, en, dz, de in items)
    return f'<section id="how">{_heading("我们怎么考", "How we test it")}<div class="facts">{cards}</div></section>'


def _findings_section() -> str:
    rows = "".join(
        f'<li><span class="date">{date}</span><div><b>{_both(tz, te)}</b>{_para(bz, be)}</div></li>'
        for date, tz, te, bz, be in FINDINGS
    )
    return f'<section id="findings">{_heading("考出了什么", "What the exams found")}{_para("考试的价值在于找出问题。下面是到目前为止最重要的几个发现。", "The point of an exam is to find problems. These are the most important findings so far.")}<ol class="timeline">{rows}</ol></section>'


def _glossary_section() -> str:
    rows = "".join(f'<div class="term"><dt>{_both(zh, en)}</dt><dd>{_both(dz, de)}</dd></div>' for zh, en, dz, de in GLOSSARY)
    return f'<section id="glossary">{_heading("名词解释", "Glossary")}<dl>{rows}</dl></section>'


def _next_section() -> str:
    rows = "".join(f"<tr><td>{_both(zh, en)}</td><td>{_both(wz, we)}</td><td>{_both(sz, se)}</td></tr>" for zh, en, wz, we, sz, se in ROADMAP)
    head = f'<tr class="head"><th>{_both("项目", "Item")}</th><th>{_both("它是什么", "What it is")}</th><th>{_both("现在的状态", "Status")}</th></tr>'
    return (
        f'<section id="next">{_heading("还没做完的", "What is not done yet")}'
        + _para("状态截至 2026-10-31。每一项都会先测基线，优化之后再测，把变化画成曲线放到这个页面。", "Status as of 2026-10-31. Each item will get a baseline first, then be measured again after tuning, with the change charted on this page.")
        + f'<div class="card scroll"><table class="plain">{head}{rows}</table></div></section>'
    )


def _about_section() -> str:
    return (
        f'<section id="about">{_heading("这些数字能信到什么程度", "How far to trust these numbers")}'
        + _para(
            "这些是内部评测集上的结果，不含用户数据，只说明系统在这些题上的表现，不能和公开榜单直接比。模型调用经第三方中转，中转可能改动请求，所以数字仅供参考；"
            "成本按它报回的用量与标价估算，可能与供应商账单不同。回退成规则超过 5% 的运行已标为作废，不计入结论。",
            "Results on our internal eval sets, not on user data. They describe how the system does on these questions and cannot be compared directly with public leaderboards. "
            "Model calls went through a third-party relay that may alter requests, so treat the numbers as indicative. Costs are estimated from the token counts it reports at list prices and may differ from vendor billing. "
            "Runs where more than 5% of decisions fell back to rules are marked invalid and left out of the conclusions.",
        )
        + "</section>"
    )


def _tuning_block(benchmarks: list[dict[str, Any]]) -> str:
    """调优前后的对比；只有基线时还没有什么可比的，不画。"""
    points = tuning_series(benchmarks)
    if len(points) < 2:
        return ""
    rows = []
    for p in points:
        if "gained" in p:
            change = f'<span class="zh">新解决 {p["gained"]} 题，丢了 {p["lost"]} 题</span><span class="en">{p["gained"]} newly solved, {p["lost"]} lost</span>'
        elif p["same_problems"]:
            change = _both("起点", "Starting point")
        else:
            change = _both("换了一批题，不能按题比", "Different problems, no per-problem comparison")
        usd = f'${p["per_problem_usd"]:.2f}' if p["per_problem_usd"] is not None else "—"
        rows.append(f'<tr><td>{_both(p["short_zh"], p["short_en"])}</td><td>{p["resolved"]}/{p["selected"]}</td><td>{usd}</td><td>{change}</td></tr>')
    head = f'<tr class="head"><th>{_both("哪一次", "Run")}</th><th>{_both("解决", "Solved")}</th><th>{_both("平均每题", "Per problem")}</th><th>{_both("和基线比", "Against the baseline")}</th></tr>'
    return (
        f'<div class="card"><b>{_both("调优前后：解决了多少题", "Before and after tuning: problems solved")}</b>'
        '<svg id="tuning" width="100%" height="300" role="img" aria-label="Share of problems solved in each tuning run"></svg></div>'
        f'<div class="card" style="margin-top:12px"><b>{_both("平均每题花多少钱", "Average cost per problem")}</b>'
        '<svg id="tuning-cost" width="100%" height="190" role="img" aria-label="Average cost per problem in each tuning run"></svg></div>'
        f'<div class="card scroll" style="margin-top:12px"><table class="plain">{head}{"".join(rows)}</table></div>'
        + _para(
            "怎么看：蓝线是同样的题，调优一次跑一遍。竖线是 95% 区间：只有 50 题，差两三题可能只是运气，所以表里还按题比了新解决和丢掉的题。橙色方块是没用过的题，我们没有看着这些题调，所以它最能说明调优有没有用；它和蓝线不是同一批题，不连线。",
            "How to read this: the blue line is the same problems, run again after each round of tuning. The vertical lines are 95% intervals: with only 50 problems, a difference of two or three may be luck, so the table also compares problem by problem. The orange square is problems we never looked at while tuning, so it says most about whether tuning worked. They are different problems from the blue line, so the two are not joined.",
            "muted",
        )
        + '<div style="height:12px"></div>'
    )


def _bars(rows: list[tuple[str, str, int, int]]) -> str:
    out = []
    for zh, en, hit, total in rows:
        width = hit / total * 100 if total else 0
        out.append(f'<div class="bar"><span>{_both(zh, en)}</span><span class="track"><i style="width:{width:.1f}%"></i></span><span class="note">{hit}/{total}</span></div>')
    return "".join(out)


def _benchmark_section(benchmarks: list[dict[str, Any]]) -> str:
    main = _main_benchmark(benchmarks)
    if main is None:
        return ""
    low, high = wilson(main["resolved"], main["selected"])
    order = {name: n for n, name in enumerate(DIFFICULTY_TEXT)}
    difficulty = sorted(main["by_difficulty"].items(), key=lambda kv: order.get(kv[0], len(order)))
    repos = sorted(main["by_repo"].items(), key=lambda kv: (-kv[1]["selected"], kv[0]))
    tiles = "".join((
        _tile("解决了多少题", "Problems solved", f"{main['resolved']}/{main['selected']}", f"解决率 {_percent(main['rate'])}", f"{_percent(main['rate'])} solved"),
        _tile("这个分数的误差", "Margin of error", f"{low * 100:.0f}%–{high * 100:.0f}%", "95% 区间：只有 50 题，误差不小" if main["selected"] == 50 else "95% 区间", "95% interval: few problems, wide margin"),
        _tile("平均每题花费", "Average cost per problem", f"${main['per_problem_usd']:.2f}" if main["per_problem_usd"] is not None else "—", f"这次共 ${main['cost_usd']:.2f}" if main["cost_usd"] is not None else "", f"${main['cost_usd']:.2f} in total" if main["cost_usd"] is not None else ""),
    ))
    table = "".join(
        f"<tr><td>{_both(*BENCHMARK_LABELS.get(b['run_id'], (b['run_id'], b['run_id'])))}</td><td>{b['resolved']}/{b['selected']}</td><td>{'$' + format(b['cost_usd'], '.2f') if b['cost_usd'] is not None else '—'}</td></tr>"
        for b in sorted(benchmarks, key=lambda b: list(BENCHMARK_LABELS).index(b["run_id"]) if b["run_id"] in BENCHMARK_LABELS else len(BENCHMARK_LABELS))
    )
    head = f'<tr class="head"><th>{_both("哪一次", "Run")}</th><th>{_both("解决", "Solved")}</th><th>{_both("花费", "Cost")}</th></tr>'
    source = BENCHMARK_LABELS.get(main["run_id"], (main["run_id"], main["run_id"]))
    body = (
        _tuning_block(benchmarks)
        + _para(f"下面的数字来自“{source[0]}”。", f"The numbers below come from “{source[1]}”.", "muted")
        + f'<div class="tiles">{tiles}</div>'
        f'<div class="card"><b>{_both("按难度（题目自带的预估修复时间）", "By difficulty (the estimated fix time that comes with each problem)")}</b>'
        + _bars([(DIFFICULTY_TEXT.get(name, (name, name))[0], DIFFICULTY_TEXT.get(name, (name, name))[1], e["resolved"], e["selected"]) for name, e in difficulty])
        + f'</div><div class="card" style="margin-top:12px"><b>{_both("按开源项目", "By open-source project")}</b>'
        + _bars([(repo, repo, e["resolved"], e["selected"]) for repo, e in repos])
        + f'</div><div class="card scroll" style="margin-top:12px"><b>{_both("每一次运行", "Every run")}</b><table class="plain">{head}{table}</table></div>'
        + _para(
            "这个分数能说明什么：判分用的是 SWE-bench 官方工具，Agent 事先看不到判分用的测试。不能说明什么：这不是榜单成绩，不能和榜单直接比。题库一共 500 题，这里只抽了 50 题，也没有选判分要连外网、结果不稳定的 psf/requests；模型调用经第三方中转，它的状态会影响结果；这一轮中途把并发从 2 提到了 6。",
            "What this score shows: it was graded by the official SWE-bench tool, and the Agent cannot see the grading tests in advance. What it does not show: this is not a leaderboard result and cannot be compared directly with one. The benchmark has 500 problems and this is a sample of 50, leaving out psf/requests because its grading needs the internet and is unreliable. Model calls went through a third-party relay whose state affects results, and concurrency was raised from 2 to 6 during this run.",
        )
    )
    return _chart_section(
        "swebench", "公开题库：SWE-bench", "Public benchmark: SWE-bench",
        "前面的考卷是我们自己写的。SWE-bench 是业内公开的题库：题目都是真实开源项目里的 bug，Agent 要自己读代码、改代码，再由官方工具跑项目的测试判断修好了没有。全部测试都通过才算解决，修对一部分不算。",
        "The papers above are our own. SWE-bench is a public benchmark: every problem is a real bug from an open-source project. The Agent has to read and change the code itself, then the official tool runs the project's tests to decide whether it is fixed. A problem only counts as solved if all the tests pass.",
        body,
    )


def _chart_section(section_id: str, zh: str, en: str, how_zh: str, how_en: str, body: str) -> str:
    return f'<section id="sec-{section_id}">{_heading(zh, en)}<p class="how zh">{how_zh}</p><p class="how en">{how_en}</p>{body}</section>'


def render_public(data: dict[str, Any], title: str, summary: dict[str, Any], counts: dict[str, int]) -> str:
    charts = "".join((
        _chart_section(
            "trend", "成绩怎么变化", "How the scores changed",
            "每个点是一次考试，从左到右按时间排。上面是规划判断，下面是验收判断。红色虚线是及格线，点在线上方算过。蓝色圆点是练习卷，橙色方块是正式卷，空心的已作废。",
            "Each mark is one exam, in time order from left to right. Planning is on top and review below. The dashed red line is the pass line, and a mark above it passes. Blue circles are the practice paper, orange squares the final paper, and hollow marks are invalid.",
            '<div class="card"><svg id="trend" width="100%" height="430" role="img" aria-label="Scores over time"></svg></div>',
        ),
        _chart_section(
            "scatter", "换模型有多大差别", "How much the model matters",
            "同一张练习卷，只换验收和规划用的模型。条越长越准，竖线是 85% 的及格线，右边是平均每道题的花费。每种配置只考了一到三次，差一两题不算数。",
            "The same practice paper, changing only the model used for planning and review. A longer bar is more accurate, the vertical line is the 85% pass line, and the right side shows the average cost per question. Each setup ran only one to three times, so a difference of one or two questions means nothing.",
            '<div class="card" id="scatter"></div>',
        ),
        _chart_section(
            "failures", "错在哪里", "Where it goes wrong",
            "把练习卷上所有答错的地方按原因汇总，条越长，这类错误越多。每次考试都计入，所以同一道题可能被算多次。",
            "All the mistakes on the practice paper, grouped by cause. A longer bar means more of that mistake. Every exam counts, so one question can be counted several times.",
            '<div class="card" id="failures"></div>',
        ),
        _chart_section(
            "context", "长任务会不会忘事", "Does it forget on long tasks?",
            "任务很长时，AI 能读的内容有上限，旧内容必须压缩。压缩之后再问它关键问题，看还答不答得对。绿色是我们的完整压缩方法，蓝色是对照：只把旧内容遮掉。绿色高于蓝色，说明这套方法确实留住了关键信息。",
            "On long tasks the AI can only read so much, so older content must be compressed. We then ask it key questions to see whether it still answers correctly. Green is our full compression method and blue is the comparison, which simply hides older content. Green above blue means the method really keeps the key information.",
            '<div class="card"><svg id="context" width="100%" height="280" role="img" aria-label="Compression results"></svg></div>',
        ),
        _chart_section(
            "runs", "每一次考试的详细数字", "Every exam in detail",
            "给想看细节的人。“规划”和“验收”是准确率；“形状”看计划的结构对不对；“完整任务”是从头到尾都判对的样本数；“重试”是该升级却原地重试的次数；灰色的行已作废。",
            "For readers who want the detail. Planning and Review are accuracy. Shape is whether the plan's structure was right. Full tasks counts samples judged right from start to finish. Retries counts times it retried when it should have escalated. Grey rows are invalid.",
            f'<details class="card scroll"><summary>{_both("展开表格", "Show the table")}</summary><table id="runs"></table></details>',
        ),
    ))
    payload = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")
    failure_text = json.dumps(FAILURE_TEXT, ensure_ascii=False).replace("</", "<\\/")
    benchmarks = data["reports"].get("benchmarks") or []
    body = "".join((
        _para(
            "Agent Runtime 是我们正在开发的平台：把一件复杂的工作交给它，它让 AI 自己拆解步骤、动手去做、检查结果，不对就返工，直到做完。AI 每次的表现都不一样，不能凭感觉说好不好，所以我们给它出题考试。这个页面公开每一次考试的成绩，也写明还没考过的地方。",
            "Agent Runtime is a platform we are building. You hand it a complex piece of work, and it lets AI break the work into steps, carry them out, check the result and redo what is wrong until the work is finished. AI behaves differently every time, so we do not judge it by feel: we give it exams. This page shows the result of every exam, including what it has not passed yet.",
            "lead",
        ),
        _summary_section(summary, benchmarks), _what_section(), _how_section(counts, summary), _findings_section(), charts, _benchmark_section(benchmarks), _next_section(), _glossary_section(), _about_section(),
    ))
    page = PUBLIC_PAGE
    for key, value in (("__TITLE__", title), ("__BODY__", body), ("__GENERATED__", str(data["generated_at"])), ("__FAILURE_TEXT__", failure_text), ("__DATA__", payload)):
        page = page.replace(key, value)
    return page


PUBLIC_PAGE = r"""<!doctype html>
<html lang="zh-CN" data-lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#fbfbfa;--fg:#1f2328;--muted:#5b636c;--line:#d8dde3;--card:#fff;--soft:#f1f3f2;--c1:#2563eb;--c2:#d97706;--c3:#059669;--bad:#dc2626;--ok:#15803d}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9aa4ae;--line:#30363d;--card:#161b22;--soft:#1c222a;--c1:#60a5fa;--c2:#f59e0b;--c3:#34d399;--bad:#f87171;--ok:#4ade80}}
*{box-sizing:border-box}html[data-lang="zh"] .en,html[data-lang="en"] .zh{display:none}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.7 system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px 72px}h1{font-size:26px;line-height:1.3;margin:0 0 12px;padding-right:128px}
h2{font-size:20px;line-height:1.4;margin:44px 0 12px}p{margin:0 0 12px}.lead{font-size:17px}.muted{color:var(--muted)}.conclusion{font-weight:600}.how{color:var(--muted)}
.lang{float:right;display:flex;gap:4px;margin-top:2px}.lang button{font:inherit;font-size:13px;padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);cursor:pointer}
.lang button[aria-pressed="true"]{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.tiles,.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(210px,100%),1fr));gap:12px;margin:0 0 16px}
.tile,.fact,.step,.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px}.tile{font-size:14px;line-height:1.5}.tile b{display:block;font-size:28px;line-height:1.3;margin:4px 0}
.fact p,.step p{margin:6px 0 0;font-size:15px}.steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(250px,100%),1fr));gap:12px;margin:0 0 14px}
.num{display:inline-block;width:24px;height:24px;line-height:24px;text-align:center;border-radius:50%;background:var(--c1);color:var(--bg);font-size:13px;font-weight:600;margin-right:8px}
.ok{color:var(--ok)}.bad{color:var(--bad)}.scroll{overflow-x:auto}summary{cursor:pointer;font-weight:600}
.timeline{list-style:none;margin:0;padding:0}.timeline li{display:grid;grid-template-columns:56px 1fr;gap:12px;padding:12px 0;border-top:1px solid var(--line)}.date{color:var(--muted);font-variant-numeric:tabular-nums}.timeline p{margin:4px 0 0;font-size:15px}
dl{margin:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(min(320px,100%),1fr));gap:0 24px}.term{padding:10px 0;border-top:1px solid var(--line)}dt{font-weight:600}dd{margin:2px 0 0;font-size:15px;color:var(--muted)}
table{border-collapse:collapse;width:100%;font-size:14px;margin-top:8px}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
table.plain th,table.plain td{text-align:left;white-space:normal;vertical-align:top}table.plain td:first-child{white-space:nowrap;font-weight:600}tr.void td{color:var(--muted)}
.bar{display:grid;grid-template-columns:minmax(0,230px) minmax(120px,1fr) minmax(0,150px);gap:12px;align-items:center;padding:6px 0;font-size:14px;line-height:1.4}.bar .note{color:var(--muted);text-align:right}
.track{position:relative;height:16px;background:var(--soft);border-radius:3px}.track i{position:absolute;left:0;top:0;bottom:0;border-radius:3px;background:var(--c1)}.track u{position:absolute;top:-4px;bottom:-4px;width:2px;background:var(--bad)}
.bars2 .bar{grid-template-columns:minmax(0,1fr) minmax(90px,30%) 40px}
#sec-swebench .bar{grid-template-columns:minmax(0,230px) minmax(120px,1fr) 50px}
@media (max-width:640px){h1{padding-right:0}.lang{float:none;margin-bottom:12px}.bar,.bars2 .bar,#sec-swebench .bar{grid-template-columns:1fr auto}.bar .track{grid-column:1/-1;grid-row:2}.timeline li{grid-template-columns:1fr;gap:0}}
svg text{fill:var(--fg);font-size:12px}svg .axis{stroke:var(--line)}svg .thr{stroke:var(--bad);stroke-dasharray:5 4}footer{margin-top:40px;font-size:13px;color:var(--muted)}
</style></head><body><main>
<div class="lang"><button type="button" data-set-lang="zh">中文</button><button type="button" data-set-lang="en">English</button></div>
<h1>Agent Runtime Eval Dashboard · 评测看板</h1>
__BODY__
<footer><span class="zh">生成于</span><span class="en">Generated</span> __GENERATED__</footer>
</main><script>
const D=__DATA__;const C=['--c1','--c2','--c3','--bad','--line'].map(v=>getComputedStyle(document.documentElement).getPropertyValue(v).trim());
const F=__FAILURE_TEXT__;
const pct=v=>v==null?'—':(v*100).toFixed(1)+'%',usd=v=>v==null?'—':'$'+v.toFixed(v<1?3:2);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const both=(zh,en)=>`<span class="zh">${esc(zh)}</span><span class="en">${esc(en)}</span>`;
const el=(t,a={},h='')=>{const e=document.createElementNS('http://www.w3.org/2000/svg',t);for(const k in a)e.setAttribute(k,a[k]);if(h)e.textContent=h;return e};
const label=(svg,a,zh,en)=>{svg.append(el('text',{...a,class:'zh'},zh));svg.append(el('text',{...a,class:'en'},en))};
const frame=(svg,h)=>{const w=svg.clientWidth||900;svg.setAttribute('viewBox',`0 0 ${w} ${h}`);return w};
const median=a=>{const s=[...a].sort((x,y)=>x-y),m=s.length>>1;return s.length%2?s[m]:(s[m-1]+s[m])/2};
const none=()=>both('还没有数据','No data yet');
(function trend(){const svg=document.getElementById('trend'),h=430,w=frame(svg,h),runs=D.reports.runs.filter(r=>r.triage!=null||r.reviewer!=null);if(!runs.length)return;
 const x=i=>58+i*(w-84)/Math.max(1,runs.length-1);
 [['triage',0.9,'规划判断（Triage）','Planning (Triage)',24],['reviewer',0.85,'验收判断（Reviewer）','Review (Reviewer)',222]].forEach(([key,gate,zh,en,top])=>{
  const ph=160,y=v=>top+ph-v*ph;label(svg,{x:0,y:top-8,style:'font-weight:600;font-size:13px'},zh,en);
  svg.append(el('line',{x1:44,x2:w-14,y1:y(0),y2:y(0),class:'axis'}));
  for(const v of [0,0.5,1]){svg.append(el('text',{x:4,y:y(v)+4},(v*100)+'%'));if(v)svg.append(el('line',{x1:44,x2:w-14,y1:y(v),y2:y(v),class:'axis',opacity:0.5}))}
  svg.append(el('line',{x1:44,x2:w-14,y1:y(gate),y2:y(gate),class:'thr'}));label(svg,{x:w-14,y:y(gate)+15,'text-anchor':'end',fill:C[3]},`及格线 ${gate*100}%`,`Pass line ${gate*100}%`);
  let last='';runs.forEach((r,i)=>{const day=String(r.created_at||'').slice(5,10);if(day!==last){last=day;svg.append(el('text',{x:x(i),y:top+ph+15,'text-anchor':'middle'},day))}
   if(r[key]==null)return;const c=r.split==='holdout'?C[1]:C[0],cx=x(i),cy=y(r[key]);
   const m=r.split==='holdout'?el('rect',{x:cx-4,y:cy-4,width:8,height:8}):el('circle',{cx,cy,r:4.5});m.setAttribute(r.valid?'fill':'stroke',c);if(!r.valid)m.setAttribute('fill','none');
   m.append(el('title',{},`${r.label_zh} · ${r.split} · ${day} · ${pct(r[key])}`));svg.append(m)})})})();
(function tuning(){const P=D.reports.tuning||[],svg=document.getElementById('tuning'),cost=document.getElementById('tuning-cost');if(!svg||P.length<2)return;
 const xs=(w)=>{const same=P.filter(p=>p.same_problems).length,left=70,right=w-40,gap=P.some(p=>!p.same_problems)?1.5:1,unit=(right-left)/Math.max(1,same-1+(gap>1?gap:0));let out=[],seen=0;P.forEach(p=>{if(p.same_problems){out.push(left+seen*unit);seen++}else out.push(left+(same-1+gap)*unit)});return out};
 const legend=(s,top)=>{const same=P.some(p=>p.same_problems),other=P.some(p=>!p.same_problems);let at=50;if(same){s.append(el('circle',{cx:at,cy:top,r:5,fill:C[0]}));label(s,{x:at+10,y:top+4},'同样的 50 题','The same 50 problems');at+=130}if(other){s.append(el('rect',{x:at-5,y:top-5,width:10,height:10,fill:C[1]}));label(s,{x:at+10,y:top+4},'没用过的 50 题','50 unseen problems')}};
 const draw=(s,h,value,fmt,top,max,whisker,ticks)=>{const w=frame(s,h),X=xs(w),base=h-26,Y=v=>base-(v/max)*(base-top);
  for(const v of ticks){s.append(el('line',{x1:44,x2:w-14,y1:Y(v),y2:Y(v),class:'axis',opacity:v?0.5:1}));s.append(el('text',{x:4,y:Y(v)+4},fmt.tick(v)))}
  const line=P.map((p,i)=>[p,X[i]]).filter(([p])=>p.same_problems&&value(p)!=null);if(line.length>1)s.append(el('polyline',{points:line.map(([p,x])=>`${x},${Y(value(p))}`).join(' '),fill:'none',stroke:C[0],'stroke-width':2,'stroke-linejoin':'round','stroke-linecap':'round'}));
  P.forEach((p,i)=>{const v=value(p),x=X[i];s.append(el('text',{x,y:base+18,'text-anchor':'middle',class:'zh'},p.short_zh));s.append(el('text',{x,y:base+18,'text-anchor':'middle',class:'en'},p.short_en));if(v==null)return;const c=p.same_problems?C[0]:C[1],y=Y(v);
   if(whisker){s.append(el('line',{x1:x,x2:x,y1:Y(p.low),y2:Y(p.high),stroke:c,'stroke-width':1.5,'stroke-linecap':'round',opacity:0.55}))}
   const m=p.same_problems?el('circle',{cx:x,cy:y,r:5,fill:c,stroke:'var(--card)','stroke-width':2}):el('rect',{x:x-5,y:y-5,width:10,height:10,fill:c,stroke:'var(--card)','stroke-width':2});s.append(m);
   const end=x>w-80;s.append(el('text',{x:end?x-10:x+10,y:y-8,'text-anchor':end?'end':'start',style:'font-weight:600'},fmt.point(p)));
   const hit=el('circle',{cx:x,cy:y,r:18,fill:'transparent'});hit.append(el('title',{},fmt.title(p)));s.append(hit)})};
 draw(svg,300,p=>p.rate,{tick:v=>(v*100)+'%',point:p=>`${p.resolved}/${p.selected}`,title:p=>`${p.short_zh} · ${p.resolved}/${p.selected} · ${pct(p.rate)} · 95%: ${pct(p.low)}–${pct(p.high)}`},44,1,true,[0,0.25,0.5,0.75,1]);legend(svg,16);
 const top=Math.max(...P.map(p=>p.per_problem_usd||0));if(cost&&top>0){const max=Math.ceil(top*2+0.5)/2,ticks=[0,max/2,max];draw(cost,190,p=>p.per_problem_usd,{tick:v=>'$'+v.toFixed(2),point:p=>'$'+p.per_problem_usd.toFixed(2),title:p=>`${p.short_zh} · ${usd(p.per_problem_usd)}`},18,max,false,ticks)}})();
(function models(){const box=document.getElementById('scatter'),by=new Map();D.reports.runs.filter(r=>r.valid&&r.split==='dev'&&r.reviewer!=null&&String(r.group).startsWith('exp-')).forEach(r=>{(by.get(r.label_en)||by.set(r.label_en,[]).get(r.label_en)).push(r)});
 const rows=[...by.values()].map(l=>({zh:l[0].label_zh,en:l[0].label_en,score:median(l.map(r=>r.reviewer)),cost:median(l.map(r=>r.per_case_usd||0)),n:l.length})).sort((a,b)=>b.score-a.score);
 box.innerHTML=rows.map(r=>`<div class="bar"><span>${both(r.zh,r.en)}</span><span class="track"><i style="width:${r.score*100}%"></i><u style="left:85%"></u></span><span class="note">${pct(r.score)} · ${usd(r.cost)} · ${both(`考了 ${r.n} 次`,`${r.n} run${r.n>1?'s':''}`)}</span></div>`).join('')||none()})();
(function failures(){const box=document.getElementById('failures'),all={};Object.values(D.reports.failures).forEach(l=>l.forEach(([k,v])=>all[k]=(all[k]||0)+v));
 const top=Object.entries(all).sort((a,b)=>b[1]-a[1]).slice(0,10),mx=Math.max(1,...top.map(t=>t[1]));box.classList.add('bars2');
 box.innerHTML=top.map(([k,v])=>`<div class="bar"><span>${both(...(F[k]||[k,k]))}</span><span class="track"><i style="width:${v/mx*100}%"></i></span><span class="note">${v}</span></div>`).join('')||none()})();
(function context(){const svg=document.getElementById('context'),h=280,w=frame(svg,h),rows=[...D.reports.contexts].sort((a,b)=>String(a.date).localeCompare(String(b.date))||String(a.split).localeCompare(String(b.split)));if(!rows.length)return;
 const y=v=>h-58-v*(h-104),bw=(w-52)/rows.length;svg.append(el('line',{x1:40,x2:w-8,y1:y(0),y2:y(0),class:'axis'}));for(const v of [0,0.5,1])svg.append(el('text',{x:0,y:y(v)+4},(v*100)+'%'));
 svg.append(el('rect',{x:40,y:4,width:11,height:11,fill:C[2]}));label(svg,{x:56,y:14},'完整压缩','Full compression');svg.append(el('rect',{x:170,y:4,width:11,height:11,fill:C[0]}));label(svg,{x:186,y:14},'只遮蔽旧内容','Older content hidden');
 rows.forEach((r,i)=>{const x0=46+i*bw;[[r.full_probe,C[2]],[r.mask_probe,C[0]]].forEach(([v,c],j)=>{if(v==null)return;const bx=x0+j*bw*0.36,bwid=bw*0.32;svg.append(el('rect',{x:bx,y:y(v),width:bwid,height:y(0)-y(v),fill:c}));if(bw>58)svg.append(el('text',{x:bx+bwid/2,y:y(v)-4,'text-anchor':'middle'},Math.round(v*100)+'%'))});
  svg.append(el('text',{x:x0+bw*0.34,y:h-38,'text-anchor':'middle'},String(r.date||'').slice(5)));label(svg,{x:x0+bw*0.34,y:h-22,'text-anchor':'middle'},r.split==='holdout'?'正式卷':'练习卷',r.split==='holdout'?'Final':'Practice');label(svg,{x:x0+bw*0.34,y:h-6,'text-anchor':'middle',class:r.gate?'':'bad'},r.gate?'过线':'没过',r.gate?'Pass':'Fail')})})();
document.getElementById('runs').innerHTML='<tr>'+[['这次考的是什么','What was tested'],['卷子','Paper'],['时间','Time'],['规划','Planning'],['形状','Shape'],['验收','Review'],['每轮判定','Round verdicts'],['重试','Retries'],['完整任务','Full tasks'],['花费','Cost'],['回退','Fallbacks']].map(([z,e])=>`<th>${both(z,e)}</th>`).join('')+'</tr>'+
 D.reports.runs.map(r=>`<tr${r.valid?'':' class="void"'}><td>${both(r.label_zh+(r.valid?'':'（作废）'),r.label_en+(r.valid?'':' (invalid)'))}</td><td>${r.split==='holdout'?both('正式卷','Final'):both('练习卷','Practice')}</td><td>${esc(String(r.created_at||'').slice(5,16).replace('T',' '))}</td><td class="${r.triage==null?'':r.triage>=0.9?'ok':'bad'}">${pct(r.triage)}</td><td>${pct(r.shape)}</td><td class="${r.reviewer==null?'':r.reviewer>=0.85?'ok':'bad'}">${pct(r.reviewer)}</td><td>${pct(r.rounds)}</td><td>${r.stuck??'—'}</td><td>${r.task_total?r.task_pass+'/'+r.task_total:'—'}</td><td>${usd(r.total_usd)}</td><td>${r.fallbacks}</td></tr>`).join('');
(function language(){const root=document.documentElement,buttons=document.querySelectorAll('[data-set-lang]');
 const set=(lang,keep)=>{root.dataset.lang=lang;root.lang=lang==='en'?'en':'zh-CN';buttons.forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.setLang===lang)));if(keep){try{localStorage.setItem('evals-lang',lang)}catch(e){}}};
 let saved=null;try{saved=localStorage.getItem('evals-lang')}catch(e){}
 set(saved==='en'?'en':'zh',false);buttons.forEach(b=>b.addEventListener('click',()=>set(b.dataset.setLang,true)))})();
</script></body></html>
"""


def public_data(reports: dict[str, Any], generated_at: str) -> dict[str, Any]:
    """公开版的数据：只有生成时间与评测的汇总，运行行里去掉报告的文件名，带上给读者看的名字。"""

    def without_file(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: v for k, v in row.items() if k != "file"} for row in rows]

    def labelled(row: dict[str, Any]) -> dict[str, Any]:
        tail = str(row["group"]).rsplit("/", 1)[-1]
        zh, en = GROUP_LABELS.get(row["group"], (tail, tail))
        return {**row, "label_zh": zh, "label_en": en}

    return {
        "generated_at": generated_at,
        "reports": {
            "runs": [labelled(r) for r in without_file(reports["runs"])], "contexts": without_file(reports["contexts"]), "failures": reports["failures"],
            "benchmarks": [{k: v for k, v in b.items() if k not in ("problems", "solved")} for b in reports.get("benchmarks") or []],
            "tuning": tuning_series(reports.get("benchmarks") or []),
        },
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
    reports = collect_reports(args.reports) if args.reports.is_dir() else {"runs": [], "contexts": [], "failures": {}, "benchmarks": []}
    out = args.out or (args.reports / "dashboard.html")
    if args.public:
        public = public_data(reports, generated_at)
        page = render_public(public, title, public_summary(public["reports"]), evalset_counts())
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
