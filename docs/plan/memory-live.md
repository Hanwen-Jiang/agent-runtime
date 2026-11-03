# 让记忆真正用起来：Milvus、真 embedding、Worker 接 Memory Service

> 状态：2026-10-31 人定用 Milvus、用真 embedding、把 Worker 接上；验收测试人确认，代码已实现，还没有部署。
> 挂在哪：实施计划第 690 行“Memory Service 检索流水线”，加上设计改动草稿 C1（[design-changes-2026-10.md](design-changes-2026-10.md)，2026-10-24 人定 VectorStore 默认改 Milvus）与 T-18（Milvus 适配器，已完成）。

## 1. 现状

| 环节 | 现在 | 后果 |
| --- | --- | --- |
| 向量库 | 线上是 Qdrant；`python -m agentruntime_memory` 只认 `AR_QDRANT_URL`；Milvus 适配器有，没接，集群里没部署 | C1 定了却没落地 |
| embedding | 入口写死 `HashingEmbedder`（64 维词面哈希）；`GatewayEmbedder` 有，没接 | 语义检索不起作用 |
| 写入 | Worker 用 `RecordingMemoryClient`，候选只在进程内 | Memory Service 收不到经验 |
| 读取 | Planner、tool_loop 都不调 `Search`；`ContextRequest.experiences` 没人填 | 模型看不到经验 |
| 部署 | Worker 没有 `AR_MEMORY_ADDRESS`；Memory Service 没有网关地址 | 链路断的 |

## 2. 工作项

### M-01 Memory Service 用 Milvus

- 新模块 `agentruntime_memory.wiring`：`vector_store(env, keys)` 按 `AR_MILVUS_URI`、`AR_QDRANT_URL` 选实现；两个都设拒绝启动，不猜。设了 `AR_RELATIONAL_DSN`（生产）却没有向量库时拒绝启动：内存实现重启就丢。
- `agentruntime-memory` 依赖加 `agentruntime-core[milvus]`。Qdrant 适配器按 C1 先保留。
- 部署：`deploy/data` 加 Milvus 3.0.2 单机版（内嵌 etcd，数据放 PVC），Chart 的 `external.milvus.uri`。部署前核对内存与磁盘。线上切换前查 Qdrant 里有没有数据；没有就不迁移。

### M-02 真 embedding

- `wiring.embedder(env)`：设了 `AR_MODEL_GATEWAY_ADDRESS` 与 `AR_EMBEDDING_VERSION` 时用 `GatewayEmbedder`；生产（有 DSN）没配齐拒绝启动，不悄悄退回哈希；只有开发才用 `HashingEmbedder`。
- 默认 `a6api/text-embedding-3-large@2026-10`（3072 维），Model Profile 加这一项。理由与探测结果见第 4 节。`embedding_version` 就是 Profile id：网关只用这一个，不切换，向量不会混。
- 难点：保距变换是纯 Python，3072 维要核对余弦不变与耗时。换模型按 `embedding_version` 分集合，旧数据不混用（附录 I.6 的双写以后再做，现在没有存量）。

### M-03 Worker 接 Memory Service

- `AR_MEMORY_ADDRESS` 设了才接；没设时行为与现在相同（`RecordingMemoryClient`，不检索）。
- 客户端放在 `agentruntime_worker.memory`（Worker 不依赖 `agentruntime-memory` 包，它会带上向量库的依赖），只用 proto 的存根；令牌按 Task 的租户现签，5 分钟有效。
- 写：`dev_reviewer` 的 `WrapUp.memory` 换成真客户端。
- 读：`RetrievalContextSource` 在规划时查（`SEARCH_TRIGGER_PLANNING`），结果进 `PlanningContext.experiences` 与 Planner 的提示；`ModelToolLoop` 只在 Node 的第一步查（`SEARCH_TRIGGER_NODE_START`），进 `ContextRequest.experiences`。
- **检索失败不能拖垮 Task**：超时或报错就当没有经验，日志只记错误码。
- 都在 activity 里，不改命令序列，不需要版本分支。不改 proto、schema。

## 3. 验收测试

| 文件 | 看什么 |
| --- | --- |
| `services/py-memory/tests/test_serve_wiring.py` | 选向量库、拒绝启动的情况、选 embedder、版本与租户传给网关 |
| `libs/py-core/tests/adapters/test_vector_crypto_dims.py` | 3072 维余弦不变，变换够快 |
| `services/py-worker/tests/test_memory_wiring.py` | 接与不接、令牌的租户、规划与第一步检索、失败不拖垮、文字不进日志 |
| `tests/contract/test_worker_memory.py` | Worker 的客户端对真 gRPC 服务：写入后查得到，别的租户查不到，令牌与请求租户不符被拒 |
| `tests/deploy/test_memory_live.py` | Chart：Milvus 与网关的环境变量、Worker 的地址，默认不变 |

## 4. 要人定的与还没验证的

- **jev**：人说明它不是 embedding（接口在 `https://api.typesafe.ai/v1`，密钥 `JEV_API_KEY`），不进这份计划。
- **线上现状（2026-10-31）**：rev 14 换了别的镜像，Memory Service 用 `memory.imageTag` 留在 52174d9，配置不变。按下面的次序部署时去掉它。
- **部署次序**：新镜像的 Memory Service 在生产配置不全时拒绝启动，所以换镜像时要同时给 `external.milvus.uri`、`memory.embeddingVersion`，并先去掉 `external.qdrant.url`；Model Profile 的 ConfigMap 要先加 3-large。
- **默认模型**：3 句话的探测（经中转）：`text-embedding-3-large` 同义 0.629、无关 0.044；`3-small` 0.573、0.047；`gemini-embedding-2-preview` 0.837、0.479。样本太小，只够定默认，以 Ragas 的结果为准。
- **`swebench` 的模型白名单**没有 embedding 的 3-large，这个租户的检索与写入会失败（按设计不拖垮 Task）。基线在跑时不要动 Worker：接上记忆会改变被测系统，部署要等基线结束。
- 设计书要人改：C1 的文字还没进原文档。
- 没覆盖：Milvus 的后端测试要 Docker，本机没有，到服务器上跑；真模型的检索质量归 Ragas。

## 5. F2：上线与 LongMemEval_S 验收（2026-11-03）

> 登记在 `work-dag-2026-11-03.md` 的 F 线。M-01～M-03 的代码已在 main 上，F2 只新写评测，其余是部署。验收测试：`services/py-learning/tests/test_longmemeval.py`、`services/py-memory/tests/test_live_retriever.py`。
> 2026-11-03：下面四条人定照建议；评测与 `live_retriever` 已实现，测试都过了。没做的：可选的 Ragas 一步还没接；真数据（约 270 MB）还没下，第一次真跑等部署之后；部署归同事的主线。

**题库**：LongMemEval（ICLR 2025，MIT），用清洗过的 `xiaowu0162/longmemeval-cleaned` 里的 `longmemeval_s_cleaned.json`（每题约 115k token 的对话历史、约 50 个会话）。2026-11-03 人定：它测 Memory Service 的检索，Ragas 照样打分，`evalsets/memory-rag` 的 10 题留作冒烟。数据不进仓库，`fetch` 下到本地。

**评测**（`agentruntime_learning.benchmarks.longmemeval`，`fetch`、`run`、`report`）：
- 每题一个检索器，只写这一题的历史：按轮切，一轮是一条用户发言加上紧跟着的助手回复，文字前面带会话日期；`has_answer` 标出证据所在的轮。
- 检索取 top 50，算 LongMemEval 官方的检索指标：会话级的 recall_any@k、recall_all@k、ndcg_any@k（会话排名由轮的排名去重得到），轮级的 recall_any@k；k 取 5、10、30。拒答题（`_abs`）没有证据，不算，报告里记个数。
- 按题型分层抽题（`--limit`、`--seed`），同一个 `--out` 续跑，开跑前按字数估 embedding 的花费，超过 `--budget-usd` 就不跑。
- 可选 Ragas：取回的轮交给 `evals/rag.py` 已有的判分（上下文的精确率与召回率，裁判经网关）。
- 检索器沿用 `--retriever lexical | <模块>:<工厂函数>`：
  - `agentruntime_memory.eval_retriever:dev_retriever`：进程内的 MemoryService，真 embedding 经网关，测代码；
  - 新加的 `agentruntime_memory.eval_retriever:live_retriever`：对着已部署的 Memory Service（`AR_MEMORY_ADDRESS`，令牌用 `AR_DEV_SIGNING_KEY` 现签），评测租户 `evals` 下每题一个新工作区，写入后等后台处理完再查，用完逐条删掉。测的是部署。

**上线的次序**（第 4 节的“部署次序”）：
1. `deploy/data` 装 Milvus 单机版。
2. 换镜像时一起给 `external.milvus.uri`、`memory.embeddingVersion`，去掉 `external.qdrant.url` 与 `memory.imageTag`。
3. Worker 设 `AR_MEMORY_ADDRESS`。

F1 的白名单规则让 `swebench` 也有了 3-large（第 4 节说它原来没有）。上线时不能有基线评测在跑（接上记忆会改变被测系统）。

**要人定的**：
1. **验收门槛**：建议第一次上线跑 50 题（`--seed 0`）。门槛是会话级 recall_any@10 明显高于 lexical 基线；绝对数值等第一次结果出来再定。
2. **预算**：50 题的 embedding 约 575 万 token，3-large 不到 1 美元；加上 Ragas（Opus 当裁判）建议总共给 15 美元。
3. **线上验收的身份**：要在集群里用 Memory Service 的签名密钥给 `evals` 租户签令牌，跑之前要人点头。
4. **部署**：由谁、什么时候做（同事的主线负责部署）。

没覆盖：Ragas 那一步只复用已有的判分，没有单独的测试；Milvus 后端要 Docker，在服务器上跑。
