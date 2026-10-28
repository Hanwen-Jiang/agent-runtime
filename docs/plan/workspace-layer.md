# Workspace 层的实现细化（D-06）

> 2026-10-27 起。设计的“是什么”在[设计书改动稿](design-changes-workspace.md)的 W2（已确认，原文档待人更新）；工作项与验收在[云上部署计划](cloud-deploy.md)的 D-06。本文把 D-06 拆成几步，写“怎么做”和实现时定下的、改动稿没写到的细节。
> 契约（D-06a）是另一个会话起草的（2026-10-27 15:16 前后），这里接着它做：`WorkspaceService`、Worker 一侧的 `WorkspaceOperationWorkflow`、租户策略的 `workspace` 一节、凭据的封装格式 `wsc1`。契约照 S-01 改了 proto 与 schema、过了 `buf breaking`，**没有更新兼容性基线**，等人审。

## 1. 拆分

| 步 | 内容 | 依赖 |
| --- | --- | --- |
| D-06a | 契约：`agentruntime.workspace.v1.WorkspaceService`（建与克隆、列出、diff、提交、推送、存档、删除）；`agentruntime.workflow.v1.WorkspaceOperationInput`、`WorkspaceOperationResult` 与工作流名 `WorkspaceOperationWorkflow`；租户策略的 `workspace`（个数、单个大小、能推送的分支）；凭据封装 `wsc1`（Go 的 `libs/go-core/sealing`、Python 的 `agentruntime_core.sealing`，同一组向量） | S-01（人审） |
| D-06b | 存档与恢复的原语：Sandbox 接口的可选能力 `SandboxArchive`（导出、导入、清单）与 `SandboxBrowse`（列目录、搜索）；sandbox-agent 的 `/v1/archive`、`/v1/manifest`、`/v1/list`、`/v1/grep`；开发实现、K8s、Cloudflare 接上；一致性测试 | — |
| D-06c | 工作区的存档（Worker 的 `agentruntime_worker.workspace`）：按文件内容寻址存进 ObjectStore，加逐文件的清单；恢复后按清单核对；保留最近 3 份、清掉没人引用的块；业务库迁移 205 | D-06b |
| D-06d | 承载与工具：登记里 `workspace: archive` 的后端不挂工作区卷，建环境后从最近的存档恢复，销毁之前存档；这些后端之间可以互相回退；`repo_read`、`repo_list`、`repo_grep`、`repo_edit` 在承载它的环境里执行 | D-06c、D-11、D-12、D-13 |
| D-06e | Workspace API：task-service 的 `WorkspaceService` 起 Worker 的 `WorkspaceOperationWorkflow`，操作在承载工作区的沙箱里执行；推送是 write_irreversible（gate 记录、幂等键、先对账） | D-06a、D-06d |
| D-06f | 应用：coding-agent 改用 Workspace API（在 coding-agent 仓库） | D-06e |
| D-06g | 部署：后端登记的 `workspace` 一项进 Helm 与 `dev_stack.py`；Cloudflare 后端登记为 archive；单节点仍默认挂卷 | D-06d |

## 2. 存档的格式

```text
ObjectStore（租户作用域）
  workspaces/<workspace_id>/blobs/<sha256>                 文件内容，按 sha256 寻址（同一个工作区内去重，增量存档只传新的）
  workspaces/<workspace_id>/snapshots/<snapshot_id>.json   清单
清单：{"version": 1, "workspace_id": …, "snapshot_id": …, "created_at": …,
       "entries": [{"path", "type": file|dir|symlink, "mode", "size", "sha256", "link"}]}
```

- 路径是相对工作区的 POSIX 路径，按路径排序；模式只留权限位（`0o777` 以内）；符号链接只存相对的、不出工作区的目标（指出去的不进存档）；设备、管道、套接字不进存档；agent 自己的标记文件 `.agentruntime` 不进存档。
- 存档：从承载的环境导出 tar（sandbox-agent 的 `GET /v1/archive`，先写进 Worker 的临时文件，不整个读进内存），逐个文件算 sha256，ObjectStore 里没有的才写；最后写清单、记一行快照。导出时按租户策略的 `workspace.max_bytes`（默认 2 GiB）限总大小，超了 invalid（`too_large`）。
- 恢复：按清单从块拼出 tar（临时文件），导入一个空的环境（`PUT /v1/archive`），再取环境的清单（`GET /v1/manifest`）逐项核对路径、类型、大小、sha256 与链接。块的内容与清单不符、或导入后的清单不一致：transient（`workspace_restore_mismatch`）；broker 销毁这个环境、换一个新的再恢复一次，仍不一致报 invalid，不在半个工作区上执行。
- 保留：每次存档后只留最近 3 份快照，块按留下的清单的引用计算，没人引用的删掉。同一个工作区同一时间只有一个可写的环境（D-13 的规则），存档与清理不会并发。
- ObjectStore 的接口还没有流式的 put：块一次一个读进内存，单个文件大时占内存（大文件多的仓库要等 ObjectStore 加流式接口）。

## 3. 表（迁移 205，租户作用域）

| 表 | 列 | 说明 |
| --- | --- | --- |
| `workspaces` | workspace_id（主键）、state（active、deleting、deleted）、repo_url、head、snapshot_id、size_bytes、files、created_at、updated_at、deleted_at | 工作区本身。经 Workspace API 建，或存档模式的环境第一次用到时补一行（两处都按 `workspace.max_workspaces` 核对，超了 denied `workspace_quota_exceeded`） |
| `workspace_snapshots` | snapshot_id（主键）、workspace_id、manifest_key、files、size_bytes、new_blobs、env_ref、created_at、deleted_at | 每次存档一行；清理时记 deleted_at |
| `workspace_pushes` | push_key（主键：幂等键）、workspace_id、branch、sha、remote、approved_by、operation_id、state（pushing、pushed、reconciled、failed）、reason、created_at、finished_at | 推送的 gate 记录与幂等记录 |

推送的幂等键是 `wsp-` 加 sha256(tenant_id, workspace_id, branch, sha) 的前 32 位：同一个提交推到同一个分支只算一次，与重试次数无关（附录 D.4）。

## 4. 承载（D-06d）

- 后端登记加一项 `workspace`：`mount`（工作区是挂进环境的目录：K8s 的 `workspace_claim`、本地实现的工作区根）或 `archive`（不挂卷，靠存档与恢复）。没写时：Cloudflare 是 archive，其余是 mount。
- archive 的后端算一组，彼此可以回退（工作区的内容在 ObjectStore 里，不在哪个后端上）；mount 的后端照旧按卷分组。两组之间不回退：挂卷的工作区最新的内容在卷上，存档可能是旧的。
- 借环境（`SandboxBroker.acquire`）：archive 的后端建环境时 `SandboxSpec.workspace_id` 置空（后端不挂卷），环境表照记 workspace_id；建好后从最近的快照恢复，再交给调用方。复用会话的环境时不恢复。
- 还环境：一次性的可写环境先存档再销毁；会话级的留着，回收（空闲到期、活满、会话关闭、被别的会话抢占）时先存档再销毁。存档失败：暂时性的（transient）留着环境，下一轮对账再试；活满或永久性的失败（invalid，例如超过大小上限）照样销毁，原因记进环境表（这次的改动丢了）。只读的环境不存档。
- 工具：存档模式下 `repo_read`、`repo_list`、`repo_grep`、`repo_edit` 经 Sandbox 在环境里执行（读文件、列目录、按行搜索、写文件）；写的工具借可写的环境，读的工具先找本会话的可写环境，没有时借一个只读的环境（在最近一次存档上）。挂卷的后端上这几个工具还是读写 Worker 的工作区目录（同一处）。`repo_grep` 在环境里是 RE2 语法（sandbox-agent 用 Go 的 regexp），开发实现用 Python 的 re，两者的公共子集一致。
- 按项目定制的运行环境（D-17）要读工作区里的定义文件，构建器挂的是工作区卷：存档模式的后端不用项目环境（K8s 改存档模式之前要先让构建器从存档取上下文）。

## 5. Workspace API（D-06e）

```text
应用 ──► task-service WorkspaceService（认证、租户、封凭据）──► Temporal：WorkspaceOperationWorkflow（task 队列，ID wsop-<operation_id>）
                                                                       └─► workspace_operation activity（program 队列）
                                                                              借环境（SandboxBroker，会话为空）→ git → 存档 → 还
```

- task-service 认证、按身份定租户、校验参数（workspace_id 同租户 ID 的格式，repo_url 只收 https，分支名按 git 的规则），生成 operation_id（`wso-` 加 26 位），有凭据时用 `workspace/credential-key` 封成 `wsc1`（关联数据是租户与 operation_id），起工作流并等结果（默认 10 分钟，克隆大仓库要更久时调 `AR_WORKSPACE_OPERATION_TIMEOUT_S`）。ListWorkspaces 直接读业务库的 `workspaces`（表不在时为空）。
- Worker 的 activity 解开凭据，只经 exec 的 env 交给这一次的 git 命令（`GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0=http.extraHeader`/`GIT_CONFIG_VALUE_0=Authorization: Basic …`，不进 argv、日志、事件历史与错误信息；git 的输出在报错前把它遮掉）。
- 联网：克隆与推送要连远端。activity 给这次操作记一条平台批准的联网授权（task_id 是 `wsop-<operation_id>`，只放行 repo_url 的主机与 443，按租户策略的 `sandbox.network` 核对，批准人是发起的用户），结束时收回；经出网网关的后端按授权放行，托管后端按域名放行。
- 操作在会话为空的可写环境里跑：别的会话的可写环境闲着就被抢占（先存档），正在跑命令时报 denied（`workspace_busy`），应用稍后再试。
- 各操作：
  - 建：`git clone [--branch ref] -- <url> .`，或没给地址时 `git init`；记 head；存档。
  - diff：用临时的 index（`GIT_INDEX_FILE`，复制自工作区的 index）`git add -A` 后 `git diff --cached <base>`，含暂存、未暂存与未跟踪的新文件，不动工作区自己的 index；输出超过 1 MiB 截断。
  - 提交：`git add -A` 后 `git commit`（作者取请求，默认 Agent Runtime）；没有改动时 committed 为 false。
  - 推送：分支要符合 `workspace.push_branches`（默认 `agent/*`），先写 gate 记录（approved_by 是发起的用户），先按远端分支的当前 sha 对账（`git ls-remote`），已经是这个 sha 就不推（reconciled）；推送不自动重试，超时或结果不明时记 failed，下一次调用先对账。
  - 存档：立即存档（挂卷的后端也可以：从环境里导出）。
  - 删除：销毁这个工作区的环境、删掉存档（块、清单、快照行），挂卷的后端另删工作区目录；行记 deleted。
- 只在 Worker 配了沙箱（`AR_TOOL_SHELL_SANDBOX=sandbox`）时可用；否则 activity 报 invalid（`workspace_api_unavailable`）。测试用的本地远端（`file://`）只在 `AR_WORKSPACE_ALLOW_LOCAL_REMOTES=1` 时收，生产只收 https。

## 6. 验收（D-06 的四条加上细化）

1. 工具在远端沙箱里工作：存档模式的后端上 `repo_*` 与 `shell_run` 都在环境里，Worker 的工作区目录里没有这些文件。
2. 任务之间工作区经存档、恢复后内容一致：逐文件的路径、类型、权限位、sha256 与链接相同；块被改过时不恢复（换环境重试一次后报 invalid）。
3. 两个会话（两个租户同名的工作区）互不可见；别的租户的工作区经 Workspace API 一律 NotFound。
4. 应用经 Workspace API 拿到的 diff 与沙箱里 `git diff` 一致。
5. 推送：分支不在策略里时 denied；同一个提交重复推送只推一次（第二次 reconciled）；凭据不出现在事件历史、日志与错误信息里。
6. 存档按内容寻址：没变的文件不重写；只留最近 3 份；没人引用的块被清掉。
