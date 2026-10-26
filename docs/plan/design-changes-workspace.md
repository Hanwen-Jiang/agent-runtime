# 设计书改动草稿：Sandbox 的后端与 Workspace 层（D-06）

> 2026-10-26。依据：[云上部署计划](cloud-deploy.md) D-06（“先做的设计改动”）、D-05、D-07、D-08 的实现，以及 Aster 的 Workspace Manager（`docs/reference/aster-runtime-gap.md`）。
> 状态：**草稿，待人确认**。设计书是 Claude Docs 的导出副本，不直接改；本文给出可以直接替换进原文档的文字。人更新原文档、重新导出之后，D-06 才开工（按第 6 节拆成工作项）。在那之前代码以现行设计书为准。
> W1、W3、W4 是追认：D-05、D-07、D-08 已经按计划实现，设计书还是旧的说法。W2 是新的一层，要先定。

## 0. 一览

| 编号 | 改什么 | 设计书位置 | 状态 |
| --- | --- | --- | --- |
| W1 | Sandbox 的后端从“只定义接口”改为已实现的三种，按租户策略选 | 4.10、L.11 | 追认（D-05、D-07） |
| W2 | 新增 Workspace 层：工作区由沙箱承载，存档到 ObjectStore，工具在工作区所在的沙箱里执行，应用经 Workspace API 取 diff、提交、推送 | 4.10 之后新增 4.10.1；3.1.1 的 Task API；附录 D.4 | **待定** |
| W3 | Task 提交时可以声明更低的 effect 上限（effect_ceiling），只能调低 | 3.1.1 第 1 条 | 追认（D-08，契约待人审） |
| W4 | 租户策略加沙箱后端的选择 | 3.1.2 | 追认（D-08，schema 待人审） |

## W1　Sandbox 的后端

### 替换原文

4.10 的“**本期**”一段替换为：

> **接口**：创建环境（资源上限、网络策略、绑定的 workspace）、执行代码、读写文件、销毁。环境只属于创建它的作用域，别的作用域访问是 not_found；空闲超时自动回收。
>
> **实现**：开发用本地子进程（不隔离，只给开发与测试）。生产有两类后端，按租户策略的 `sandbox.backend` 选（W4），模型与请求都不能指定：
>
> - **Kubernetes Pod**：每个环境一个一次性 Pod，建在专用命名空间（Pod Security restricted、默认拒绝进出的网络策略、没有 ServiceAccount 令牌、非 root、只读根文件系统）。隔离强度由 RuntimeClass 决定：gVisor（容器级，不需要 KVM）或 Kata（microVM，需要 KVM）。Pod 里的 agent 经 HTTP 接受执行与读写文件的请求，令牌由部署的密钥对环境 id 派生；Worker 只有在该命名空间里管理 Pod 的权限。
> - **Cloudflare Containers**：每个环境一个 Firecracker microVM，经 Cloudflare Worker 写的桥调用；出网可以全断或按域名放行。磁盘是临时的。
>
> 按域名放行的网络（`network = allowlist`）只有 microVM 后端支持；Kubernetes 的网络策略只能按 IP 放行，K8s 后端收到这类请求返回 invalid。

L.11 的表格后加一句：

> 本期实现了“容器 + gVisor”“microVM”两类（K8s 的 Kata、Cloudflare Containers），WASM 暂不做。

## W2　Workspace 层

### 为什么

现在的工作区是 Worker、应用与沙箱共用的一块本地盘（`<根>/<租户>/<workspace>`）。单节点能用，但多节点、Cloudflare 后端（磁盘是临时的、不在集群里）都不成立；应用直接读写这块盘，也绕过了 Runtime 的租户隔离与审计。

### 决定（建议）

1. 工作区是 Runtime 的一等对象，属于（租户，workspace id），生命周期跨 Task；应用不再直接碰它的文件。
2. 工作区由某个沙箱后端**承载**：活跃时在一个沙箱环境里（该环境挂着工作区的文件系统），空闲时**存档**到 ObjectStore，用时**恢复**。
3. 工作区内的工具（`repo_read`、`repo_list`、`repo_grep`、`repo_edit`、`shell_run`）都在承载它的沙箱里执行，不在 Worker 的文件系统上。
4. 应用经 **Workspace API** 管理工作区：建（从仓库地址克隆，凭据由应用传入、用完即弃）、看 diff、提交、推送、删除。推送是 write_irreversible，要有通过的 gate。
5. 同一个工作区同一时间只有一个写者：会写的 Task 持有工作区的租约，只读 Task 在快照上执行。

### 替换原文

4.10 之后新增一节：

> #### 4.10.1 Workspace
>
> **职责**：承载 Task 要读写的文件（代码仓库的工作副本、上传的数据），生命周期跨 Task；把文件放在沙箱里，让工具在隔离环境中读写与执行。
>
> **身份与作用域**：工作区属于（tenant_id, workspace_id），workspace_id 取自租户上下文（附录 A），别的作用域访问是 not_found。
>
> **生命周期**：
>
> 1. 创建：应用经 Workspace API 发起，可以从仓库地址克隆。克隆用的凭据由应用随请求传入，只在这次克隆里使用，Runtime 不保存。
> 2. 承载：用到时在租户策略选定的沙箱后端里起一个环境挂上工作区；环境空闲超时后存档并销毁。
> 3. 存档：打包成按内容寻址的分块，连同逐文件的清单（路径、权限、sha256）写入 ObjectStore；恢复时按清单核对，不一致按 transient 处理并重试恢复，仍不一致就报 invalid、不在半个工作区上执行。
> 4. 删除：工作区与它的存档一并删除；租户删除时级联删除（附录 I）。
>
> **写者**：会写的 Task 开始时取得工作区的租约（带到期时间，随心跳续期），结束或失败时释放；租约在业务库里，以工作区为键唯一。只读 Task 在最近一次存档的快照上执行，看不到正在写的内容。
>
> **工具**：工作区内的读写与命令都经 Sandbox 接口在承载它的环境里执行；工具的 effect 不变（读是 read，改文件与执行命令是 write_reversible，作用范围限于工作区）。
>
> **Workspace API**（Task API 之外的一组接口，同样经 Ingress 认证）：创建、列出、取 diff（相对创建时或上次提交的版本）、提交、推送、存档、删除。推送是 write_irreversible：要有通过的 gate 记录，凭据由应用随请求传入，用完即弃；推送的目标分支由租户策略限定（例如只能推到 `agent/*`）。
>
> **计量**：每个工作区的沙箱存活时间、CPU 时间与存档大小记入 trace，计入租户的花费。

3.1.1 的“核心逻辑”后加：

> 7. 工作区：Task 可以绑定一个工作区（租户上下文里的 workspace_id）；会写的 Task 在 Session 工作流里串行的同时，还要取得工作区的租约（4.10.1）。

附录 D.4 的副作用表加一行：

> | 推送工作区的提交 | write_irreversible | 幂等键含提交的 sha；超时先按远端分支的当前 sha 对账 |

### 影响

- **契约**（S-01）：新增 `WorkspaceService`（/proto）；租户策略的 schema 加工作区配额（个数、单个大小、可推送的分支模式）。
- **Provider**：新增 Workspace 的 Provider 接口（创建、承载、存档、恢复、删除），开发实现用本地目录加 ObjectStore；Sandbox 的 `SandboxSpec.workspace_id` 已有（D-05）。
- **Worker**：`repo_*` 与 `shell_run` 改走 Sandbox（`shell_run` 已经走了，D-05）；不再读 `AR_TOOL_WORKSPACE_ROOT`。
- **应用**：coding-agent 改用 Workspace API（克隆、diff、推送），不再挂共用的工作区卷。
- **部署**：去掉 `deploy/cluster/workspaces.yaml` 的宿主机目录；Cloudflare 后端从此能跑 coding 任务。

### 待定的问题

1. 存档的格式：整包 tar（简单）还是按内容寻址的分块（增量、去重）？建议分块，大仓库的增量存档便宜得多。
2. 只读 Task 用快照还是直接读活跃环境？快照更干净，但要多一次恢复；建议快照。
3. Workspace API 放 task-service 还是单独的服务？建议先放 task-service（同一个 Ingress 与业务库），量大了再拆。

## W3　Task 提交时的 effect 上限

### 替换原文

3.1.1 第 1 条替换为：

> 1. 提交：校验租户配额与权限，生成 task_id；同一 Session 内带相同 client_request_id 的重复提交返回同一个 task_id。请求可以带 effect_ceiling，把这个 Task 的 effect 上限调得比租户策略更低（例如应用按用户选的沙箱等级传 read 或 write_reversible）；高于租户策略时拒绝（denied，说明原因），不悄悄截断。

## W4　租户策略里的沙箱后端

### 替换原文

3.1.2 “职责”一段的括号改为：

> （Hook 声明、模型白名单、预算、沙箱后端）

并在 4.10 的“实现”之后加：

> 用哪个沙箱后端由租户策略的 `sandbox.backend` 决定（后端的名字由部署登记），没写时用部署的默认后端；登记里没有的名字一律拒绝，不悄悄换后端。

## 6. 工作项草案（设计书更新后写进计划）

| 编号 | 内容 | 档 |
| --- | --- | --- |
| D-06a | 契约：WorkspaceService 的 proto、租户策略的工作区配额（S-01，人审） | 强 |
| D-06b | Workspace 的 Provider 接口、开发实现与一致性测试；存档与恢复（逐文件哈希一致） | 强 |
| D-06c | Worker 的 `repo_*` 经 Sandbox 执行；会写的 Task 取工作区租约 | 强（动 Worker 的工具，不在核心链路上） |
| D-06d | task-service 的 Workspace API；推送的 gate 与对账 | 强 |
| D-06e | coding-agent 改用 Workspace API（在 coding-agent 仓库） | 中 |

验收沿用 D-06：工具在远端沙箱里工作；任务之间工作区经存档、恢复后逐文件哈希相同；两个会话的工作区互不可见；应用经 Workspace API 拿到的 diff 与沙箱里 `git diff` 一致。
