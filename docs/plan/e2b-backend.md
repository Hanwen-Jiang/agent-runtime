# 自托管 E2B 后端（e2b 分支）

> 2026-10-27 按用户要求建的**可插拔分支**：本分支 = main + 自托管 E2B 作为 Sandbox 的又一个 microVM 后端。**main 永远不接 E2B**，只在 W1 与 D-18 处简要提到本分支。
> 可插拔的意思：E2B 的东西都在新文件里；共用代码只在沙箱后端登记（`tools/sandboxes.py`）多认一种 `kind: e2b`（三处，各一两行）。main 往前走之后，把 main 合进本分支，按第 5 节补对接，跑第 6 节的验收。
> 状态：**设计与脚手架**（适配器、Worker 登记、部署 overlay、防火墙、模板脚本，都对着假 E2B 测过）；真机验收要一台 KVM 节点（E-05）。设计书是 Claude Docs 的导出副本，本分支同样不直接改；要并进设计书时用第 8 节的文字。
> 依据：E2B 的开源运行时 [e2b-dev/runtime](https://github.com/e2b-dev/runtime)（原名 infra，Go，Apache-2.0），固定在提交 `67b318240f4b6acc497411aa968b817b338f62e6`（2026-10-12，满两周；E2B 最近的 tag 2026.30 早于 Embed，没有单机打包）。[e2b-dev/e2b](https://github.com/e2b-dev/e2b) 只是 SDK 仓库，本分支不用（不引入 SDK）。

## 1. 什么时候值得用

三种 microVM 后端的隔离强度是同一档（KVM 硬件虚拟化，各自的客户机内核）；区别在“谁管生命周期”和 VMM 能做什么：

| | K8s + Kata（main） | Cloudflare Containers（main） | 自托管 E2B（本分支） |
| --- | --- | --- | --- |
| 控制面 | K8s 调度器、kubelet、containerd | Cloudflare 自己的调度（Worker、Durable Object） | E2B 的 api + 每个节点上的 orchestrator（自己的放置算法） |
| VMM | Cloud Hypervisor（Kata 4 默认 Dragonball，也可 QEMU、Firecracker） | Firecracker（托管） | Firecracker，由 orchestrator 直接驱动 |
| 新建 | 冷启动一个 VM（秒级） | 1～3 秒 | 从模板的内存快照恢复，不走内核启动 |
| 暂停 | 没有（Pod 只有在跑和删掉） | 休眠即停机，不留内存 | 暂停后内存、进程、文件都在；来流量或 connect 时恢复；还能 fork |
| 文件进 VM | virtio-fs，直接挂节点上的工作区 | 临时盘 | NBD 上的写时复制根盘；暂停时的增量进快照 |
| 出网 | NetworkPolicy 只能按 IP；按域名靠 D-16 的网关 | 全断或按域名白名单 | 按域名的允许与拒绝（SNI、Host）；还能经 SOCKS5 的 egressProxy 交给我们的网关 |
| 机器要求 | KVM | 无（托管） | KVM、内核 6.8+、预留大页、nbd 模块；orchestrator 要 root、宿主网络，专用节点 |
| 打包 | 我们自己的 deploy/cluster | 托管 | 公开的只有单机的 Embed；多节点要自己组装（官方是 BYOC） |

**结论**：节点有 KVM 时 Kata 就够；E2B 只在要“空闲时带内存暂停、回来马上恢复”（会话里的 dev server、数据库不用重起）、fork、或高密度快速新建时值得接。代价是第二套控制面（它自己的 Postgres、Redis、ClickHouse）与一台专用节点。

## 2. 拓扑

- 一台**单独的** KVM 机器，作为 k3s 的 agent 节点加入，标签 `e2b.dev/single-node=true`、污点 `agentruntime.io/dedicated=e2b:NoSchedule`；上面只跑 E2B Embed 的 StatefulSet（宿主网络与 pid、三个特权容器）。
- Worker 经 E2B 的 api（3000）管生命周期，经 client-proxy（3002，按头路由）连到沙箱里的 envd（49983）执行命令、读写文件。别的十一个端口只在本机：5008 是没有认证的 orchestrator 控制口，谁连上谁就有整个 E2B。`deploy/e2b/firewall.sh` 只放行集群来的 3000、3002。
- E2B 只有一个 team，API key 是部署共用的（Secret `e2b/e2b-api` 的 `TEAM_API_KEY`，同一个值进 Runtime 的 SecretStore：平台作用域的 `sandbox/e2b-api-key`）。上游的 `ready` 容器会把 key 打进日志，overlay 换掉了它。

## 3. 适配器（`agentruntime_core.adapters.e2b_sandbox`、`adapters.envd`）

- **生命周期**：`POST /v2/sandboxes`（templateID 取自 runtime 对应的模板；timeout 就是空闲期限；带 Idempotency-Key，响应丢了用同一个 key 重试，不会多建）、`GET /sandboxes/{id}`、`DELETE`、`POST /sandboxes/{id}/timeout`（每次用时顺延，同一个环境最多每分钟一次）、`POST /sandboxes/{id}/pause`、`POST /v2/sandboxes/{id}/connect`（恢复，并拿到 envd 令牌）、`GET /v2/sandboxes`（按部署标签列出 running 与 paused，跟着分页，给环境表对账）。
- **命令与文件直接经 envd**，模板里不放 sandbox-agent：E2B 模板的启动命令在构建时就跑起来并进了快照，每个环境的令牌与环境变量（包镜像地址、出网凭据、测试密钥）带不进去；envd 的每个进程可以带 envs，令牌由 E2B 签发。进程用 Connect 协议的 `process.Process/Start`（服务端流）与 `SendInput`、`CloseStdin`、`SendSignal`；超时发 SIGKILL，返回 timed_out。文件用 `/files`（octet-stream）。只用标准库。
- **作用域**：metadata 记 `agentruntime.owner`（租户 ID 或 `_platform`）与 `agentruntime.deployment`；每次访问先核对（进程内缓存，换了 Worker 副本时经 `GET /sandboxes/{id}` 再查），不对就是 not_found。envd 令牌只在内存，不进日志、trace 与错误信息。
- **网络**：none → `allow_internet_access: false`；allowlist → `network.allowOut` 加 `denyOut: 0.0.0.0/0`；沙箱的端口一律要 traffic 令牌（`allowPublicTraffic: false`）。配了 `egress_proxy` 时 `network.egressProxy` 指到出网网关的 SOCKS5 入口，用户名是 `e2b-<随机>`、口令由部署的 `sandbox/token-key` 派生（HMAC），记在 metadata 的 `agentruntime.egress_user`。
- **资源**：CPU、内存由模板定；spec 的数值只校验。CPU 时间 envd 不报（`cpu_time` 为 None），存活时间照记。
- **工作区**：VM 的盘只属于这个沙箱，绑定 workspace 要等 D-06 的存档与恢复（与 Cloudflare 一样报 `unsupported_workspace`）。
- **错误**：400、422 invalid；401、403 denied（消息里没有 key）；404 not_found（建的时候是模板不存在：invalid `unknown_template`）；429（并发或频率上限）、409、5xx transient，可以按 D-12 回退到下一个后端。

Worker 一侧：后端登记的 `kind: e2b`（`tools/sandbox_e2b.py`），要写明 `isolation: microvm`，有 `api_url`、`sandbox_url`、`template`（或 `templates`）、`deployment`、`pause_on_idle`、`egress_proxy`、`envs`。Helm 用 `deploy/e2b/values-e2b.yaml` 叠在 `values-cloud.yaml` 后面。

## 4. 模板与运行环境

- 模板从 Runtime 的沙箱镜像构建，镜像**按 digest 固定**：`scripts/e2b_template.py`（`POST /v3/templates` → `POST /v2/templates/{id}/builds/{build}` 的 `fromImage` → 轮询状态）。私有仓库的凭据、E2B 的 key 只经环境变量给。
- main 的 D-14 有了 `universal` 镜像之后，模板改用它，与 K8s、Cloudflare 同一份 Dockerfile，换后端不涉及迁移语言环境。
- 一个 runtime 一个模板名（登记里 `templates: {python: agentruntime-python, …}`）。

## 5. 与 main 的工作项怎么接（合并 main 之后补）

| main 的工作项 | 本分支要补的 |
| --- | --- |
| D-11 环境表 | 对账时 E2B 也是一个后端：`E2BSandboxProvider.list_environments()` 已经按部署标签列出；接到对账工作流里。暂停的沙箱也记在环境表里（占存储，计入配额）。 |
| D-12 配额与回退 | E2B 的 isolation 是 microvm，可以与 kata、cloudflare 互为回退；429（E2B 的并发上限）是 transient，会回退。E2B 的 team 上限（`sandbox_max_length_in_hours` 等）与我们的配额并存，取更严的。 |
| D-13 会话级沙箱 | 空闲到期暂停（`pause_on_idle` → autoPause），下次用时 connect 恢复，内存与进程都在；活满 max_lifetime 才删。这是 E2B 后端存在的主要理由。 |
| D-14 运行环境 | 模板改用 `universal` 镜像的 digest。 |
| D-16 出网网关 | 网关加一个 SOCKS5 入口（RFC 1928、1929，标准库能写），按 egressProxy 的用户名在环境表里查到环境与授权；这样 E2B 沙箱的出网也有字节上限与逐连接审计。没有网关时只能用 E2B 原生的 allowOut（同 Cloudflare：没有字节上限与审计）。 |
| D-06 工作区 | 存档与恢复做好后，E2B 后端也能绑定 workspace；暂停期间不用存档（盘在快照里）。 |

## 6. 工作项

| 编号 | 内容 | 状态 |
| --- | --- | --- |
| E-01 | 适配器与 envd 客户端；Sandbox 一致性测试对假 E2B 通过；网络映射、作用域、换副本、暂停与恢复、顺延、幂等重试、列出与分页、错误映射的专门用例 | 已做（`libs/py-core/tests/adapters/test_e2b_sandbox.py`） |
| E-02 | Worker 的后端登记 `kind: e2b`；API key 取自 SecretStore；登记文件读进来时就核对 | 已做（`services/py-worker/tests/test_sandbox_e2b.py`） |
| E-03 | 部署：Embed 的 overlay（固定提交、四个补丁）、防火墙、Secret 样例、Helm 叠加、模板脚本 | 已做脚手架（`tests/deploy/test_e2b.py`）；`kubectl kustomize` 要联网取上游，没在本机跑 |
| E-04 | 合并 main 后按第 5 节对接 D-11、D-12、D-13、D-16 | 2026-10-27 合并了 main（到 D-17）。已接：exec 的 `env`（包镜像、代理与测试用密钥只给那一条命令）；`list_environments` 改成 main 的 `SandboxLister`，对账照常列 E2B；`SandboxSpec.image`（D-17）拒绝（模板定环境）；登记里 `pause_on_idle` 的后端空闲到期不删、复用时找回暂停的环境，只在活满时删（`test_sandbox_sessions.py`）；回退按 microvm 与 429 transient（D-12 原有逻辑）。没接：出网网关的 SOCKS5 入口（main 的网关只有 HTTP 代理），所以 E2B 按托管后端处理：授权的主机走 E2B 原生的 allowOut，没有字节上限与逐连接审计，带用量上限的授权落不到 E2B；预览地址（`SandboxPreview`）还没做 |
| E-05 | 真机验收：KVM 节点上 `kubectl apply -k deploy/e2b`、建模板、`AR_LIVE_E2B=1` 的一致性测试与断网、暂停后恢复时后台进程还在 | 要机器（你来买） |

**验收**（E-05）：

1. `AR_LIVE_E2B=1` 时 Sandbox 一致性测试对真实的 E2B 通过；`network = none` 时出网失败。
2. 暂停后恢复：先起一个后台进程写文件，暂停，换一个 Worker 副本恢复，进程还在、文件还在。
3. 从集群外连 5008、5010 失败；从 Worker 的 Pod 连 3000、3002 成功。
4. `kubectl -n e2b logs e2b-0 --all-containers` 里没有 team 的 API key。

## 7. 风险与限制

- Embed 是单机、明文 HTTP、没有高可用：只放内网，防火墙必须先跑。
- orchestrator 要 root、宿主网络与 pid 命名空间，还改内核参数：节点只给 E2B 用。
- 多节点：E2B 的代码支持多节点放置（节点发现可以用 K8s、DNS、静态列表），但公开的打包只有单机；第二台 E2B 节点要自己写部署，或者买 E2B 的 BYOC。
- E2B 的接口演进快（v2 的创建与 connect 才几个月）：升级上游要同时改 overlay 的提交、适配器里的接口说明，并跑 E-05。
- 哈希种子（`SANDBOX_ACCESS_TOKEN_HASH_SEED`）能从沙箱 id 算出 envd 与 traffic 令牌，和 ADMIN_TOKEN 一样是最高级别的密钥。

## 8. 设计书改动稿（要并进设计书时用）

4.10 的“实现”在 Cloudflare 那一条之后加：

> - **自托管 E2B**（可选，单独的分支）：E2B 的开源运行时跑在专用的 KVM 节点上，每个环境一台 Firecracker microVM，从模板的内存快照恢复；空闲时可以连内存一起暂停、用时恢复。生命周期经 E2B 的 api，命令与文件直接经沙箱里的 envd；租户隔离由 Runtime 按 metadata 核对（E2B 只有一个 team）。出网可以按域名放行，或经 SOCKS5 交给出网网关。

L.11 的 E2B 那一句改为：

> 托管或自托管的 E2B 作备选，只在要“带内存暂停”的长寿命沙箱或 fork 时接，实现在单独的分支（e2b），main 不依赖它。
