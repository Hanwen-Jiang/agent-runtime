# Firecracker 后端（D-19、D-20、D-21、D-22、D-26）

> 2026-11-01 起草。**状态：草案，等人确认。** 确认后按仓库的流程走：先写验收测试、写完停下等人确认、再实现；凡要改 `/proto`、`/schemas` 的，走 S-01，本稿不改它们。
> 本文写工作项与取舍；用户的环境配置、多仓库工作区在 [environment-config.md](environment-config.md)（D-23～D-25、D-27）。依据：[云上部署计划](cloud-deploy.md)第 4 节、[沙箱平台](sandbox-platform.md)、[Workspace 层](workspace-layer.md)，以及 2026-11-01 对当前代码的核对（第 2 节）。
> 编号：D-18 已被“E2B 不在 main”占用，新的从 D-19 起。

## 1. 决定（2026-11-01，用户确认）

1. **Firecracker 做主路，自己写后端**（不走 Kata 的 Firecracker 后端）。理由：D-26 要用 Firecracker 原生的快照与恢复，Kata 不暴露这一层；Kata 配 Firecracker 还要 devmapper 快照器、且不支持 virtio-fs（cloud-deploy.md D-02 的难点已经写过）。Kata 保留，D-17 的构建仍在 Kata 里。
2. **gVisor 做旁路**：留在后端登记里，**由配置文件决定哪个启用、哪个是默认**；不删。
3. **不自动降级**：firecracker 不可用时不回退到 gVisor，除非租户策略显式写 `min_isolation: container`。这是 W11 现有规则（回退只能平级或升级隔离强度）的直接结果，不加特例。
4. 用户的环境配置（环境变量、联网模式、启动脚本，以及以后的个人配置包）**经 Sandbox 接口的通用操作交付**（`exec(env=…)`、写文件、出网网关的授权），不依赖 Firecracker 独有的能力。所以旁路的 gVisor 上同样可用；快照、块盘只是加速。

## 2. 现状（对照代码，2026-11-01）

| 事实 | 出处 |
| --- | --- |
| 后端登记的 `kind` 只有 `k8s`、`cloudflare`、`local`；`isolation` 取 `none`、`container`、`microvm`；`workspace` 取 `mount`、`archive`；**没有启用开关** | `services/py-worker/src/agentruntime_worker/tools/sandboxes.py` |
| Sandbox 接口：`create`、`exec`（带 `env`、超时）、`write_file`、`read_file`、`destroy`；可选能力是独立的 Protocol：`SandboxArchive`、`SandboxBrowse`、`SandboxLister`、`SandboxPreview` | `libs/py-core/src/agentruntime_core/providers/sandbox.py` |
| 环境里的 agent 说 HTTP：`/v1/exec`、`/v1/exec/cancel`、`/v1/files`、`/v1/archive`、`/v1/manifest`、`/v1/list`、`/v1/grep`；命令只拿白名单环境变量，`AGENT_*` 一个都不给 | `services/go-sandbox-agent/internal/agent/agent.go` |
| 适配器：K8s、Cloudflare；agent 的 HTTP 客户端是共用的 | `libs/py-core/src/agentruntime_core/adapters/{k8s_sandbox,cloudflare_sandbox,sandbox_agent}.py` |
| 一致性测试 | `libs/py-core/src/agentruntime_core/conformance/sandbox.py` |
| 回退：首选加回退去重；隔离强度低于下限的跳过；`denied`、`invalid` 不回退 | `services/py-worker/src/agentruntime_worker/sandbox/policy.py`、`broker.py` |
| Chart 里后端登记在 `worker.shell.backends`，默认后端是 `worker.shell.defaultBackend`（现在是 `gvisor`），`kata` 并存 | `deploy/helm/agent-runtime/values-cloud.yaml` |
| 宿主级服务已有先例：`ar-firewall.service`（systemd，开机在 k3s 之前装好防火墙） | `deploy/cluster/ar-firewall.service` |
| 联网通路：沙箱里只有代理变量与包镜像变量，凭据在 userinfo 或路径前缀里（环境凭据 `ar1.…`），网关校验 | [sandbox-platform.md](sandbox-platform.md) 第 3 节 |
| 生产独服有 KVM（24 线程、125 GB）；**当前这个云开发环境没有 `/dev/kvm`**（2026-11-01 `ls /dev/kvm` 不存在） | cloud-deploy.md 第 10 节；实测 |

最后一条决定了测试策略（第 6 节）：开发机、CI 和这类云环境都跑不了真的 microVM。

## 3. 架构

```text
Worker（SandboxBroker）──► Sandboxes（登记：enabled、default、capabilities）
        │ kind = firecracker
        ▼
FirecrackerSandboxProvider（Python 适配器；复用 sandbox_agent 的 HTTP 客户端）
        │ HTTPS + 令牌（令牌在 SecretStore 平台作用域的 sandbox/fc-manager-token）
        ▼
fc-manager（Go，宿主机上的 systemd 服务，不在 Pod 里）
   ├─ 每个环境：jailer ► firecracker 进程 ► 一台 microVM
   ├─ 磁盘：只读 rootfs（按镜像 digest 缓存）＋ 每台一个稀疏的可写层 scratch
   └─ vsock：agent 的管理通道 ＋ 两条出口转发（出网网关、包镜像）＋ 预览隧道
        ▼
客户机：sandbox-agent 当 PID 1（--init），监听 vsock；127.0.0.1 上的转发器把代理与镜像请求送进 vsock
        没有网卡，只有 lo
```

要点：

- **管理通道走 vsock**，客户机没有对外的网络入口；命令执行不再依赖 Pod IP 与 NetworkPolicy。
- **客户机里没有网卡**。联网只有“环回地址 → vsock → fc-manager 的转发 → 出网网关或包镜像”这一条路（D-22）。凭据与授权照旧由网关校验，所以 D-16 的联网授权、SSRF 防线、审计原样适用。
- **工作区用 `workspace: archive`**：Firecracker 没有 virtio-fs，不挂卷；Worker 建环境后从 ObjectStore 的存档恢复、销毁前存档（D-06 已有的流程）。
- fc-manager 是宿主上新增的**最高权限组件**（要开 `/dev/kvm`、起 jailer）：代码保持小、只用标准库、输入全部校验、不把用户可控的字符串拼进命令、只允许 Worker 访问（见 D-20 的防错）。

## 4. 与其他后端的差异

| | gVisor（旁路） | Kata | Cloudflare | Firecracker（主路） |
| --- | --- | --- | --- | --- |
| 隔离 | 用户态内核（容器级） | 每 Pod 一台 VM（Cloud Hypervisor） | 每实例一台 Firecracker VM | 每环境一台 Firecracker VM |
| 需要 KVM | 否 | 是 | 托管 | 是 |
| 工作区 | mount（挂卷） | mount | archive | archive |
| 联网通路 | NetworkPolicy ＋ 代理变量 | 同左 | 平台按域名白名单 | vsock 转发 ＋ 代理变量 |
| 快照与恢复 | 无 | 无 | 平台内部，不暴露 | 有（D-26） |
| 运行环境镜像 | OCI 镜像直接用 | 同左 | 桥里写死 | 转成 ext4 rootfs（D-21） |
| 按项目定制的环境（D-17） | 支持 | 支持（构建在这里） | 不支持 | **本期不支持**，退回 universal 并说明 |
| 冷启动、并发密度 | 未量 | 未量 | 1–3 秒（文档值） | **未量，D-20 验收里记录** |

## 5. 工作项

档位与评审按 [强档实施计划](strong-tier-plan.md) 第 1 节。

### D-19 后端登记：启用开关与能力（中档）

**目标**：firecracker 与 gVisor 并存，配置文件决定谁可用、谁是默认。

**交付**：

- 登记（`AR_SANDBOX_BACKENDS_FILE`，Chart 的 `worker.shell.backends`）里每个后端加 `enabled`（默认 true）与 `capabilities`（字符串数组，取值 `snapshot`、`block_attach`、`vsock`）；`KINDS` 加 `firecracker`。
- `Sandboxes` 区分“建新环境用”与“已有环境用”：`enabled: false` 的后端不接新环境，已有的继续可 exec、可销毁、被对账看见（排空）。
- 选到不可用后端时新建被拒：`denied`，错误码 `sandbox_backend_disabled`，说明是哪个后端、谁禁的（登记）。
- Chart 的 `values.yaml`、`values-cloud.yaml` 与 `scripts/dev_stack.py` 的登记样例加 `firecracker`（开发默认不启用）。
- 依赖某个能力的功能（D-24 的 setup 缓存要 `snapshot`，D-27 的块盘要 `block_attach`）在后端缺这个能力时走通用路径，并在结果的 `environment` 里说明，不报错、也不悄悄忽略。

**难点与防错**

- 登记写错一律启动失败，不悄悄换后端（沿用）：`default` 指向 `enabled: false` 的后端、`capabilities` 取值不认识、`firecracker` 没配 `workspace: archive`，都要在加载时报错并写明原因。
- 改登记靠滚动重启 Worker（Chart 的 ConfigMap 变了就触发），不做热加载。
- **回退规则不动**：不给 firecracker 到 gVisor 开特例。测试要覆盖“首选 firecracker、回退写了 gvisor、没写 `min_isolation`”时拒绝，写了 `min_isolation: container` 才回退。
- 排空期间对账不能把 `enabled: false` 的后端里的环境当孤儿删掉。

**验收**

1. `default` 指向 `enabled: false` 的后端时 Worker 启动失败，原因里有后端名。
2. 租户策略 `backend: gvisor`（启用）走 gVisor；不写时走 `default`。
3. 把 firecracker 设为 `enabled: false`：新建被拒（`sandbox_backend_disabled`）；已有环境 exec、销毁正常；对账不删它。
4. 首选 firecracker、`fallback: [gvisor]`、无 `min_isolation`，firecracker 不可用时拒绝；加上 `min_isolation: container` 才回退，且环境表记录 `fallback_from` 与原因。
5. `capabilities` 取值非法、`firecracker` 缺 `workspace: archive` 都在加载时失败。
6. 一致性测试对每个 `enabled` 的后端参数化各跑一遍。

**依赖**：D-12。无契约变更（登记文件不是 schema）。

### D-20 firecracker 后端：fc-manager 与 vsock agent（强档）

**目标**：每个环境一台 Firecracker microVM；Worker 经 `firecracker` 后端使用，接口与其他后端相同。

**交付**：

- `services/go-fc-manager`（新的 Go 模块，进 `go.work`；只用标准库）：宿主机上的服务，管 VM 的创建、销毁、列出，以及到客户机 agent 的反向代理（`/v1/envs/{id}/agent/…`）。
- `libs/py-core/src/agentruntime_core/adapters/firecracker_sandbox.py`：`FirecrackerSandboxProvider`，实现 Sandbox 与 `SandboxArchive`、`SandboxBrowse`、`SandboxLister`；agent 的 HTTP 客户端复用 `sandbox_agent.py`。登记字段：`manager_url`、`images`（运行环境 → 镜像，写法同 K8s）、`kernel`、`workspace: archive`、`egress_gateway: true`。
- `services/go-sandbox-agent`：`AGENT_LISTEN=vsock:<端口>`，以及 `--init`（当 PID 1：挂 `/proc`、`/sys`、`/dev`，设主机名，挂数据盘，回收孤儿进程，响应关机信号）。
- `deploy/images/firecracker/`：客户机内核的固定版本与构建脚本（配置裁剪：无模块、串口控制台）；版本要发布满两周（沿用 D-14 的原则）。
- `deploy/cluster/fc-manager.service`（systemd，仿 `ar-firewall.service`）；`install.sh` 核对 `/dev/kvm`；Worker 到 fc-manager 的 NetworkPolicy（只放行 Worker）。
- 资源：vCPU、内存按 `SandboxSpec`；可写层大小有上限；jailer（非 root、chroot、独立 PID 与网络命名空间）、seccomp、cgroup v2；每宿主并发 VM 数上限。
- 对账：`list_environments()` 按 fc-manager 的清单，owner 放在 VM 的标签里。

**难点与防错**

- **不能真跑的机器**：见第 6 节。
- **纯标准库做 AF_VSOCK**：Go 标准库的 `net` 不直接支持，要用 `syscall` 手写 sockaddr。如果这样不可行，先说明理由，再决定是否引入 `golang.org/x/sys`（违反“agent 只用标准库”，不默认引入）。
- **PID 1 的职责**：回收僵尸进程；命令超时或取消时杀整个进程组；取消后没确认命令已停（`ExecNotStopped`）则这台环境视为被污染，销毁而不是复用。
- **命令拿不到 agent 令牌**（沿用 D-05 的检查）：`AGENT_*` 不给命令；令牌不放在客户机里非 agent 进程能读到的地方（`/proc/cmdline`、`/proc/1/environ`、磁盘文件）。具体怎么交付令牌留到写验收测试时定。
- **非 root、只读根**：命令以普通用户运行，根文件系统只读，可写的只有工作目录与 `/tmp`（与 K8s 后端一致）。
- **fc-manager 重启不留孤儿 VM**：要么接管仍在跑的 VM，要么终止，二者必居其一，并有故障注入测试；具体选哪个留到实现时定。
- **容量**：超过宿主容量的创建返回 `transient`，不能拖垮已有 VM；`transient` 会走 D-12 的回退，但回退不能降级（D-19）。
- **fc-manager 的 API 令牌**取自 SecretStore 平台作用域，日志与错误里不出现令牌与环境凭据。
- 磁盘不假设能热插：所有盘在启动前配好。

**验收**

1. `agentruntime_core.conformance.sandbox` 的全套用例对 firecracker 适配器通过（用假 fc-manager，在没有 KVM 的机器上也跑）。
2. 真实 VM（`AR_TEST_FIRECRACKER=1`）：同一套用例通过；客户机内核版本与宿主不同；PID 1 是 sandbox-agent；`/sys/class/net` 里只有 `lo`。
3. 命令拿不到 agent 令牌；命令以非 root 运行；根文件系统写不进去。
4. 内存超限时命令被 OOM 杀掉而 VM 与宿主不受影响；磁盘写满返回错误而不是撑爆宿主；CPU 受限。
5. 创建超过宿主容量时返回 `transient`，已有 VM 不受影响。
6. 销毁后宿主上无残留（进程、jail 目录、cgroup、scratch 文件、vsock socket）；销毁重复调用不报错；fc-manager 被杀再起后没有孤儿 VM（故障注入）。
7. 手工杀掉一台 VM：环境表记为 `lost`，下一条命令透明换新（D-13）。
8. 记录：建环境到第一条命令返回的耗时（P50、P95）、单宿主能同时跑的 VM 数。**只记录，不设门槛**，因为没有基线。

**依赖**：D-19、D-05、D-06。无契约变更。

### D-21 客户机的 rootfs 与镜像（中档）

**目标**：把运行环境镜像（D-14）转成 Firecracker 能挂的只读 rootfs，按 digest 缓存，每台 VM 的可写层独立。

**交付**：

- 转换器：OCI 布局 → ext4（不需要 root，用 `mke2fs -d` 一类的做法），复用 `agentruntime_core.adapters.oci_registry.read_layout` 对路径与符号链接的检查；转换时注入同版本的静态 `sandbox-agent`。
- 宿主上按镜像 digest 内容寻址的缓存，带容量水位与淘汰；同一个 digest 并发只转换一次；产物旁边存哈希，读之前核对。
- 每台 VM：只读 rootfs（`nosuid`、`nodev`）＋一个稀疏的可写层（overlay 的上层），大小受配额限制。
- 环境表和 span 记镜像 digest（沿用 D-14）。

**难点与防错**

- **不可信输入**：本期**只转换平台构建的镜像**（`deploy/images/images.json` 里的）。D-17 的租户镜像不进：在 firecracker 后端上遇到项目自带的环境定义，退回 universal，`environment.state` 为 `unsupported` 并写明原因（沿用 D-17 的说法）。以后要支持，转换器要先过恶意 tar 套件，并升档评审。
- 转换产物的大小（universal 有三四个 GB）会让首次转换很慢：缓存必须按 digest 去重，首次耗时要记录，预拉思路同 D-14。
- 可写层写满必须只影响这台 VM。

**验收**

1. 同一 digest 第二次建环境不再转换（记录两次耗时）。
2. 在 universal 里编译并测试五种语言的样例各一个（沿用 D-14 的样例），在 firecracker 上通过。
3. 恶意 tar（`..`、绝对路径、指出去的符号链接、设备文件、硬链接）转换失败，宿主上没有写出到缓存目录之外。
4. 改动缓存里的 rootfs 后，下一次读之前校验失败并重新转换。
5. 项目自带环境定义时退回 universal，结果里 `environment.state` 为 `unsupported`，有原因。
6. 可写层写满不影响宿主与别的 VM。

**依赖**：D-20、D-14。无契约变更。

### D-22 出网与进网经 vsock（强档）

**目标**：客户机没有网卡，联网只走 vsock 到出网网关与包镜像；预览也走 vsock。

**交付**：

- `sandbox-agent` 里的环回转发器：监听客户机的 `127.0.0.1:<端口>`，把连接送进 vsock。代理与包镜像的变量（`HTTP(S)_PROXY`、`PIP_INDEX_URL`、`npm_config_registry`、`GOPROXY` 等）由 Worker 的 `sandbox.credentials` 按后端生成，firecracker 后端给环回地址，凭据部分不变。
- fc-manager 为每台 VM 开两条转发（出网网关、包镜像），目标写在登记里、不解析客户机给的任何地址；其他 vsock 端口一概不通。
- 预览：`SandboxPreview.preview_address` 由 fc-manager 提供隧道入口，网关的预览入口转发到它。
- 联网授权的收回延迟不能比 D-16 现在更差（网关每秒核对开着的连接，授权缓存 2 秒）。

**难点与防错**

- 客户机里**不认代理变量的程序**（原生 TCP/UDP/ICMP、ssh、直接查 DNS）会失败。这与 K8s 后端现在“只放行到网关”的效果一致，但要在界面与 `environment` 的说明里写明，免得用户当成 bug。
- **转发器不能成为跳板**：目标写死；每台 VM 的连接数与带宽有上限；两端都有超时。
- 据我对 Firecracker 文档的记忆，vsock 在宿主侧是按端口映射的 UDS，没建对应的 socket 就不通；**以选定版本的实测为准**，要用测试证明客户机连不到 fc-manager 的管理端口，也连不到别的 VM 的转发。
- 仍然不解密 HTTPS（沿用 W8），按 SNI 放行。
- fc-manager 的访问日志把路径里的环境凭据换成 `***`（同包镜像的做法）。

**验收**

1. 客户机里 `/sys/class/net` 只有 `lo`；用原生 socket 连任何外部地址都失败。
2. 没有授权时，经代理连外网失败、连包镜像成功（D-16 验收 1）。
3. 批准某主机 15 分钟后可以访问、别的主机被拒、到期后也被拒（D-16 验收 2）。
4. D-16 的 SSRF 套件在 firecracker 后端上全部被拒。
5. 客户机连 fc-manager 的管理端口、连别的 VM 的转发，都失败。
6. 预览：没有令牌或令牌过期被拒；授权收回后失效；有效时能访问客户机里的 dev server。
7. 授权收回后，已建立的连接在 5 秒内被断开。
8. fc-manager 的日志与错误里搜不到环境凭据。

**依赖**：D-20、D-15、D-16。无契约变更。

### D-26 预热快照（强档）

**目标**：用 Firecracker 的快照缓存 setup 的结果，并可选地预热待命 VM，让新环境很快就绪。

**交付**：

- fc-manager 的快照与恢复（登记里的 `capabilities` 含 `snapshot`）。
- 缓存键：运行环境镜像 digest、setup 脚本哈希、锁文件哈希、**租户与工作区**、内核版本、Firecracker 版本、CPU 模板。记在业务库（新迁移，号接现有最大号之后，实现时查）。
- 宿主本地缓存与淘汰（快照与宿主绑定，不跨机器）。
- 恢复后的处置：重新播种随机数、校时、重置 vsock 连接与转发；工作区在快照**之后**从存档导入。
- 可选的预热池：按运行环境预先起 N 台待命 VM，**只含基础镜像，不含任何租户数据**，取走一台后补满。

**难点与防错**

- **快照里不能有机密和租户数据**：只快照“基础环境加 setup 的结果”，在导入工作区、注入用户变量之前。setup 阶段不给机密（D-24）。
- **setup 是用户代码，其结果可能含恶意状态**：缓存键必须含租户与工作区，**不跨租户共享**。只有无租户数据的基础预热池可以共享。
- 恢复出来的多台 VM 默认共享随机数状态、机器 ID、MAC：恢复后必须重置，测试要查。Firecracker 是否通过 VMGenID 之类机制通知客户机，要在选定版本上核对，不凭记忆。
- 内存快照的体积约等于客户机内存：要有磁盘预算与 LRU。
- 升级 Firecracker 或内核后全部缓存失效（键里含版本），不能恢复到不兼容的版本上。
- 恢复失败退回冷启动并记录，不向用户报错。

**验收**

1. 同键第二次建环境不再跑 setup，耗时下降（记录两次的数字）。
2. 同一快照恢复出的两台 VM，随机数与机器 ID 不同。
3. 构造含标记字符串的机密和别的租户数据：快照文件里 grep 不到。
4. 两个租户用同一份 setup 脚本，不共用快照。
5. Firecracker 版本变化使缓存失效。
6. 恢复失败时回退冷启动，且留下记录。
7. 预热池：取走一台后补满；池里的 VM 不带任何租户数据。

**依赖**：D-20、D-21、D-24。无契约变更。

## 6. 测试策略（没有 KVM 的机器）

沿用仓库已有的做法（`AR_TEST_KIND=1`、`AR_LIVE_CLOUDFLARE=1`）：

- **默认**：适配器与 Worker 的测试对**假 fc-manager**跑。假的 fc-manager 在进程里起一个真的 `sandbox-agent` 当“客户机”，走和真的一样的 HTTP 路由，所以一致性测试、环境表、对账、回退、配额都能在任何机器上测。
- **真 VM**：设 `AR_TEST_FIRECRACKER=1`。要 `/dev/kvm` 可读写、PATH 上有 `firecracker` 与 `jailer`，缺任何一个就失败（不静默跳过）；不设这个变量时这些用例不收集。夜间任务和生产独服上跑。
- vsock 的单元测试（`syscall` 手写部分）只在 Linux 上编译，Windows 开发机上用构建标签隔开。

## 7. 顺序与上线

| 步 | 内容 | 说明 |
| --- | --- | --- |
| 1 | D-19 | 先做；它和 D-23 都改 `Sandboxes`/Broker，同一时间只由一个人改 |
| 2 | D-20（先用假 fc-manager 跑通） | 与 environment-config.md 的 D-23、D-25 可并行：它们不依赖 Firecracker |
| 3 | D-21、D-22 | D-20 之后；两者可并行 |
| 4 | 独服上跑真 VM 的验收 | 记录耗时与密度 |
| 5 | D-26 | 依赖 D-24 的 setup；放在最后 |

**切换是改配置，不是改代码**：上线时 `defaultBackend` 先保持 `gvisor`，firecracker `enabled: true` 但只给指定租户（租户策略 `backend: firecracker`）灰度；真 VM 验收通过后把 `defaultBackend` 改成 firecracker，gVisor 留作旁路。

## 8. 没有验证的、要实测的

- 单宿主上同时能跑多少台 VM、冷启动 P50 与 P95、快照恢复耗时：**都没有数字**。
- `AF_VSOCK` 能否只靠标准库做到。
- 选定的 Firecracker 与内核版本是否通知客户机“被克隆过”（影响 D-26 的重置方式）。
- fc-manager 重启时接管还是终止（D-20 细化时定）。
- 多宿主调度不在本期（登记里 `manager_url` 先是单个地址）。

## 9. 与既有设计的出入

- cloud-deploy.md 第 4.2 节“公开注册的租户 → microVM（Kata 或 Cloudflare）”：建议加上 firecracker。陌生租户的默认仍应是 microVM 级，这一点不变。
- 同一节“完全访问只能落在 microVM 后端”：沿用 [design-changes-workspace.md](design-changes-workspace.md) 已经建议的口径（按租户策略决定）；firecracker 属于 microVM。
- 设计书 4.10、附录 L.11 里“Sandbox 的后端”要加 firecracker 一种；这是设计书的改动，列在 [environment-config.md](environment-config.md) 第 7 节，由人更新原文档。
