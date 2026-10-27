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
