# C、D、E 三条线的合并与缺口（2026-11-03）

> 同事把三条线推到了 `origin/lane/c`、`origin/lane/e`（agent-runtime）与 `origin/lane/d`（coding-agent），由这边接着做。
> 2026-11-03 人审通过（照草案）：C、E 两条线已合进 main，兼容性基线已更新（连同 `environment.proto`）。coding-agent 的 lane/d 等 Runtime 部署之后再合。

## 1. 试合并（本地，没推）

| 仓库 | 本地分支 | 内容 | 结果 |
| --- | --- | --- | --- |
| agent-runtime | `integration/lanes-2026-11-03`（在 `~/.local/work/agent-runtime`） | main（`9c1377d`）+ lane/c + lane/e，另加一个测试修正 | 只有生成的 proto 代码冲突（N-04 与 F3 都改了 `task_api.proto`），重新 `buf generate` 解决；`buf lint`、`buf breaking` 过 |
| coding-agent | `integration/lane-d-2026-11-03`（在 `~/.local/work/coding-agent-int`） | main（`ba14689`）+ lane/d | 没有冲突；`go vet`、`go test` 全过 |

agent-runtime 的测试（这台 Windows 机器，没有 Postgres，`AR_ALLOW_MISSING_TOOLS=1`）：

- Go：`go.work` 里 8 个模块的 build、vet、test 全过。
- 契约：572 过，1 失败。`test_no_legacy_terms` 是 main 上本来就有的旧叫法（`账本`、`精确节点`、`Orchestrator`），不在三条线里。
- py-worker：1523 过，5 失败：
  - main 上本来就有 2 条：`test_n01_references`（同事新加的版本分支 q87 没同步这条测试）与 `test_n05_shell…`（Windows 不支持 `pass_fds`）。
  - lane/c 上单独跑也失败 2 条，都是 Windows 环境的问题，要在 Linux 的 CI 上确认：`test_env02_environment::…gateway_ca…` 写的证书路径找不到；`test_n10_workspace_copy::test_mount_mode…` 断言对象文件是硬链接。
  - lane/e 上单独跑也失败 1 条：`test_context_usage::test_a_sub_agent_does_not_publish_context_usage`。N-16 有意让顶层子 Agent 推自己的 `context_usage`，测试还是旧行为。已在整合分支上改成新行为（`dbb875e`），现在过了。

**迁移号冲突（已修）**：lane/c 用了 207、208，main 上 F3 的 `task_artifacts` 原来也是 207（同一个库里第二个 207 会被跳过）。F3 还没部署，已经改成 209（main 的 `9c1377d`）。

## 2. 要人审的契约（S-01）

| 提交 | 线 | 改什么 |
| --- | --- | --- |
| `50d4e0e` | C | ENV-02：工作区的环境配置、setup 结果、`DeleteEnvironmentConfig`（`environment.proto`、`workspace.proto`、`workflow/v1/workspace.proto`） |
| `9b3ab79` | C | ENV-02：`SetWorkspaceEnvironment`，工作副本建好之后再绑定环境 |
| `f504281` | C | ENV-02：删除配置时保留会话的快照、只删机密 |
| `0d73bd5` | E | N-04：Task Plan 的步骤与 `plan_updated` 事件（`task_api.proto` 加事件类型；`defs.schema.json` 等） |
| `63a3d1d` | E | N-19：会话快照的响应（`session_snapshot.schema.json`） |
| `6e93851` | E | N-19：退回业务库时的快照游标 |
| `48d029d` | E | N-16：子 Agent 的进度进 Task 事件（`task_event_payload.schema.json`） |

细节见各自的实现记录：`env-02-implementation-2026-11-03.md`、`n04-task-plan-implementation-2026-11-03.md`、`n19-n20-implementation-2026-11-03.md`、`n16-sub-agents-implementation-2026-11-03.md`（都在分支上）。

另有一条：`environment.proto`（ENV-01 的半成品，`3c8b249`）一直没进兼容性基线，审 ENV-02 时一起审，审过再把它放进基线。

审过之后的次序：
1. 整合分支合进 main，用 `buf build` 更新基线（这次连 `environment.proto` 一起）。
2. 部署 Runtime。
3. coding-agent 合 lane/d（它调 ENV-02 的新接口，Runtime 先上线）。
4. 部署 coding-web。

## 3. 同事留下的缺口，谁来做

| 缺口 | 来自 | 处理 |
| --- | --- | --- |
| coding-web 的计划区：读 `plan_updated` 与快照里的 `plan`，高亮当前步骤 | N-04 | 我们做（D 线的网页），等合并后先写测试 |
| coding-web 的并行子 Agent 进度 | N-16 | 同上 |
| coding-web 用快照画首屏、处理 409 | N-19 | 同上 |
| Grafana 加 `runtime.task.first_event_ms` 按段的分位数 | N-20 | 2026-11-03 已加（`deploy/data/dashboards/quality.yaml`，验收 `tests/deploy/test_first_event_panel.py`），没部署 |
| F3 剩下的：Reviewer 把文件排前面、端到端 | F3 | 前者 2026-11-03 已做（schema 草案人审通过）；端到端在后面 |
| `token_delta`、`thinking_delta` 的推送 | N-20 | **要人定**：要在 task-service 的内部端口开一个新入口和平台令牌，属于安全评审 |
| 用户密钥的写入、列举、删除接口（coding-agent 的“网络密钥”页依赖它） | ENV-02 | 要新的契约（S-01），先出草案 |
| `open` 档位的公网授权（`sandbox.network.modes`、授权的 `scope: public`） | ENV-02 | 另一份契约，原记录里是 DEFER |
| coding-agent 的 `.env` 上限 64 KiB 改成 16 KiB、64 个（不然 Runtime 拒绝 `env_too_large`） | ENV-02 | 我们做，合 lane/d 时一起改 |
| 真模型下规划质量与子 Agent 拆分的评测 | N-04、N-16 | **要人定**（花钱） |
| 设计书附录 A 补“环境配置”“联网档位”“安装脚本”“启动命令” | ENV-02 | 人改设计书 |
