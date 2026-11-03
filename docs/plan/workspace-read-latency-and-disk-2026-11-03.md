# 工作区查看的延迟、工作区磁盘与 Cache（2026-11-03 起草）

> 状态：**草案，等人确认。** 本文只记问题、依据与拟定的做法，不改代码。确认后按仓库流程走：每个工作项先写验收测试、停下等人确认、再实现；凡要改 `/proto`、`/schemas` 的走 S-01。
> 起因：用户反映 coding-agent 里看文件树、看 diff 很慢很卡；刷新页面后会话的历史记录一批一批慢慢出来；并问到挂卷模式的工作区放在本地盘上撑不撑得住。
> 下面的“现状”都是读代码得出的，**没有在线上测过耗时**，所以第一项是先测。

## 1. 现状

### 1.1 Workspace API 的每次读操作都新建一个沙箱

coding-agent 点一下文件树（或读一个文件、看 diff、看提交记录）经过的路径：

```text
coding-agent → task-service WorkspaceService（workspaceapi/server.go: operate）
  → 起一个 WorkspaceOperationWorkflow（task 队列），activity 在 program 队列：与正在跑的 Task 抢同一批 Worker
  → operations.py: _lease(..., writable=False) → broker.acquire(..., reuse=False)     每次都新建环境
       挂卷（集群里 gvisor 的默认）：新起一个沙箱 Pod
       存档（Cloudflare，加节点之后的 K8s）：先把会话里闲着的可写环境存一份快照到 ObjectStore（_current），
                                             再新建环境、从快照恢复整个工作区、逐文件核对
  → 跑一次 git / 列目录 / 读文件 → release → 销毁 → 工作流结束 → 原路返回
```

读一个文件与克隆一个仓库走的是同一条重路径。延迟的大头应当在建沙箱与（存档模式下的）存档加恢复，而不在业务库查询。

### 1.2 会话历史的加载（coding-agent 一侧，记在这里是因为与 Task API 有关）

- 前端 `replayTasks`（`internal/web/static/app.js`）取最近 12 个 Task，已结束的**逐个**（`await`）开一条 SSE，从头重放全部事件，收到 `task_finished` 才轮到下一个；每个 Task 另有一次 `QueryTask` 取 Task State 里的文字。注释写的原因是浏览器对同一主机的连接数有限。
- 列任务时 `refresh`（`internal/server/tasks.go`）对没定论的 Task 依次 `QueryTask`（已结束的最多补 8 个）。
- 已结束的 Task 内容不会再变，却在每次刷新时重放。
- 事件只存在 Cache 的流里（`session/activities.go`：`events.Append`，保留 24 小时），业务库只在 Task 结束时记 `last_event_id` 与结局（`taskstore.RecordFinished`）。所以进行中的 Task 刷新后只能从 Cache 重放；Cache 一丢（现在是进程内，task-service 重启即丢），进行中 Task 的过程就没了。

### 1.3 task-service 的 Provider：业务库已能接 Postgres，Cache 与 Ingress 还是开发实现

- 业务库按配置选：设了 `AR_RELATIONAL_DSN` 用 Postgres（行级安全），没设回落到 `AR_DATA_DIR` 下的 SQLite（`cmd/task-service/main.go` 的 `newRelationalStore`）。云上的 `values-cloud.yaml` 配了 `relationalDsn`，线上是 Postgres；但回落是静默的，配漏了也能起来，只是数据写进了容器里的 SQLite。
- Ingress 是内置的 `localingress`（一把签名密钥验令牌）。

设计书 4.5 的 Cache（推荐 Valkey：限流计数、SSE 跨节点广播、短期缓存、分布式锁）在 task-service 里接的是 `memcache.New`（`cmd/task-service/main.go`）。后果：

- Task 的事件流在进程内存里，task-service 只能单副本；重启后流丢失，SSE 端点只按业务库补发 `task_finished`，前端只能从 Task State 补文字。
- 线上的 Valkey 只是 Langfuse 自带的，不归 Runtime 用。

### 1.4 挂卷模式的工作区磁盘

- 单节点上 K8s 后端挂工作区卷：`deploy/cluster/workspaces.yaml` 的静态 PV 指向节点的 `hostPath: /var/lib/agentruntime/workspaces`，声明 200Gi（hostPath 的 capacity 只是声明，不限住实际用量）。
- coding-agent 每个会话是一份完整副本（workspace id `<工作区>--<会话>`），租户默认最多 20 个工作区（`sandbox/policy.py` 的 `WorkspaceLimits`）。
- `workspace.max_bytes`（默认 2 GiB）**只在存档时核对**；挂卷模式不存档，所以没有任何大小限制。沙箱 Pod 的 `ephemeral-storage` 限额也管不到挂进来的卷。
- 挂卷的工作区只有被删除时才删目录；长期不用的副本不会清理（coding-agent 计划第 8 节写着“清理长期不用的副本”还没做）。
- 结论：一个租户理论上就能占 20 × 2 GiB 以上，几个活跃租户（加上 `node_modules`、构建产物）就能写满节点的盘，而盘满会连带 Postgres、Temporal 等同节点上的组件。

### 1.5 沙箱的代码分在三处，Broker 在 Worker 里

- Sandbox 接口与各后端的适配器（K8s、Cloudflare、本地；自写的标准库 K8s REST 客户端）在 Python 的 `libs/py-core`（`providers/sandbox.py`、`adapters/k8s_sandbox.py` 等）。
- 借还环境的 Broker、环境表（`sandbox_envs`）、配额、联网授权、按项目构建环境、工作区存档与恢复在 Worker 的 `agentruntime_worker.sandbox` 与 `agentruntime_worker.workspace`；对账是 Worker 里的一个 Temporal 工作流（`workflows/sandbox_reconcile.py`）。这几处合计约 4300 行 Python。
- 沙箱里跑的 sandbox-agent 是 Go（只用标准库），明文 HTTP 加令牌（`go-sandbox-agent/cmd`：`ListenAndServe`，令牌由 Worker 按 `sandbox/token-key` 签），接口有 exec、读写文件、列目录、grep、存档的导出导入与清单。Cloudflare 的环境经 HTTPS 的桥。
- task-service（Go）只读环境表（EnvironmentService），不会找环境、连环境。设计书 2.4 对 Sandbox 只写了“只定义接口”。

后果：谁要在沙箱里做点事（Task 的 shell_run、Workspace API 的每次操作），都得进 Worker；task-service 想直接读文件，只能在 Go 里再实现一份“找环境、连环境”。

## 2. 工作项

编号沿用 [人工验收后的实现工作项](post-acceptance-work-items-2026-11-03.md) 的写法。决定一栏全部是“待确认”。

| 工作项 | Failure / Evidence | Minimum fix | 成本与新失败面 | 当前决定 |
| --- | --- | --- | --- | --- |
| PERF-00 先测 | 第 1.1、1.2 节的判断来自读代码，没有耗时数据；不测就说不清改完快了多少 | Workspace 操作记一个 `runtime.workspace_op` span，分段记：工作流排队、建环境、存档、恢复、命令本身、总时长；coding-agent 记历史加载的总时长与请求数。线上取一周的 p50、p95 作基线 | 只加 span 与属性，不改行为；属性不含路径以外的文件内容 | 待确认 |
| WS-READ-01 只读操作走快路径，不经 Temporal、不新建沙箱 | 第 1.1 节：列目录、读文件、diff、log 每次起一个工作流、新建环境 | 读与写分开。**写**（建、克隆、提交、丢弃、同步、推送）仍走 `WorkspaceOperationWorkflow`：要耐久、要幂等与对账（推送是 write_irreversible）。**读**（ListFiles、ReadFile、GetDiff、GetLog）由 task-service 调 SBX-01 的 Sandbox Service 的只读方法，只给（租户、工作区、会话、操作），不关心环境在哪、怎么连；git 带 `--no-optional-locks`，读不占用环境、不改 busy、不触发存档。SBX-01 的第一阶段之前不做：不在 task-service 里另写一份 Go 版的“找环境、连环境” | 读到的可能是命令执行到一半的文件，界面要标出“任务进行中”；越权测试覆盖跨租户、跨工作区与符号链接 | 待确认；依赖 SBX-01 第一阶段 |
| WS-READ-02 Workspace 操作单独一个队列 | 第 1.1 节：查看文件与 Task 的 activity 抢同一批 Worker | Workspace 操作的工作流与 activity 换到单独的队列（如 `workspace`），按设计书 3.6 的分池方式单独一个 Worker 池与 KEDA ScaledObject | 多一个 Deployment；队列名是工作流启动参数，新启动的工作流生效，不改命令序列。在途的旧操作仍在旧队列，旧池要留到它们跑完 | 待确认 |
| WS-READ-03 文件树与 diff 按版本缓存 | 打开抽屉、切文件、节点结束后都会重取，同样的结果反复算 | 业务库 `workspaces` 加一个 `generation`：可写环境里的写工具结束、提交、丢弃改动、同步时加一。task-service 按（租户、工作区、generation、操作与参数）缓存只读操作的结果，经 Cache 接口（先进程内，CACHE-01 之后是 Valkey），设上限与短 TTL 兜底 | `shell_run` 能改任何文件，只能按“可写节点结束”粗粒度加一，节点执行中途看到的可能是旧结果；缓存键必须带租户，跨租户命中要有测试 | 待确认 |
| WS-BLOB-01 存档块的节点本地缓存 | 存档模式每次恢复都从 ObjectStore 拉全部块 | 块按 sha256 寻址、内容不可变：Worker 节点本地盘一个按大小上限 LRU 淘汰的块目录，恢复时先读本地、读到后核对哈希，没有再取 ObjectStore 并顺手存一份。存档仍然**写穿透**：块先成功写进 ObjectStore 才算存档成功，本地只是读缓存 | 不引入一致性问题（内容寻址）；占节点磁盘，要有上限与监控；本地块损坏时按哈希核对后丢弃重取 | 待确认 |
| WS-DISK-01 挂卷工作区的大小与冷热分层 | 第 1.4 节：挂卷模式没有大小上限、不清理，单节点的盘会被写满 | ① 挂卷模式也执行 `max_bytes`：可写节点结束时量一次工作区大小，超了拒绝后续写工具并告诉用户；② 冷热分层：工作区闲置超过 N 天（租户策略可配）先按 D-06c 存档到 ObjectStore、核对成功后再删本地目录，状态记进 `workspaces`；下次用到时从存档恢复到本地盘；③ 卷用量超过阈值告警（Grafana） | 冷的工作区第一次打开要等恢复；存档成功之前绝不删本地（不做“先删后存”的写回）；量大小本身要扫目录，大仓库有开销，只在节点结束时做 | 待确认 |
| HIST-01 事件持久化与增量取历史 | 第 1.2 节：刷新后逐个重放；事件只在 Cache 里，进行中的 Task 刷新也要能看到已经发生的部分 | **Runtime**：Session 工作流的 activity 发事件时，先按（租户、task_id、event_id）追加写进业务库一张只追加的事件表（event_id 已由业务库分配、严格递增，天然是增量游标），再写 Cache 流做实时推送；SSE 端点按 Last-Event-ID 先从表里补、再接 Cache；Task API 加一个按 Session 批量取事件的方法（每个 Task 给一个 after_event_id，分页），走 S-01。**coding-agent**：刷新时一次请求取回最近 Task 的事件（进行中的也一样，取到当前为止），之后只对进行中的 Task 开 SSE 并从各自最后的 event_id 续接；浏览器可按 Task 缓存已收到的事件与最后的 event_id，再打开时只取增量。持久化必须在 Runtime 一侧：coding-agent 只在有浏览器连着时才代理 SSE，没人看时的事件它收不到 | 每个事件多一次业务库写（事件很小，文字在 Task State 里）；表要按保留期清理（与 Task 的保留一致）；写库失败时不能丢推送也不能重复：先写库、后写 Cache，Cache 失败只影响实时性 | 待确认；Runtime 与 coding-agent 两边都有实现 |
| CACHE-01 Cache 接口的 Valkey 适配器 | 第 1.3 节：事件流在进程内，task-service 不能多副本、重启丢流 | 按设计书 4.5 写 Valkey 适配器（`libs/go-core/adapters`），过 Cache 接口的一致性测试；事件流用 Stream、限流用 Lua 脚本的滑动窗口；`deploy/data` 单独装一个 Valkey（不与 Langfuse 共用）；task-service 的 `serve` 按配置选择实现 | 多一个有状态组件与它的备份、监控；Cache 只存可丢失的数据（设计书 4.5），丢了要能从业务库补 | 待确认 |
| TS-PROD-01 task-service 的生产 Provider | 第 1.3 节：业务库回落 SQLite 是静默的；Cache、Ingress 是开发实现，task-service 只能单副本 | ① 业务库：加 `AR_PROFILE=production`（或等价开关），生产模式下没有 `AR_RELATIONAL_DSN` 直接拒绝启动，不回落 SQLite；连接池上限可配，Chart 给出与 Postgres `max_connections` 的配比，副本多了以后前面加 PgBouncer（transaction 模式，核对 RLS 用的会话变量在事务内设置）；② Cache：接 CACHE-01 的 Valkey，同样生产模式下必填；③ 两者都接好后 task-service 的 Chart 允许 `replicas > 1`，SSE 端点在任一副本上都能续传（端到端测试：两个副本，连 A 提交、连 B 收事件、杀掉 A 再续传）。Ingress 换生产实现另起工作项，不在这里 | 生产模式开关是新的配置面，开发、端到端测试不设它，行为不变；多副本后 Session 工作流的 Worker 也随副本变多，Temporal 按队列分发，不需要选主 | 待确认 |
| SBX-01 Sandbox Service（Broker 独立成服务） | 第 1.5 节：沙箱能力散在 py-core 与 Worker，只有 Worker 能用；task-service 要读文件只能重写一份 | 新服务 `services/go-sandbox-service`（Go，只用标准库与 `libs/go-core`；K8s 照 `kube.py` 的做法自写 REST，不引 client-go），gRPC（与 Model Gateway、Memory Service 一致）。调用方只说“对（租户、工作区、会话）以只读或可写方式做某个操作”：`Exec`、`ReadFile`、`ListDir`、`Grep`、`Snapshot`，以及显式的 `Acquire` / `Release`（Task 一次借用里跑多条命令时用）；找环境、建环境、复用、配额、联网授权、存档与恢复、对账都在服务里。租户由调用方的服务令牌与请求里的 TenantContext 双重核对（服务端定租户，同 CLAUDE.md 的租户隔离规则）。分三期：**一期**只读方法，读现有的环境表、连已有的环境，task-service 的 WS-READ-01 用它；**二期**借还与 exec 搬过来，Worker 的 `shell_run`、`repo_*` 与 Workspace 操作改调服务，Python 的 Broker 只留一个薄客户端；**三期**对账、环境构建搬过来，删掉 Python 里的重复实现 | 多一个在每个 Task 关键路径上的有状态服务：要多副本、要和 Worker 一样能扛重启（借出中的环境以环境表为准，服务重启后按表恢复）；每次 exec 多一跳网内 gRPC（毫秒级，相对命令本身可忽略）；二期动 Node Executor 的工具实现，按 CLAUDE.md“核心链路同一时间只由一个 agent 修改”排期；环境表的迁移现在由 Worker（Python）管，搬家期间两边读写同一张表，迁移归属要先定；这是架构改动，设计书 4.10 要由人更新 | 待确认；需要先写设计改动稿 |

## 3. 顺序

1. PERF-00：先拿到基线。
2. WS-DISK-01 的 ① 与 ③：可用性风险（盘满会拖垮同节点的数据库），不依赖性能数据。
3. TS-PROD-01 的 ①（生产模式不回落 SQLite）：改动小，防配错。
4. HIST-01（先走 S-01）与 CACHE-01、TS-PROD-01 的 ②③：事件持久化、Valkey、多副本是一组。
5. SBX-01 一期 → WS-READ-01：先写设计改动稿，人定之后做。
6. WS-READ-02、WS-READ-03；SBX-01 二期、三期。
7. WS-BLOB-01（加节点、改存档模式之后）、WS-DISK-01 的 ②（冷热分层）。

## 4. 不做的与要人定的

- **不做写回**（先写本地、过期再存 ObjectStore）：节点在存回之前挂掉，用户的改动就丢了；增量存档只传新块，写穿透的代价不大。
- 要人定：冷热分层的闲置天数；`max_bytes` 超限时是拒绝写工具还是只警告；Workspace 单独队列的 Worker 池副本数下限；Valkey 是否要持久化（AOF）。
- **协议**：环境内的 sandbox-agent 现在是集群内明文 HTTP 加令牌，Cloudflare 经 HTTPS 的桥，Firecracker 计划里改走 vsock。协议开销是毫秒级，慢在建沙箱（秒级）与存档恢复，不在协议；Sandbox Service 对内用 gRPC 是为了与其他内部服务一致、契约有类型，不是为了快。语言另见第 5 节。
- 要人定：SBX-01 是否做、是否分三期；环境表迁移的归属。（第 5 节的 Rust 评估已推迟。）

## 5. 沙箱内外换成 Rust 的评估（2026-11-03）

前提：Firecracker 做主路已定（[firecracker-backend.md](firecracker-backend.md)，2026-11-01），但 fc-manager 还没写；sandbox-agent 现有约 1150 行 Go（不含测试 810 行），只用标准库。沙箱是这个项目以后最吃性能的部分，所以按“性能落在哪一层”逐层看，不拿“多一种语言”当理由，但把它的真实成本列出来。

### 5.1 沙箱的性能落在哪里

| 环节 | 现在谁做 | 量级 | 语言能改变多少 |
| --- | --- | --- | --- |
| 冷启动：内核引导、rootfs、agent 就绪 | Firecracker（Rust）、客户机内核、agent | 未量（D-20 验收 8 只记录） | agent 的启动是毫秒级，占比小 |
| 快照恢复（D-26） | Firecracker 加载内存文件、缺页 | 与客户机已用内存成正比 | agent 常驻内存越小，快照越小、恢复越快：这里语言有作用 |
| 单机密度 | 每台 VM 的固定开销：内核、agent、页表 | 未量 | agent 常驻内存直接叠加到每台 VM |
| 工作区存档与恢复 | **Python 的 Worker**：导出 tar 写临时文件、逐个文件算 sha256、上传；恢复时拼 tar、导入、再取清单逐项核对 | 与仓库大小成正比，GB 级仓库是秒到分钟 | 最大的一块，但问题是数据在 Worker 和环境之间来回搬，不是哪种语言算哈希 |
| 出网、预览转发 | fc-manager（计划中） | 字节搬运 | Go、Rust 都能到 GB/s，差别不在这 |
| 每次查看都新建环境 | Worker 的 Workspace 操作 | 秒级 | 与语言无关（WS-READ-01、SBX-01 解决） |

### 5.2 逐个组件

**客户机里的 sandbox-agent（PID 1、vsock、exec、读写文件、存档）：适合 Rust。**

- 它在每一台 VM、每一份快照里都有一份。Go 运行时与垃圾回收的常驻内存通常是十几 MB 量级，Rust 写的同类程序一般在几 MB 以内（数字是经验值，没在这台机器上量过，SBX-RS-01 要量）。每台 VM 省下的部分同时降低快照体积。按独服 125 GB、每台 VM 0.5–2 GB 估，密度上的收益在百分之一到几之间，不算大，但 D-26 的恢复延迟会直接受益。
- PID 1 的职责（回收孤儿进程、信号、杀进程组）在 Go 里有一个已知的坑：用 `wait4(-1)` 回收孤儿会抢走 `os/exec` 子进程的退出状态，需要额外绕开（单独一个小 init，或改用 pidfd）。Rust 下进程管理完全由自己控制，没有运行时在背后等子进程。
- AF_VSOCK：Go 标准库不支持，计划里要用 `syscall` 手写 sockaddr；Rust 用 `libc` 直接调。
- 同类项目的选择（凭记忆，没在本会话核实）：Kata Containers 2.0 把客户机里的 agent 从 Go 改写成了 Rust，公开的理由主要是内存占用；Firecracker 本身是 Rust；E2B 的环境内 agent 是 Go。
- 改写成本：约 1150 行，接口照旧（HTTP 的 `/v1/*`，Firecracker 下换成 vsock 上的同一套），现有的一致性测试（`agentruntime_core.conformance.sandbox`）是黑盒的，可以原样验证 Rust 版本。

**宿主上的 fc-manager（起 jailer 与 firecracker、vsock 转发、rootfs 缓存、快照）：可以用 Rust，最好与 agent 一起定。**

- 还没写，没有改写成本，现在定语言代价最小。
- 它是宿主上权限最高的组件。Go 与 Rust 都是内存安全的；Rust 多的是编译期的数据竞争检查与没有空指针，对“长期跑、并发管很多 VM”的守护进程有价值，但不是决定性的。
- 性能上它主要是起进程、搬字节、读写文件，Go 不吃亏；namespace、chroot 这些交给 jailer，不在 fc-manager 里做。
- 如果 agent 用 Rust，vsock 两端的协议、帧格式可以是同一个 crate，这是用 Rust 写 fc-manager 最实在的理由。

**Sandbox Service（SBX-01：借还、配额、环境表、存档编排、对账）：留在 Go。**

- 它是控制面：慢在业务库、K8s API、对象存储与网络，CPU 和内存不是瓶颈，换 Rust 拿不到性能。
- 它必须与 task-service 共用租户绑定、Postgres 行级安全的连接方式、SecretStore、错误模型与 Telemetry（`libs/go-core`），并过 `tests/contract` 的跨语言一致性比对。用 Rust 写，就要在第三种语言里再实现并持续对齐这些安全相关的代码：这是安全面的成本，不只是“多学一种语言”。

**存档与恢复的数据路径：先换位置，再谈语言。**

- 现在 GB 级的数据在“环境 → Worker 临时文件 → 对象存储”之间来回搬，哈希在 Worker 里算。更快的做法是让数据少走一跳：由 agent（在环境里）直接算清单与哈希、直接与对象存储（或 fc-manager 的本地块缓存，WS-BLOB-01）交换块；Firecracker 下还可以考虑把工作区做成块设备镜像，而不是 tar。这一步与 agent 用什么语言写是同一件事，如果 agent 改 Rust，这部分一起做。

### 5.3 结论与代价

| 组件 | 建议 | 理由 |
| --- | --- | --- |
| sandbox-agent | Rust，经 SBX-RS-01 实测确认后切换 | 每台 VM 都有一份；PID 1 与 vsock 是底层工作；代码小、有黑盒一致性测试 |
| fc-manager | Rust（若 agent 用 Rust）；否则 Go | 还没写；与 agent 共用 vsock 协议 |
| Sandbox Service | Go | 控制面，瓶颈不在 CPU；与 task-service 共用租户与安全相关的代码 |
| 存档、恢复的数据路径 | 挪到 agent 与 fc-manager 一侧 | 收益最大的一块，与语言选择绑在一起做 |

Rust 的真实成本（不是理由，是要付的）：

- 依赖规则要另立：CLAUDE.md 的“不引入设计书 4.15 之外的新依赖”与设计书 4.15 的技术栈都要改（由人更新设计书）。拟定：`Cargo.lock` 提交；只收发布满两周的版本（与 Python 的 `exclude-newer` 同一原则）；`cargo deny` 检查许可证与安全公告；agent 只用 `std` 加 `libc`，fc-manager 另列清单。
- 工具链：CI 加 Rust 的构建、`clippy`、测试；静态链接 musl；镜像构建（`deploy/images`）加 Rust 阶段；本机是 Windows，交叉编译 Linux 目标用容器或 `cargo-zigbuild`。
- 维护：会写 Rust 的人要能评审 PID 1 与 vsock 这类底层代码。

### 5.4 工作项

**2026-11-03 用户定：SBX-RS-01、SBX-RS-02 推迟（DEFER），先保开发速度。** D-20 按 [firecracker-backend.md](firecracker-backend.md) 原计划用 Go 写；本节的评估留作以后实测时的依据，Firecracker 上线、有了冷启动与密度的基线之后再考虑。

| 工作项 | Failure / Evidence | Minimum fix | 成本与新失败面 | 当前决定 |
| --- | --- | --- | --- | --- |
| SBX-RS-01 Rust 版 agent 的原型与实测 | 第 5.2 节的内存、快照、恢复收益是经验值，没有本项目的数字；Firecracker 上线前是换语言代价最小的时候 | 用 Rust 写 agent 的核心：`--init`（PID 1）、vsock 监听、`/v1/exec`（含取消与杀进程组）、`/v1/files`、`/v1/manifest`；只用 `std` 加 `libc`。与 Go 版在独服上同条件对比：常驻内存、静态二进制大小、冷启动到第一条命令返回、快照文件大小与恢复到第一条命令返回（P50、P95），各跑 20 次以上。一致性测试的子集对两版都跑 | 原型约千行；独服上要有 Firecracker 与 KVM（D-20 的真 VM 环境）；原型不进生产 | **DEFER**（2026-11-03 用户定） |
| SBX-RS-02 定语言与依赖规则 | SBX-RS-01 的数字出来之前不定 | 收益明确（例如每台 VM 常驻内存、快照恢复的 P95 有可见下降）时：agent 与 fc-manager 用 Rust，写设计改动稿（4.15 技术栈、4.10 后端）与 Rust 依赖规则，人审后改 firecracker-backend.md 的 D-20；收益不明显时维持 Go，结论与数字记在本节 | 改设计书由人做；D-20 推迟到定下之后开工 | **DEFER**（2026-11-03 用户定） |
