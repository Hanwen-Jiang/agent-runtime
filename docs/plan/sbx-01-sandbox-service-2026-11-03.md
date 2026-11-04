# SBX-01 Sandbox Service：设计改动稿（2026-11-03）

> 状态：**草案，等人审。** 只写稿，不改代码。依据：[workspace-read 文档](workspace-read-latency-and-disk-2026-11-03.md) 第 1.1、1.5 节与第 2 节的 SBX-01 一行，以及 PERF-00 已经在打的分段计时（`runtime.workspace_op`，`fb971d2`）。
> 设计书是导出的副本，不直接改；第 2 节是建议的改动，由人更新原文档后重新导出。要改 `/proto` 的部分（第 4 节的接口）审过之后另走 S-01。

## 1. 为什么

- **读一个文件走的是借沙箱的重路径**：Workspace API 的每次操作起一个 Temporal 工作流、在 program 队列上新建一个环境、跑一次命令、销毁（第 1.1 节）。WS-READ-03（`d35923b`）已经让重复的读命中缓存，但第一次读、版本号变了之后的读，照旧要建环境。
- **沙箱能力只在 Worker 里**：后端适配器在 py-core，借还、配额、复用、存档与恢复、对账在 Worker 的 `agentruntime_worker.sandbox` 与 `workspace`（约 4300 行 Python）。task-service 想直接读文件，就得在 Go 里再写一份“找环境、连环境”（第 1.5 节）。
- **后面几项都要一个“签发方”**：WS-PULL-01、WK-LITE-01/02 要给环境签单对象的存储地址（STOR-SEC 的 `ObjectStore.SignedUpload` 已就位），Firecracker 下要和 fc-manager 打交道。这些放在一个独立的服务里，比散在每个 Worker 进程里好管。

## 2. 建议的设计书改动

| 章节 | 现在 | 建议 |
| --- | --- | --- |
| 2.4 组件 | Sandbox 只是 Provider 接口 | 加一个组件 **Sandbox Service**：管理沙箱环境的生命周期与对环境的操作；Worker 与 task-service 都经它用沙箱，不直接持有后端 |
| 3.6 Worker 分池 | program 池执行 program 节点，环境由 Worker 建 | program 池仍执行节点，但借还环境、执行命令经 Sandbox Service；Workspace 操作单独一个池（WS-READ-02，契约草案 `ebf963e`） |
| 4.10 Sandbox | “本期只定义接口；开发时用本地子进程” | 写明：Sandbox Service 实现借还、选后端（D-12 的回退规则、D-19 的启用开关）、配额与预算、会话级复用、存档与恢复（D-06）、对账（D-11）、按项目构建环境（D-17）；对外是 gRPC（与 Model Gateway、Memory Service 一致）；后端适配器照旧是 Provider |
| 4.15 工程基座 | Go：Task Service、Model Gateway | Go 加 Sandbox Service（只用标准库与 `libs/go-core`；K8s 照 `kube.py` 自写 REST，不引 client-go）；仓库结构加 `/services/go-sandbox-service` |
| 附录 A | — | 加术语 **Sandbox Service**（不叫 sandbox manager、broker service） |

## 3. 边界

**Sandbox Service 做的**：给（租户、工作区、会话）找或建环境；按租户策略选后端；配额与预算（环境表 `sandbox_envs` 的事务）；会话级复用与回收；存档模式的恢复与存档；对账；按项目构建环境；签发给环境用的单对象存储地址（WS-PULL-01 以后）。

**不做的**：联网授权的审批（仍是 Task 的 `request_network` 与 task-service 的核对，D-16）；凭据的签发（SecretStore）；工具的语义（`repo_edit` 怎么改文件、`shell_run` 怎么截断输出仍在 Worker 的工具里）。

**调用方**：Worker（`shell_run`、`repo_*`、Workspace 操作）、task-service（WS-READ-01 的只读操作）。用户代码永远不直接连它。

## 4. 接口（草案；进 `/proto` 时走 S-01）

```text
service SandboxService {
  // 一期：只读，不进 Temporal、不改环境的 busy、不触发存档
  rpc ListDir(WorkspaceRead) returns (ListDirResult);
  rpc ReadFile(WorkspaceRead) returns (ReadFileResult);   // 大文件只给大小，二进制不给内容（同现在的 Workspace API）
  rpc Grep(WorkspaceRead) returns (GrepResult);
  rpc Diff(WorkspaceRead) returns (DiffResult);           // git diff，--no-optional-locks
  rpc Log(WorkspaceRead) returns (LogResult);
  // 二期：借还与执行
  rpc Acquire(AcquireRequest) returns (Lease);            // 可写或只读、会话复用、busy_for
  rpc Exec(ExecRequest) returns (ExecResult);             // 带 lease_id；输出有上限（WK-LITE-01 以后可外置）
  rpc WriteFile(WriteFileRequest) returns (WriteFileResult);
  rpc Release(ReleaseRequest) returns (ReleaseResult);    // keep、存档模式的可写环境先存档
  rpc Snapshot(SnapshotRequest) returns (SnapshotResult);
}
message WorkspaceRead { TenantContext tenant; string workspace_id; string session_id; string path; ... }
```

- **租户**：调用方用服务令牌认证（SecretStore 平台作用域的 `sandbox-service/token`，按调用方分发），请求里带 TenantContext；服务按它绑定所有 Provider（CLAUDE.md 的“每次 Provider 调用都带租户上下文”）。只信任已登记的调用方；task-service 转来的租户是它按用户的认证身份定的。
- **错误**：统一错误模型（`providererr`）；环境忙 `workspace_busy`、后端停用 `sandbox_backend_disabled` 等现有的码原样保留。

## 5. 读路径怎么快（一期的核心）

| 情形 | 现在 | 一期 |
| --- | --- | --- |
| 会话里有活着的可写环境 | 新建环境（挂卷）或先存档再恢复（存档模式） | 直接在那个环境里读（不改 busy，不存档）：看到的是最新的，包括没提交的改动 |
| 挂卷、没有活着的环境 | 新建环境 | **ListDir、ReadFile、Grep 直接读卷**（服务的 Pod 只读挂工作区卷，路径规则同 `repo_*`，不跟随指出工作区的符号链接）；Diff、Log 仍建一个只读的短命环境 |
| 存档模式、没有活着的环境 | 新建环境、从快照恢复全部 | **ListDir、ReadFile 读最近的快照清单与块**（ObjectStore，按 sha256 取一个块），不建环境；Diff、Log 仍要环境 |

不在服务里对用户的仓库跑 git：仓库是用户可控的，`.git/config` 能配 `core.fsmonitor`、外部 diff、textconv 等会执行命令的项；即使逐项关掉也是在拿服务进程冒险。Diff、Log 仍在沙箱里跑，只是走一期的快路径（不进 Temporal，环境用完即毁或复用会话的）。

## 6. 分期

| 期 | 内容 | 验收 | 动到的 |
| --- | --- | --- | --- |
| 一期 | 服务骨架（gRPC、认证、租户绑定、Telemetry）；只读方法；读现有的环境表；WS-READ-01 的 task-service 改调它 | 列目录、读文件在挂卷与存档模式下都不建环境（PERF-00 的 span 里 acquire 为 0）；会话里有可写环境时读到未提交的改动；越权：别的租户、别的工作区、`..`、符号链接都拒绝；服务重启不影响 | 新服务；task-service 的 workspaceapi；Chart |
| 二期 | Acquire、Exec、WriteFile、Release、Snapshot；Worker 的 `shell_run`、`repo_*`、Workspace 操作改调服务；Python 的 Broker 只留薄客户端 | 现有的沙箱、会话、配额、回退、存档测试对着服务全部通过；故障注入：服务被杀，借出中的环境按环境表恢复 | **核心链路**（Node Executor 的工具），按单写者排期 |
| 三期 | 对账、环境构建搬进服务，删掉 Python 里的实现 | 对账与构建的现有测试对着服务通过；Worker 里不再有后端适配器的依赖 | Worker、py-core 的适配器（Go 版重写） |

## 7. 并存与迁移

- 环境表（迁移 201～210）现在由 Worker 建。一期服务只读这些表；二期起由服务负责迁移：Go 侧按同样的版本号登记同样的语句（业务库的迁移表已记着这些版本，不会重跑），Worker 不再登记。两边同时在线时以版本号为准，谁先启动谁建。
- 二期切换按租户灰度：租户策略加一个开关（S-01），Worker 按它决定自己借还还是调服务；同一个工作区不能两边同时持有可写环境（环境表的唯一约束已经保证）。

## 8. 成本与新的失败面

- 多一个在每个 Task 关键路径上（二期起）的有状态服务：要多副本，重启后按环境表恢复借出的环境；每次 exec 多一跳网内 gRPC（毫秒级）。
- 服务的 Pod 只读挂工作区卷（一期的快路径）：卷要能被多个 Pod 挂（单节点的 local-path 可以；多节点时只有存档模式，不挂卷）。
- K8s 适配器要用 Go 重写一份（三期之前 Python 与 Go 两份并存，一致性测试 `agentruntime_core.conformance.sandbox` 对两边都跑）。

## 9. 要人定的

1. 做不做 SBX-01，分不分三期（建议做，先做一期：收益最直接，不动核心链路）。
2. 一期的快路径：挂卷时直接读卷、存档模式读快照的块，是否接受（建议接受；git 仍在沙箱里跑）。
3. 环境表迁移的归属（建议二期起归服务）。
4. 设计书第 2 节的改动由人更新原文档。
5. 服务令牌的分发方式（建议 SecretStore 平台作用域，按调用方各一个）。

## 10. 与其他工作项

- **WS-READ-01** 依赖一期；**WS-READ-03** 的缓存（`d35923b`）在一期之后仍有用（服务也可以按 generation 缓存）。
- **WS-READ-02**（单独队列）在一期之后只剩写操作走 Temporal，队列的量会小很多。
- **D-19**（`f9f0c4d`）的启用开关与能力随二期搬进服务；**D-20** 的 fc-manager 由服务调用。
- **WS-PULL-01、WK-LITE-01/02** 的签名地址由服务签发（STOR-SEC 的 `SignedUpload` 已就位）。
- **PERF-00** 的数字（部署后一周）用来确认一期的收益，建议拿到数字再开工二期。
