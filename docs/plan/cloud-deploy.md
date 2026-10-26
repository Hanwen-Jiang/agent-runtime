# 云上部署与沙箱后端（D-01～D-10）

> 2026-10-26 用户要求：把系统部署到云端，以最有性价比的布局展示完整形态；沙箱考虑 Cloudflare Containers，或者容器与 microVM 混用。
> 依据：设计书 4.10（Sandbox）、4.12（SecretStore）、4.15（仓库结构）、附录 L.11、L.13、L.19；T-15 的 [Helm、KEDA 与 Telemetry](deploy-telemetry.md)。
> 范围只是 Runtime。建在 Runtime 上的应用（例如 coding-agent 仓库）各自部署，它们要 Runtime 提供的东西列在第 7 节。
> 状态（2026-10-26）：代码与配置的适配做完了（D-06 只有设计书改动稿），实体资源（机器、域名、R2、Cloudflare 账号）由用户选购配置之后在真机上验收；进度与没验的部分见第 10 节。价格与产品能力是 2026-10 查的（见第 9 节），下单前再核对一次。

## 1. 目标与不做

**目标**：一套公网可访问的环境，能演示 Runtime 的完整形态：

- 多租户的 Task Loop，Temporal 持久执行：任务跑到一半删掉 Worker Pod，任务从事件历史接着跑完。
- KEDA 按 Temporal 积压给 Worker 池扩缩。
- 两种隔离强度的沙箱：容器级（gVisor）与 microVM 级（Kata 或 Cloudflare Containers），按沙箱等级与租户策略选。
- 观测：Tempo 看调用链，Langfuse 看 token 与花费；Learning Pipeline 从 trace 产出候选 Recipe。
- 至少一个应用（coding agent）经 Task API 跑在上面。

**不做**：高可用（单节点，靠备份兜底）、多区域、正式计费、合规认证、生产级身份（第一期仍用开发 Ingress，见 D-09）。

## 2. 定下的布局

| 部分 | 选什么 | 理由 | 月成本（2026-10） |
| --- | --- | --- | --- |
| 集群 | 一台独立服务器（64 GB 内存、NVMe、带 KVM），k3s 单节点 | 完整跑起来内存是大头（Temporal、Postgres、向量库、Redpanda、Langfuse 与 ClickHouse、Tempo、沙箱）；独服比同等内存的云主机便宜得多，还带 `/dev/kvm`，Kata 能在集群里跑 | Hetzner 拍卖机约 €40–65、通常免开通费；新机 AX42 €97.30 + €49 开通费（税前） |
| 入口 | Cloudflare 免费版：DNS、TLS、代理 | 不暴露源站地址；证书省事 | 0 |
| 对象与备份 | Cloudflare R2 | S3 兼容、出网不收费；Postgres 备份、trace 归档、以后的 ObjectStore | 演示量级接近 0 |
| microVM 沙箱（第二后端） | Cloudflare Containers（Workers 付费版） | 每个实例一个 Firecracker microVM；出网可全断或按域名白名单；按 10 毫秒计费、休眠不收钱 | $5 + 按量（一次编码任务约 $0.02，见第 4.3 节） |
| Temporal | 同机自建（官方 Helm）；嫌运维重时换 Temporal Cloud | 自建不另花钱；Cloud 按量 $50/百万 Actions 起，没有月最低消费 | 0，或演示量级每月几美元 |
| 模型 | 现有 Model Gateway 接 OpenAI 兼容的供应商 | 已有 | 按用量 |

合计约 €50–110/月，另加模型费。

**为什么不是几台云主机**：Hetzner 2026 年调价后，专用 vCPU 的 CCX 与 AMD 共享 vCPU 的 CPX 涨了一倍多（例如 CCX33 €62.49 → €138.49），而且 Hetzner 云主机不给 `/dev/kvm`，Kata 跑不了。要展示多节点时，再加两台便宜的小机器跑无状态的服务（CAX 是 ARM，镜像要做成多架构）。

**地区**：用户主要在中国大陆时，德国、芬兰的机房延迟高，Cloudflare 在大陆也没有正式加速（要企业版的 China Network）。那就把集群放到香港、新加坡或日本，另选主机商（要有 KVM 的独服或支持嵌套虚拟化的云主机：AWS 的 C8i、M8i、R8i 从 2026 年 2 月起支持，GCP、Azure、DigitalOcean 也支持）。Cloudflare Containers 仍可做沙箱后端：那是服务器之间的调用，与用户在哪里无关。

## 3. 现状与缺口

| 缺口 | 现在 | 上云要的 | 工作项 |
| --- | --- | --- | --- |
| 镜像 | 没有 Dockerfile，Chart 里的镜像名是占位 | task-service、model-gateway、worker、memory、learning 五个镜像 | D-01 |
| 集群与第三方组件 | 只有 Chart，第三方组件只在 values 里填地址 | 一份能在新机器上复现的集群基线与数据层 | D-02、D-03 |
| 入口 | 生产 values 样例写的是 nginx 的 Ingress | ingress-nginx 2026 年 3 月起停止维护；用 k3s 自带的 Traefik，或 Gateway API | D-04 |
| 沙箱 | shell_run 直接调 docker 命令行（`AR_TOOL_SHELL_SANDBOX=docker`），没走设计书 4.10 的 Sandbox 接口 | 集群里不能把 docker.sock 挂给 Worker（等于节点 root）；Sandbox 接口要有 K8s 后端，shell_run 改走接口 | D-05 |
| 工作区 | Worker 的工具、沙箱与应用共用本地目录 | 单节点：共用一块本地盘还能用；之后要“工作区跟着沙箱走” | D-05（单节点）、D-06 |
| 沙箱等级 | 应用改写开发租户策略文件，task-service 提交时固定 | Task API 里逐个任务、只能调低的 effect 上限；沙箱等级到后端的路由 | D-08 |
| ObjectStore、SecretStore、Ingress | 只有开发实现（本地目录、本地文件、开发签名密钥） | 单节点演示可用（PVC + K8s Secret）；R2 的 ObjectStore 适配器先做，OpenBao 与正式身份后做 | D-09 |

## 4. 沙箱：分层与后端

### 4.1 概念

containerd 只管容器，隔离强度由它下面的 OCI 运行时决定：runc（普通容器）、runsc（gVisor，用户态内核，不需要 KVM）、Kata（每个 Pod 一台轻量 VM，后端 QEMU、Cloud Hypervisor 或 Firecracker）。三者都作为 containerd 的 shim 接入，K8s 用 RuntimeClass 按 Pod 选，所以“一部分容器、一部分 microVM”在一个集群里就能做。Cloudflare Containers 不是自管的 containerd，是托管服务（每个实例一个 Firecracker microVM），只能经 Cloudflare Worker 调用；它在这里是 Sandbox 接口的另一个后端。

### 4.2 沙箱等级到后端

| 沙箱等级（effect 上限） | 后端 | 理由 |
| --- | --- | --- |
| 只读（read） | 不起沙箱 | 不跑命令（shell_run 是 write_reversible，被拒） |
| 工作区可写（write_reversible），邀请制租户 | gVisor Pod，断网 | 启动快，任何机器都能跑 |
| 工作区可写，公开注册的租户 | microVM（Kata 或 Cloudflare） | 陌生人的代码按不可信对待（附录 L.11：microVM 适合运行不可信的用户代码） |
| 完全访问（联网，现在界面上标为未开放） | microVM + 出网白名单 | K8s 的 NetworkPolicy 只能按 IP 放行；按域名放行 Cloudflare 原生支持（`allowedHosts`），集群里要另配出网代理 |

用哪个后端由租户策略决定（Registry 的 tenant_policy 加一个字段，见 D-08），不由模型决定。

### 4.3 Cloudflare Containers 的要点

- 隔离：每个实例一个 Firecracker microVM，自己的内核与网络。
- 出网：`enableInternet = false` 全断；`allowedHosts` 是默认拒绝的白名单（2026-04 起）；只能走 80、443 与 DNS。
- 磁盘：临时的，实例休眠后清空；要跨任务保留工作区，用快照或 R2。
- 冷启动 1–3 秒；最大规格 standard-4（4 vCPU、12 GiB、20 GB 盘）。
- 计费：CPU 按实际用量（$0.000020/vCPU 秒），内存、磁盘按配置（$0.0000025/GiB 秒、$0.00000007/GB 秒）。
  一次编码任务开 standard-2（1 vCPU、6 GiB、12 GB）存活 15 分钟、实际跑命令 3 分钟：内存 $0.0135 + CPU $0.0036 + 磁盘 $0.0008 ≈ $0.02。
- 只能经 Worker 与 Durable Object 访问：要写一个桥（Cloudflare Worker），Runtime 的适配器经 HTTPS 调它，桥要鉴权。

## 5. 工作项

档位与评审按 [强档实施计划](strong-tier-plan.md) 第 1 节；评审用它第 6.2 节的模板。每项先写验收测试、经人确认再实现。

### D-01 镜像（中档）

**目标**：Runtime 的五个服务都有镜像，能被 Chart 直接用。

**交付**：`deploy/images/` 下的 Dockerfile（task-service、model-gateway、worker、memory、learning）；构建脚本；打标签时由 CI 推到 GHCR。

**难点与防错**

- 以 uid 10001 运行、根文件系统只读（与 Chart 的 securityContext 一致），可写的只有挂载的卷与 `/tmp`。
- Worker 镜像的 Python 依赖按 `uv.lock` 装（`--locked`），不在镜像里联网解析。
- 镜像里不能有 `.env`、密钥、开发签名密钥；`deploy/dev` 的开发文件不打进去。
- 先只做 amd64；要上 ARM 节点时再做多架构。

**验收**

1. 五个镜像都能构建；以 10001 运行、只读根文件系统下能启动（task-service、model-gateway、memory 的 `/healthz` 返回 200；Worker 能连上 Temporal 开始轮询）。
2. `tests/deploy` 检查 Chart 引用的镜像名与构建产物一致。
3. 扫描镜像内容：没有 `.env`、`*.key`、`secrets.json`。

**依赖**：无。

### D-02 单节点集群基线（中档）

**目标**：一份脚本把一台新机器装成可用的 k3s 集群，含两种沙箱运行时。

**交付**：`deploy/cluster/`：k3s 配置；containerd 的运行时配置（gVisor 的 runsc、Kata 用 kata-deploy）；RuntimeClass `gvisor`、`kata`；命名空间 `agent-runtime`、`sandboxes`、`data`、`observability`；cert-manager；KEDA；Traefik 的配置；一份安装说明。

**难点与防错**

- Kata 用 Cloud Hypervisor 或 QEMU 后端；Firecracker 不支持 virtio-fs，用它要另配 devmapper 快照器（动手前查 kata-deploy 的文档确认）。
- `sandboxes` 命名空间：Pod Security 用 restricted；ResourceQuota 与 LimitRange；默认拒绝进出的 NetworkPolicy（k3s 自带网络策略控制器）；Pod 不挂 ServiceAccount 令牌。
- Traefik 不能缓冲 SSE（Task API 的事件流）。
- 密钥用 SOPS + age 加密进仓库，解密只在部署机上做。

**验收**

1. 在一台新机器上跑安装脚本后，`kubectl get runtimeclass` 有 `gvisor` 与 `kata`。
2. 在 `sandboxes` 里起一个 gvisor Pod，`dmesg` 显示 gVisor；起一个 kata Pod，内核版本与宿主不同。
3. `sandboxes` 里的 Pod 访问外网与集群内服务都失败；Pod 里没有 ServiceAccount 令牌文件。
4. 经 Traefik 的 SSE 事件逐条到达（不攒到连接结束）。

**依赖**：无（与 D-01 并行）。

### D-03 数据层与第三方组件（中档）

**目标**：Runtime 依赖的第三方组件在集群里跑起来，并有备份。

**交付**：CloudNativePG（一套 Postgres：业务库、Temporal、Langfuse 各一个库）、Temporal（官方 Helm）、向量库（按设计书现行版本用 Qdrant；设计书改成 Milvus 后切，见 `docs/plan/design-changes-2026-10.md`）、Redpanda、Langfuse、Tempo 与 Grafana；Postgres 每天备份到 R2；对应的 values 文件。

**难点与防错**

- 业务库的连接角色不能是超级用户、不能带 BYPASSRLS（行级安全，S-12a）。
- 备份要演练恢复，不只看备份任务成功。
- 单节点资源有限：每个组件给出请求与上限，总和留出沙箱的余量（建议沙箱至少 16 GB）。

**验收**

1. 各组件健康检查通过；Temporal 建好 Runtime 用的命名空间。
2. 查业务库连接角色：`rolsuper = false`、`rolbypassrls = false`。
3. 从 R2 的备份恢复到另一个库，行数与校验和一致。
4. Collector 的三路导出（Tempo、Langfuse、Redpanda）都能看到同一次任务的 span。

**依赖**：D-02。

### D-04 Runtime 上线（中档）

**目标**：Runtime 的 Chart 在集群里跑起来，公网经 HTTPS 访问 Task API。

**交付**：`deploy/helm/agent-runtime/values-cloud.yaml`；Chart 的入口改为可选 Traefik（IngressRoute）或 Gateway API（HTTPRoute），不再默认 nginx；ObjectStore 用本地 PVC（单节点，所有 Pod 在同一台机器上）；部署说明。

**难点与防错**

- 单节点上 ObjectStore 与工作区用 local-path 的卷能共用，是因为所有 Pod 在一台机器上；多节点之前要换成 D-09 的 R2 适配器与 D-06。
- Registry 的内部端口仍然不能有对外入口（T-08）。
- 改 Chart 后 `tests/deploy` 的结构检查要跟着改，不能删检查项。

**验收**

1. `helm upgrade --install` 成功；Task API 经 HTTPS 可达，Registry 端口从公网不可达。
2. mock 模型下提交一个 Task 跑到 done。
3. 任务跑到一半删掉处理它的 Worker Pod，任务照样跑完，结果与不删时一致（S-07 的场景在线上演一遍）。
4. 积压时 KEDA 把 Worker 池扩到不止一个副本，积压消化后缩回去。

**依赖**：D-01、D-02、D-03。

### D-05 Sandbox 的 K8s 后端，shell_run 改走 Sandbox 接口（强档）

**目标**：沙箱在集群里以一次性 Pod 运行，gVisor 与 Kata 两种 RuntimeClass；shell_run 不再直接调 docker。

**交付**：`agentruntime_core.adapters.k8s_sandbox`（Sandbox 接口的 K8s 实现）：create 起 Pod（RuntimeClass、资源上限与网络取自 SandboxSpec）、exec 经 K8s 的 exec 接口、读写文件、destroy 删 Pod、空闲超时回收；Worker 的 shell_run 按配置选后端（local、docker、k8s）；Worker 的 RBAC 只在 `sandboxes` 命名空间里建、删 Pod 与 exec；单节点上工作区以 subPath 挂进沙箱 Pod。

**难点与防错**

- 要用 Kubernetes 的客户端库，是设计书 4.15 之外的新依赖：开工前说明理由、经人确认（备选：只用标准库直接调 API Server 的 REST 与 exec 的 WebSocket）。
- 作用域：沙箱只属于创建它的租户与 Task；别的作用域访问是 not_found（同 S-02 的一致性语义）。Pod 的标签与名字里不放可被猜到、能拼出别人沙箱的东西。
- exec 超时不抛异常，返回 timed_out（与现在的 docker 后端一致）；输出截断规则不变。
- 只挂本会话的工作副本（subPath），不挂工作区根。
- Worker 自己的 ServiceAccount 不能在别的命名空间建 Pod，也不能读 Secret。

**验收**

1. Sandbox 一致性测试（`agentruntime_core.conformance.sandbox`）对 K8s 后端在 `gvisor` 与 `kata` 两个 RuntimeClass 上都通过。
2. shell_run 经 K8s 后端时，现有的 shell_run 测试（超时、截断、非零退出码、环境变量不外泄）全部通过。
3. 越权：用租户 A 的作用域访问租户 B 的沙箱是 not_found；沙箱 Pod 里看不到别的会话的工作副本。
4. Worker 的 ServiceAccount 在 `agent-runtime` 命名空间建 Pod 被拒；沙箱 Pod 里没有 ServiceAccount 令牌。
5. 断网：沙箱里访问外网与集群内服务都失败。

**依赖**：D-02（集群），D-04（线上验证）。

### D-06 工作区跟着沙箱走（强档，设计改动先行）

**目标**：工作区由 Runtime 的沙箱持有，Worker 的工具与应用都经 Runtime 读写，不再共用一块盘。这是多节点与 Cloudflare 后端的前提。

**先做的设计改动**：设计书 4.10 现在只有 Sandbox 接口，没有“工作区”这一层（对照 Aster 的 Workspace Manager，`docs/reference/aster-runtime-gap.md`）。建议在设计书加一节 Workspace：

- 工作区属于（租户、workspace id），生命周期跨 Task；由某个沙箱后端承载，空闲时存档到 ObjectStore（快照或打包），用时恢复。
- repo_read、repo_list、repo_grep、repo_edit、shell_run 都在工作区所在的沙箱里执行。
- 应用经 Task API 之外的 Workspace API 取 diff、提交、推送（推送用应用传入的短期凭据，Runtime 不保存）。

按 CLAUDE.md 的规则，设计书由人更新原文档后重新导出；在那之前本项不开工。

**验收**（设计确认后细化）：工具在远端沙箱里工作；任务之间工作区经存档、恢复后内容一致（逐文件哈希相同）；两个会话的工作区互不可见；应用经 Workspace API 拿到的 diff 与沙箱里 `git diff` 一致。

**依赖**：D-05；设计书更新。

### D-07 Cloudflare Containers 后端（强档）

**目标**：Sandbox 接口的第二个 microVM 后端，用于公开注册的租户与“完全访问”。

**交付**：`deploy/cloudflare/sandbox-bridge`（Cloudflare Worker + Durable Object + Container，每个沙箱一个 Durable Object）；`agentruntime_core.adapters.cloudflare_sandbox`（经 HTTPS 调桥）；桥的鉴权令牌由 SecretStore 签发、短期有效。

**难点与防错**

- 桥用 TypeScript 与 wrangler，是新的语言与工具链：开工前说明理由、经人确认。
- 默认 `enableInternet = false`；SandboxSpec 的 `network = allowlist` 映射到 `allowedHosts`；不打开 `interceptHttps`。
- 磁盘是临时的：工作区靠 D-06 的存档与恢复。
- 计量：每个沙箱的存活时间、CPU 时间记进 trace（`execute_tool` span 的属性），算进 Task 的花费。
- 桥只接受带有效令牌的请求；令牌绑定租户与沙箱，不能拿 A 的令牌操作 B 的沙箱。

**验收**

1. Sandbox 一致性测试对 Cloudflare 后端通过（真实端点，设 `AR_LIVE_CLOUDFLARE=1` 才跑，花钱、不进 CI）。
2. `network = none` 时出网失败；`allowlist` 时只有名单内的主机可达，别的返回拒绝。
3. 没有令牌、令牌过期、令牌属于别的租户：一律拒绝。
4. 跑完一个沙箱后，trace 里有它的存活时间与 CPU 时间。

**依赖**：D-05（接口与 shell_run 的改造）、D-06（工作区）。

### D-08 沙箱等级的路由与逐个任务的 effect 上限（强档，含契约变更）

**目标**：应用不再改写租户策略文件；沙箱等级经 Task API 传入，租户策略决定用哪个后端。

**交付**：`SubmitTaskRequest` 加一个可选的 `effect_ceiling`，只能比租户上限更低（走 S-01：改 proto、`buf breaking`、人审）；Registry 的 tenant_policy 加沙箱后端的选择（例如 `sandbox_backend: gvisor | kata | cloudflare`，按 effect 分档）；Worker 按策略选后端。

**难点与防错**

- 调高被拒，不是静默截断（应用要知道它要的等级没给）。
- 后端由租户策略决定，模型与请求都不能指定。
- “完全访问”只能落在 microVM 后端；策略配成 gVisor 时拒绝启动这类任务。

**验收**

1. 契约测试：`effect_ceiling` 低于租户上限时生效，高于时请求被拒并说明原因。
2. 同一个任务按租户策略分别落到 gvisor 与 kata（或 cloudflare）后端，trace 里记着用的是哪个。
3. 联网的任务在 gVisor 策略下被拒。

**依赖**：D-05；S-01 流程。

### D-09 对象存储与密钥的生产实现（中档）

**目标**：去掉“所有 Pod 共用一块盘”的前提，为多节点做准备。

**交付**：ObjectStore 的 S3 兼容适配器（Python 与 Go 各一，对 R2）；SecretStore 的 OpenBao 适配器（设计书 4.12）可以后做；正式身份（Ingress）这一期不做，演示仍用开发 Ingress，并在入口处限流。

**难点与防错**：Claim-Check 两端（task-service 与 Worker）都要换；预签名地址的过期时间与现在的开发实现一致。

**验收**：ObjectStore 一致性测试对 S3 适配器通过（真实 R2 端点设开关才跑）；task-service 与 Worker 在不共用卷的情况下，大载荷的 Claim-Check 照样取回。

**依赖**：D-04。

### D-10 演示与运维（轻档）

**目标**：能照着做的演示与日常运维。

**交付**：演示手册（登录应用、下任务、删 Worker Pod 续跑、KEDA 扩缩、两种沙箱、Tempo 与 Langfuse、Learning Pipeline）；Grafana 面板（任务数、各后端的沙箱时长与花费、模型花费）；预算告警（模型供应商、Cloudflare）；每个租户的沙箱配额。

**验收**：照手册从头做一遍每一步都能复现；面板上能看到一次任务的模型花费与沙箱花费。

**依赖**：D-04、D-05。

## 6. 顺序与里程碑

| 里程碑 | 包含 | 做完之后 |
| --- | --- | --- |
| M1 先上线 | D-01、D-02、D-03、D-04、D-05（gVisor） | 公网可用；邀请制租户在 gVisor 沙箱里跑任务；工作区仍是单节点本地盘 |
| M2 microVM | D-05（Kata）、D-08 | 按租户策略选 gVisor 或 Kata；沙箱等级经 Task API 传入 |
| M3 完整形态 | D-06（设计书更新后）、D-07、D-09、D-10 | 工作区跟着沙箱走；Cloudflare 做第二个 microVM 后端；可以加节点 |

D-01 与 D-02 可以并行；D-05 动 Worker 的工具（不在 Task Loop → Stage Runner → Node Executor 核心链路上），可以与 D-03、D-04 并行。

## 7. 应用要 Runtime 提供的（以 coding-agent 为例）

应用各自部署（coding-agent 仓库自己出镜像与清单），经 Task API 接 Runtime。现在本机靠共用的东西接入（`dev_stack.py status --json` 报出：工作区根、开发租户策略文件、开发签名密钥的位置），上云后逐项换掉：

| 现在共用的 | 上云后 | 工作项 |
| --- | --- | --- |
| 开发租户策略文件（沙箱等级） | `SubmitTaskRequest.effect_ceiling` | D-08 |
| 开发签名密钥（应用自己签 Runtime 令牌） | M1、M2 仍用开发 Ingress，应用与 task-service 共用一个 K8s Secret；正式身份另立工作项 | D-09 之后 |
| 工作区目录（应用克隆仓库、看 diff、推送） | M1、M2：应用与 Worker 在同一台机器上共用一个本地卷；M3：Workspace API | D-06 |

## 8. 与设计书的出入

- 设计书 4.10 说“本期只定义接口；开发时用本地子进程实现”。为 coding agent 加的 Docker 沙箱绕过了接口，直接在 shell_run 里调 docker；D-05 改回走接口。
- 设计书没有“工作区”一层；D-06 要先改设计书（建议见 D-06）。
- 设计书 4.4 的向量库现行是 Qdrant；改为 Milvus 的草稿（C1）还没进设计书，D-03 按现行版本做。
- 设计书改动稿：[design-changes-workspace.md](design-changes-workspace.md)（W1 Sandbox 的后端、W2 Workspace 层、W3 effect_ceiling、W4 租户策略的沙箱后端）。

## 10. 进度（2026-10-26）

| 项 | 做了什么 | 本机验过的 | 要真机器或人做的 |
| --- | --- | --- | --- |
| D-01 | `deploy/images`：Go 与 Python 两种 Dockerfile、sandbox-agent、sandbox-python；`scripts/build_images.py`；`.github/workflows/images.yml` | `tests/deploy/test_images.py` 的静态检查（以 10001 运行、基础镜像写明版本、按 uv.lock 装、`.dockerignore`、Chart 的镜像名） | 构建与运行（`AR_TEST_IMAGES=1`，要拉基础镜像、下载依赖，还没跑） |
| D-02 | `deploy/cluster`：k3s 配置、Pod Security 默认值、runsc 的 containerd 模板、Traefik、install.sh、verify.sh、SOPS 密钥样例 | `tests/deploy/test_cloud.py`；沙箱命名空间的约束在 kind 上（D-05 的测试） | 新机器上跑 install.sh 与 verify.sh（gVisor、Kata 的内核）；经 Traefik 的 SSE（`scripts/sse_probe.py`） |
| D-03 | `deploy/data`：CloudNativePG（三个角色无超级用户、无 BYPASSRLS，每天备份到 R2）、恢复演练脚本、Temporal、Qdrant、Redpanda、Langfuse、Tempo、Grafana 的 values | 静态检查；内存预算（requests 合计不超过 60 GiB） | 各组件起来、角色核对、从 R2 恢复演练、Collector 三路导出；第三方 Chart 的键名按 versions.conf 的版本核对 |
| D-04 | Chart：入口改 Traefik / Gateway API / 通用 Ingress；Worker 经 Sandbox 跑 shell_run 与只在沙箱命名空间里的 Role；ObjectStore 的 S3 变量；可写的 SecretStore 加种子（`AR_SECRETS_SEED_FILE`）；`values-cloud.yaml` | 三套 values 的 helm lint 与结构检查；Worker 的 RBAC 在 kind 的 API Server 上（`kubectl auth can-i`） | helm 装上、mock 模型跑到 done、删 Worker Pod 续跑、KEDA 扩缩 |
| D-05 | K8s 沙箱后端、sandbox-agent、shell_run 走 Sandbox | kind 上的一致性测试、非 root、只读根、没令牌、断网、令牌、回收；Worker 经 K8s 后端跑 shell_run | gVisor 与 Kata 的 RuntimeClass（kind 里没有） |
| D-06 | 只有设计书改动稿 | — | 人确认 W2、更新设计书之后再开工 |
| D-07 | Cloudflare 桥（TypeScript）与 Python 适配器 | 适配器对着假桥 | 桥本身（本机没有 Node）；真端点（`AR_LIVE_CLOUDFLARE=1`，花钱） |
| D-08 | `SubmitTaskRequest.effect_ceiling`、租户策略的 `sandbox.backend` | 契约测试、Task API 的测试、按策略选后端 | **proto 与 schema 的改动待人审**，之后更新 `proto/breaking-baseline.binpb` |
| D-09 | ObjectStore 的 S3 兼容实现（Python 与 Go，标准库的 SigV4），task-service 与 Worker 接上 | 一致性测试对着会核对签名的假服务（带与不带前缀）、AWS 文档里的签名例子 | 真实 R2（`AR_LIVE_S3=1`）；task-service 与 Worker 不共用卷时的 Claim-Check（线上） |
| D-10 | `deploy/cluster/demo.md`；Grafana 面板；`runtime.sandbox` span（后端、存活、执行与 CPU 时间）；K8s 后端每个租户的配额 | 面板查的名字与代码对得上；CPU 时间与配额在 kind 上 | 照手册在线上走一遍；面板的 TraceQL 指标查询在真 Tempo 上 |

实现中与本计划写法不同的：

- D-01 验收 1 说 Memory Service 的 `/healthz` 返回 200：它只有 gRPC，镜像测试改为核对 gRPC 端口接受连接。
- D-07 说沙箱的存活时间与 CPU 时间记在 `execute_tool` span 上：tool_loop 里的工具调用没有 execute_tool span，program 节点又会嵌套重名，所以单独记一个 `runtime.sandbox` span（不挂在任务的调用链下，按租户与时间找）。
- D-04 说 ObjectStore 用本地 PVC：开发 SecretStore 也要可写（Claim-Check 第一次用到租户时写回 kek，副本要共用），所以多了一个 SecretStore 的卷与种子合并；这是单节点的做法，正式环境换 OpenBao。

## 9. 来源

- Cloudflare Containers：[价格](https://developers.cloudflare.com/containers/pricing/)、[平台细节](https://developers.cloudflare.com/containers/platform-details/)、[出网控制](https://developers.cloudflare.com/containers/configuration/outbound-traffic/)、[2026-04-13 的出网白名单更新](https://developers.cloudflare.com/changelog/post/2026-04-13-sandbox-outbound-workers-tls-auth/)、[Sandbox 的隔离](https://developers.cloudflare.com/sandbox/concepts/security/)
- 嵌套虚拟化：[AWS EC2 文档](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/amazon-ec2-nested-virtualization.html)、[The Register 的报道](https://theregister.com/2026/02/17/nested_virtualization_aws_ec2)、[Hetzner 云主机不给 KVM 的实例](https://hive.tuist.dev/forage/items/github-issue/6ecca7f8-ed22-4c7e-b5ed-8534ece782c0)、[云主机上的 Firecracker](https://blog.alexellis.io/how-to-run-firecracker-without-kvm-on-regular-cloud-vms/)
- Hetzner 价格：[官方调价说明](https://docs.hetzner.com/de/general/infrastructure-and-availability/price-adjustment/)、[2026 年 6 月云主机调价](https://privatedevops.com/news/hetzner-june-2026-cloud-price-increase-what-to-do)、[云主机与独服对比](https://wz-it.com/en/blog/hetzner-cloud-vs-dedicated-server/)
- [Temporal Cloud 价格](https://docs.temporal.io/cloud/pricing)、[没有最低消费](https://temporal.io/blog/paygo-developer-support)
- [Ingress NGINX 停止维护](https://kubernetes.io/blog/2025/11/11/ingress-Nginx-retirement/)
- Kata：[kata-deploy](https://github.com/kata-containers/kata-containers/blob/3.3.0-test/tools/packaging/kata-deploy/README.md)、[AWS 上的 Kata](https://aws.amazon.com/blogs/containers/enhancing-kubernetes-workload-isolation-and-security-using-kata-containers/)
- 对照：[E2B 的计价](https://docs.e2b.dev/faq/calculate-sandbox-price)、[Fly.io 2026 年价格](https://fly.io/pricing-update/)
