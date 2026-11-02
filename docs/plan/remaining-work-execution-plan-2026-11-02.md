# 未完成计划执行表（2026-11-02）

> 基线：远端 `main` `8ee00fe`。本表把现有设计计划投影成可交付批次；不修改权威设计书，不把真实基础设施验收伪装成代码完成。

## 发布原则

1. 契约变更先写正反验收用例，再走 S-01：`buf format`、`buf lint`、生成代码检查、`buf breaking`、人工评审和基线更新。
2. Task Loop / Stage Runner / Node Executor 仍遵守单写者；Provider、评测、部署脚本和文档可以并行。
3. 每个批次必须有可重复的本地验证；需要 Go、Docker、Kata、真实云服务或供应商额度时，只报告阻塞，不把静态检查当线上验收。
4. 不新增第二套环境状态机、缓存表、跨系统 generation/hash 或后台轮询；优先复用现有 ledger、Task State、Workspace 和 SecretStore 生命周期。

## 批次

| 批次 | 范围 | 依赖 | 交付与验收 | 当前决策 |
| --- | --- | --- | --- | --- |
| P0-A | S-01/S-02 审查包 | 无 | 契约正反例、字段编号清单、兼容性说明、buf/schema 检查命令；人工验收基线已冻结，生成代码仍按工作项验收 | **契约与设计已批准**，实现逐项放行 |
| P0-B | D-23/D-24 首版 | P0-A | 私有环境 CRUD、repo 解耦绑定、`.env` 普通值/引用、setup→start 门禁、有限日志；租户/用户隔离与密钥不回显测试 | 契约已批准；先做 Runtime EnvRecord，再接 coding-agent |
| P0-C | 开放注册预算 | P0-A、Model Gateway 计量 | `model_budget.daily_usd/monthly_usd`、`tenant_budget_exceeded`、Task 准入与网关调用双重检查、跨日/月边界测试；默认免费模型白名单已在 `2b6f655` 合并，云上 `values-cloud.yaml` 已在 `bdabf91` 显式启用内置 ConfigMap；字段与 `$3/$20` seed 已在 `ad0aa6a` 合并 | 配置切片完成；账本预留与双重 enforcement 等权威用量 API |
| P1-A | R-02/R-03 剩余缺口 | 现有运行时代码 | 单次网关预算硬限制、多供应商/限流边界、模型多因素选档、Activity ContextBuilder；Goal 输入、精确 state、`context_read` 与对象按需取回已在主线实现；R-02/R-03 前置契约包已合并 | 路由元数据/权重与 Activity payload 需单独工作项；不把 `price_at` 当发布时间 |
| P1-B | R-04/R-05/R-06 剩余缺口 | SecretStore/Ingress/Registry 当前接口 | 生产 SecretStore 适配边界、工具输出脱敏、Learning 成本与版本固定；R-04/R-06 前置契约包已合并；R-05 的 round/stage/node/tool/human/verdict/SSE 续传与限流已有，`token_delta`/`thinking_delta` 仍依赖流式模型通道 | 契约已批准，按模块实现；真实 SecretStore/StreamGenerate 仍需资源 |
| P1-C | Workspace 与会话 UI | coding-agent 仓库、Runtime EnvironmentService | Workspace API 文件树/read/log/discard/copy、`/env` 状态投影、旧 SSE 兼容测试；D-06 前置验收包已合并；当前 coding-agent 没有 EnvRecord/ledger 查询 API，不能安全推断 ready/reused | 跨仓库独立合并，先补 Runtime 契约 |
| P1-D | Memory-live 部署 | Chart/镜像/模型 Profile | Milvus、真 embedding、Worker→Memory gRPC、租户过滤与部署 smoke；真实集群验收 | 代码先行，资源可用后验收 |
| P2-A | D-19～D-22/D-26 | P0-B、SandboxBroker | Firecracker/vsock/rootfs、快照顺序冲突决议；不在首版环境配置前引入缓存；已合并前置验收包 | 契约已批准；按 Sandbox 后端逐项实现，缺 KVM 标记 `blocked_external` |
| P2-B | N-04/N-19/N-20 | S-01、coding-agent | Task Plan/`plan_updated`、会话快照+增量、首事件延迟分段；前置验收包已合并，token/thinking delta 仍依赖 StreamGenerate | 契约已批准；运行时和流式通道逐项实现 |
| P2-C | N-14～N-16/N-18 | Registry/SecretStore/S-01 | 用户 Skill、用户 MCP、多 Agent 协作、文件型交付；N-14/D-27、D-25、N-15/N-16、N-18 已合并前置验收包 | 契约已批准；依赖 Registry/SecretStore/对象存储实现 |
| ENV | D-02～D-05/D-07/D-09～D-17 | 真实主机/域名/凭据 | 真 Postgres/Temporal/Qdrant/Redpanda/Langfuse/R2/Cloudflare/Kata/DNS/TLS/BuildKit/包源/Grafana/Tempo | 资源未提供前只做脚本和 kind gate |

评测分支 `codex/evals-design-closeout` 当前包含会删除主线评测集、bridge、契约包和部署文档的大范围重构，不能作为合并候选；主线已有的 `fe28432` N-11～N-13 实现继续作为唯一基线。

## 完成定义

- 代码批次：验收测试通过、`git diff --check` 通过、提交已推送并合并远端 `main`。
- 契约批次：人工评审、buf/schema 检查和兼容性基线完成后，才允许生成代码和业务实现。
- 真实环境批次：记录环境、时间、镜像/配置版本和可复现命令；没有真实资源时标记 `blocked_external`。
- 设计草案：只在计划文档中记录边界、Failure/Evidence/Minimum fix/成本/Decision，不创建未被需求支持的持久状态。

## 第一轮并行任务

1. P0-A：1～112 人工验收已由 [人工验收基线](manual-acceptance-baseline-2026-11-03.md) 冻结；默认免费模型白名单、云上 Helm 接线、模型预算字段/默认额度和严格租户配置解析已合并。
2. P1-A：R-01 Hook 有限重试、R-05 进度事件/SSE 续传与限流已有；R-02/R-03 的 Goal 输入、精确 state、`context_read`、对象按需取回已有；下一步分别是权威预算账本、路由元数据和 Activity ContextBuilder。
3. P1-D：Memory-live 的 wiring 与 Helm smoke 已有；本轮只保留真实集群验收，不重复造第二套配置。
4. ENV：已合并 D-02～D-17 gate 矩阵；下一步按资源可用性执行并记录 `blocked_external`。

第一轮完成后，再按单写者边界进入 P0-B/P0-C；任何 proto/schema 变更都暂停在人工评审点。

## 2026-11-03 执行检查点

- 远端 `main`：`8ee00fe`；人工验收基线、模型预算字段/默认额度、严格租户配置解析和工作项门禁已推送并合并，没有未提交改动。
- 新增契约包定向验证：`uv run pytest -q` 加载环境、预算、Task Plan、N-19/N-20、Firecracker、D-25、Workspace、N-15/N-16、N-18、R-02/R-03、R-04/R-06 共 **24 passed**；`git diff --check` 通过。
- 本轮白名单验证：`uv run pytest -q tests/deploy/test_default_tenant_policy.py tests/contract/test_terminology.py` 为 **4 passed**；Helm 渲染测试因本机未安装 `helm` 按 `AR_ALLOW_MISSING_TOOLS=1` 为 **3 skipped**。默认 values 保持关闭，`values-cloud.yaml` 显式开启，外部 ConfigMap 优先。
- 本轮人工验收后策略切片验证：模型预算契约、默认 seed、租户配置未知字段拒绝和术语检查为 **6 passed**；Go/gofmt 未安装，Go 编译/单测与原子预算 enforcement 未宣称完成。
- 全量 `tests/contract` 在已同步 Python 依赖的 `.venv` 中，以 `AR_ALLOW_MISSING_TOOLS=1` 运行结果为 **485 passed、40 skipped**；跳过项集中在缺少 `go`/`buf`，未发现本轮改动引入的失败。未设置该变量时，工具缺失会按测试约定报告失败；真实 Temporal/Postgres/Qdrant/Docker/Kind/Kubernetes 仍需外部资源验收。
- `scripts/check.py` 已完成依赖同步；其余 proto、Go、部署和真实环境 gate 继续按 `blocked_external` 记录，不能用本地静态结果替代。
