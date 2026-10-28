# Helm、KEDA 与 Telemetry（中档工作项细化，TMP 计划 T-15）

> 实施计划第 7 节的两个中档工作项“Telemetry 打点与 Collector 配置”（阶段 1，中 · 低）与“Helm 与 KEDA”（贯穿，中 · 低），都没有强档评审项。
> 依据：设计书 2.3、3.5、3.6（并发）、4.13、4.15、附录 J.1、K.1（可观测性一行）、K.5、L.14、L.19；`libs/*/providers/telemetry`。
> 状态：已实现（2026-10-23）。第 4 节的决定与第 5 节的验收测试经人确认；实现中的出入见第 7 节。

## 1. 目标

TMP 计划的“完成”只要求 `helm lint` 通过、`helm template` 出合法清单，不要求真部署；Telemetry 的打点与 Collector 配置补齐。拆成三块：

| 块 | 现在 | 做完之后 |
| --- | --- | --- |
| Telemetry 实现 | 只有控制台实现（Go `dev/consoletelemetry`、Python `dev/telemetry`），写标准错误 | 加 OpenTelemetry 实现：经 OTLP/HTTP 发给 Collector；同一套一致性测试 |
| 打点（附录 J.1） | 只有 `node.execute`、Policy Engine 的 Hook span 与收尾写回的指标；Model Gateway 只写计量行 | span 按 J.1 与 GenAI 语义约定命名，带上 Temporal 的 WorkflowID、RunID；收尾写回补 Task 与 Round 两级 span；Model Gateway 每次调用一个模型调用 span |
| 部署 | `deploy/` 只有 README 与开发用的 Model Profile | Helm Chart：各服务、按队列分池的 Worker、KEDA 扩缩、OpenTelemetry Collector 与它的三路导出 |

## 2. 第一期范围

**做**

1. **OpenTelemetry 实现**（Python `agentruntime_core.adapters.otel`，Go `libs/go-core/adapters/oteltelemetry`）：
   - 语义与控制台实现完全相同（租户属性由绑定加上、不能伪造；敏感键与 Secret 值脱敏；traceparent 接上游；异常记 error；导出失败只在 flush 体现、只能是 transient）。
     脱敏与保留属性的规则抽成共用函数，两个实现调同一份，不各写一遍。
   - span、指标、日志三类都走 OTLP/HTTP（protobuf）发到同一个 Collector 地址：`/v1/traces`、`/v1/metrics`、`/v1/logs`。
     用 HTTP 而不是 gRPC：与 Collector 之后转给 Langfuse 的协议一致（L.14），Python 端也少一个 grpc 导出器。
   - 资源属性：`service.name`（task-service、model-gateway、worker、memory、learning）、`service.version`、`deployment.environment`。
   - 指标：`count` 是 Int64 counter，`observe` 是 histogram（显式边界用 SDK 默认）；按 delta 时间性导出，flush 时强制导出一次。
   - 选择：设了标准环境变量 `OTEL_EXPORTER_OTLP_ENDPOINT` 就用 OpenTelemetry 实现，否则仍用控制台实现（本地开发与现有测试不变）。
     Worker、Memory Service、task-service、Model Gateway 四个进程都按这一条组装。
2. **打点（J.1）**。只改 activity 与服务端代码，不改工作流代码（不改命令序列，不需要版本分支）：

   | 层级 | span 名 | 关键属性 | 在哪里打 |
   | --- | --- | --- | --- |
   | Task | `invoke_workflow` | tenant_id、session_id、task_id、`temporal.workflow_id`、`temporal.run_id`、recipe、`runtime.task_family`、`runtime.task_status`、`runtime.cost_usd`、`runtime.rounds` | 收尾写回（finalize activity），事后补记 |
   | Round | `runtime.round`（`invoke_workflow` 的子 span） | round_seq、triage_level、verdict、`runtime.cost_usd`、`runtime.stage_shape`（见下） | 同上，每个 Round 一个 |
   | model 节点 | `chat` | `gen_ai.operation.name=chat`、`gen_ai.response.model`、`gen_ai.usage.input_tokens`、`gen_ai.usage.output_tokens`、role、template、reasoning、quality、tier、`runtime.cost_usd` | Node Executor |
   | program 节点 | `execute_tool {工具名}` | `gen_ai.operation.name=execute_tool`、`gen_ai.tool.name`、effect、idempotency_key | Node Executor |
   | Model Gateway 的一次调用 | `chat {模型}` | `gen_ai.operation.name=chat`、`gen_ai.provider.name`、`gen_ai.request.model`、`gen_ai.response.model`、tokens、tier、`runtime.cost_usd`、task_id、node_id | Model Gateway 的 Generate 与 StreamGenerate |

   - 每个 activity 里的 span 都带 `temporal.workflow_id`、`temporal.run_id`（K.1“WorkflowID、RunID 注入 trace 上下文”）。
   - `runtime.stage_shape` 是 Stage 的形状（JSON 字符串）：每个 Node 的 id、kind、template、after、tier、mode、reasoning、quality，
     以及 Stage 的 goal、exit_when；**不含** inputs、rationale、结果。T-16 从它归纳 Recipe 的 Stage 划分。
   - 不打 Prompt 与模型输出的原文（GenAI 约定里的 `gen_ai.input.messages` 等是可选的；原文进 trace 等于 PII 进第三方后端，见第 4 节第 4 条）。
3. **Collector 配置**（`deploy/helm/agent-runtime/files/otel-collector.yaml`，Helm 用 `.Files.Get` 装进 ConfigMap，本地也能直接拿去跑）：
   - 接收：OTLP gRPC 4317 与 HTTP 4318。
   - 处理：`memory_limiter`、`batch`；`attributes/redact` 按与应用端相同的敏感键模式删除属性（纵深防御，应用端已经脱敏）。
   - 三路导出（4.13）：全部 span → Grafana Tempo（`otlp/tempo`，gRPC）；语义 span（带 `gen_ai.operation.name` 或名字以 `runtime.`、`invoke_` 开头）→
     Langfuse（`otlphttp/langfuse`，`/api/public/otel`，Basic 认证取自环境变量）与 Redpanda（`kafka` 导出器，主题 `telemetry.spans`，`otlp_json` 编码，供 Learning Pipeline 消费）。
   - 指标与日志：设计书没有指定后端，第一期导出到 `debug`（见第 4 节第 3 条）。
4. **Helm Chart**（`deploy/helm/agent-runtime`）：
   - task-service：一个 Deployment，三个端口（Task API、Registry 内部端口、`/healthz` 与 Task API 同端口）；Registry 端口只有集群内的 Service，没有对外入口。
     Go 的 Session Worker 在这个进程里（session 队列）。
   - Model Gateway、Memory Service：各一个 Deployment 与 Service。
   - Worker 按任务队列分池（3.6、K.5）：`task`（工作流）、`decision`、`model`、`program` 各一个 Deployment，同一镜像，`--queues` 不同。
   - KEDA：每个 Worker 池一个 ScaledObject，`temporal` 触发器，队列名与池一致，工作流池按 workflow 积压、其余按 activity 积压；
     最小、最大副本数与目标积压在 values 里按池配。Learning Pipeline 是 KEDA ScaledJob，`kafka` 触发器按消费组 `learning` 在 `telemetry.spans` 上的延迟起 Job（3.5）。
   - OpenTelemetry Collector：Deployment、Service、ConfigMap；各服务的 `OTEL_EXPORTER_OTLP_ENDPOINT` 指向它。
   - 密钥：values 里只写已有 Secret 的名字，不写值；SecretStore 的文件（`AR_SECRETS_FILE`）、开发签名密钥、Langfuse 的认证都从 Secret 挂载或引用。
   - 公共：非 root、只读根文件系统、去掉全部 capabilities、资源请求与上限必填；有 `/healthz` 的服务配就绪与存活探针。
   - 第三方组件（Temporal、Postgres、Qdrant、Valkey、Redpanda、Langfuse、Tempo）不打进 Chart，只在 values 的 `external` 里填地址。
5. **测试**：`helm lint`；`helm template` 用默认 values 与一份生产样例 values 各渲染一次，对渲染结果做结构检查（第 5 节）。

**不做（登记到后续）**

- 镜像与 Dockerfile：仓库里还没有，Chart 里镜像名是占位；真部署前补。
- 第三方组件的 Chart 依赖（subchart）：要联网拉 Chart 仓库，`helm lint` 不应依赖网络。
- ~~Learning Pipeline 消费 Redpanda~~：2026-10-28 起 Job 直接消费 `telemetry.spans`（用 Kafka 客户端读 Collector 写的主题，不经 EventStream 接口），见 `learning-pipeline.md` 第 7 节。EventStream 按租户的 Kafka 适配器仍属“存储类 Provider 的生产实现”，没做。
- offline 队列的 Worker 池：现在没有任何工作流或 activity 用 offline 队列（Learning 第一期不是 Temporal 工作流），等 T-16 之后有了再加。
- deliver 节点的 token 增量、思考与工具状态的推流 span（附录 K.1；T-11b 登记的后续）。
- Planner、Reviewer 决策调用的 span（J.1 第三行）：它们经 Model Gateway，Gateway 的 `chat` span 已经带 tier、tokens、cost；
  Worker 一侧再包一层要改 Planner、Reviewer 的 activity，第一期不做。
- sub_agent 的 `invoke_agent`：sub_agent 是子工作流，span 要在工作流里打，需要 Temporal 的 OpenTelemetry 拦截器，单独排期。
- 指标与日志的后端（Prometheus、Loki 一类）：设计书没有选型。

## 3. 难点与防错

- **脱敏不能只靠 Collector**：应用端实现必须先脱敏（一致性测试覆盖），Collector 的 `attributes/redact` 只是第二道。两边的敏感键模式要一致：测试读 Collector 配置里的正则，和应用端的列表逐个比对。
  例外是 `token`：GenAI 约定的 `gen_ai.usage.input_tokens`、`gen_ai.usage.output_tokens` 键名含 token。应用端对这两个键的**整数值**不脱敏（Telemetry 语义的补充，
  两边的一致性测试各加一条），其余含 token 的键照旧；Collector 的删除规则（RE2 没有否定前瞻）不含 token，这一项只靠应用端。
- **导出失败不影响业务**：OTLP 导出器的重试与超时放在后台；Collector 不可达时 `start_span`、`count` 照常返回，只有 flush 报 transient，且 flush 的超时有上限（不能卡住 activity）。
- **租户属性**：OpenTelemetry 实现与控制台实现一样，调用方传入的 tenant_id 等一律忽略；资源属性里不放租户（一个进程服务多个租户）。
- **事后补记的 span**：`invoke_workflow` 与 `runtime.round` 在收尾写回时补记，时间戳不是 Task 的真实起止；属性里的数字（cost、rounds）才是 Learning 用的。
  在 span 上标 `runtime.synthetic=true`，看 Tempo 的人不会误读耗时。
- **形状不带用户数据**：`runtime.stage_shape` 只留结构字段；测试构造带 inputs、rationale 的 Stage，断言它们不出现在任何导出的记录里。
- **工作流代码不动**：J.1 的 Task、Round span 都在 activity 里打；工作流里不读时间、不做 I/O，重放测试照旧。
- **Chart 与代码的名字一致**：队列名、环境变量名、端口在代码里有定义，Chart 里写错不会被 `helm lint` 发现；测试从代码里读出这些名字，与渲染结果比对。
- **Registry 端口不外露**：Registry 的写方法只有发布令牌保护，端口不能进 Ingress 或 LoadBalancer。

## 4. 待人确认的决定

1. **新依赖**（设计书 4.13 指定 OpenTelemetry，但 4.15 没有列出具体的包）：
   - Python：`opentelemetry-api`、`opentelemetry-sdk`、`opentelemetry-exporter-otlp-proto-http`（只给 `libs/py-core` 的可选 extra `otel`）；
     测试用的假 Collector 解码 OTLP 用 `opentelemetry-proto`（导出器已经依赖它）。
   - Go：`go.opentelemetry.io/otel/sdk`（已在 go.sum，Temporal 间接依赖）、`go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp`、
     `.../otlpmetric/otlpmetrichttp`、`.../otlplog/otlploghttp`、`go.opentelemetry.io/otel/sdk/log`，以及测试解码用的 `go.opentelemetry.io/proto/otlp`。
     只给 `libs/go-core`。
   - 部署测试：`pyyaml`（读 `helm template` 的输出与 Collector 配置），只给新的测试包。
2. **新目录 `/tests/deploy`**（CLAUDE.md：不在 4.15 里的目录要人确认）：uv 工作区成员 `agentruntime-deploy-tests`，放 Chart 与 Collector 配置的结构检查。
   放在 `deploy/` 下也可以，但仓库的测试都在 `tests/` 或各包的 `tests/` 里。
3. **指标与日志导出到 `debug`**：设计书只给了 span 的去向。第一期 Collector 的 metrics、logs 管道只有 `debug` 导出器，后端选型登记到风险与开放问题。
4. **Prompt 与输出原文不进 trace**：Langfuse 的价值之一是看提示与输出（L.14），但原文含用户数据，进第三方后端要先过 PII 与租户隔离的评审；第一期只记结构与计量。
5. **Tempo 收全部 span**：设计书说“基础设施的 span 送到 Tempo”；第一期 Tempo 收全部 span（语义 span 也在调用链里，排查时要看到），Langfuse 与 Redpanda 只收语义 span。

## 5. 验收（测试已写，待人确认）

| 验收 | 测试 |
| --- | --- |
| Python 的 OpenTelemetry 实现过 Telemetry 一致性测试：记录经真的 OTLP/HTTP 导出器发到假 Collector 再解码检查；Collector 返回 503 时 flush 为 transient、业务调用不受影响；flush 超时有上限；资源属性有 service.name、没有租户 | `libs/py-core/tests/adapters/test_otel_telemetry.py` |
| Go 的 OpenTelemetry 实现过同一套一致性测试（`telemetrytest.Run`），同样经假 Collector；资源属性 | `libs/go-core/adapters/oteltelemetry/oteltelemetry_test.go` |
| 两边的敏感键列表与 Collector 的删除规则一致（token 除外，见第 3 节）；GenAI 的 token 计数在两个实现里都不脱敏，其他含 token 的键照旧 | `tests/deploy/test_collector_config.py`；一致性测试新增的 `GenAIUsageCountsAreKept` / `test_genai_usage_counts_are_kept`（控制台与 OpenTelemetry 实现都跑） |
| 按 `OTEL_EXPORTER_OTLP_ENDPOINT` 选实现：设了用 OpenTelemetry，没设用控制台 | `libs/py-core/tests/adapters/test_otel_telemetry.py`（`telemetry_from_env`）、`oteltelemetry_test.go`（`FromEnv`） |
| Node Executor：model 节点 `chat` span 的 GenAI 属性与 tokens；program 节点 `execute_tool {工具}` span 的工具名、effect、幂等键；两者都带 WorkflowID、RunID；rationale 与 inputs 不出现在导出记录里 | `services/py-worker/tests/test_trace_spans.py` |
| 收尾写回：一个 `invoke_workflow` span 带 J.1 的属性与 `runtime.synthetic`；每个 Round 一个 `runtime.round` 子 span，带 verdict、cost 与 `runtime.stage_shape`；形状里没有 inputs 与 rationale；cancelled 与 failed 的 Task 也记 | 同上 |
| Model Gateway：Generate 成功与失败各一个 `chat {模型}` span，带租户、GenAI 属性、tier、cost、task_id；消息与输出原文、密钥不出现 | `services/go-model-gateway/internal/gateway/telemetry_test.go` |
| Chart：默认 values 与生产样例 values 都能 `helm lint`、`helm template` | `tests/deploy/test_helm_chart.py` |
| Chart 与代码一致：每个 Worker 池的 `--queues` 是 `agentruntime_worker.names` 里的队列；每个池恰好一个 ScaledObject，触发器是 temporal、队列名一致、工作流池按 workflow 积压；环境变量名都在各服务的配置代码里出现过；Registry 端口不等于 Task API 端口 | 同上 |
| Chart 的安全约束：Registry 端口只有 ClusterIP Service、不进 Ingress；清单里没有任何 Secret 的明文（values 里填了样例密钥值也不渲染进去，只引用 Secret 名）；每个容器非 root、只读根文件系统、去掉全部 capabilities、有资源请求与上限；有 `/healthz` 的服务有探针 | 同上 |
| Learning 的 ScaledJob：kafka 触发器、主题 `telemetry.spans`、消费组 `learning`；各服务的 `OTEL_EXPORTER_OTLP_ENDPOINT` 指向 Collector 的 Service | 同上 |
| Collector：三路导出（Tempo 是 otlp、Langfuse 是 otlphttp 且路径 `/api/public/otel`、Redpanda 是 kafka 且 `otlp_json`）；Langfuse 与 kafka 的管道过滤只留语义 span；每个管道先 memory_limiter 后 batch、都经过 redact；认证取自环境变量不写死；用 Collector 自己的 `validate` 校验（有 `otelcol-contrib` 时） | `tests/deploy/test_collector_config.py` |

## 6. 工具链

测试要用到、本机现在没有的：

| 工具 | 版本 | 装到 | 用途 |
| --- | --- | --- | --- |
| Helm | 3.19 或更新（Helm 4 也可以） | `%USERPROFILE%\.local\bin\helm.exe` | `helm lint`、`helm template`；缺时 `AR_ALLOW_MISSING_TOOLS=1` 跳过 |
| otelcol-contrib | 与 Chart 里的 Collector 镜像同一版本 | `%USERPROFILE%\.local\bin\otelcol-contrib.exe` | 可选：`otelcol-contrib validate` 校验 Collector 配置，没有就跳过这一条 |

## 7. 实现记录（2026-10-23）

**依赖（第 4 节第 1 条）**：Python `opentelemetry-api`、`-sdk`、`-exporter-otlp-proto-http` 1.45.0（py-core 的 extra `otel`，Worker 依赖它）；
Go `go.opentelemetry.io/otel` 系列 v1.44.0（与 go.sum 里 Temporal 间接依赖的版本一致）、`otel/log`、`sdk/log`、`otlploghttp` v0.20.0、`proto/otlp` v1.10.0
（`go work sync` 后各模块 tidy；grpc-gateway 随之从 v2.22.0 升到 v2.29.0）；`pyyaml` 6.0.3（只给 `tests/deploy`）。

**与第 2 节的出入**

1. **Model Gateway 不在每次调用后 flush**：OpenTelemetry 实现的 flush 是同步导出，每次 RPC 都 flush 等于放弃批量导出。
   网关只结束 span，由 SDK 后台按批导出，关闭时导出剩下的；测试支架（`telemetry_test.go` 的 `flushed`）自己 flush。
   没设 `OTEL_EXPORTER_OTLP_ENDPOINT` 时网关不记 span（控制台实现要调用方 flush，否则记录只进内存）。
2. **只组装用到 Telemetry 的进程**：Worker（`agentruntime_worker.telemetry.worker_telemetry`，设了地址时整个进程共用一个）与 Model Gateway。
   task-service 与 Memory Service 现在没有任何打点，J.1 也没有它们的 span，不组装；Chart 照样给它们设 `OTEL_EXPORTER_OTLP_ENDPOINT`，以后加打点不用改部署。
3. **span 名**：model 节点输出为 embed 时是 `embeddings`；Model Gateway 的 Score、Classify 记 `chat`，Embed 记 `embeddings`。
   网关的 span 名按 GenAI 约定用请求的模型（选档的第一个候选），实际服务的模型在 `gen_ai.response.model`。
4. **Python 的 flush**：SDK 的批处理器 `force_flush` 不理会超时，实现里三个信号并发 flush，整体等待不超过导出超时加 0.5 秒（默认导出超时 2 秒），
   超时也报 transient。Collector 不可达时 Worker 每个 Node 结束多等这么久（Node Executor 原本就在每个 Node 结束时 flush）。
5. **Chart 里 Learning 的 ScaledJob**：Job 的参数是固定的租户与任务族（values 的 `learning.tenant`、`learning.family`），
   读 `learning.tracesClaim` 卷上的 OTLP JSON 文件；多个任务族要多份 values 或以后改成按任务族发现。

**测试支架的改动**（不改验收）：`test_trace_spans.py` 的控制台输出从 `open(os.devnull)` 换成 `io.StringIO()`（Windows 的默认编码写不了中文）；
`telemetry_test.go` 改为自己 flush（第 1 条）。

**本机补测（2026-10-23）**：装上 Qdrant 1.19.1、buf 1.73.0、otelcol-contrib 0.135.0 后，不设 `AR_ALLOW_MISSING_TOOLS` 重跑：
py-core（Postgres + Qdrant）671 通过、py-memory 133 通过、Go 全部模块（含 Postgres 行级安全，没有跳过）、契约测试加 `tests/deploy`
（含 `otelcol-contrib validate`）449 通过、故障注入套件 35 通过。CI 的契约 job 起初没装 otelcol-contrib，`test_collector_validates_its_own_config`
失败，已由 e7c37e4 在 CI 里装上（T-14）。

**没验证的**：Chart 没有在真集群上装过，KEDA 的 temporal 触发器（KEDA 2.17 起）与 kafka 触发器的参数只按文档写；镜像与 Dockerfile 还没有。
