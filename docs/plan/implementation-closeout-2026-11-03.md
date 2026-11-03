# 推送收尾与缺口（2026-11-03）

按用户要求先推送代码，再写本文并单独推送文档。本记录接续 [上一暂停点](implementation-pause-checkpoint-2026-11-03.md)，不修改冻结设计或人工验收决策。

## 本轮已推送代码

| 仓库 / 位置 | 提交 | 内容 |
| --- | --- | --- |
| agent-runtime main | `f74a530`、`e15b58d` | EnvironmentService Get/List、认证与限流、租户隔离只读查询、health/reuse 事实、Broker 与后端健康接线，以及 task-service 主程序挂载 |
| coding-agent main | `a319601` | 环境配置校验接入中英文消息，包括 `.env` 格式、机密名称和 UTF-8 字节上限错误 |
| coding-agent 草稿分支 | `4e6b216` | [codex/env-config-ui-draft](https://github.com/Hanwen-Jiang/coding-agent/tree/codex/env-config-ui-draft)：界面浏览器验收草稿，未合入 main；对应界面实现尚未完成，不能计作验收通过（之后界面在 coding-agent main 的 `b417f4e` 实现，草稿的浏览器验收随之调整后通过，见“剩余缺口”） |

本轮没有新增 N-18 源码或 coding-agent `/env` 后端改动。临时 Go modfile 与工具缓存仅在本地忽略；原始共享检出中的既有未提交改动未参与本轮操作。

## 已执行的验证

- WSL Debian、Go 1.26.5、PostgreSQL 17.10：`go test -count=1 ./services/go-task-service/internal/environmentapi ./services/go-task-service/cmd/task-service` 通过。Environment API 覆盖 SQLite/PostgreSQL、认证/租户边界、只读事务、未知/过期状态、Session/Workspace 过滤、数量上限与表缺失错误。Go API 测试使用最小表夹具，未证明 Python 迁移到 Go 查询的完整跨语言链路。
- buf 1.73.0：lint、format、breaking 全部通过；此次收尾未执行重新生成后的工作树一致性比较。
- coding-agent 环境配置归一化、英文错误、删除墓碑和 CRUD/Session 绑定共 4 个测试通过；字节上限用多字节中文输入验证。浏览器测试草稿参与编译，未运行验收。
- Worker 状态/会话定向回归由实现分支报告 9 项通过；本轮未把真实 K8s/Cloudflare 健康检查验收写成已通过。
- 提交前 `git diff --check` 通过。GitHub Actions 上一次任务因账户付款/支出额度问题未启动，不能声称远端 CI 全绿；需要在额度恢复后运行。

## 剩余缺口

| 范围 | 当前缺口 |
| --- | --- |
| ENV-01 跨语言与后端验收 | Python ledger migration 206 → Go EnvironmentService 的同库链路尚未做真实联验；真实 K8s/Cloudflare 创建与复借健康检查、完整 Provider 回归和生成代码一致性仍需补验 |
| coding-agent `/env` | **之后已完成**（coding-agent `4970480`）：改读 EnvironmentService 的 `ListEnvironments`（按工作副本与 Runtime Session 限定），不再调 Workspace API；ready、reused、到期原样投影，没有 Runtime Session、服务连不上或读不到时报 `unknown` / `blocked_external`。剩下的只有 ENV-02：会话选定的环境经接缝 `environmentHandoff` 交给 Runtime，契约定下前不发请求，会话记 `environment_handoff=pending` |
| 环境配置界面 | **之后已完成**（coding-agent `b417f4e`、`f131e18`）：列表、新建、编辑、删除，新会话选环境，右栏显示 Runtime 的环境记录，前端按与服务端相同的 UTF-8 字节上限先挡，中英文齐全；草稿 `4e6b216` 的浏览器验收调了两处后通过。真正交给 Runtime 执行同样等 ENV-02 |
| 云环境执行链 | 配置尚未贯通 Runtime 的 setup/start 执行、失败门禁、普通 `.env` 注入、网络策略及 SecretStore/代理引用；按“脚本内容 + 基础镜像”命中的 setup 缓存尚未落地 |
| N-18 文件交付 | 仍只有内部 FileDelivery helper；受控发布工具、Task artifacts 收集、认证 resolve/download 与浏览器下载尚未实现；对象生命周期部署接线尚未完成 |
| 模型与上下文 | 多因素路由缺有来源的能力/可靠性实测矩阵；ContextBuilder 有效窗口尚未动态接入 Model Profile；供应商真实调用、预算账单与线上发布验收未执行 |
| 其余已批准工作 | R-04/R-06、流式事件、Skill/MCP、多 Agent、Sandbox 后端及真实部署验收继续以 [现有执行表](remaining-work-execution-plan-2026-11-02.md) 的未闭合范围为准；本轮未对这些模块重新全面审计。Workspace UI 之后已在 coding-agent `8e6c557`、`96219d2` 完成（复制会话、拉取基线分支、文件树标出改动、提交记录标出本会话的提交） |

## ENV-01 最小设计决策

| 字段 | 决策依据 |
| --- | --- |
| Failure | 仅凭 state、Pod 或命令成功会误报健康；普通 touch 无法证明同会话实际复借 |
| Evidence | 人工验收 Q99–105 明确要求健康确认、同 Session/Workspace 复用和 ledger 明确期限 |
| Minimum fix | 在既有 sandbox_envs 加 health_confirmed_at/reused_at 两列，仅在创建/实际复借边界记录；Go 只读投影 |
| Benefit | 返回可追溯的健康/复借事实，未知值保持未知 |
| Performance cost | 创建复用 K8s 已有 healthz 结果；Cloudflare 创建及实际复借增加专用 healthz；每次查询仅一次有界业务库读取，无查询侧探测或扫描 |
| Implementation cost | 一项 Worker 迁移、Provider 可选健康能力、现有 Broker 局部接线与一个只读服务 |
| Operational cost | 发布时先运行 Worker 迁移；未迁移的查询返回 environment_status_unavailable，不承担额外服务或后台作业 |
| New failure surface | 健康探测不可用时无法确认 ready；迁移或表不可读时查询明确不可用 |
| Release slice | 本轮权威环境查询 API；coding-agent 展示与真实后端联验单独完成 |
| Decision | KEEP；复用现有记录与生命周期，不新增状态表、周期探测或独立状态机 |
