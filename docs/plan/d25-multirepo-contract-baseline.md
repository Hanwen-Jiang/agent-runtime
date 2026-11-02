# D-25 多仓库工作区契约候选基线

> 状态：契约与设计已由人工验收基线批准；Workspace API 实现按独立工作项验收。本文仍不替代生成代码和真实桥接验收。

## 首版候选行为

- 继续支持旧的 `root` 工作区；空项目和单仓库 root 的现有请求、数据和操作保持兼容。
- 新增 `multi` 布局：根目录只是容器，仓库通过 `AttachRepo` / `DetachRepo` / `ListRepos` 管理，每个仓库有独立 `repo` 标识和子目录。
- 仓库记录候选字段为 `name`、`url`、`ref`、`path`、`access`（`read` / `write`）和 `primary`；路径只能是单层安全目录名，不能绝对路径、包含 `..`、重叠、嵌套或命中保留名。
- `GetDiff`、`CommitWorkspace`、`PushWorkspace`、`SyncWorkspace`、`GetLog`、`DiscardChanges` 在 multi 布局中必须明确指定 `repo`；root 布局可以沿用旧的省略语义。
- 每次远端操作只解开该仓库的 `sealed_credential`；只读仓库的提交和推送返回 `repo_read_only`，分支限制和网络授权仍逐仓库检查。
- 存档、恢复、复制和删除操作覆盖完整布局；操作工作流保持一个 Workspace 一个 activity，改动前必须补 golden 回放样本。

## 验收边界

验收 fixture 覆盖：root/multi 兼容、逐仓库 attach/detach/list、repo 参数、只读访问、路径冲突与嵌套、`max_repos`/`max_bytes`、逐仓库凭据隔离、主机授权、存档恢复/复制、golden 回放、跨租户隔离。fixture 只描述候选行为，不宣称当前运行时代码已经支持 multi 布局。

## 复杂度决策

| 项目 | Failure | Evidence | Minimum fix | Benefit | Performance cost | Implementation cost | Operational cost | New failure surface | Release slice | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `workspace_repos` 子记录 | 多仓库的路径、权限和远端目标无法独立校验 | 一个 workspace 可连接多个仓库且每仓库权限不同 | 每个 workspace 一条逐仓库记录 | 逐仓库 diff/推送/凭据隔离 | 每次操作多一次按 repo 查询 | 中，需迁移与 Workspace API 契约 | 归档、复制、删除需覆盖子记录 | 部分 attach 失败后的恢复语义 | D-25/S-01 | KEEP |
| 逐仓库凭据解封 | 操作 A 时误读或泄露仓库 B 的凭据 | Runtime 当前只接受单次 sealed credential | 只解开请求中指定 repo 的凭据 | 降低凭据暴露范围 | 每次远端操作一次解封 | 中 | 凭据错误按仓库定位 | 错误 repo 选择造成拒绝 | D-25 | KEEP |
| 独立 multi 布局 | 旧 root 工作区数据迁移风险 | root 与 multi 的目录语义不同 | 创建时固定布局，不做转换 | 保持旧 workspace 行为 | 创建时增加一次布局校验 | 低 | 两种布局并存 | 客户端省略 repo 的错误 | D-25 | KEEP |
| 第二套 workspace 状态机 | 归档/恢复与 repo 子记录状态分裂 | 现有 WorkspaceOperationWorkflow 已有生命周期 | 复用 Workspace 状态和操作历史 | 少一套状态源 | 无额外后台工作 | 低 | 无新对账循环 | 子记录与主记录不一致 | D-25 | REJECT |
| `AttachRepo` 批量事务 | N 个仓库一起失败难以恢复 | 设计要求部分成功可保留 | 一个仓库一次 activity，失败单独报告 | 可重试且不拖长单个 activity | N 个仓库 N 次调用 | 低 | 需要展示部分成功 | 中间状态更多 | D-25 | KEEP |

任何字段、RPC 或 schema 变化都必须先补正反例，再执行 S-01 的 buf/schema 检查、兼容性检查和人工评审。
