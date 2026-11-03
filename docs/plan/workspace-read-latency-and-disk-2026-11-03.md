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

### 1.3 Cache 接口只有进程内的开发实现

设计书 4.5 的 Cache（推荐 Valkey：限流计数、SSE 跨节点广播、短期缓存、分布式锁）在 task-service 里接的是 `memcache.New`（`cmd/task-service/main.go`）。后果：

- Task 的事件流在进程内存里，task-service 只能单副本；重启后流丢失，SSE 端点只按业务库补发 `task_finished`，前端只能从 Task State 补文字。
- 线上的 Valkey 只是 Langfuse 自带的，不归 Runtime 用。

### 1.4 挂卷模式的工作区磁盘

- 单节点上 K8s 后端挂工作区卷：`deploy/cluster/workspaces.yaml` 的静态 PV 指向节点的 `hostPath: /var/lib/agentruntime/workspaces`，声明 200Gi（hostPath 的 capacity 只是声明，不限住实际用量）。
- coding-agent 每个会话是一份完整副本（workspace id `<工作区>--<会话>`），租户默认最多 20 个工作区（`sandbox/policy.py` 的 `WorkspaceLimits`）。
- `workspace.max_bytes`（默认 2 GiB）**只在存档时核对**；挂卷模式不存档，所以没有任何大小限制。沙箱 Pod 的 `ephemeral-storage` 限额也管不到挂进来的卷。
- 挂卷的工作区只有被删除时才删目录；长期不用的副本不会清理（coding-agent 计划第 8 节写着“清理长期不用的副本”还没做）。
- 结论：一个租户理论上就能占 20 × 2 GiB 以上，几个活跃租户（加上 `node_modules`、构建产物）就能写满节点的盘，而盘满会连带 Postgres、Temporal 等同节点上的组件。

## 2. 工作项

编号沿用 [人工验收后的实现工作项](post-acceptance-work-items-2026-11-03.md) 的写法。决定一栏全部是“待确认”。

| 工作项 | Failure / Evidence | Minimum fix | 成本与新失败面 | 当前决定 |
| --- | --- | --- | --- | --- |
| PERF-00 先测 | 第 1.1、1.2 节的判断来自读代码，没有耗时数据；不测就说不清改完快了多少 | Workspace 操作记一个 `runtime.workspace_op` span，分段记：工作流排队、建环境、存档、恢复、命令本身、总时长；coding-agent 记历史加载的总时长与请求数。线上取一周的 p50、p95 作基线 | 只加 span 与属性，不改行为；属性不含路径以外的文件内容 | 待确认 |
| WS-READ-01 读操作不新建沙箱 | 第 1.1 节：列目录、读文件每次新建环境 | 挂卷模式：`list_files`、`read_file` 由 Worker 直接读它挂着的工作区目录（`AR_TOOL_WORKSPACE_ROOT`），路径按（租户、工作区）由服务端拼出，逐级拒绝符号链接与 `..`，不跟随指出工作区的链接。存档模式：会话里有闲着的环境时借它执行（标为 busy，执行完放回），没有时再按现在的做法。`diff`、`log` 要 git，同样先借闲着的环境 | 少了“每次新环境”这层隔离：Worker 直接读用户代码写出来的文件树，符号链接与特殊文件是新的攻击面，要有专门的越权测试；借用闲着的环境要与 D-13 的“同一工作区只有一个可写环境”一致，不能让读操作与正在跑的命令并发 | 待确认 |
| WS-READ-02 Workspace 操作单独一个队列 | 第 1.1 节：查看文件与 Task 的 activity 抢同一批 Worker | Workspace 操作的工作流与 activity 换到单独的队列（如 `workspace`），按设计书 3.6 的分池方式单独一个 Worker 池与 KEDA ScaledObject | 多一个 Deployment；队列名是工作流启动参数，新启动的工作流生效，不改命令序列。在途的旧操作仍在旧队列，旧池要留到它们跑完 | 待确认 |
| WS-READ-03 文件树与 diff 按版本缓存 | 打开抽屉、切文件、节点结束后都会重取，同样的结果反复算 | 业务库 `workspaces` 加一个 `generation`：可写环境里的写工具结束、提交、丢弃改动、同步时加一。task-service 按（租户、工作区、generation、操作与参数）缓存只读操作的结果，经 Cache 接口（先进程内，CACHE-01 之后是 Valkey），设上限与短 TTL 兜底 | `shell_run` 能改任何文件，只能按“可写节点结束”粗粒度加一，节点执行中途看到的可能是旧结果；缓存键必须带租户，跨租户命中要有测试 | 待确认 |
| WS-BLOB-01 存档块的节点本地缓存 | 存档模式每次恢复都从 ObjectStore 拉全部块 | 块按 sha256 寻址、内容不可变：Worker 节点本地盘一个按大小上限 LRU 淘汰的块目录，恢复时先读本地、读到后核对哈希，没有再取 ObjectStore 并顺手存一份。存档仍然**写穿透**：块先成功写进 ObjectStore 才算存档成功，本地只是读缓存 | 不引入一致性问题（内容寻址）；占节点磁盘，要有上限与监控；本地块损坏时按哈希核对后丢弃重取 | 待确认 |
| WS-DISK-01 挂卷工作区的大小与冷热分层 | 第 1.4 节：挂卷模式没有大小上限、不清理，单节点的盘会被写满 | ① 挂卷模式也执行 `max_bytes`：可写节点结束时量一次工作区大小，超了拒绝后续写工具并告诉用户；② 冷热分层：工作区闲置超过 N 天（租户策略可配）先按 D-06c 存档到 ObjectStore、核对成功后再删本地目录，状态记进 `workspaces`；下次用到时从存档恢复到本地盘；③ 卷用量超过阈值告警（Grafana） | 冷的工作区第一次打开要等恢复；存档成功之前绝不删本地（不做“先删后存”的写回）；量大小本身要扫目录，大仓库有开销，只在节点结束时做 | 待确认 |
| HIST-01 历史一次取完（coding-agent 为主） | 第 1.2 节：每次刷新逐个重放已结束 Task 的事件 | 第一步不改契约：coding-agent 在 Task 结束时把要显示的结果（交付的文字、轮次摘要、花费）存进自己的记录，刷新时一次返回，只对进行中的 Task 开 SSE；已结束的加 ETag。第二步（要的话）：Task API 加按 Session 批量取结局的方法，走 S-01 | 第一步只动 coding-agent；存下来的结果与 Runtime 的 Task State 可能不一致，以 Task 结束时的那一份为准、只读 | 待确认；实现放在 coding-agent 仓库 |
| CACHE-01 Cache 接口的 Valkey 适配器 | 第 1.3 节：事件流在进程内，task-service 不能多副本、重启丢流 | 按设计书 4.5 写 Valkey 适配器（`libs/go-core/adapters`），过 Cache 接口的一致性测试；事件流用 Stream、限流用 Lua 脚本的滑动窗口；`deploy/data` 单独装一个 Valkey（不与 Langfuse 共用）；task-service 的 `serve` 按配置选择实现 | 多一个有状态组件与它的备份、监控；Cache 只存可丢失的数据（设计书 4.5），丢了要能从业务库补 | 待确认 |

## 3. 顺序

1. PERF-00：先拿到基线。
2. WS-DISK-01 的 ① 与 ③：这是可用性风险（盘满会拖垮同节点的数据库），不依赖性能数据。
3. WS-READ-01、HIST-01 第一步：按 PERF-00 的数据，预计收益最大。
4. WS-READ-02、WS-READ-03。
5. CACHE-01、WS-BLOB-01：前者是 task-service 多副本的前提，后者在加节点、改成存档模式之后才有收益。
6. WS-DISK-01 的 ②（冷热分层）：活跃租户多了再做。

## 4. 不做的与要人定的

- **不做写回**（先写本地、过期再存 ObjectStore）：节点在存回之前挂掉，用户的改动就丢了；增量存档只传新块，写穿透的代价不大。
- 要人定：冷热分层的闲置天数；`max_bytes` 超限时是拒绝写工具还是只警告；Workspace 单独队列的 Worker 池副本数下限；Valkey 是否要持久化（AOF）。
