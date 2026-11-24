import React from "react";
import { Card } from "@heroui/react";

const HEADLINE_CARDS = [
  {
    id: "swe",
    tag_zh: "代码生成与修复",
    tag_en: "Coding & Repair",
    title_zh: "SWE-bench Verified（抽样 50 题）",
    title_en: "SWE-bench Verified (50-case sample)",
    before: "60%",
    after: "86%",
    delta_zh: "+26 pp · 30 → 43 题",
    delta_en: "+26 pp · 30 → 43 resolved",
    beforePct: 60,
    afterPct: 86,
    summary_zh: "官方 Docker 隐藏测试判分，面向代码任务的 Planner → Stage（DAG）→ Reviewer 执行闭环持续调优。",
    summary_en: "Official Docker hidden-test grading across a Planner → Stage (DAG) → Reviewer execution loop.",
    href: "/results?type=runtime&trend=swebench&metric=resolved_rate&batch=init-50#trend-swebench",
    cta_zh: "查看趋势图",
    cta_en: "View trend",
  },
  {
    id: "rag",
    tag_zh: "知识库与记忆检索",
    tag_en: "RAG & Code Retrieval",
    title_zh: "LongMemEval & 代码定义定位",
    title_en: "LongMemEval & Code Definition RAG",
    before: "18.2%",
    after: "75.8%",
    delta_zh: "代码首引 6/33 → 25/33 · 记忆 Recall@5 88% → 96%",
    delta_en: "Code Hit@1 6/33 → 25/33 · Memory Recall@5 88% → 96%",
    beforePct: 18.2,
    afterPct: 75.8,
    summary_zh: "Tree-sitter 结构切分 + Milvus／BM25 混合召回 + RRF 重排与显式符号召回，支持源码哈希核验。",
    summary_en: "Tree-sitter structural chunking, Milvus + BM25 hybrid retrieval, RRF reranking and symbol lookup.",
    href: "/results?type=ablation#experiment-engineering-20261123-rag-lookup",
    cta_zh: "查看检索对照",
    cta_en: "View RAG charts",
  },
  {
    id: "context",
    tag_zh: "长上下文工程",
    tag_en: "Context Engineering",
    title_zh: "LongBench v2（40 题对照）",
    title_en: "LongBench v2 (40-question cohort)",
    before: "47.5%",
    after: "75.0%",
    delta_zh: "+27.5 pp · 事实保留率显著提升",
    delta_en: "+27.5 pp · higher fact retention",
    beforePct: 47.5,
    afterPct: 75,
    summary_zh: "稳定提示词前缀缓存、大输出按余量转存与关键事实回填，兼顾长任务上下文成本与记忆保真度。",
    summary_en: "Prefix caching, output offloading, and key-fact verification for long-horizon agent tasks.",
    href: "/results?type=ablation&trend=longbench-context&metric=confirmed_share&batch=longbench-40#trend-longbench-context",
    cta_zh: "查看上下文对照",
    cta_en: "View context chart",
  },
  {
    id: "infra",
    tag_zh: "云原生沙箱与高并发",
    tag_en: "Sandbox & High Concurrency",
    title_zh: "Firecracker 预热池 & 200 并发压测",
    title_en: "Firecracker Warm Pool & 200-Conc. API",
    before: "1.23 s",
    after: "0.052 s",
    delta_zh: "沙箱 P95 降 95.7% · 200 并发吞吐 6.15万 → 19.02万 req/s",
    delta_en: "Sandbox P95 -95.7% · 200-conc. QPS 61.5k → 190.2k",
    beforePct: 100,
    afterPct: 4.3,
    lowerIsBetter: true,
    summary_zh: "Rust 沙箱预热池消除冷启动等待；Go 网关层以 RWMutex + singleflight + 终态缓存实现 3.09× 吞吐提升。",
    summary_en: "Rust microVM warm pool cuts cold boot wait; Go RWMutex + singleflight + terminal cache lifts QPS 3.09x.",
    href: "/results?type=ablation#experiment-engineering-20261107-sandbox-warmpool",
    cta_zh: "查看性能图表",
    cta_en: "View perf charts",
  },
];

const QUICK_NAV = [
  {
    href: "/results?type=runtime",
    title_zh: "代码与运行时基准",
    title_en: "Runtime & Coding Benchmarks",
    desc_zh: "SWE-bench Verified 与 Terminal-Bench 2.0 端到端官方判分与历史演进。",
    desc_en: "End-to-end official grading on SWE-bench Verified and Terminal-Bench 2.0.",
  },
  {
    href: "/results?type=ablation",
    title_zh: "工程性能与消融对照",
    title_en: "Engineering & Ablation Studies",
    desc_zh: "沙箱启动速度、200 并发高吞吐压测、代码与记忆 RAG、长上下文策略控制变量实验。",
    desc_en: "Controlled comparisons for sandbox latency, 200-concurrency throughput, RAG, and context management.",
  },
  {
    href: "/results?type=model#capability-matrix",
    title_zh: "多角色模型能力矩阵",
    title_en: "Multi-Role Capability Matrix",
    desc_zh: "覆盖规划、分诊、工具调用（BFCL v4）、评审（JudgeBench）与压缩 5 类角色，支持最新与 21 模型历史视图切换。",
    desc_en: "Model selection matrix across planning, routing, tool use (BFCL v4), judging and compaction.",
  },
];

export function OverviewShowcase({ t, lang, site, total, number, tr, Badge, Frameworks, props }) {
  const cats = {};
  for (const d of site?.datasets || []) {
    cats[d.eval_category] = (cats[d.eval_category] || 0) + d.counts.total;
  }

  return (
    <div className="overview-redesign">
      <section className="headline-section" aria-label={t("核心评测成果", "Headline evaluation results")}>
        <div className="section-heading">
          <h2>{t("核心实测成果", "Verified highlights")}</h2>
          <a href="/results">{t("进入完整评测看板", "Open full dashboard")} →</a>
        </div>
        <div className="headline-grid">
          {HEADLINE_CARDS.map((card) => (
            <Card key={card.id} className="headline-card">
              <Card.Header className="headline-head">
                <span className="headline-tag">{card[`tag_${lang}`]}</span>
                <a className="headline-link" href={card.href}>
                  {card[`cta_${lang}`]} →
                </a>
              </Card.Header>
              <Card.Content className="headline-body">
                <h3>{card[`title_${lang}`]}</h3>
                <div className="headline-numbers">
                  <span className="num-before">{card.before}</span>
                  <span className="num-arrow" aria-hidden="true">→</span>
                  <strong className="num-after">{card.after}</strong>
                </div>
                <p className="headline-delta">{card[`delta_${lang}`]}</p>
                <div className="mini-bars" aria-hidden="true">
                  <div className="mini-bar-row">
                    <span>{t("基线", "Base")}</span>
                    <div className="mini-track">
                      <div className="mini-fill base" style={{ width: `${Math.max(4, Math.min(100, card.beforePct))}%` }} />
                    </div>
                  </div>
                  <div className="mini-bar-row">
                    <span>{t("优化", "Opt.")}</span>
                    <div className="mini-track">
                      <div className={`mini-fill ${card.lowerIsBetter ? "fast" : "best"}`} style={{ width: `${Math.max(4, Math.min(100, card.afterPct))}%` }} />
                    </div>
                  </div>
                </div>
                <p className="headline-summary">{card[`summary_${lang}`]}</p>
              </Card.Content>
            </Card>
          ))}
        </div>
      </section>

      <section className="quick-nav-section" aria-label={t("专题视图", "Evaluation views")}>
        <div className="section-heading">
          <h2>{t("按专题浏览", "Explore by track")}</h2>
          <a href="/results?view=archive#runs">{t("查看完整历史运行归档", "Browse raw run archive")} →</a>
        </div>
        <div className="quick-nav-grid">
          {QUICK_NAV.map((item) => (
            <a key={item.href} href={item.href} className="quick-nav-card">
              <strong>{item[`title_${lang}`]}</strong>
              <p>{item[`desc_${lang}`]}</p>
              <span className="quick-nav-cta">{t("打开专题", "Explore")} →</span>
            </a>
          ))}
        </div>
      </section>

      {site && !site.unavailable && (
        <section className="catalog-compact" aria-label={t("题库与框架概览", "Dataset and framework overview")}>
          <div className="section-heading">
            <h2>{t("评测题库与方法", "Datasets & methodology")}</h2>
            <a href="/datasets">{t("浏览全部题目", "Browse all datasets")} →</a>
          </div>
          <Card className="overview-panel">
            <Card.Content>
              <div className="catalog-total">
                <b>{number(total.total)}</b>
                <span>
                  {t("道题目", "questions")} · {site.datasets.length} {t("个数据集", "datasets")} · {site.frameworks.length} {t("套框架", "frameworks")}
                </span>
              </div>
              <div className="category-chips">
                {Object.entries(cats)
                  .sort((a, b) => b[1] - a[1])
                  .map(([k, v]) => (
                    <a href={`/datasets?category=${encodeURIComponent(k)}`} key={k}>
                      <Badge>
                        {tr(site.categories?.[k])} <span className="mono">{v}</span>
                      </Badge>
                    </a>
                  ))}
              </div>
            </Card.Content>
          </Card>
          <Frameworks {...props} compact />
        </section>
      )}
    </div>
  );
}
