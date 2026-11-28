# 平台 MCP Server 操作单元建设计划（2026-11-28）

> 状态：草案（已规划待实施），2026-11-28。
> 定位：将平台现有核心能力抽象为标准化、可组合的 **MCP Server 操作单元（“平台遥控器”）**，使外部 AI 客户端（Claude Code、Antigravity、Codex、Cursor 等）及内部 Agent 编排器能够通过统一协议安全操控各子系统。
> 关联文档：[N-15 用户 MCP 连接契约](n15-mcp-contract.md)、`deploy/dev/mcp-test/server.py`（协议验收基线）、`coding-agent/docs/plan/coding-agent-mcp-server-2026-11-28.md`。

---

## 1. 背景与设计理念

当前平台已在 **Client / Gateway 侧** 完成较完整的 MCP 接入能力：
- Worker 侧实现了 `stdio` 与 `Streamable HTTP (2025-06-18)` 双协议 MCP Gateway（`services/py-worker/src/agentruntime_worker/tools/{mcp,mcp_http}.py`）；
- Task Service 侧实现了连接与租约治理（`services/go-task-service/internal/mcp`）；
- Coding Agent 侧实现了「我的 MCP」全栈控制台（`internal/server/mcp.go` + React 配置页）。

而在 **Server 侧（对外暴露自身能力）**，目前仅有验收桩 `deploy/dev/mcp-test/server.py`（106 行，仅含 `echo`、`list_notes`、`add_note`）。

本计划的核心思路是把每个业务子系统对应的 **MCP Server 视为平台的“遥控器与操作单元”**：
1. 单个平台能力（如提交编码任务、拉取 Diff、查询评测矩阵、检索代码符号与记忆）封装为职责单一、Schema 清晰的 MCP Tool；
2. 每个子系统独立暴露一组高内聚的 MCP Server 端点，后续可按业务演进持续扩展更多操作单元；
3. 所有 MCP Server 共用统一的协议外壳、鉴权边界、权限分级注解与幂等约束，同时可反向接入本平台「我的 MCP」实现自举联调。

---

## 2. 统一协议与安全治理规范

所有平台 MCP Server 均遵循以下基线（与 `n15-mcp-contract.md` 保持完全兼容）：

| 维度 | 规范要求 |
| --- | --- |
| 传输协议 | 线上统一采用 **Streamable HTTP（JSON-RPC 2.0，协议版本 `2025-06-18`）**，支持 `initialize`、`tools/list`、`tools/call` 与 `DELETE` 会话注销；本地开发可选支持 `stdio` 包装器。 |
| 会话管理 | `initialize` 返回 `Mcp-Session-Id` 响应头；有状态会话超期返回 `404`，无状态只读工具支持无会话直接调用。 |
| 鉴权与租户隔离 | 非公开只读服务强制校验请求头 `Authorization: Bearer <token>` 或 `X-Api-Key`；租户与用户身份严格由令牌解析，请求参数中的 `tenant_id` 仅作一致性校验；越权资源统一返回 `not_found`。 |
| 权限分级注解 | `tools/list` 必须声明 `annotations`：`readOnlyHint`（只读）、`destructiveHint`（不可逆写，如推送 PR/删除）、`idempotentHint`（是否幂等）。 |
| 幂等防重入 | 所有写操作（`write_reversible` / `write_irreversible`）读取并校验 `_meta["agentruntime/idempotency_key"]`，重复请求返回已有结果，不重复触发副作用。 |
| 输出格式 | 每次 `tools/call` 同时返回人类可读的 `content`（文本摘要）与机器可稳定解析的 `structuredContent`（强类型 JSON 对象）。 |
| 可观测性 | 每次调用写入结构化日志与 OpenTelemetry span（记录 `mcp.server`、`mcp.tool`、`tenant_id`、`duration_ms`、`status`、`idempotency_key`），**严禁记录凭据与源码正文**。 |

---

## 3. 首批规划的三大 MCP Server 操作单元

### 3.1 Coding Agent MCP Server（云端编码工作台遥控器）

- **部署端点**：`https://code.cgent.link/mcp`（归属仓库：`coding-agent`，详细实现见 `coding-agent/docs/plan/coding-agent-mcp-server-2026-11-28.md`）
- **鉴权方式**：用户级 API Token / JWT（绑定 `coding-web` 既有身份体系与租户隔离）
- **定位**：让外部客户端（Claude Code、Antigravity、Codex）无需打开 Web UI，即可直接遥控云端工作区派发异步编码任务、跟踪进度并拉取代码 Diff。

| Tool 名称 | 权限分级 | 关键入参 | 返回内容（`structuredContent`） | 底层复用模块 |
| --- | --- | --- | --- | --- |
| `list_projects` | `readOnly` | `limit?` | 项目列表（`project_id`, `name`, `repos`, `updated_at`） | `coding-agent` store |
| `list_sessions` | `readOnly` | `project_id`, `limit?` | 工作区/会话列表（`session_id`, `status`, `branch`） | `coding-agent` store |
| `create_session` | `write_reversible` | `project_id`, `name?`, `repo?` | 新建会话元数据（支持幂等键防重） | `workspaceHTTP("create_session")` |
| `submit_task` | `write_reversible` | `session_id`, `prompt`, `effect_ceiling?` | `task_id`, `status`, `stream_url` | Runtime `SubmitTask` gRPC |
| `get_task_status` | `readOnly` | `task_id` | 当前阶段、工具调用摘要、耗时、成本、交付物列表 | Runtime `GetTask` gRPC |
| `get_workspace_diff` | `readOnly` | `session_id`, `path_prefix?` | 变更文件清单与分页 `git diff` | Workspace API `GetDiff` |
| `list_workspace_files` | `readOnly` | `session_id`, `dir?` | 目录树与文件元信息 | Workspace API 文件浏览 |
| `create_draft_pr` | `write_irreversible` | `session_id`, `title`, `body`, `base_branch?` | `pr_url`, `commit_sha`（强审批 + 强幂等） | GitHub App / Workspace Push |

**典型场景**：本地在 Antigravity / Claude Code 处理主线开发时，把耗时的兼容改造或依赖升级派发到云端隔离沙箱异步执行，完成后拉回 Diff 审阅。

---

### 3.2 Evals & Benchmark MCP Server（评测看板与模型选型遥控器）

- **部署端点**：`https://evals.cgent.link/mcp`（归属仓库：`agent-runtime/services/py-learning`）
- **鉴权方式**：公开只读数据默认开放（与 `evals.cgent.link/api/*.json` 同级），并配请求频率限制与只读白名单校验；不暴露任何未公开评测集原始题干。
- **定位**：将线上评测看板转化为任意研发/评测 Agent 可直接查询的结构化决策数据源，辅助模型路由选型与版本回归比对。

| Tool 名称 | 权限分级 | 关键入参 | 返回内容（`structuredContent`） | 底层复用模块 |
| --- | --- | --- | --- | --- |
| `get_capability_matrix` | `readOnly` | `role?`, `model?` | 规划/分诊/工具调用/评审/压缩等角色下各模型得分、成本与样本覆盖 | `public_capability.py` + `public_matrix_panel.py` |
| `list_benchmark_runs` | `readOnly` | `suite?`, `limit?` | SWE-bench Verified、BFCL v4 等历史批次解决率与元数据 | `public_benchmarks.py` |
| `get_engineering_experiments` | `readOnly` | `experiment_id?` | 所有 SHA-256 校验通过的工程对照卡片（RAG、上下文、沙箱、高并发） | `public_experiments.py` + `public_engineering.py` |
| `compare_runs` | `readOnly` | `baseline_run_id`, `candidate_run_id` | 关键指标差值、共同样本数、成本与耗时对比结论 | `public_benchmarks.py` / `public_experiments.py` |
| `list_eval_datasets` | `readOnly` | `category?` | 公开评测集目录、规模与评测口径说明（不含 held-out 题干） | `site_catalog.py` |

**典型场景**：调整 Prompt、工具 Schema 或切换模型路由前，Claude Code 直接调用 `get_capability_matrix` 与 `compare_runs`，用线上实测数据验证收益与成本变化。

---

### 3.3 Code RAG & Memory MCP Server（代码知识库与记忆遥控器）

- **部署端点**：集群内 `http://ar-agent-runtime-memory:8098/mcp`（或经 Ingress 暴露独立认证子域，归属仓库：`agent-runtime/services/py-memory`）
- **鉴权方式**：租户级 API Key / JWT（强租户隔离，复用 `py-memory` 既有租户作用域与 RLS 约束）
- **定位**：将刚落地的 **PyTorch Bi-Encoder 向量化 + Cross-Encoder 重排** 与 **Milvus + BM25 + Tree-sitter** 混合检索能力封装为标准 MCP Server，供外部 IDE/Agent 复用高质量代码导航与跨会话记忆。

| Tool 名称 | 权限分级 | 关键入参 | 返回内容（`structuredContent`） | 底层复用模块 |
| --- | --- | --- | --- | --- |
| `search_code_symbol` | `readOnly` | `repo`, `query`, `top_k?` | 命中的符号定义行、文件路径、AST 节点类型与调用关系摘要 | Tree-sitter 索引 + 混合检索 |
| `search_memory` | `readOnly` | `query`, `scope?`, `limit?`, `rerank_mode?` | 按相关度精排的记忆片段、来源锚点与置信度分数 | `RetrievalEngine` + `torch_models.py` + Milvus/BM25 |
| `write_memory` | `write_reversible` | `scope`, `title`, `content`, `tags?` | `memory_id`, `embedding_version`（带幂等键防重复写入） | `py-memory` 写入管线 |
| `get_retrieval_health` | `readOnly` | 无 | 当前 embedder/reranker 后端类型（`torch` / `bm25` / `remote`）、设备与索引状态 | `wiring.py` 状态探针 |

**典型场景**：在不同 AI 工具（Claude Code、Antigravity、Cursor）间切换时，通过 `search_memory` 共享项目架构约定与排障记录，避免重复喂上下文。

---

## 4. 后续可扩展的平台 MCP Server 矩阵（远期规划）

当上述 3 个核心操作单元验证成熟后，可按同一模式继续扩展平台其他模块：
- **Sandbox Inspector MCP Server**：只读查询沙箱环境账本（`sandbox_environments`）、运行状态、网络授权审计日志；
- **Ops & Telemetry MCP Server**：封装 OpenTelemetry / VictoriaMetrics 查询，支持按 `task_id` 查调用链耗时、模型花费与失败归因；
- **Skill & Registry MCP Server**：查询与管理租户 Skill 版本、发布草稿与回滚。

---

## 5. 分阶段实施路线图

| 阶段 | 范围 | 核心交付物 | 预计依赖与风险 |
| --- | --- | --- | --- |
| **Phase 1（优先）** | **Evals MCP Server** + **Coding Agent MCP Server** | 1. `py-learning/evals/mcp_server.py` 挂载至 `dashboard_server.py` 的 `/mcp`；<br>2. `coding-agent/internal/server/mcp_server.go` 挂载至 `coding-web` 的 `/mcp`；<br>3. 完整单测与 JSON-RPC 2.0 协议合规测试。 | 底层数据与业务接口全部现成，增量开发成本低、风险可控。 |
| **Phase 2** | **Code RAG & Memory MCP Server** | 1. `py-memory` 新增 HTTP MCP 入口适配层，复用 `RetrievalEngine` 与 `torch_models.py`；<br>2. 租户鉴权、限流与检索超时保护；<br>3. 接入离线与在线回归测试。 | 需明确对外暴露域名、TLS 入口与租户令牌签发策略。 |
| **Phase 3** | **线上部署与真实客户端联调** | 1. K8s / Traefik 路由放行 `/mcp`（禁用缓冲）；<br>2. 使用 Claude Code 与 Antigravity 真实连接 `evals.cgent.link/mcp` 和 `code.cgent.link/mcp` 完成端到端操作验收；<br>3. 在 `coding-web`「我的 MCP」中自举接入 `evals.cgent.link/mcp` 验证全链路闭环。 | 需配合集群迁移进度同步更新新旧集群配置。 |

---

## 6. 验收标准

1. **协议合规**：通过 `initialize → tools/list → tools/call → DELETE` 完整握手与会话测试；未知方法返回 `-32601`，非法参数返回 `-32602`，坏 JSON 返回 `400`。
2. **安全隔离**：无令牌或错误令牌返回 `401`；租户 A 令牌查询租户 B 的项目/会话/记忆返回 `not_found`；日志与响应中无任何密钥泄漏。
3. **写操作幂等**：对 `create_session`、`submit_task`、`write_memory` 使用相同 `idempotency_key` 并发重放 10 次，仅产生 1 次实际创建/写入。
4. **真客户端联调**：Claude Code / Antigravity 成功发现全部 Tool 并完成至少 1 次只读查询与 1 次受控任务提交。
