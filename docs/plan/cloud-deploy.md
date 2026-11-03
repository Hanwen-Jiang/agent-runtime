# 云上部署与沙箱后端（D-01～D-17）

> 2026-10-26 用户要求：把系统部署到云端，以最有性价比的布局展示完整形态；沙箱考虑 Cloudflare Containers，或者容器与 microVM 混用。
> 依据：设计书 4.10（Sandbox）、4.12（SecretStore）、4.15（仓库结构）、附录 L.11、L.13、L.19；T-15 的 [Helm、KEDA 与 Telemetry](deploy-telemetry.md)。
> 范围只是 Runtime。建在 Runtime 上的应用（例如 coding-agent 仓库）各自部署，它们要 Runtime 提供的东西列在第 7 节。
> 状态（2026-10-26）：代码与配置的适配做完了（D-06 在 2026-10-27 按设计书改动稿实现，coding-agent 一侧卡在契约），实体资源（机器、域名、R2、Cloudflare 账号）由用户选购配置之后在真机上验收；进度与没验的部分见第 10 节。价格与产品能力是 2026-10 查的（见第 9 节），下单前再核对一次。

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

containerd 只管容器，隔离强度由它下面的 OCI 运行时决定：runc（普通容器）、runsc（gVisor，用户态内核，不需要 KVM）、Kata（每个 Pod 一台轻量 VM，后端 QEMU、Cloud Hypervisor、Dragonball 或 Firecracker；我们用 Cloud Hypervisor，跑在 Kata 4 默认的 Rust 运行时上，handler `kata-clh-runtime-rs`）。三者都作为 containerd 的 shim 接入，K8s 用 RuntimeClass 按 Pod 选，所以“一部分容器、一部分 microVM”在一个集群里就能做。Cloudflare Containers 不是自管的 containerd，是托管服务（每个实例一个 Firecracker microVM），只能经 Cloudflare Worker 调用；它在这里是 Sandbox 接口的另一个后端。

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

按 CLAUDE.md 的规则，设计书由人更新原文档后重新导出；2026-10-27 用户确认 W2、要求先实现（原文档仍待人更新），实现的细化与进度见 [workspace-layer.md](workspace-layer.md)。

**验收**（细化见 workspace-layer.md 第 6 节）：工具在远端沙箱里工作；任务之间工作区经存档、恢复后内容一致（逐文件哈希相同）；两个会话的工作区互不可见；应用经 Workspace API 拿到的 diff 与沙箱里 `git diff` 一致。

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

### D-11～D-17：沙箱平台

2026-10-27 用户要求加入，同日确认改动稿并要求开始实现（设计书原文档仍待人更新，见 [design-changes-workspace.md](design-changes-workspace.md) 第 8 节）。改动稿：[design-changes-workspace.md](design-changes-workspace.md) 的 W5～W11；实现的细化（表结构、凭据、各组件之间怎么连）见 [sandbox-platform.md](sandbox-platform.md)。各项之间：D-11 是底座；D-12、D-13、D-15 建在它上面；D-16 要 D-11、D-12；D-14 只依赖设计确认，可以最先做；D-17 最后做。

### D-11 沙箱环境表与对账（强档）

**目标**：Runtime 记下每个环境，跨后端对账、清孤儿；用量与花费从环境表出（W9）。

**交付**：业务库的环境表（按租户行级安全）；Sandbox 的调用经环境表（创建中 → 就绪，销毁中 → 已销毁）；对账的定时工作流（Worker 里跑，每个后端列出在跑的环境、清孤儿、记丢失）；Cloudflare 桥加列表接口；按后端价目算花费，计入 Task 与租户的用量；租户删除时销毁全部环境。

**难点与防错**

- 崩溃一致：调后端之前先落账，之后补状态；对账能修复任何一步中断留下的状态，不靠进程内的记忆。
- 对账只删环境表里没有或已销毁的环境；环境表里就绪而后端没有的只记为丢失，不在后端重建。
- 列出各后端的环境时带租户过滤：别的部署或别的租户的环境不碰（K8s 用部署的标签，桥按令牌）。
- 定时工作流的代码必须确定（CLAUDE.md），调用后端都在 activity 里。

**验收**

1. 一致性测试：每个后端（K8s、Cloudflare 假桥、本地）建、用、销毁都在环境表里留下正确的状态序列。
2. 故障注入：在“记了创建中、后端已建好”之间杀掉 Worker，对账后要么补成就绪、要么销毁，不留孤儿；后端手工删掉的环境被记为丢失。
3. 越权：租户 A 的环境表查询看不到租户 B 的环境（行级安全）。
4. 一次任务的沙箱花费出现在 Task 的用量与 Grafana 面板上。

**依赖**：设计确认（W9）。

### D-12 跨后端配额、预算与有序回退（强档，含契约变更）

**目标**：配额与预算跨所有后端按环境表算；后端暂时不可用时按列表回退，绝不降级（W10、W11）。

**交付**：租户策略 schema 加 `sandbox.quota`、`sandbox.fallback`、`sandbox.min_isolation`（走 S-01：改 schema、契约测试、人审）；创建环境时在事务里核对配额与预算；Worker 的后端选择改为“首选 + 回退”；落点与回退原因记进 trace 与环境表。

**难点与防错**

- 核对与落账在同一个事务里，并发创建不能多出来（现在 K8s 的按标签数是软上限）。
- 只在 transient 时回退；denied、invalid 原样报出。
- 隔离强度按 none < container < microvm 排序，回退不低于 `min_isolation`；要出网审计的 Task 只在支持网关的后端之间回退。
- 工作区跨后端的回退要等 D-06；之前只在共用工作区卷的后端之间回退。

**验收**

1. 契约测试：schema 的正例与反例；配额超了是 `sandbox_quota_exceeded`、花费超了是 `sandbox_budget_exceeded`，都是 denied。
2. 并发：同时建 20 个环境、配额 5，恰好成功 5 个。
3. 首选后端注入 transient 时落到下一个；注入 denied 时不回退；回退列表里只剩更弱的后端时拒绝。
4. trace 与环境表里记着实际落点与回退原因。

**依赖**：D-11；S-01 流程。

### D-13 会话级沙箱（强档）

**目标**：同一会话复用一个环境，空闲到期或活满才销毁（W5）。

**交付**：`SandboxSpec.session_id`；Worker 的 `shell_run`（D-06 之后还有 `repo_*`）改为“按（租户，workspace，Session）查环境表、没有就建”；idle_timeout 与 max_lifetime 由租户策略约束；Session 关闭时销毁；联网授权随 Task 收回、不随环境。

**难点与防错**

- 环境由任何 Worker 副本都能找到（查环境表），不靠进程内的缓存。
- 环境被回收后，下一条命令透明地在新环境里跑；工作区内容不丢。
- 会写的 Task 持有工作区租约（D-06）；在 D-06 之前，同一工作区同一时间只允许一个可写的环境（按环境表核对）。

**验收**

1. 同一会话的两条命令落在同一个环境（第一条起的后台进程第二条看得到）；不同会话互不可见。
2. 空闲到期后再跑命令换了新环境，工作区里的文件还在。
3. 命令落在不同的 Worker 副本上（删掉第一个 Worker 的 Pod）照样复用同一个环境。
4. 一个会话里连续 20 条命令的总耗时明显少于每条一个环境（在 Kata 上测，记录数字）。

**依赖**：D-11。

### D-14 多语言运行环境（中档）

**目标**：沙箱里有常用语言的工具链；各后端用同一个镜像（W6 第 1、2 条）。

**交付**：`deploy/images/universal`（Python 3.13、Node 22 LTS、Go、JDK 21、Rust stable、build-essential、git，加 sandbox-agent），与几个单语言的精简镜像；沙箱后端登记把 runtime 映射到镜像；镜像按 digest 固定，每次运行记进 trace；节点预拉；Cloudflare 桥用同一份 Dockerfile。

**难点与防错**

- 镜像大（预计 3～4 GB）：分层让各语言工具链各占一层，更新一种语言只换一层。
- 以 10001 运行、根文件系统只读，各工具的缓存目录指到工作区或 `/tmp`（与 D-15 的镜像源变量一起配好）。
- 版本写死、发布满两周（与仓库的依赖规则一致）。

**验收**

1. 在 `universal` 里编译并测试五种语言的样例项目各一个（Python、Node、Go、Java、Rust），在 gVisor 与 Kata 上都过。
2. trace 里有镜像的 digest；换镜像版本后旧 digest 的记录不变。
3. 节点预拉之后，第一次建环境不再现下载镜像（冷启动时间记录数字）。

**依赖**：设计确认（W6）；D-01（镜像流水线）。

### D-15 包镜像（强档）

**目标**：沙箱经本地镜像源装依赖，有缓存、不撑爆容量（W7）。

**交付**：`services/go-pkg-mirror`（Go，只用标准库）：PyPI、npm、Go 三种协议的按需拉取；包文件按 sha256 内容寻址与校验；元数据短时缓存、过期先给旧的；下载地址改写；同一文件并发只拉一次；包文件与元数据各自的预算、高低水位、两段 LRU、大小上限（只转发不缓存）、常驻的常用包、临时文件清理；按租户按天的未命中字节配额；按“包名 + 版本”清除与拉黑；环境的短期凭据；Telemetry 指标与 Grafana 面板；Chart 与网络策略（沙箱只能连它，它只能连官方源）；沙箱里注入镜像源变量。Maven、crates 与 R2 冷层放第二步。

**难点与防错**

- 校验：包文件的哈希对不上时不缓存、不返回（报错让客户端重试），并记告警。
- 淘汰不能删掉正在被读的文件（引用计数或延迟删除）。
- 未命中配额按租户计，命中不计；凭据由环境表解析到租户，请求里写的租户不作数。
- 上游只用写死的官方源；私有源第一期不支持。

**验收**

1. 三种生态的真实客户端（pip、npm、go）经镜像装一个样例项目的依赖，第二次装时上游流量为 0。
2. 篡改缓存里的一个文件，客户端拿不到它（校验失败），镜像记下告警。
3. 容量：把预算设成 1 GB，灌入 3 GB 的不同文件，用量始终不超过高水位；被反复使用的文件没有被一次性拉取的大批文件挤掉。
4. 超过大小上限的文件照样能装，但不落盘。
5. 一个租户超了未命中配额后只转发不缓存，再超被拒绝；别的租户不受影响。
6. 清除一个版本后，所有租户都拿不到它。

**依赖**：D-11（凭据到租户的解析）；设计确认（W7）。

### D-16 出网网关与联网授权（强档，含契约变更）

**目标**：沙箱默认断网；联网经申请、批准、到期收回，网关侧限制与审计；进网只有预览地址（W8）。

**交付**：`services/go-egress-gateway`（Go，只用标准库）；Task API 的 `network_request` 与租户策略的 `sandbox.network`（走 S-01）；Worker 的 `request_network` 工具（write_irreversible，经 HumanChannel 审批）；联网授权记在环境表；网关按 SNI、Host、端口放行，自己解析并锁定地址，拒绝私有网段、链路本地与云元数据地址、集群内服务；按授权的字节、连接、频率与时长上限；逐连接的审计进 trace；预览地址（经入口、签名的短期令牌、频率限制）；测试用密钥从租户的 SecretStore 注入环境；托管的 microVM 后端把主机名单交给它们原生的白名单。

**难点与防错**

- SSRF：DNS 重绑定（解析后锁定地址再连）、IPv6 与 IPv4 映射地址、十进制或八进制写法的 IP、重定向到内网都要拦。
- 授权属于 Task：Task 结束、到期、被撤销时立即生效（网关缓存授权的时间要短，撤销要主动通知）。
- 只读 Task 不能申请联网；授权就是 gate 记录（附录 D.4），在范围与时限内不再逐次审批。
- 凭据与密钥不进日志、trace 与错误信息。

**验收**

1. 没有授权时，沙箱连外网失败；连包镜像成功。
2. 批准 `api.example.com` 15 分钟后可以访问它、访问别的主机被拒；到期后它也被拒。
3. SSRF 套件：私有网段、169.254.169.254、集群内服务、DNS 重绑定、各种 IP 写法、重定向到内网，全部被拒。
4. 字节上限：上行超过上限后连接被断开，审计记录里有原因。
5. 审计：一次任务访问过的主机、字节数与时长都在 trace 里，界面上能列出来。
6. 预览地址：不带令牌或令牌过期访问被拒；授权收回后失效。
7. 契约测试：`network_request` 与 `sandbox.network` 的正例、反例；超出租户策略的申请被拒绝并说明原因。

**依赖**：D-11、D-12；S-01 流程；设计确认（W8）。

### D-17 按项目定制的运行环境（强档）

**目标**：仓库自带环境定义时，构建一次、缓存复用（W6 第 3 条）。

**交付**：识别 `devcontainer.json`、`.tool-versions`、`Dockerfile`；在 microVM 沙箱里构建（只能经包镜像与出网网关联网）；按“定义的哈希 + 基础镜像 digest”缓存，推到租户自己的镜像仓库路径；大小上限、构建超时、每个租户的个数配额；长期不用的镜像定期清掉。

**难点与防错**

- 用户的 Dockerfile 不可信：构建只在 microVM 里，不挂任何宿主的套接字或凭据；构建产物只能推到本租户的路径。
- 构建失败时退回 `universal`，并告诉用户原因，不悄悄忽略。

**验收**

1. 带 `.tool-versions` 的样例仓库第一次用时构建、第二次直接用缓存（记录两次的耗时）。
2. 恶意 Dockerfile（试图访问宿主、内网、别的租户的镜像）全部失败。
3. 超过大小上限或超时的构建被终止，环境退回 `universal`。

**依赖**：D-14、D-15、D-16。

### D-18（不在 main）

main 不接 E2B（2026-10-27 用户定）。自托管 E2B 的设计、适配器与部署脚手架在可插拔的分支 `e2b`（那里的 `docs/plan/e2b-backend.md`）。

### D-19～D-27：Firecracker 后端与环境配置（草案，2026-11-02 更新）

细化在两份单独的文档里，等人确认后再写验收测试：

- [firecracker-backend.md](firecracker-backend.md)：D-19 后端登记的启用开关与能力、D-20 firecracker 后端（fc-manager 与 vsock agent）、D-21 客户机的 rootfs、D-22 出网与进网经 vsock、D-26 预热快照。Firecracker 做主路，gVisor 做旁路，由配置文件决定启用哪个。
- [environment-config.md](environment-config.md)：D-23 私有可复用环境、个人凭据引用与联网模式（网络密钥分第二阶段）；D-24 显式 setup / start，首个交付不依赖缓存；D-25 多仓库工作区；D-27 个人配置包（占位）。2026-11-02 的调整清单、最小验收与成本见该文第 1.1、6、10～11 节；保留原已确认决定。
- 外部字段及语义依据见 [Codex / Claude 云环境核实](../reference/cloud-environments-codex-claude.md)。调研事实与本系统拟议行为分开记录，上述能力仍为草案，未宣称已实现。

## 6. 顺序与里程碑

| 里程碑 | 包含 | 做完之后 |
| --- | --- | --- |
| M1 先上线 | D-01、D-02、D-03、D-04、D-05（gVisor） | 公网可用；邀请制租户在 gVisor 沙箱里跑任务；工作区仍是单节点本地盘 |
| M2 microVM | D-05（Kata）、D-08 | 按租户策略选 gVisor 或 Kata；沙箱等级经 Task API 传入 |
| M3 完整形态 | D-06（设计书更新后）、D-07、D-09、D-10 | 工作区跟着沙箱走；Cloudflare 做第二个 microVM 后端；可以加节点 |
| M4 沙箱平台 | D-11～D-17 | 会话级沙箱、多语言环境；依赖经包镜像、联网经申请与网关；配额、预算与回退跨后端统一 |
| M5 Firecracker 与环境配置（草案） | D-19～D-27 | Firecracker 做主路、gVisor 做旁路；用户能跨 repo 选用环境、引用个人凭据、配置联网与脚本；网络密钥分阶段交付，工作区可连多个仓库 |

D-01 与 D-02 可以并行；D-05 动 Worker 的工具（不在 Task Loop → Stage Runner → Node Executor 核心链路上），可以与 D-03、D-04 并行。
M4 的顺序：D-14 只依赖设计确认，可以最先做；D-11 之后 D-12、D-13、D-15 可以并行（D-13 与 D-12 都改 Worker 的后端选择，同一时间只由一个人改）；D-16 在 D-11、D-12 之后；D-17 最后。
M5 的后端顺序见 [firecracker-backend.md](firecracker-backend.md) 第 7 节。环境能力按 [environment-config.md](environment-config.md) 第 11 节：D-23 第一阶段 + D-24 + 应用入口可先在现有后端闭环，不等待 Firecracker 或 D-26；D-23 第二阶段另验外置网络密钥；D-25 独立推进。D-19 与 D-23 涉及 Broker 的修改仍顺序合并。D-26 最后，接入前需单独处置 setup 与工作区导入顺序差异。

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
- 设计书改动稿：[design-changes-workspace.md](design-changes-workspace.md)（W1 Sandbox 的后端、W2 Workspace 层、W3 effect_ceiling、W4 租户策略的沙箱后端；W5～W11：会话级沙箱、运行环境、包镜像、出网网关与联网授权、沙箱环境表、跨后端配额、有序回退）。
- 第 4.2 节“完全访问只能落在 microVM 后端”：有了出网网关（W8、D-16），集群里的 gVisor 也能按域名放行；改动稿建议改为按租户策略决定，仍建议陌生租户用 microVM。
- 2026-11-01 草案及 2026-11-02 环境增强建议的设计书改动见 [environment-config.md](environment-config.md) 第 7 节（W12～W15、SecretStore 边界与附录 A 新术语），待单列处置的差异见该文第 11 节；第 4.2 节的表要加上 firecracker（见 [firecracker-backend.md](firecracker-backend.md) 第 9 节）。

## 10. 进度（2026-10-26）

| 项 | 做了什么 | 本机验过的 | 要真机器或人做的 |
| --- | --- | --- | --- |
| D-01 | `deploy/images`：Go 与 Python 两种 Dockerfile、sandbox-agent、sandbox-python；`scripts/build_images.py`；`.github/workflows/images.yml` | `tests/deploy/test_images.py` 的静态检查；`AR_TEST_IMAGES=1`（2026-10-26 跑过）：七个镜像都能构建，以 10001、只读根文件系统起来：task-service、model-gateway、sandbox-agent 的 `/healthz` 是 200，Memory Service 的 gRPC 端口接受连接，Worker 在 Temporal 上轮询，learning 的命令行能跑；镜像里我们加的部分没有密钥文件、开发文件与测试代码。大小：task-service 52 MB、model-gateway 31 MB、sandbox-agent 10 MB、sandbox-python 186 MB、worker 与 learning 327 MB、memory 389 MB | 多架构（要上 ARM 节点时） |
| D-02 | `deploy/cluster`：k3s 配置、Pod Security 默认值、runsc 的 containerd 模板、Traefik、install.sh、verify.sh、SOPS 密钥样例 | `tests/deploy/test_cloud.py`；沙箱命名空间的约束在 kind 上（D-05 的测试） | 新机器上跑 install.sh 与 verify.sh（gVisor、Kata 的内核）；经 Traefik 的 SSE（`scripts/sse_probe.py`） |
| D-03 | `deploy/data`：CloudNativePG（三个角色无超级用户、无 BYPASSRLS，每天备份到 R2）、恢复演练脚本、Temporal、Qdrant、Redpanda、Langfuse、Tempo、Grafana 的 values | 静态检查；内存预算（requests 合计不超过 60 GiB）；版本 2026-10-27 按各项目的发布页与 index.yaml 核对过（都满两周），`scripts/check_chart_values.py` 核对 values 的键名与固定版本的 Chart 源码对得上（Temporal 1.x 与 Langfuse 2.x 的结构变了，已改；Langfuse 2.x 要先装 ClickHouse operator） | 各组件起来、角色核对、从 R2 恢复演练、Collector 三路导出 |
| D-04 | Chart：入口改 Traefik / Gateway API / 通用 Ingress；Worker 经 Sandbox 跑 shell_run 与只在沙箱命名空间里的 Role；ObjectStore 的 S3 变量；可写的 SecretStore 加种子（`AR_SECRETS_SEED_FILE`）；`values-cloud.yaml` | 三套 values 的 helm lint 与结构检查；Worker 的 RBAC 在 kind 的 API Server 上（`kubectl auth can-i`）；**kind 上彩排**（`scripts/kind_rehearsal.py`，2026-10-27 九项全过）：values-cloud.yaml 装上、各 Deployment 与 KEDA 就绪、IngressRoute 被接受、mock 模型跑到 done、100 KB 的 Claim-Check 经共用卷与可写 SecretStore 跨 Pod 往返、Worker 用自己的 ServiceAccount 在 gVisor 沙箱里跑 shell_run、30 个积压时 KEDA 扩到 3 个副本又缩回 1 个、任务进行到一半删掉 task 池的 Pod 后 30 个都跑完 | 真机器上：经 Traefik 与 Cloudflare 的 HTTPS、Postgres 业务库、Registry 端口从公网不可达 |
| D-05 | K8s 沙箱后端、sandbox-agent、shell_run 走 Sandbox | kind 上的一致性测试、非 root、只读根、没令牌、断网、令牌、回收、配额、CPU 时间；Worker 经 K8s 后端跑 shell_run；**gVisor**：设 `AR_KIND_GVISOR` 时 kindtest 把 gVisor 装进节点，55 项都在 RuntimeClass gvisor 上过了（沙箱里是 gVisor 的内核） | Kata 的 RuntimeClass（要嵌套虚拟化，kind 里没有） |
| D-06 | 2026-10-27 按改动稿 W2 实现（细化与进度在 [workspace-layer.md](workspace-layer.md)）：WorkspaceService 与凭据封装（契约待人审）；Sandbox 的存档与浏览能力（sandbox-agent、开发实现、K8s、Cloudflare）；工作区的存档（内容寻址、清单核对、保留 3 份，迁移 205）；`workspace: archive` 的后端建环境时恢复、销毁前存档、工具在环境里执行、彼此回退；Workspace API（task-service 起 Worker 的操作工作流：克隆、diff、提交、推送带 gate 与对账、存档、删除）；`dev_stack.py --sandbox local --workspace archive`；Chart 只在有挂卷的后端时要 `workspacesClaim` | 一致性测试（开发实现、Cloudflare 假桥、kind 里的 K8s）；Worker 的存档模式与操作（含 Temporal 上的工作流、事件历史里没有凭据明文）；task-service 的 Workspace API；端到端经整套走完生命周期；部署的结构检查 | **契约待人审**；coding-agent 改用 Workspace API 要先给契约补文件树、读文件、log、丢弃改动、从别的工作区复制（workspace-layer.md 第 7 节）；Kata、真 Cloudflare 桥与 R2 上的存档恢复；设计书原文档待人更新 |
| D-07 | Cloudflare 桥（TypeScript）与 Python 适配器 | 适配器对着假桥；桥过了 `tsc`（修了三处）与 `wrangler deploy --dry-run`（打包、按仓库根构建镜像）；Windows 上不带容器的 `wrangler dev` 里鉴权与路由（401、400、404） | 带容器的本地运行（wrangler 只在 Linux、macOS 上支持，要 WSL）；真端点（`AR_LIVE_CLOUDFLARE=1`，花钱） |
| D-08 | `SubmitTaskRequest.effect_ceiling`、租户策略的 `sandbox.backend` | 契约测试、Task API 的测试、按策略选后端 | **proto 与 schema 的改动待人审**，之后更新 `proto/breaking-baseline.binpb` |
| D-09 | ObjectStore 的 S3 兼容实现（Python 与 Go，标准库的 SigV4），task-service 与 Worker 接上 | 一致性测试对着会核对签名的假服务（带与不带前缀）、AWS 文档里的签名例子 | 真实 R2（`AR_LIVE_S3=1`）；task-service 与 Worker 不共用卷时的 Claim-Check（线上） |
| D-10 | `deploy/cluster/demo.md`；Grafana 面板；`runtime.sandbox` span（后端、存活、执行与 CPU 时间）；K8s 后端每个租户的配额 | 面板查的名字与代码对得上；CPU 时间与配额在 kind 上 | 照手册在线上走一遍；面板的 TraceQL 指标查询在真 Tempo 上 |
| D-11～D-17 | 2026-10-27 开始实现（见下面各项的进度） | — | 设计书原文档待人更新 |
| D-11 | 沙箱环境表（业务库，迁移 201、202）、SandboxBroker、对账工作流；三种后端都能列出环境（桥加了列表接口与目录）；按价目结算花费并计入 Task；租户删除时销毁全部环境 | `test_sandbox_ledger.py`：本地与 Cloudflare 假桥的状态序列、花费与用量（SQLite 与 Postgres 各一遍）、两种中断后对账修好、手工删掉记为丢失、宽限内不删、空闲回收与忙时不回收、越权查不到、对账工作流只起一个；桥过了 `tsc` | K8s 的那一条要 `AR_TEST_KIND=1`；真 Postgres 业务库与真桥（线上） |
| D-12 | 租户策略 schema 的 `sandbox.fallback`、`min_isolation`、`quota` 等（契约待人审）；SandboxBroker 的首选 + 回退、事务里的配额与预算 | `test_sandbox_quota.py`：20 个并发配额 5 恰好 5 个（SQLite 与 Postgres）、日与月的预算、当天时长、transient 回退并记录、denied 与 invalid 不回退、只剩更弱的拒绝、min_isolation、工作区同组、策略写错拒绝、span 里的回退；契约的正例与反例 | 契约人审后更新 `proto/breaking-baseline.binpb`（这一项只有 schema） |
| D-13 | 会话级沙箱：按（工作区，会话）复用、空闲与活满由对账回收、丢失时透明换新、一个工作区只有一个可写环境 | `test_sandbox_sessions.py`：同一会话复用（后台进程第二条命令看得到）、不同工作区各用各的、关掉或没有会话时一次性、空闲回收后工作区还在、另一个副本（只共用业务库与后端）复用同一个环境、可写环境的抢占与拒绝、丢失后透明换新 | 20 条命令的耗时要在 Kata 上量；Session 关闭的信号 |
| D-17 | 识别 devcontainer.json、`.tool-versions`、Dockerfile；静态检查；按“定义 + 基础镜像 digest”缓存（`sandbox_images`，迁移 204）、个数配额、长期不用的清掉；构建工作流与 Kata 里的构建 Job（BuildKit，联网只经网关与包镜像，产物由 Worker 去取）；推到环境镜像仓库里本租户的路径；shell_run 用上镜像或退回 universal 并说明；Chart 的 `worker.shell.environments` 与 `env-registry`，`deploy/cluster/builds.yaml` | Worker（`test_sandbox_environments.py`）：识别与生成、恶意 Dockerfile 被拒且不起构建、第一次构建第二次命中缓存、超时与超大小与构建器出错都退回并说明、配额与清理、构建 Job 的清单（只在 Kata、无令牌无宿主路径、令牌与凭据只在 Secret）、取回与日志去凭据、shell_run 里的说明；py-core：OCI 打包的读与推（`test_oci_registry.py`）；部署：登记、RBAC、网络策略、环境镜像仓库、工作区的只读 PV | 真实的 BuildKit 在 Kata 里构建与两次耗时、运行时的恶意样例、节点拉镜像（要真集群）；registry 的 blob 回收 |
| D-15 | `services/go-pkg-mirror`（PyPI、npm、Go；内容寻址与校验、两段 LRU 与水位、常驻、只转发的大文件、元数据过期先给旧的、并发只拉一次、未命中配额、清除与拉黑）；环境凭据（Python 与 Go 对同一组向量）；Sandbox 每次执行的 `env`；Worker 注入镜像变量；Chart 的 `pkgMirror` 与网络策略 | Go：哈希对不上不缓存不返回、篡改后重拉并告警、1 GB 预算灌 3 GB 不超高水位且热文件不被挤掉、大文件只转发、超配额先只转发再 429 且别的租户不受影响、清除后谁都拿不到、并发只拉一次、上游挂了给旧元数据；真实的 pip、npm、go 经镜像装两遍、第二遍上游包文件流量为 0（对着本机的假官方源）；契约：凭据向量；Worker：注入的变量与凭据不进 span 与环境表；部署：网络策略、预算、Worker 的地址 | 真官方源、集群里的连通性；Grafana 面板（还没有 Prometheus） |
| D-16 | `services/go-egress-gateway`（代理、SSRF、用量、逐连接审计、预览入口、管理接口）；提交时的 `network_request`（task-service 核对并写授权，Task 结束时 `FinishTask` 收回并通知网关）；Worker 的 `request_network`、代理变量、测试用密钥、托管后端按域名放行与要审计时只落在经网关的后端；Chart 的 `egressGateway` 与网络策略 | Go：没有授权连不出去、授权的主机与端口与时限、SSRF 套件（各种 IP 写法、内网名字、重绑定、元数据地址、NAT64、重定向到内网、CONNECT 写地址）、上行超限断开并审计、审计的主机字节时长、收回（管理接口立即断开、只改业务库几秒内断开）、环境销毁、连接数与频率、SNI、每天的出网字节、预览（没令牌、过期、别的环境、收回之后、Cookie、限频）；task-service：申请的正例与反例、被拒不留 Task、重复提交不多写、Task 结束收回、表没建时 transient；Worker：`test_sandbox_egress.py`；契约：proto、schema、凭据向量；部署：网络策略、预览路由、单副本与业务库 | **契约待人审**；集群里的连通性、真实 DNS 与 TLS 站点、`runtime.egress` 进 Tempo 与按 Task 列出的界面、`dev_stack.py` 拉起网关 |
| D-14 | `sandbox-universal`（五种语言各一层，版本写死）、`sandbox-node`、`sandbox-go`；后端登记的 `images` 与 `AR_SANDBOX_RUNTIME`；环境表与 span 记运行环境与镜像（K8s 报的 imageID 优先）；预拉的 DaemonSet | 静态检查（各层、版本、缓存在 /tmp、单语言镜像的基础镜像）；Chart 的预拉覆盖全部 K8s 后端的镜像且满足 restricted；Worker 记下的镜像与换版本后旧行不变（`test_sandbox_runtime.py`） | 构建并在镜像里编译、测试五种语言（`AR_TEST_IMAGES=1`，要下载基础镜像）；gVisor、Kata 上与冷启动的数字 |

第一次上线（2026-10-28，美国的独服，Ubuntu 24.04、24 线程、125 GB、有 KVM；域名 cgent.link）：

- 入口：`api.cgent.link`（Task API 与 Workspace API）、`code.cgent.link`（coding-web），Cloudflare 代理；源站证书 `cgent.link`、`*.cgent.link`，
  这两个主机用配置规则设为 Full (strict)（区域的其余主机不动）。防火墙用 `deploy/cluster/firewall.sh`。
- 镜像在服务器上构建、导进 k3s（不经镜像仓库）；密钥在服务器上生成，SOPS 加密。
- 验过的：verify.sh 全过（gVisor 与 Kata 的内核、沙箱断网、没令牌）；业务库三个角色都不是超级用户、没有 BYPASSRLS；经 Cloudflare 的
  Workspace API 在 gVisor 沙箱里建工作区、真模型的只读 Task 跑到 done、SSE 逐条到达；80 跳 443；源站的 443、6443、NodePort 从公网连不上。
- 还没开的：R2（Postgres 的 WAL 归档与每日备份、Langfuse）等 R2 的令牌；预览地址（`*.preview.cgent.link` 要 Cloudflare 的付费证书，
  先关着）；coding-web 的第三方登录等新的 OAuth 应用（回调 `https://code.cgent.link/api/auth/<平台>/callback`），上线初期只放行名单里的账号。
- 上线时改的：Traefik Chart 40 的跳转键位置、Kata 4.2 的 handler 在 `config-v3.toml.d` 里、Redpanda 的特权 tuning 容器关掉、
  Workspace API 在挂卷的后端上要先建工作区目录（否则 kubelet 以 root 建 subPath）、租户策略的种子不起名字。
- Cloudflare 的 Browser Integrity Check 拒绝 Python-urllib 之类的默认 User-Agent（错误 1010）：直接调 `api.cgent.link` 的脚本要带自己的 User-Agent。
- 应用的租户策略要带 `sandbox.network`（`allowed: true`，`hosts` 列出 Git 平台的主机）：集群里开着出网网关，Workspace API 克隆、推送、
  同步时按策略给本次操作发联网授权，没有这一节就报 `network_not_allowed`。Task 自己联网仍要逐次申请。
- task-service 的错误带 `google.rpc.ErrorInfo`（reason 是错误模型的 code）：说明是中文，应用按 code 翻成界面的语言（coding-web 的 `runtime_errors.go`）。
- Learning Pipeline 线上先关（`--set learning.enabled=false`）：`values-cloud.yaml` 的 ScaledJob 按 Redpanda 的积压起 Job，Job 却从
  `/traces/spans.jsonl` 读 trace，而 `tracesClaim` 是空的，于是每半分钟起一个失败的 Job。
  **2026-10-28 已接上，可以重新打开**：Job 直接消费 Collector 写的 `telemetry.spans`，用的就是 KEDA 盯的消费组，跑完才提交
  （[learning-pipeline.md](learning-pipeline.md) 第 7 节）；失败的 Job 不提交，`learning.pollingInterval`（600 秒）之后重跑。
  重新打开：用新的 learning 镜像，`helm upgrade` 去掉 `--set learning.enabled=false`，把 `learning.tenant`、`learning.family` 设成要学的租户与任务族。
  之后核对：`kubectl -n agent-runtime get scaledjob,job -l app.kubernetes.io/component=learning`，以及
  `kubectl -n data exec redpanda-0 -- rpk group describe learning`（第一个 Job 跑完后位置等于末尾，延迟归零）。

升级到 13924d7（2026-10-30，TMP 计划第 2～10 节；之前线上是 5f2658a）：

- 服务器上的代码在 `/opt/agentruntime/agent-runtime`（没有 `.git`，提交记在 `/opt/agentruntime/REVISION`；旧的一份留在
  `agent-runtime.5f2658a`）。服务器自己的 `deploy/cluster/secrets/.sops.yaml` 与 `secrets.enc.yaml` 不在仓库里，换代码时要带过去。
- 镜像：`build_images.py --tag 13924d7` 重建 7 个 Runtime 镜像（沙箱的镜像没变，仍是 0.1.0），`docker save | k3s ctr -n k8s.io images import -`
  导进 k3s。新标签而不是覆盖 0.1.0：回滚就是 `helm rollback ar 6`。
- 数据层：按 `deploy/data/install.sh` 新加的一步装了 VictoriaMetrics（Chart 0.49.0）；Grafana 用新的 values 升级（数据源、告警），
  `kubectl apply -f deploy/data/dashboards/` 加了质量面板。没有重跑整个 `install.sh`。
- Runtime：`helm upgrade` 用第 6 版的 values 快照（`/root/ar-release/values-rev6.yaml`）加 `image.tag=13924d7` 与
  `external.victoriametrics.remoteWrite`；升级前 `helm template` 与线上 manifest 比过，只差镜像标签、`OTEL_SERVICE_VERSION` 与 Collector 的指标导出。
  当时没有在跑的 Task；Session 工作流与沙箱对账工作流的代码这次没改。
- task-service、出网网关、包镜像的新 Pod 第一次启动连不上 Temporal 或业务库、重启一次就好（上一版也一样）：新 Pod 的网络策略
  生效前有几秒的空档。

升级到 b05aa63（2026-10-30，TMP 计划第 11.11 节的用量、预算与网关的防线，加上 Chart 的 `AR_PUBLIC_BASE_URL`；之前线上是 13924d7）：

- 代码同上一次的做法换成 b05aa63（`git archive` 打包、scp、核对 sha256；旧的一份留在 `agent-runtime.13924d7`，`REVISION.13924d7`
  是旧的提交记录）。服务器自己的两个密钥文件照样带过去。
- 镜像：`build_images.py --tag b05aa63 --only task-service,model-gateway,worker,memory,learning,pkg-mirror,egress-gateway`（层大多
  命中缓存，3 分钟），`docker save | k3s ctr -n k8s.io images import -`。
- Runtime：第 7 版的 values 快照（`/root/ar-release/values-live-rev7.yaml`，与 `values-rev7.yaml` 相同）只改 `image.tag`，存为
  `values-rev8.yaml`；`helm template` 与线上的 manifest（`manifest-rev7.yaml`）比过，只差镜像标签、`OTEL_SERVICE_VERSION` 与新加的
  `AR_PUBLIC_BASE_URL=https://api.cgent.link`。升级前确认没有在跑的 TaskWorkflow（只有空闲的 Session 工作流与沙箱对账工作流，它们的
  代码没改）。回滚：`helm rollback ar 7`。
- 新 Pod 里 task-service、出网网关、包镜像照旧第一次启动连不上 Temporal、重启一次就好。
- 核对（不花钱）：各 Pod 的镜像都是 b05aa63；经 `api.cgent.link` 的 `/healthz` 200；QueryTask 对冒烟留下的已结束 Task 给出了用量
  （0YVRP7 2.847 美元，与网关计量一致；ZJ82K7 2.868 美元，比网关少的 0.327 是旧 Worker 没记上的那次已计费的失败评审）。
  SubmitTask 的订阅地址与网关的防线要真提交 Task 才看得到，没有验证。

评测看板（Eval Dashboard，`evals.cgent.link`；TMP 计划第 11.12 节）：

- 只有评测集上的汇总，不含生产数据；Grafana 不开放。服务是 learning 镜像里的静态服务，Chart 的 `evalDashboard.enabled=true`、`evalDashboard.host=evals.cgent.link`。
- Cloudflare（人做）：加 DNS 记录 `evals`，开代理，指向与 `api` 相同的源站；把 `evals.cgent.link` 加进设为 Full (strict) 的配置规则（否则可能走 Flexible，源站的 80 跳 443 造成重定向循环）。源站证书已含 `*.cgent.link`。
  2026-10-31 已开放：配置规则的表达式加上 `evals.cgent.link`，再加了开代理的 A 记录（先规则后记录，免得中间走 full）。公网核对：页面 200、带 CSP，`/secret`、`/.git/config` 404，POST 405，http 跳 https。
- 发布或更新页面（评测报告在跑评测的机器上）：
  `uv run --no-sync python scripts/eval_dashboard.py --public --out index.html`，传到服务器，
  `kubectl -n agent-runtime create configmap ar-agent-runtime-eval-dashboard --from-file=index.html --dry-run=client -o yaml | kubectl apply -f -`。
  约一分钟后 Pod 里的文件更新，不用重启；Cloudflare 最多再缓存 5 分钟。Chart 不渲染这个 ConfigMap，`helm upgrade` 不会清掉它。
- 页面写给不懂这个项目的读者（2026-10-31，人定，例如 HR）：默认中文、可切英文，有结论、怎么考、发现了什么、每张图怎么看、名词解释、还没做完的。“发现了什么”与“还没做完的”是 `scripts/eval_dashboard.py` 里写好的文字（`FINDINGS`、`ROADMAP`），有新进展时改了再发布；其余数字来自报告与评测集。

升级到 7210e3e（2026-10-30，TMP 计划第 11.13 节的 Session 上下文，加上第 11.12 节的评测看板；之前线上是 b05aa63）：

- 同上一次的做法：`git archive` 打包、核对 sha256、换代码（旧的一份留在 `agent-runtime.b05aa63`）、重建 7 个 Runtime 镜像并导进 k3s。第 9 版的 values 是第 8 版改 `image.tag`，加上
  `evalDashboard.enabled=true`、`evalDashboard.host=evals.cgent.link`（`/root/ar-release/values-rev9.yaml`）。`helm template` 与线上的 manifest 比过，只差镜像标签、`OTEL_SERVICE_VERSION` 与看板的 4 个资源。
  升级前确认没有在跑的 TaskWorkflow。回滚：`helm rollback ar 8`。
- task-service 启动时跑迁移 4（`tasks.message`）。回滚到第 8 版不用撤这一列：旧代码不读它，有默认值。
- 页面用 `eval_dashboard.py --public` 生成，按上面的步骤发布。
- 核对（不花钱）：各 Pod 的镜像都是 7210e3e、都在跑，新 Pod 的日志没有错误（task-service、出网网关、包镜像照旧第一次启动连不上 Temporal、重启一次就好）；`api.cgent.link/healthz` 200。
  在 Worker 的 Pod 里：用它的连接角色读得到 `tasks`（含 `message` 列），同一个 Session id 换一个租户读到 0 行（行级安全）；按线上配置组装出来的 Planner、Node Executor、tool_loop 都带着 `SessionTranscript`。
  看板经 Traefik（`curl --resolve evals.cgent.link:443:127.0.0.1`）：页面 200，带 CSP 等响应头，`/secret`、`/.git/config` 404，POST 405。
- **没有验证**：模型实际答得对不对（要真调模型）；`evals.cgent.link` 的公网访问（DNS 记录与 Full (strict) 规则要人在 Cloudflare 上加，核对时还没有记录）。

升级到 52174d9（2026-10-31，运行中的纠偏、立刻打断与撤销、上下文占比、各轮汇总；之前线上是 7210e3e，coding-web 是 380f559）：

- 同上一次的做法换代码（旧的留在 `agent-runtime.7210e3e`、`coding-agent.380f559`）。重建 7 个 Runtime 镜像，这次多了 `sandbox-agent`（K8s 沙箱 Pod 的 initContainer 从它拷 agent，新的才有 `POST /v1/exec/cancel`）与 `coding-web`，都打新标签。运行环境的镜像没变，仍是 0.1.0。
- Runtime：第 10 版的 values 是第 9 版改 `image.tag` 与三处 `sandbox-agent` 的标签（`/root/ar-release/values-rev10.yaml`）。`helm template` 与线上的 manifest 比过，只差镜像标签、`OTEL_SERVICE_VERSION` 与后端登记里的 agent 镜像。Chart 没改，没有新迁移。升级前确认没有在跑的 TaskWorkflow。回滚：`helm rollback ar 9`。
- coding-web：`kubectl -n agent-runtime set image deploy/coding-web coding-web=ghcr.io/agentruntime/coding-web:1be5776`；`/root/ar-secrets/coding-web.yaml` 的标签同步改了。刚滚动完时经 Cloudflare 还有 503，几秒后 200。回滚：`kubectl -n agent-runtime rollout undo deploy/coding-web`。
- Cloudflare 这个回退后端没动：桥部署的 `sandbox-universal:0.1.0` 里的 agent 是旧的，回 404。这个后端上被打断的命令报“没能撤销”（`ExecNotStopped`），命令可能跑到自己超时。换镜像要 Cloudflare 的令牌（`deploy/cloudflare/sandbox-bridge/README.md`）。
- 核对（不花钱）：各 Pod 的镜像都是新标签、都在跑，新 Pod 最近的日志没有错误（task-service、出网网关、包镜像照旧第一次启动连不上 Temporal、重启一次就好）；`api.cgent.link/healthz` 与 `code.cgent.link` 200；页面的 `app.js`、`i18n.js` 是新版。用新的 task-service 回放线上两条 Session 历史（SWE-bench 冒烟留下的，只有公开数据）通过，改掉活动名的对照失败。经 `api.cgent.link` 给已结束的 Task 发纠偏与修改排队的消息，都是 `failed_precondition` / `task_finished`；不选生效时机 `invalid_argument`；没有令牌 401。
- 真实沙箱（人批准后在 Worker 的 Pod 里跑探测脚本，租户 `swebench`，不调模型）：Kata 与 gVisor 各建一个环境，建检查点，跑一条会改文件、起子进程的命令，取消。两边都是 agent 确认取消（约 0.03 秒）、命令与子进程都停了、改的文件还原、新建的文件删掉，环境随后删除。脚本直接调 Provider，不经 SandboxBroker 与存档，取消也不经 Temporal 的心跳。
- **没有验证**：运行中的 Task 真的被纠偏、打断（要真调模型）；Kata 的存档模式下打断之后的存档；coding-web 登录之后的界面（只放行名单里的账号）。

升级到 8dd79a0（2026-11-02，第 18 版：`docs/plan/feedback-2026-11-01.md` 的 N-01～N-09；之前线上是 f43a19f，coding-web 是 1be5776）：

- 这次从云端会话经 Cloudflare Access 的 SSH（`ssh.cgent.link`，root 用密钥）部署。换代码同以前（`git archive`、核对 sha256，旧的留在
  `agent-runtime.f43a19f`、`coding-agent.1be5776`，`REVISION.f43a19f`；服务器自己的 `.sops.yaml` 与 `secrets.enc.yaml` 带过去）。
- 镜像：`build_images.py --tag 9bde1aa --only task-service,model-gateway,worker,learning,pkg-mirror,egress-gateway`，coding-web 是
  `e06de4a`，都导进 k3s；Memory Service 仍是 52174d9。代码是 8dd79a0：比 9bde1aa 只多了 Chart 里 Jev 密钥卷的 `optional: true`，镜像不受影响。
- Runtime：第 18 版的 values 是第 17 版改 `image.tag`，加 `worker.judge: jev` 与 `worker.judgeKeySecret.name: agent-runtime-jev`
  （`/root/ar-release/values-rev18.yaml`，脚本 `rev18.sh`，日志 `rev18.log`）。`helm template` 与第 17 版比过：只差镜像标签、`OTEL_SERVICE_VERSION`，
  以及 4 个 Worker 的 `AR_JUDGE`、`AR_JUDGE_KEY_FILE` 与可选的密钥卷。升级前确认没有在跑的 TaskWorkflow。回滚：`helm rollback ar 17`。
- Model Profile 的 ConfigMap（`agent-runtime-model-profiles`）换成 `deploy/dev/model-profiles.json`（models.dev 的单价，加了 gpt-6.1-sol 与
  text-embedding-3-large）；原来的存在 `rev18/model-profiles.before.yaml`。经网关各打了一次（租户 smoke，固定 Profile）：gpt-6.1-sol 与 gpt-5.5 都正常回答。
- 核对（不花钱）：各 Pod 都在跑、镜像都是新标签；api、code、evals 的公网地址 200；新 Pod 半小时内的日志没有错误；Worker 读到 `AR_JUDGE=jev`。
- **人要做的（这个会话的权限不允许改线上的密钥与共用配置）**：建 `agent-runtime-jev` 这个 Secret（在那之前 Jev 没有密钥、不检查，不影响别的）；
  `agent-runtime-model-allowlist` 里给 `swebench` 加 `a6api/gpt-6.1-sol@2026-11`（加了之后它比 gpt-5.5 便宜，强档就会选它），改完重启网关。
- **没有验证**：真模型下 N-01～N-09 的效果（日志里那两个任务没有重跑：要用签名密钥签发令牌，这个会话不做）。

升级到 b4f37e2（2026-11-03，第 19 版；另一个会话部署，脚本 `/root/ar-release/rev21/rev21.sh`，这里按脚本补记）：

- 预算账本（task-service 启动时建 `model_usage` 等表）、多因素选档、Activity 里的 ContextBuilder、只读 EnvironmentService、N-17；Worker 启动时给
  `sandbox_envs` 加两列（迁移 206）。coding-web 是 `a319601`。
- 租户策略的 ConfigMap `agent-runtime-model-allowlist` 改写：平台硬上限每天 10 万美元；没列出的租户每天 3 美元、每月 20 美元，用免费白名单；
  内部租户 `gh-hanwen-jiang`、`evals` 用全部模型，`swebench` 留着不含 Claude 的白名单，都给硬上限。原来的存在 `rev21/tenant-policies.before.json`。
- 那时 task-service 对每个租户写死 2 个未终态 Task（`be3cfc5`），SWE-bench 的内部租户也被卡住，见下一版。回滚：`helm rollback ar 18`。

升级到 26b30ee（2026-11-03，第 20 版；`/root/ar-release` 下的目录按那边的编号叫 `rev22`，脚本、日志与 values 都在那里）：

- 内容：`f21626d` 按租户覆盖未终态 Task 的上限，values 加 `taskService.activeTaskLimits`（`evals`、`gh-hanwen-jiang`、`swebench` 都是 0，即不设上限；
  别的租户仍是 2）；`93110a6` 网关启动时限时重试连业务库；评测与能力矩阵的代码（不在请求路径上）；coding-web `d61bfef`（环境配置界面、新会话选环境、
  `/env` 读 EnvironmentService、Workspace 界面）。py-worker 与 libs 自 b4f37e2 没变，Worker 镜像只是按新标签重建。没有迁移；租户策略、
  Model Profile、Memory Service（52174d9）与评测页都没动。
- 换代码同以前（`git archive`、核对 sha256；旧的留在 `agent-runtime.b4f37e2`、`coding-agent.a319601`、`REVISION.b4f37e2`）。values 是
  `values-rev21.yaml` 改 `image.tag`、加 `activeTaskLimits`。升级前脚本核对：没有在跑的评测与 TaskWorkflow、线上 manifest 与 `manifest-rev21.yaml`
  一致；`helm template` 与第 19 版只差镜像标签、`OTEL_SERVICE_VERSION` 与 task-service 的 `AR_TENANT_ACTIVE_TASKS`，多出别的就停。
  `/root/ar-secrets/coding-web.yaml` 的镜像原来还写着 1be5776，改成 d61bfef（原来的存在 `rev22/coding-web.yaml.before`）。
- Chart 里 worker 与 pkg-mirror 的模板是 CRLF，渲染出的行尾带 `\r`：按行比 manifest 时要容忍行尾空白（这次的脚本因此停过一次，在 helm 之前，集群没变）。
- 核对（不花钱）：各 Pod 都在跑、镜像都是新标签；task-service 读到 `AR_TENANT_ACTIVE_TASKS=evals=0,gh-hanwen-jiang=0,swebench=0`；api、evals 与
  `evals.cgent.link/api/live.json` 200；code 在切换的那一刻 503，随后 200；网关这次没有重启；出网网关与包镜像照旧第一次启动连不上业务库、
  重启一次就好；新 Pod 的日志里只有一种错误：出网网关每分钟一次 `failed to upload metrics`（连 Collector 的 4318 被拒）。它的网络策略
  （`templates/egress-gateway.yaml`）只放行 DNS、data 里的 Postgres、沙箱与公网（排除内网段），没有放行同一命名空间的 Collector，
  所以出网网关的指标一直没进 Collector；这一版没改这个模板，不是这次引入的，要另修。回滚：`helm rollback ar 19`，coding-web 用
  `kubectl -n agent-runtime set image deploy/coding-web coding-web=ghcr.io/agentruntime/coding-web:a319601`。
- **没有验证**：内部租户真的能同时跑 2 个以上的 Task（要提交 Task）；coding-web 的新界面登录之后的样子。

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
- 对照：[Fly.io 2026 年价格](https://fly.io/pricing-update/)；三类后端与 VMM 的对比见 [sandbox-backends.md](../reference/sandbox-backends.md)

2026-11-01：第 14～16 版（调优第一轮，TMP 计划第 11.15 节）。第 14 版换镜像 3608351；第 15 版用 `memory.imageTag` 把 Memory Service 留在 52174d9：`3eb4fde` 之后的新镜像在生产配置不全时拒绝启动（`docs/plan/memory-live.md` 第 4 节），只换镜像会让它崩溃重启，`helm --wait` 发现不了，要看重启次数；第 16 版换镜像 f43a19f（修 Reviewer 的 effect 上限与预算压缩）。回滚：`helm rollback ar 15`。全是数字的提交号（3608351）当 `image.tag` 时要加引号，现在 Chart 直接报错，不再渲染出错误的镜像名。swebench 的租户策略到第 4 版（工具调用额度 400）。
