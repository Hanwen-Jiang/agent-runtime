"""Project paired, public case IDs and metrics from frozen local experiment receipts."""
import hashlib
import json
import math
from pathlib import Path

OUT = Path(__file__).resolve().parent
RAG = OUT.parent / "developer-rag"
FC = OUT.parents[1] / "fc-pin-20261107/first"
WORKSPACE = OUT.parents[5]
CODING = WORKSPACE / "coding-agent/docs/plan/evidence/resume-exp-20261123/workspace-delivery"
CODING_BENCH = WORKSPACE / "coding-agent/docs/plan/evidence/workspace-high-concurrency-20261125"


def read(path):
    return json.loads(path.read_bytes())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metric(key, zh, en, unit, before, after):
    return dict(key=key, label_zh=zh, label_en=en, unit=unit, baseline=before, candidate=after)


def card(slug, title, title_en, ids, method, method_en, corpus, arm_labels, metrics, notes, source, digest):
    assert len(ids) == len(set(ids))
    return {"id":slug, "kind":"engineering-comparison", "evaluation_type":"ablation", "evaluation_scope":"component",
            "title_zh":title, "title_en":title_en, "baseline_label":arm_labels[0], "candidate_label":arm_labels[1],
            "evaluated_on":"2026-11-23", "sample_count":len(ids), "completed_count":len(ids), "collection_complete":True,
            "baseline_completed":len(ids), "candidate_completed":len(ids), "quality_valid":True, "cost_known":True,
            "actual_cost_usd":0, "known_cost_usd":0, "metrics":metrics,
            "limitations_zh":[notes[0]], "limitations_en":[notes[1]], "source_sha256":digest,
            "selection_info":{"dataset":corpus, "method":"explicit_ids", "split":"validation", "source":source,
                              "selected_count":len(ids), "method_detail_zh":method, "method_detail_en":method_en,
                              "selection_ids":ids}}


def rag(lookup):
    src = RAG / ("lookup-validation.json" if lookup else "validation.json")
    manifest = RAG / ("reserve.json" if lookup else "cohort.json")
    r = read(src)
    expected_hash = r["holdout_manifest" if lookup else "manifest_sha256"]
    assert sha(manifest) == expected_hash
    rows = r["rows"]
    ids = [row["id"] for row in rows]
    expected_ids = [row["id"] for row in read(manifest)["questions"] if row["id"].startswith("res-" if lookup else "val-")]
    assert set(ids) == set(expected_ids) and len(ids)==(33 if lookup else 48)
    arms = ("B","C") if lookup else ("A","B")
    def hits(arm,k):
        eligible = [row[arm]["rank"] for row in rows]
        count = sum(isinstance(i,int) and 1 <= i <= k for i in eligible)
        assert count == r["metrics"][arm]["hit"+str(k)]
        return count / len(ids)
    def p95(arm):
        spans = sorted(x for row in rows for x in row[arm]["search_ms"])
        assert len(spans) == len(ids)*3 and all(math.isfinite(x) and x>=0 for x in spans)
        value = spans[math.ceil(len(spans)*.95)-1]
        assert abs(value-r["metrics"][arm]["p95_ms"])<.0011
        return value/1000
    metrics = [metric("hit1","首条引用命中定义行","First citation covers definition","ratio",*(hits(a,1) for a in arms)),
               metric("hit8","前八条命中定义行","Top 8 cover definition","ratio",*(hits(a,8) for a in arms)),
               metric("p95","检索 P95","Search P95","seconds",*(p95(a) for a in arms))]
    if lookup:
        slug="engineering-20261123-rag-lookup"
        title=("代码导航检索 · 符号召回","Code navigation retrieval")
        method=("从同一冻结源码库余下的 33 个目标文件各取 1 题。它们与前一轮 24 个开发、48 个验证目标文件完全不同。", "One question per remaining target file, disjoint from the original 24 development and 48 validation files.")
        labels=("仅重排 · Rerank","符号召回 · Symbol lookup")
    else:
        slug="engineering-20261123-rag-citation"
        title=("代码知识库 · 引用定义行","Repository RAG citation accuracy")
        method=("真实测试调用定位源码定义，按目标文件隔离。24 个文件用于开发，48 个文件各取 1 题独立验收。", "One test-call query per definition file, with 48 validation targets disjoint from the 24 development targets.")
        labels=("原检索 · Baseline","同名定义优先 · Rerank")
    c = card(slug,*title,ids,*method,"Developer RAG",labels,metrics,
             ("本地检索服务，固定 283 个源码文件和 4,303 个代码块。问题按真实调用关系构造，P95 为单进程暖索引，费用仅指模型接口。", "Local retrieval, 283 source files and 4,303 chunks. Constructed queries from actual calls; single-process warm timings and model API cost only."),
             src.name,sha(src))
    c["selection_info"]["cohort_sha256"] = expected_hash
    return c


def workspace():
    manifest = CODING / "cohort.json"
    receipts = [read(CODING/name) for name in ("baseline.json","candidate.json")]
    ids = [r["id"] for r in receipts[0]["cases"]]
    assert ids == [r["id"] for r in receipts[1]["cases"]]
    c = read(manifest)
    assert set(ids)=={f"workspace-eval-{s}-{i:02d}" for s in c["scenarios"] for i in range(1,c["repetitions"]+1)}
    passed, peak = [], []
    for arm in receipts:
        assert arm["cohort_sha256"]==sha(manifest) and arm["total"]==len(ids)
        cases=arm["cases"]
        assert all(r["pass"]==all(v for v in r["checks"].values() if v is not None) for r in cases)
        passed.append(sum(r["pass"] for r in cases)/len(cases))
        peak.append(max(r["max_concurrent_status_calls_one_key"] for r in cases))
    metrics=[metric("pass_rate","场景验收通过率","Scenario pass rate","ratio",*passed),
             metric("peak_queries","同一操作并发查询峰值","Peak status queries per operation","number",*peak)]
    card_doc=card("engineering-20261123-workspace","工作区交付 · 跨进程单执行者","Workspace delivery across processes",ids,
                  "7 种固定操作场景，每种重复 4 次。覆盖重试、状态查询、同键冲突、不同键并行和杀进程接管。",
                  "Seven predetermined scenarios, four repeats each, covering retries, polling, conflicts, independent keys and process handoff.",
                  "Coding Agent workspace delivery",("进程内去重 · Per-process","跨进程执行权 · Cross-process"),metrics,
                  ("本机真实 Go Web 进程和 HTTP，共享本地卷，下游 Runtime 协议服务为模拟。两版都正确创建 32 个项目。","Real Go processes on a shared local volume, HTTP and a simulated downstream Runtime. Both arms created 32 expected projects."),
                  "baseline.json + candidate.json",hashlib.sha256("".join(sha(CODING/n) for n in ("baseline.json","candidate.json")).encode()).hexdigest())
    card_doc["selection_info"]["cohort_sha256"]=sha(manifest)
    return card_doc


def sandbox_warmpool():
    cold_path, warm_path = FC / "cold/samples.jsonl", FC / "warm/samples.jsonl"
    cold_sum, warm_sum = read(FC / "cold/summary.json"), read(FC / "warm/summary.json")
    def load_rows(path, expected_source):
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        rows = [r for r in rows if not r.get("priming")]
        assert len(rows) == 20 and all(r["outcome"] == "completed" and r["boot_source"] == expected_source for r in rows)
        return rows
    def nearest_rank(values, q):
        ordered = sorted(values)
        return ordered[math.ceil(len(ordered) * q) - 1]
    cold_rows, warm_rows = load_rows(cold_path, "cold"), load_rows(warm_path, "warm")
    cold_p50 = nearest_rank([r["first_command_ms"] for r in cold_rows], 0.50)
    cold_p95 = nearest_rank([r["first_command_ms"] for r in cold_rows], 0.95)
    cold_create_p95 = nearest_rank([r["create_ms"] for r in cold_rows], 0.95)
    warm_p50 = nearest_rank([r["first_command_ms"] for r in warm_rows], 0.50)
    warm_p95 = nearest_rank([r["first_command_ms"] for r in warm_rows], 0.95)
    warm_create_p95 = nearest_rank([r["create_ms"] for r in warm_rows], 0.95)
    assert abs(cold_p50 - cold_sum["by_boot_source"]["cold"]["p50_ms"]) < 1e-6
    assert abs(cold_p95 - cold_sum["by_boot_source"]["cold"]["p95_ms"]) < 1e-6
    assert abs(warm_p50 - warm_sum["by_boot_source"]["warm"]["p50_ms"]) < 1e-6
    assert abs(warm_p95 - warm_sum["by_boot_source"]["warm"]["p95_ms"]) < 1e-6
    ids = [f"fc-startup-{i + 1:02d}" for i in range(20)]
    metrics = [
        metric("first_cmd_p95", "首条命令完成 P95", "First command P95", "seconds", cold_p95 / 1000, warm_p95 / 1000),
        metric("first_cmd_p50", "首条命令完成 P50", "First command P50", "seconds", cold_p50 / 1000, warm_p50 / 1000),
        metric("create_p95", "实例创建 API P95", "Create API P95", "seconds", cold_create_p95 / 1000, warm_create_p95 / 1000),
    ]
    digest = hashlib.sha256((sha(cold_path) + sha(warm_path)).encode()).hexdigest()
    card_doc = card(
        "engineering-20261107-sandbox-warmpool",
        "沙箱启动速度 · Firecracker 预热池",
        "Sandbox startup latency · Firecracker warm pool",
        ids,
        "固定同规格 Firecracker microVM（1 vCPU、256 MiB 内存、256 MiB 磁盘、Python 基础镜像），冷启动与预热池各逐次测量 20 次（失败 0 次），计时从创建请求发出到首条真实 shell 命令完成。",
        "Fixed Firecracker microVM spec (1 vCPU, 256 MiB RAM, 256 MiB scratch, Python image), 20 sequential runs each (0 failures), timed from create request to first shell command completion.",
        "Firecracker sandbox startup",
        ("冷启动 · Cold boot", "预热池 · Warm pool"),
        metrics,
        (
            "共享生产宿主上逐次实测，并发为 1，预热池常驻 2 台空闲 VM；P95 由 1.228 s 降至 0.052 s（降幅 95.7%），未计入提前建池与常驻资源开销。",
            "Measured sequentially (concurrency 1) on a shared host with 2 pre-warmed VMs; P95 dropped from 1.228 s to 0.052 s (-95.7%), excluding background pool refill cost.",
        ),
        "cold/samples.jsonl + warm/samples.jsonl",
        digest,
    )
    card_doc["evaluated_on"] = "2026-11-07"
    card_doc["selection_info"]["cohort_sha256"] = digest
    return card_doc


def workspace_high_concurrency():
    base_path, cand_path = CODING_BENCH / "baseline.json", CODING_BENCH / "candidate.json"
    base, cand = read(base_path), read(cand_path)
    assert base["total_requests"] == cand["total_requests"] == 14000
    assert base["total_succeeded"] == cand["total_succeeded"] == 14000
    ids = [s["name"] for s in base["scenarios"]]
    assert ids == [s["name"] for s in cand["scenarios"]]
    metrics = [
        metric("overall_qps", "综合吞吐 QPS（4副本·200并发）", "Overall QPS (4 replicas · 200 concurrency)", "number", round(base["overall_qps"], 1), round(cand["overall_qps"], 1)),
        metric("overall_p50", "综合中位延迟 P50", "Overall P50 latency", "seconds", base["overall_p50_ms"] / 1000, cand["overall_p50_ms"] / 1000),
        metric("running_qps", "运行态轮询 QPS（6000 请求）", "Running poll QPS (6k reqs)", "number", round(base["scenarios"][1]["qps"], 1), round(cand["scenarios"][1]["qps"], 1)),
        metric("completed_qps", "完成态读取 QPS（6000 请求）", "Completed poll QPS (6k reqs)", "number", round(base["scenarios"][2]["qps"], 1), round(cand["scenarios"][2]["qps"], 1)),
    ]
    digest = hashlib.sha256((sha(base_path) + sha(cand_path)).encode()).hexdigest()
    card_doc = card(
        "engineering-20261125-workspace-concurrency",
        "高并发工作区接口 · 200 并发压测",
        "High-concurrency workspace API · 200 concurrent clients",
        ids,
        "4 个 Go Server 副本共享卷、200 并发客户端共发起 14,000 次请求（2,000 次热点同键重复提交 + 6,000 次运行态轮询 + 6,000 次完成态读取），对比加锁优化前与 RWMutex + singleflight + 终态缓存 + 跨副本退避优化后的吞吐与延迟。",
        "4 Go replicas on a shared volume with 200 concurrent clients issuing 14,000 requests (2k hot duplicate submits + 6k running polls + 6k completed polls), comparing serialized mutex vs RWMutex + singleflight + immutable terminal cache + lock backoff.",
        "Coding Agent high-concurrency benchmark",
        ("优化前 · Baseline", "高并发优化 · Candidate"),
        metrics,
        (
            "本机 Go httptest + 真实处理器与磁盘卷压测，无外部网络往返；14,000 请求 100% 成功，2,000 次重复提交仅触发 1 次下游创建，综合吞吐由 6.15 万提升至 19.02 万 req/s（3.09×），P50 由 2.82ms 降至 0.084ms。",
            "Local Go handler + shared disk benchmark without external network hops; 14,000/14,000 succeeded with exactly 1 downstream create across 2,000 retries; throughput jumped from 61.5k to 190.2k req/s (3.09x), P50 dropped from 2.82ms to 0.084ms.",
        ),
        "baseline.json + candidate.json",
        digest,
    )
    card_doc["evaluated_on"] = "2026-11-25"
    card_doc["selection_info"]["cohort_sha256"] = digest
    return card_doc


if __name__ == "__main__":
    for doc in (workspace(), rag(False), rag(True), sandbox_warmpool(), workspace_high_concurrency()):
        path=OUT/(doc["id"]+".json")
        path.write_text(json.dumps(doc,ensure_ascii=False,indent=2,allow_nan=False)+"\n")
        print(doc["id"],sha(path))
