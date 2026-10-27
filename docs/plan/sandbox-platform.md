# 沙箱平台的实现细化（D-11～D-17）

> 2026-10-27 起。设计的“是什么”在[设计书改动稿](design-changes-workspace.md) W5～W11，工作项与验收在[云上部署计划](cloud-deploy.md)第 5 节；本文写“怎么做”：表、凭据、各组件之间怎么连，以及实现时定下的、改动稿没写到的细节。改动稿里叫“账本”的东西，代码与本文叫**沙箱环境表**（“账本”是调研文档里 Task State 的旧叫法，`tests/contract/test_terminology.py` 拦它）。

## 1. 组件与数据流

```text
            Worker（Python）                                         业务库（RelationalStore，按租户行级安全）
  shell_run / sandbox_exec ──► SandboxBroker ──────────────────────► sandbox_envs、sandbox_grants、sandbox_usage（租户作用域）
            │  选后端（策略 + 回退）、配额（事务里核对）、复用会话的环境      sandbox_env_directory（平台作用域：环境 → 所属作用域）
            ▼                                                              ▲            ▲
       Sandbox 后端（K8s、Cloudflare、本地）                               │            │
            │  环境里注入：包镜像地址、出网代理（带环境凭据）                 │            │
            ▼                                                              │            │
   沙箱 ──► go-pkg-mirror（凭据 → 租户，未命中字节记进 sandbox_usage）─────┘            │
       └──► go-egress-gateway（凭据 → 环境 → 会话的授权；字节、连接、审计）──────────────┘
  SandboxReconcile（Temporal 工作流，Worker 里跑）：列各后端 → 清孤儿、记丢失、回收到期的环境
```

- **沙箱环境表是 Worker 写的**（建、用、销毁都经 SandboxBroker）；包镜像与出网网关（Go）只读环境凭据能解出的那个租户的数据，再写回用量。三方经同一个 RelationalStore：开发时是 `AR_DATA_DIR` 下的 SQLite（Go 与 Python 的布局相同：每个作用域一个库文件），生产是 Postgres（`AR_RELATIONAL_DSN`，行级安全，连接角色没有 BYPASSRLS）。
- 表的迁移版本从 201 起（taskstore 用 1～3，Registry 用 101 起），定义在 Python 的 `agentruntime_worker.sandbox.ledger`；Go 两个服务不迁移，表不在时报 transient（Worker 先起）。

## 2. 表（迁移 201 起）

`sandbox_envs`（租户作用域）

| 列 | 说明 |
| --- | --- |
| env_ref | 主键，`sbe-` 加 16 位十六进制；Runtime 自己的编号，建之前就有 |
| backend、kind、isolation | 落在哪个后端（登记里的名字）、后端的类型、隔离强度 |
| backend_env_id | 后端给的环境 id（K8s 的 `sbx-…`、Cloudflare 的 `cfs-…`）；建好之前为空 |
| workspace_id、session_id、task_id | 属于哪个工作区与会话；最近一次由哪个 Task 使用 |
| writable | 1 表示可写的环境（会写的 Task 用；一个工作区同一时间只有一个） |
| runtime、image | 运行环境的名字与镜像（按 digest） |
| state | creating → ready → destroying → destroyed；或 failed（没建成）、lost（后端里没了） |
| created_at、ready_at、last_used_at、expires_at、max_until、destroyed_at | ISO 8601 UTC；expires_at 是空闲到期，max_until 是活满 |
| cpu_ms、alive_ms、cost_usd | 计量 |
| fallback_from、reason | 回退前的首选后端与原因；failed、lost 的原因 |

`sandbox_grants`（租户作用域，D-16）：grant_id、task_id、session_id、hosts（JSON 数组）、ports（JSON 数组）、expires_at、限额（max_up_bytes、max_down_bytes、max_conns、max_rps）、已用的 up_bytes、down_bytes、state（active、revoked、expired）、approved_by、reason、created_at、revoked_at。

`sandbox_usage`（租户作用域）：day（`YYYY-MM-DD`，主键）、sandbox_seconds、egress_bytes、mirror_miss_bytes、cost_usd。月度花费是当月各天之和。

`sandbox_quota_lock`（租户作用域，一行）：建环境的事务先更新这一行，Postgres 上就是行锁，并发建环境在核对配额这一步串行（SQLite 每个库一个连接，本来就串行）。

`sandbox_env_directory`（平台作用域）：env_ref、owner（租户 ID 或 `_platform`）、backend、state、updated_at。只给对账用：知道一个环境属于哪个作用域，好绑定到那个租户去读它的行。先写这里再写租户的行；两边不一致时对账按后端的实际情况修。

## 3. 环境凭据

沙箱访问包镜像与出网网关用的短期凭据，Worker 签发、Go 服务校验，不查表就能知道租户：

```text
ar1.<base64url(JSON)>.<base64url(HMAC-SHA256(key, "ar1." + base64url(JSON)))>
JSON：{"e": env_ref, "t": tenant_id, "w": workspace_id, "s": session_id, "x": 到期的 Unix 秒, "k": 用途}
```

- 密钥取自 SecretStore 平台作用域的 `sandbox/env-credential-key`（部署共用，至少 32 字节），与 agent 令牌的 `sandbox/token-key` 分开。
- 用途 `k`：`mirror`（包镜像）或 `egress`（出网网关）；一个凭据只能用在一处。
- 到期时间等于环境的 max_until；环境销毁后网关与镜像按环境表拒绝（凭据本身还没过期也一样）。
- 两种语言的签发与校验对着同一组向量测试（`tests/contract/sandbox_credential_vectors.json`）。
- 沙箱里的样子：`PIP_INDEX_URL=https://mirror:<凭据>@pkg-mirror…/pypi/simple/`、`npm_config_registry`（`//pkg-mirror…/npm/:_authToken=<凭据>`）、`GOPROXY=https://mirror:<凭据>@pkg-mirror…/go/`；出网 `HTTPS_PROXY=http://<env_ref>:<凭据>@egress…:3128`。日志与 trace 里只记环境号，不记凭据。

## 4. 各项的落点

| 工作项 | 代码 |
| --- | --- |
| D-11 | `agentruntime_worker.sandbox.ledger`（表、状态迁移、用量）、`sandbox.broker`（经环境表建与销毁）、`sandbox.reconcile`（Temporal 工作流与 activity）；各后端的 `list_environments()`（K8s 按标签、Cloudflare 桥的列表接口、本地实现）；Worker 配置 `AR_RELATIONAL_DSN` |
| D-12 | `tenant_policy.schema.json` 的 `sandbox.fallback`、`min_isolation`、`quota`（S-01，待人审）；`sandbox.broker` 的首选 + 回退与事务里的配额核对 |
| D-13 | `SandboxSpec.session_id`；`ToolInvocation.session_id`、`task_id`；`sandbox.broker` 按（工作区、会话）复用；空闲与活满的回收在对账里 |
| D-14 | `deploy/images/universal`；后端登记的 `images`（runtime → 镜像 digest）；`runtime.sandbox` span 的 `sandbox.image` |
| D-15 | `services/go-pkg-mirror` |
| D-16 | `services/go-egress-gateway`；`SubmitTaskRequest.network_request` 与 `sandbox.network`（S-01，待人审）；Worker 的 `request_network` 工具 |
| D-17 | `agentruntime_worker.sandbox.environments`（识别定义文件、算键、配额、退回 universal）与构建器的 Job 清单 |

## 5. 与改动稿、计划的出入（实现时定的）

（按工作项写，做完一项补一项。）

**D-11（2026-10-27）**

- 环境表在 Worker 里写：`agentruntime_worker.sandbox.ledger`（表与状态迁移）、`sandbox.broker`（SandboxBroker：shell_run 经它借还环境）、`sandbox.reconcile`（对账的 activity，在 program 队列）与 `workflows/sandbox_reconcile.py`（平台级的定时工作流，固定 ID `platform-sandbox-reconcile`，订阅 task 队列又配了沙箱后端的 Worker 启动时确保它在跑；每 `AR_SANDBOX_RECONCILE_INTERVAL_S` 秒一轮，默认 60，跑满 120 轮 continue_as_new）。
- 业务库：Worker 设了 `AR_RELATIONAL_DSN` 时用 Postgres（行级安全；Chart 在 sandbox 模式下把 task-service 的那个连接串也给 Worker），没设时是 `<AR_DATA_DIR>/relational` 下的 SQLite（与 task-service、包镜像、出网网关的布局相同）。py-worker 的测试因此要 Postgres 的二进制（与 go 一样，check.py 与 CI 都加上了）。
- 后端的列表：`ListedEnvironment` 与 `SandboxLister`（`providers/sandbox.py`）；K8s 按 managed-by 标签列 Pod，Cloudflare 桥加了 `GET /v1/sandboxes`（一个名为 directory 的 Durable Object 记着环境，建时登记、删时去掉；只给平台作用域），开发实现列进程内的环境。同一个命名空间里的 gvisor 与 kata 会互相列出对方的 Pod，所以对账按后端给的环境 id 找行、不限后端。
- 孤儿的宽限按后端记的创建时间（默认 10 分钟）：刚建、还没来得及落账的环境不删。
- 花费：一次性的环境在销毁时按后端登记里的价目（`price: {per_hour, per_cpu_hour}`）结算，写进行、当天的 `sandbox_usage`，并经 shell_run 的输出（`sandbox_cost_usd`）进 Node 的 `meta.cost_usd`，由 Stage Runner 计入 Round 与 Task 的花费（工作流代码没改）；span 上是 `sandbox.cost_usd`，Grafana 加了按租户的沙箱花费。会话级的环境在回收时才结算，花费只在环境表里（不进当时那个 Task 的花费）。
- 租户删除：`SandboxBroker.destroy_tenant`（设计书还没有租户删除的流程，先给出沙箱一侧要做的）。
- 没覆盖的：程序节点 `sandbox_exec`（Node Executor 的开发沙箱）还没经 broker；“杀掉 Worker”的故障注入是在进程内用异常模拟的（`test_sandbox_ledger.py`），没在端到端里杀进程。

**D-12（2026-10-27）**

- 契约（S-01，**待人审**，基线没更新）：`tenant_policy.schema.json` 的 `sandbox` 加 `fallback`、`min_isolation`、`idle_timeout_s`、`max_lifetime_s`、`quota`、`network`（D-13、D-16 要的字段一并加了，免得审三次）；新的枚举放在 `defs.schema.json`（`sandbox_isolation`、`network_approver`）。`backend` 仍是写了 `sandbox` 就必填。
- 回退在 SandboxBroker：候选是首选加回退去重；隔离强度低于下限（`min_isolation`，没写取首选的）的跳过并在报错里写明；绑定 workspace 时只在同一组之间回退（同一个 PVC 的 K8s 后端、本地实现；托管后端各自一组）；只有 transient 才换下一个。首选本身低于 `min_isolation` 时直接从够强的回退开始。
- 配额在建环境的事务里核对（`sandbox_quota_lock` 那一行先更新，Postgres 上是行锁）：同时在跑的数、当天的沙箱秒数（已结束的加上在跑的）、当天的出网与镜像未命中字节、当天与当月的花费。
- 没做的：“要出网审计的 Task 只在支持网关的后端之间回退”等 D-16 有了网关再加（后端登记要标明是否经网关出网）。

**D-13（2026-10-27）**

- 复用在 SandboxBroker：shell_run 带着会话（`ToolInvocation.session_id`）时按（租户，工作区，会话）在环境表里找没到期、没在别处占用的 ready 环境；找不到才建。会话级默认打开（Worker 的 `AR_SANDBOX_SESSIONS=0` 关掉）；没有会话的调用仍是一次性的。
- 空闲期限按租户策略的 `idle_timeout_s`（只能调低部署默认的 15 分钟）；活满按 `max_lifetime_s`（默认 6 小时）；每次用都顺延空闲期限（不超过活满）。回收在对账里做（命令还在跑的不回收：`busy_until`）。
- 环境在后端里没了（被后端自己回收、节点重启）：exec 报 not_found 时记为丢失，换新环境把这条命令跑完。
- 一个工作区同一时间只有一个可写的环境：别的会话的那个闲着就回收，正在跑命令就拒绝（`workspace_busy`）。所以同一个工作区的两个会话会轮流拿到环境（后来的回收先来的那个闲着的环境），工作区的文件不受影响。
- 没做的：Session 关闭时销毁（`SandboxBroker.destroy_session` 有了，但 Session 工作流在 Go 里，还没有把关闭的信号送过来；靠空闲期限兜底，W5 的待定问题 1）；验收 4 的“20 条命令在 Kata 上的总耗时”要 Kata 节点。

**D-14（2026-10-27）**

- 镜像：`deploy/images/universal/Dockerfile`（`sandbox-universal`）以 Python 3.13.16 的官方镜像为底，Node 22.23.3、Go 1.27.2、Temurin 21.0.11 加 Maven 3.10.0、Rust 1.99.0 各从官方镜像复制成一层，外加 build-essential、git 与 sandbox-agent；版本都发布满两周。单语言的 `sandbox-node`、`sandbox-go` 用 `deploy/images/sandbox.Dockerfile`（`BASE_IMAGE` 在 `images.json` 里写死）。Python 的那个仍是 `sandbox-python`（Cloudflare 桥引用它）。
- 后端登记的 `images` 把运行环境映射到镜像（只给 `image` 时就是 python 的）；shell_run 用 Worker 的 `AR_SANDBOX_RUNTIME`（Chart 的 `worker.shell.runtime`，`values-cloud.yaml` 是 universal）。环境表与 span 记 `runtime` 与 `image`：K8s 后端报了实际的 imageID（按 digest）就以它为准，否则记登记里配的。Chart 里的镜像仍按标签写（构建前不知道 digest），实际用的 digest 从 kubelet 来。
- 预拉：`worker.shell.prepull`（`values-cloud.yaml` 打开）在沙箱命名空间起一个 DaemonSet，initContainer 逐个用一遍 K8s 后端登记里的镜像与 agent 镜像。
- 没做的：Cloudflare 桥仍只有 python 的镜像（universal 三四 GB，先不放到 Cloudflare 上）；构建与在镜像里编译、测试五种语言的样例（`tests/deploy/test_images.py`，`AR_TEST_IMAGES=1`）要拉六个基础镜像，等你同意下载再跑；gVisor、Kata 上的同一项与冷启动的数字要在集群里量。

