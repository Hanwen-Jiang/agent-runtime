# 部署、Telemetry 与 CI：Helm 与 KEDA、OpenTelemetry、check.py、本地整套

> 学习笔记，2026-10-24。对应代码：Chart `deploy/helm/agent-runtime/`（Collector 配置 `files/otel-collector.yaml`）；Telemetry 的 OpenTelemetry 实现
> `libs/py-core/src/agentruntime_core/adapters/otel.py`、`libs/go-core/adapters/oteltelemetry`；检查脚本 `scripts/check.py`、CI `.github/workflows/ci.yml` 与 `nightly.yml`；
> 本地整套 `scripts/dev_stack.py`；部署测试 `tests/deploy`。设计依据：设计书 4.13、4.15，附录 J.1、K.5、L.19；细化 `docs/plan/deploy-telemetry.md`、`docs/plan/live-e2e.md`。

## 1. 部署形态（Helm Chart）

| 组件 | 形态 | 扩缩 |
| --- | --- | --- |
| task-service（Task API、Session Worker、Registry 的内部端口） | Deployment | 固定副本 |
| Model Gateway | Deployment | 固定副本 |
| Memory Service | Deployment | 固定副本 |
| Python Worker | 按任务队列分池（task、decision、model、program） | KEDA 的 Temporal 触发器，按各自队列的积压 |
| Learning Pipeline | KEDA ScaledJob | 按 trace 主题的消费延迟；同时只跑一个 Job |
| OpenTelemetry Collector | Deployment | 一路收（OTLP），三路发：Tempo、Langfuse、Redpanda |

按队列分池的原因：model 队列的 activity 等模型，program 队列跑工具，资源形态不同，分开扩缩。密钥只从 Secret 卷挂进来，Chart 里不渲染任何密钥值；容器只读根文件系统、非 root。
`helm lint --strict` 与 `helm template` 的结构检查在 `tests/deploy`（名字、端口、环境变量从代码里取来比对，因为写错这些 lint 发现不了）。真集群安装没验证过。

## 2. Telemetry

- span 按附录 J.1 与 GenAI 语义约定：Node Executor 的 `chat`、`execute_tool {工具}`；收尾写回补记 `invoke_workflow`（Task）与 `runtime.round`（Round，带 Stage 形状）；
  Model Gateway 每次调用一个 span。
- 绑定租户的客户端自动给每个 span、指标、日志加上 tenant_id 等属性，调用方传入的同名属性被覆盖（一致性测试验证）。
- 设了 `OTEL_EXPORTER_OTLP_ENDPOINT` 时发 OTLP/HTTP 给 Collector；没设时 Worker 写标准错误。Collector 配置里有脱敏规则，与应用侧一致。
- Learning Pipeline 读的就是 Collector 写进 Redpanda 的 OTLP JSON。

## 3. 检查与 CI

`scripts/check.py`（只用标准库）是本地与 CI 共用的检查：步骤 proto（buf lint、format、breaking）、contract、go、py-core、py-memory、py-learning、py-worker、e2e、faults。
pr 档是除 faults 外的全部；`--tier full` 加上故障注入套件。`--clone <目录>` 把当前提交克隆到本地再跑（仓库在网络盘上时 Temporal 的原生模块不能从网络盘加载）。
缺工具的步骤失败并写明缺什么。CI 在 PR 与 main 上分五个 job 调用同一个脚本；夜间任务跑端到端（含故障注入与越权）、Postgres 行级安全、Postgres + Qdrant 的记忆测试。

**重放是硬门槛**：go test 与 pytest 都回放 golden 事件历史，并验证调换 replay-mutation 两行后回放必须失败。

## 4. 本地拉起整套（T-13）

`scripts/dev_stack.py up` 编译 task-service 与 model-gateway，种 Registry，拉起 Temporal 开发服务、task-service、Model Gateway 与 Worker；`kill worker`、`start worker`
用来演示恢复。真模型时密钥只从本地密钥文件读、经 `dev-secret` 的标准输入进开发 SecretStore。真模型端到端用例在 `tests/e2e/live`，手动跑，每条按计量不超过 3 美元，
写一份端到端日志（SSE 事件、activity 时间线、工具调用、计量、缓存命中率）。

## 5. 面试速查

**Q：Worker 为什么按任务队列分池？**
A：不同 activity 的资源形态不同：model 队列主要在等网络、并发高；program 队列跑工具、吃 CPU 与磁盘；task 队列只跑工作流代码。分池后各自按队列积压扩缩，互不拖累。

**Q：为什么用 KEDA 而不是 HPA 看 CPU？**
A：Worker 的负载体现在 Temporal 任务队列的积压上，CPU 不一定高（大部分时间在等模型）。KEDA 的 Temporal 触发器按积压扩缩，更贴近真实负载；Learning 按 trace 主题的消费延迟起 Job。

**Q：可观测性怎么设计才能支持后面的学习？**
A：span 命名按 GenAI 语义约定，带上 Task、Round、Stage 形状、成本、Verdict 这些业务属性；Collector 一份数据三路发：链路追踪、LLM 评测平台、消息队列给 Learning Pipeline。
租户属性由绑定时加上，不能被调用方伪造，Learning 才能按租户过滤。

**Q：怎么保证测试在 CI 和本地一致？**
A：同一个检查脚本，CI 只是分 job 调用它；工具版本固定；缺工具就失败而不是悄悄跳过（本地可以显式设 AR_ALLOW_MISSING_TOOLS 跳过）。
