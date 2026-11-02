# 实现暂停检查点（2026-11-03）

按用户最新要求：将已完成部分推送干净，在当前范围暂停。此次收尾只整理进度与验证记录，不扩展功能，不修改冻结的人工验收基线；设计批准不等于所有实现已验收。

## 已在主线的交付

| 仓库 / 提交 | 已完成范围 | 验证与边界 |
| --- | --- | --- |
| agent-runtime `5d248ea` | 修复租户作用域 Task 准入、锁表迁移和重复提交 | SQLite 与真实 PostgreSQL 的准入、迁移、RLS；见 [记录](task-admission-repair-2026-11-03.md) |
| agent-runtime `8d49b3b` | Q78 多因素模型选档、严格 Profile 元数据、任务族传参与真实发布日期 | 路由/Registry 和 359 项 schema/同步测试；实测能力矩阵尚未填齐，见 [记录](model-routing-implementation-2026-11-03.md) |
| agent-runtime `377e1db`、`b2bd9f6`、`c5ee28c` | 共享预算账本、原子预留与实际结算、Task/Gateway 接线、同源部署配置 | SQLite/PostgreSQL、Gateway/Task、Helm 验证；供应商账单和线上发布验收尚未执行，见 [记录](model-budget-enforcement-2026-11-03.md) |
| agent-runtime `23de00b` | Activity 内 ContextBuilder、完整 Task State、父版本继承、旧历史兼容与对象读取边界 | 180 项组合及 34 项最终增量测试通过，含本地真实 Temporal 与新旧历史重放；见 [记录](contextbuilder-activity-implementation-2026-11-03.md) |
| coding-agent `384ea04` | 独立环境配置 CRUD、仅自己可见、repo 解耦、Session 环境绑定与输入校验 | 已推送该仓库 main；环境配置界面、真实执行和 Runtime 状态投影尚未实现 |

agent-runtime 最后一个代码提交为 `23de00b`；本检查点所在提交仅更新文档。coding-agent 收尾时本地 HEAD 与 origin/main 均为 `384ea04`。

## 草稿与未完成项

- ENV-01 协议、生成代码和目标测试已保存到独立分支 [codex/env-01-draft-checkpoint](https://github.com/Hanwen-Jiang/agent-runtime/tree/codex/env-01-draft-checkpoint)，提交 `3c8b249`。该分支基于 `8d49b3b`，没有合并 main。Go 查询服务、认证接线、ledger 健康事实和 Broker 复借接线尚未实现，行为测试不能计为通过；恢复时先同步主线并重新核对最小字段。
- ENV-02 仍缺配置界面、会话环境选择、setup/start 的真实执行与失败门禁、按“脚本内容 + 基础镜像”的缓存命中，以及 SecretStore/网络密钥代理接线。OIDC/VPN 等首期暂缓项维持原人工决策。
- N-18 目前只有已推送的 FileDelivery 内部 helper；受控发布工具、Task 产物收集、授权解析/下载入口和浏览器验收尚未交付。本轮未产生新的 N-18 源码改动。
- 模型路由仍缺有来源的真实能力/可靠性评分矩阵；ContextBuilder 的有效窗口尚未动态接入 Model Profile。
- 其他未闭合的 R-04/R-06、流式事件、Workspace/UI、Skill/MCP、多 Agent、Sandbox 后端与真实部署工作保持 [执行表](remaining-work-execution-plan-2026-11-02.md) 中的范围，不因本次收尾视为完成，也不追加新的防御机制或改写冻结设计。

三个并行工作项均已停止；原始共享检出中既有的未提交改动未参与本次提交或清理。本次隔离工作树仅清理自己的收尾状态；Temporal 本地工具缓存保留并通过 Git 本地 exclude 忽略，不提交二进制。

## 收尾验证

在 agent-runtime 集成 worktree 中执行：

```text
uv run --project services/py-worker --extra test python scripts/check.py --only proto,contract --no-sync
```

命令进程 PATH 使用 Go 1.26.5、buf 1.73.0、Helm 3.19.0、otelcol-contrib 0.135.0。Collector 官方发布包已核对 GitHub release 的 SHA256；只放本地工具目录，不修改全局 PATH。

- `buf lint`、格式检查、`buf breaking` 全部通过。
- 契约与部署检查：**780 passed、28 skipped、1 deselected、2 warnings**。Collector 自身配置校验和 Helm 渲染已实际执行，早先缺工具的失败已消除。
- 28 项跳过为 Windows 下的 4 项 Bash 语法检查、1 项需显式启动的 kind 验收、23 项需显式启用的镜像构建/运行测试。Go 模块测试由独立步骤负责，故本命令排除 1 项入口测试。
- 2 个 warning 来自镜像脚本测试的 Windows 子进程输出解码；未将它们描述成无告警。暂停收尾不扩展到该测试工具修复。
- 前述业务修改的 Go、PostgreSQL 与 Temporal 定向验证见各实施记录；这里不将局部验证扩称为完整线上全组件验收。
- `git diff --check` 在提交前执行；远端 main 的 GitHub Actions 在本次核对时仍在运行，未宣称远端 CI 已全绿。

后续恢复从上述缺口继续，不重复实现已经在主线的预算账本、路由和 ContextBuilder，也不将草稿测试或契约清单当作真实运行验收。
