# 检索实测：从质量、时延冲突到候选缩减

2026-11-21，覆盖三件事：真实模型 CPU 检索、旧排名的离线重放、仓库技术 FAQ 的检索验证。以下数据都衡量检索。生成答案准确率、不可答题的拒答率、生产数据库和真实用户流量本轮未测。

## 真实 CPU 检索

数据沿用[原始文件基准](../../file-engines-benchmark/README.md)：120 份合成文件、100 个中英文问题、自动关联证据，人工复核锚点为 0。按 20 个主题的哈希顺序固定 8 个开发主题（40 题）与 12 个验证主题（60 题），同一主题的五种问法只进一侧。检索时可见全部文档。验证主题未用于本轮调参，但是旧实验已经公开，不是全新盲测集。问题模板和自动标注体系仍然共享。[完整题号、种子、数据和代码指纹](run/protocol.json)

开发集的历史宽池重排把 Hit@8 从 32/40 提到 39/40，代价是 P95 从 0.215 秒增加至 6.093 秒。原策略每路取 40 个稠密/稀疏候选，合并去重后由交叉编码器打分。新候选只改每路进入重排的数量，仍然返回前 8 个块。预先设定的验收条件为：验证 Hit@8 比同场混合召回基线至少高 5 个百分点、宏平均 Recall@8 不下降、查询 RPC P95 ≤2 秒。三个条件要同时满足。

每轮在单独的 Linux 容器中重新载入原始模型、把冻结解析块重新嵌入并建索引；Hybrid 和重排各有一次不计分的预热。每道题对两个分支各发一次实际 gRPC search，轮流交换先后顺序，计时包含查询嵌入、检索、权限过滤和启用的交叉编码器。容器顺序运行，均限制为 4 个 CPU、8 GiB 内存、断外网，付费 API 调用为 0。内存向量后端与 SQLite 为本地隔离夹具；机器共享且每题每分支只计时一次，不能据单轮 P95 承诺生产 SLO。

| 验证 60 题 | Hit@8 | Recall@8 | P95 | 判定 |
|---|---:|---:|---:|---|
| 历史宽池（40+40），历史测量 | 56/60 | 51.11% | 6.808 s | 质量参考，不达时延门槛 |
| 第一轮当场 Hybrid | 47/60 | 30.28% | 0.228 s | 对照 |
| 第一轮缩池（8+8） | 51/60 | 35.56% | 2.049 s | 时延未过；新增命中 4 题，无丢失 |
| 第二轮当场 Hybrid | 47/60 | 30.28% | 0.206 s | 第二次独立重跑对照 |
| 第二轮缩池（6+6） | **51/60** | **33.72%** | **1.817 s** | 三项门槛均通过；新增命中 4 题，无丢失 |

第一轮开发集 32→36/40、Recall@8 28.58%→36.92%，但 P95 为 2.012 秒。因此在查看该轮验证结果前，将每路候选从 8 减至 6，冻结相同分组与门槛。决策保存在 [iteration-decision.json](run/iteration-decision.json)。第一轮[逐题记录](run/rows.jsonl)与[汇总](run/results.json)保留了时延、题目状态和检索来源。

第二轮在开发题上取得 36/40、Recall@8 为 35.58%、P95 为 1.688 秒。验证集上，6+6 保住 8+8 的 51 个命中，打分的 query–passage 对数由 738 降为 580，但多来源覆盖 Recall@8 由 35.56% 回落到 33.72%。与当场无重排基线相比仍高 3.44 个百分点。该候选适合进入后续业务问答验收；宽池的 56/60 命中和 51.11% Recall 仍有质量优势，不能宣传缩池优于宽池质量。第二轮[逐题计时与排名](run6/rows.jsonl)、[验收结果](run6/results.json)、[容器约束与退出状态](containers.json)均已保留。两轮合计 400 次计分 search RPC，另有预热 RPC。

可用于简历的准确措辞：“在 4 CPU、8 GiB 的隔离环境中，按主题留出 60 道合成文档检索题，通过缩减交叉重排候选，将命中从 47/60 提升至 51/60，查询 P95 为 1.82 秒。”若要强调提速，可写“8+8 缩到 6+6 后，两次测量分别为 2.05 秒和 1.82 秒，命中题数相同，Recall@8 略有下降”。

**怎样读指标。** Hit@8 每题只要有一个带正确位置和原文的相关证据即为命中；Recall@8 取应命中的来源文档中覆盖到的比例，再按题平均。P95 为排序后第 ceil(0.95 × N) 项。上一轮历史宽池的延迟单列；改进幅度优先比较同轮实测 Hybrid，不将离线计算的耗时算为 RPC 时延。

## 如何在正常服务中选用或回滚

原先 `AR_KNOWLEDGE_RERANK=1` 只能开启重排，两个召回通道固定各取 40。现在 Memory 服务启动时支持可选参数 `AR_KNOWLEDGE_RERANK_TOP8_DEPTH`（整数 1–40，默认 40）。在已配置并可正常服务的 Model Gateway 上启用文档重排，给 Memory Service 新进程设置：

```yaml
memory:
  extraEnv:
    - {name: AR_KNOWLEDGE_RERANK, value: "1"}
    - {name: AR_KNOWLEDGE_RERANK_TOP8_DEPTH, value: "6"}
```

这两项是增量示例，合并保留现有 `memory.extraEnv`。它们只调整文档 Knowledge 检索，不控制经验记忆的 `AR_MEMORY_RERANK_MODE`。请求 `limit <= 8` 时才使用配置深度，6+6 合并去重后最多 12 个候选。文档被排除或两路候选重合时，最终返回可能少于请求的最大条数。`limit > 8` 保留原 40+40；关闭重排时仍走原混合召回路径。回滚到宽池只需去掉深度配置或改为 `40`，按常规重启 Memory；去掉原重排开关可退回 Hybrid。此为可上线的选择，没有发布或修改集群配置。

参数经过 Memory 服务启动入口接入 `RetrievalEngine`。[服务测试](../../../../../services/py-memory/tests/test_knowledge_rerank_depth.py) 从 `KnowledgeService.execute("index"/"search")` 进入实际的 SQLite/内存检索路径，检查两路向量调用、交给 scorer 的候选数、权限过滤流程，以及 RPC 返回的 `retrieval.top_k` 与 `candidate_limit`。同时验证正式服务启动时把配置传到注册的 KnowledgeService。定向测试[18 项通过](run6/entrypoint-tests.txt)。权重复验采用已登记的 E5 与本地交叉编码器；其他网关模型、数据库或硬件要重新测量延迟和质量。

## 廉价融合的负结果

另用冻结的 E5 混合检索 top-8 和精确 BM25 top-8 做 RRF（k=60），在开发集比较每来源 1、2、8 个候选的约束。按不降低 Hit 的约束、再按 Recall 和 MRR 选出上限 8。开发集 Recall 从 28.58% 升到 29.83%，验证却从 30.28% 降至 28.17%，命中从 47/60 降至 45/60，拒绝这一方案。[开发选择](run/fusion-policy.json) · [验证](run/fusion-validation.json)。这部分是缓存排名重放；两个历史耗时之和只是顺序执行估算。

## 仓库 FAQ：用真实项目文本补充验证

从固定提交 `da58416909fc54f02519de95706df01d9bbd1701` 的 14 篇技术文档提取全部 83 对既有 FAQ 问答。评测目标是检索到原文配对的答案段落；问题标题从索引中全部移除。按源文档而非单条问题分组，7 篇/38 题开发，另 7 篇/45 题验证。两臂均使用仓库的 `LexicalRetriever`，唯一改动是在每个答案前加入其文档的模块标题。采用之前设定的验证 Hit@3 与 MRR@8 均应提高的门槛。

| 分组 | 召回配置 | Hit@1 | Hit@3 | MRR@8 |
|---|---|---:|---:|---:|
| 开发 38 题 | 仅答案，TF-IDF | 16 | 25 | 0.555 |
| 开发 38 题 | 增加模块标题，同一 TF-IDF | 20 | 26 | 0.616 |
| 验证 45 题 | 仅答案，TF-IDF | 15 | 28 | 0.481 |
| 验证 45 题 | 增加模块标题，同一 TF-IDF | 21 | **32** | **0.590** |

例如，“幂等键为什么不能含 attempt 编号？”对应答案由第 4 升至第 2。验证 Hit@3 新增 4 题、丢失 0 题。该评测从真实项目文档抽取，但并非真实用户日志，配对答案也不是本轮独立人工标注；引用方应注明任务是“项目 FAQ 的答案段落检索”。[题单与切分](run/faq-protocol.json) · [逐题结果](run/faq-rows.jsonl) · [汇总](run/faq-results.json)

## 如何复现

本目录中的 `prepare.py` 核验原始基准脚本、引擎代码和 manifest 的 SHA256，然后生成一次性实验覆盖文件。只覆盖容器中挂载的代码，原始登记的模型文件在加载前也逐个核对哈希。下面从仓库根目录运行：

```sh
evidence=docs/plan/evidence/resume-eval-loop-20261121/rag
archive=docs/plan/evidence/file-engines-benchmark
python3 "$evidence/replay_fusion.py" --run "$evidence/run" --archive "$archive/run"
.venv/bin/python "$evidence/repo_faq.py"
python3 "$evidence/summarize.py" --run "$evidence/run" --archive "$archive/run"
python3 "$evidence/summarize.py" --run "$evidence/run6" --archive "$archive/run"
```

在有 [原始 Linux 环境和模型](../../file-engines-benchmark/README.md) 的主机上，可用以下命令另起一轮；每轮指定一个新的输出目录，等待容器退出后再运行下一轮。初次启动发现输出目录不能由镜像的 uid 10001 写入；已修正新目录属主后重跑，失败启动未生成任何查询结果。

```sh
sh launch.sh NEW_RUN_DIR PINNED_RUNTIME_SRC FROZEN_BENCHMARK_DIR reranked-executed-benchmark.py 8
docker wait "$(cat NEW_RUN_DIR/container-name)"
# 第二轮指定另一个 NEW_RUN_DIR，并把最后一个参数改为 6。
```
