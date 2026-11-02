# S-01 环境契约基线（D-23 / D-24）

> 状态：候选基线，未修改 `/proto`、`/schemas` 或兼容性基线。
> 日期：2026-11-03。
> 目的：把可复用云环境、工作区绑定、个人凭据和 setup/start 的契约边界固定下来，供后续 S-01 人审与实现切片使用。

本文件只冻结“实现前要回答什么”和“首版允许做什么”。具体消息名、字段编号、数据库表名和生成代码必须在 S-01 评审后确定。权威产品行为仍以 [环境配置草案](environment-config.md) 为准，部署验收仍以 [云上部署计划](cloud-deploy.md) 为准。

## 1. 首版范围

D-23 第一阶段与 D-24 组成一个可验收的最小流程：

1. 用户创建一个仅自己可见的可复用环境，保存名称、描述、普通变量、直传值引用、联网意图和 Bash `setup` / `start`。
2. 会话入口分别选择环境和 repo / branch / 空项目；环境记录不保存仓库清单或 Git 凭据。
3. 新实例恢复工作区后按 `setup → start → 首条业务命令` 执行；setup 失败阻止首条业务命令，start 的 warn/block 行为按配置执行。
4. 普通变量和直传值遵守现有 SecretStore 边界；网络密钥仍留在代理外部，未选定真实服务、客户端和 CA 方案前不宣称支持。
5. 环境更新使用 `expected_version` 防止覆盖；日志只提供有限长度的状态和尾部，不携带密钥原值。

D-25 多仓库、D-26 缓存 / 快照、D-27 启动技能、OIDC、VPN、组织共享不作为本流程的前置依赖。D-23 第二阶段的网络密钥注入单独验收。

## 2. 契约候选

### 2.1 EnvironmentConfig

候选字段及约束：

| 字段 | 约束 |
| --- | --- |
| `id`, `version` | 服务端生成；`version` 只用于并发更新，不暴露内部存储细节 |
| `name`, `description` | 名称用于展示；长度、保留字和重复规则由 API 错误模型统一定义 |
| `visibility` | 首版只有 `PRIVATE`，所有读、改、绑操作检查 `tenant_id + user_id` |
| `env` | `.env` 输入格式解析为普通值或直传值引用；限制键名、数量和总字节数 |
| `scripts.setup`, `scripts.start` | Bash 文本，受部署上限和租户收窄上限约束；不接受工作流中传入的秘密值 |
| `scripts.start_on_failure` | `WARN` 或 `BLOCK`；setup 失败始终阻止业务命令 |
| `network.mode` | `DENY`、`ALLOWLIST` 或 `OPEN`；只能在租户策略允许范围内选择 |
| `network.allowed_hosts` | 仅用于 allowlist；按现有 host / port / 时限校验，拒绝内网、元数据和危险端口 |
| `secret_refs` | 只保存 SecretStore 引用和非敏感元数据，响应、事件、日志和工作流载荷不得回显原值 |

### 2.2 Workspace binding

工作区只保存所选环境的非敏感引用（`environment_id` 与版本）。绑定、显式应用更新和解绑必须：

- 检查租户与用户归属；
- 支持 `expected_version` 冲突；
- 不把环境配置复制成另一份业务状态；
- 不把 D-25 的 repo / layout 字段混入首版；
- 在应用到活动实例前遵守现有会话/沙箱生命周期边界。

### 2.3 Environment log

`GetEnvironmentLog` 只返回最近一次 setup/start 的步骤、退出分类、耗时和受上限约束的日志尾部。日志必须经过现有脱敏规则；secret value、完整 token、代理内部引用和宿主凭据永不进入 API、事件或 trace。

### 2.4 错误与兼容

错误至少区分：未授权、越租户、版本冲突、配置非法、脚本超时、脚本非零退出、缺少 SecretStore 引用和网络策略拒绝。旧客户端收到未知字段应继续工作；新客户端不能把未知状态当成功。契约评审前不添加新的 TaskEventType 或 Workspace proto 字段。

## 3. S-01 必经流程

任何 `/proto` 或 `/schemas` 变更都必须在独立 S-01 提交中完成：

1. 先提交验收测试和正反例，覆盖字段约束、越权、版本冲突、密钥不回显、setup/start 门禁和旧客户端兼容。
2. 修改 proto/schema 后运行 `buf format --diff`、`buf lint`、生成代码检查以及 `buf breaking --against <baseline>`；schema 变更同时运行仓库的 schema contract tests。
3. 更新兼容性基线前，提交字段编号、删除/保留规则和迁移说明，接受人工审查与另一家强模型审查。
4. 生成 Go/Python 代码后再进入实现分支；业务实现不得在同一提交里偷偷改契约。
5. 评审未通过时保留候选文档和测试，停止在运行时代码上扩张。

S-01 的最小提交边界是：EnvironmentConfig、binding、log 和错误模型所需的增量字段；D-25 repo/layout、D-26 cache key、D-23 第二阶段网络密钥透明注入均另起契约提交。

## 4. 求同存异

- **共同点**：环境和 repo 分开选择；环境可复用；普通变量采用 `.env` 输入；setup 在会话启动前运行；秘密原值不进入工作流载荷。
- **本系统保留的差异**：首版只做本人私有；环境不自动扩大 Task 网络权限；直传值与代理持有的网络密钥分开；setup 失败固定阻断，start 是否阻断由显式配置决定。
- **延后能力**：组织共享 ACL、个人通用密钥的自动注入、多层覆盖、OIDC、VPN、自然语言启动技能和 Codex 风格占位符替换。它们需要独立的身份、代理或客户端证据，不能从 `.env` 语法推导出来。

## 5. 复杂度决策记录

| 项目 | Failure | Evidence | Minimum fix | Benefit | Performance cost | Implementation cost | Operational cost | New failure surface | Release slice | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `expected_version` | 两次编辑覆盖配置 | 环境可被多个会话复用 | 单个版本字段和冲突错误 | 防止静默覆盖 | 一次条件写入 | 低 | 需要客户端重读 | 客户端处理冲突 | D-23 第一阶段 | KEEP |
| 独立 SecretStore 引用 | secret 进入工作流或日志 | 现有 SecretStore 只有读/发放能力，管理契约尚缺 | 先只保存 ref/metadata，值只在受控 activity 读取 | 缩小泄露面 | 读取时一次 provider 调用 | 中，需双语言一致性测试 | 需要轮换/撤销流程 | 引用过期或缺失 | S-01/S-02 后的 D-23 | KEEP |
| setup 结果缓存 | 重复安装增加启动时间 | 当前没有首版缓存验收，D-26 还有输入顺序冲突 | 每个新实例执行一次 setup | 简化首版语义 | 付出重复 setup 时间 | 低 | 暂无缓存失效运维 | 长 setup 影响体验 | D-24 第一阶段 | DEFER |
| 新增环境状态机 | 失败恢复语义不清 | 现有 sandbox/ledger 已有生命周期状态 | 复用现有 activity 结果和有限日志 | 避免第二套状态源 | 无新增轮询 | 低 | 无新表/worker | 状态分裂 | D-24 第一阶段 | REJECT |
| 网络密钥透明注入 | 脚本需要调用第三方但平台不能见值 | 现有 CONNECT 代理不能改 HTTPS 头，真实客户端/CA 未选 | 先冻结首个服务和客户端，再做独立 phase 2 | 满足高价值第三方集成 | 代理每请求处理 | 高，含代理、SecretStore、CA 和跨语言测试 | 证书、审计、轮换 | 注入误用或客户端不兼容 | D-23 第二阶段 | DEFER |

## 6. 通过门槛

可执行的首版验收清单保存在 [`tests/contract/fixtures/environment_contract_cases.json`](../../tests/contract/fixtures/environment_contract_cases.json)，对应的结构与基线锚点检查在 `tests/contract/test_environment_contract_cases.py`。这些用例只冻结评审输入，不代表运行时契约已经批准或实现。

S-01 候选基线可以进入人工评审的条件：

- 字段、错误和归属边界与 `environment-config.md` 一致；
- 测试清单覆盖首版流程和越权/泄露失败；
- 没有把 D-25、D-26 或网络密钥 phase 2 偷渡成首版依赖；
- proto/schema 变更清单、兼容性基线计划和生成代码路径明确；
- 任何实现分支都能在不修改权威设计书的前提下落地。

相关前置审查包：模型预算、R-02/R-03、R-04/R-06、Task Plan、N-19/N-20、Firecracker、用户 Skill、D-25 多仓库、Workspace/会话、N-15/N-16 和 N-18 文件交付，均在 `docs/plan/` 中以独立候选基线和 `tests/contract/` 正反例维护；它们不绕过本节的人工审查门槛。

未满足上述条件时，继续补文档或验收测试，不在业务代码中创建并行契约。
