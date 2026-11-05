# Firecracker 后端（D-19、D-20、D-21、D-22、D-26）

> 2026-11-01 起草。**状态：草案，等人确认。** 确认后按仓库的流程走：先写验收测试、写完停下等人确认、再实现；凡要改 `/proto`、`/schemas` 的，走 S-01，本稿不改它们。
> 本文写工作项与取舍；用户的环境配置、多仓库工作区在 [environment-config.md](environment-config.md)（D-23～D-25、D-27）。依据：[云上部署计划](cloud-deploy.md)第 4 节、[沙箱平台](sandbox-platform.md)、[Workspace 层](workspace-layer.md)，以及 2026-11-01 对当前代码的核对（第 2 节）。
> 编号：D-18 已被“E2B 不在 main”占用，新的从 D-19 起。

## 1. 决定（2026-11-01，用户确认）

1. **Firecracker 做主路，自己写后端**（不走 Kata 的 Firecracker 后端）。理由：D-26 要用 Firecracker 原生的快照与恢复，Kata 不暴露这一层；Kata 配 Firecracker 还要 devmapper 快照器、且不支持 virtio-fs（cloud-deploy.md D-02 的难点已经写过）。Kata 保留，D-17 的构建仍在 Kata 里。
2. **gVisor 做旁路**：留在后端登记里，**由配置文件决定哪个启用、哪个是默认**；不删。
3. **不自动降级**：firecracker 不可用时不回退到 gVisor，除非租户策略显式写 `min_isolation: container`。这是 W11 现有规则（回退只能平级或升级隔离强度）的直接结果，不加特例。
4. 用户的环境配置（环境变量、联网模式、启动脚本，以及以后的个人配置包）**经 Sandbox 接口的通用操作交付**（`exec(env=…)`、写文件、出网网关的授权），不依赖 Firecracker 独有的能力。所以旁路的 gVisor 上同样可用；快照、块盘只是加速。
5. **fc-manager 与 sandbox-agent 都用 Rust 写**（2026-11-03 用户定）。sandbox-agent 先按现有 HTTP 接口原样改写（D-28），K8s、Cloudflare、本地后端一起换过去，D-20 的 `--init` 与 vsock 直接写在 Rust 版里，不再在 Go 版里做。两者放在同一个 Cargo workspace，共用 vsock 与系统调用的薄封装。规则见第 10 节。

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
fc-manager（Rust，宿主机上的 systemd 服务，不在 Pod 里）
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
- fc-manager 是宿主上新增的**最高权限组件**（要开 `/dev/kvm`、起 jailer）：代码保持小、依赖按第 10 节的清单人审、输入全部校验、不把用户可控的字符串拼进命令、只允许 Worker 访问（见 D-20 的防错）。

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

> 状态（2026-11-03）：已完成，没部署（`51716da` 测试、`f9f0c4d` 实现）。firecracker 种类在 D-20 之前只能登记为停用；验收 6 没做（一致性测试按适配器在 py-core 里跑，不按登记文件参数化）。

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

> 状态（2026-11-05）：**第二段已完成，fc-manager 已装在线上独服**（见本节末尾“D-20 第二段、D-21、D-22”）。2026-11-04：第一段已完成，没部署（`ac3f243`、`79ef29f`、`04e0e2c`、`3eb02c6`）：`services/rs-fc-manager`、`deploy/images/firecracker` 的客户机内核、`deploy/cluster/fc-manager.service` 与 `install.sh` 第 3b 步，测试对着假的 jailer 与 firecracker（实现记录见本节末尾的“D-20 第一段”）。下一段：firecracker 适配器写进 SBX-01 的 Go Sandbox Service（`services/go-sandbox-service`，2026-11-04 起它接管所有后端适配器，不再写下面交付里的 Python 适配器）；客户机里 agent 的 `--init` 与 vsock 由 D-28 的 Rust 版实现；真 VM 的验收（FC-REAL）在生产独服上跑，要人同意。

**目标**：每个环境一台 Firecracker microVM；Worker 经 `firecracker` 后端使用，接口与其他后端相同。

**交付**：

- `services/rs-fc-manager`（新的 Rust crate，单独的 `Cargo.toml` 与提交进仓库的 `Cargo.lock`；依赖见第 10 节）：宿主机上的服务，管 VM 的创建、销毁、列出，以及到客户机 agent 的反向代理（`/v1/envs/{id}/agent/…`）。
- `libs/py-core/src/agentruntime_core/adapters/firecracker_sandbox.py`：`FirecrackerSandboxProvider`，实现 Sandbox 与 `SandboxArchive`、`SandboxBrowse`、`SandboxLister`；agent 的 HTTP 客户端复用 `sandbox_agent.py`。登记字段：`manager_url`、`images`（运行环境 → 镜像，写法同 K8s）、`kernel`、`workspace: archive`、`egress_gateway: true`。
- `services/rs-sandbox-agent`（D-28 改写后的 Rust 版）：`AGENT_LISTEN=vsock:<端口>`，以及 `--init`（当 PID 1：挂 `/proc`、`/sys`、`/dev`，设主机名，挂数据盘，回收孤儿进程，响应关机信号）。
- `deploy/images/firecracker/`：客户机内核的固定版本与构建脚本（配置裁剪：无模块、串口控制台）；版本要发布满两周（沿用 D-14 的原则）。
- `deploy/cluster/fc-manager.service`（systemd，仿 `ar-firewall.service`）；`install.sh` 核对 `/dev/kvm`；Worker 到 fc-manager 的 NetworkPolicy（只放行 Worker）。
- 资源：vCPU、内存按 `SandboxSpec`；可写层大小有上限；jailer（非 root、chroot、独立 PID 与网络命名空间）、seccomp、cgroup v2；每宿主并发 VM 数上限。
- 对账：`list_environments()` 按 fc-manager 的清单，owner 放在 VM 的标签里。

**难点与防错**

- **不能真跑的机器**：见第 6 节。
- **AF_VSOCK**：两端都是 Rust，经 `libc` 调用，封装放在 workspace 里共用的一个 crate，不需要别的库。
- **fc-manager 里不写 `unsafe`，除了 vsock 与少数系统调用的薄封装**：这些封装集中在一个模块里，单独评审。
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

#### D-20 第一段（2026-11-04）

做了什么（都在 `services/rs-fc-manager`，说明在 `src/lib.rs`）：

- **接口**与 Cloudflare 桥同形：`GET /healthz`；`GET /v1/envs`（只给 `_platform`，带 owner、labels、created_at，给对账）；`POST /v1/envs`（`runtime`、`cpu_millis`、`memory_mb`、`disk_mb`、`labels`，未知字段拒绝）；`DELETE /v1/envs/<id>`（没有也是 204，别的作用域 404）；`/v1/envs/<id>/agent/<路径>` 原样转给客户机里的 agent。认证 `Authorization: Bearer`，令牌是 SecretStore 平台作用域的 `sandbox/fc-manager-token`（`AR_SECRETS_FILE`，文件变了就重新读）；作用域 `X-Sandbox-Owner`。超容量 503 `host_capacity`、启动失败 503 `boot_failed`，都带 `Retry-After`；firecracker 拒绝配置是 500 `vm_config_rejected`。
- **每台 VM**：jailer（每台自己的 uid、chroot、`--new-pid-ns`、自己的空 netns、cgroup v2 的 `cpu.max`、`memory.max`（加 64 MiB 给 VMM）、`pids.max`），firecracker 自带的 seccomp（不传 `--no-seccomp`、`--seccomp-filter`），jailer 与工具的环境清空；内核与 rootfs 硬链接进 jail（要同一个文件系统，启动时核对），rootfs 只读，scratch 是稀疏文件、0600、属于这台 VM 的 uid；没有网卡。每宿主的 VM 数、vCPU、内存、盘的总量有上限，并发建也守得住。
- **agent 令牌**：每台 VM 随机生成，只在 fc-manager 的内存里；不进内核命令行、磁盘、日志。客户机启动时 agent（PID 1，最先起来的进程）连宿主（CID 2）的 701 端口读一行令牌，fc-manager 交出一次就关掉这个端口。转发时去掉调用方的 `Authorization`、`X-Sandbox-Owner` 与自带的 `X-Sandbox-Token`，换上这台 VM 的。
- **重启：终止，不接管**（第 8 节那条“接管还是终止”定了）。理由：接管要把 agent 令牌落盘；宿主上权限最高的组件要小；环境没了 Worker 已经会记 lost、透明换新（D-13）。代价是 fc-manager 一重启，VM 里上次存档之后的改动都没了，所以**升级 fc-manager 前先把后端设成 `enabled: false` 排空**。建环境先写记录、再动宿主；清扫在 `serve` 启动时、systemd 的 ExecStopPost（`fc-manager sweep`）各跑一次，按记录、jail、cgroup、netns、日志里认得出的环境 ID 拆干净；锁文件保证同一个 state 目录只有一个 fc-manager，sweep 不会误杀在服务的 VM。VM 进程自己死了（被杀、内核 panic）由定时的 reaper 发现并拆掉，之后的请求是 `unknown_env`。
- **宿主侧不需要 AF_VSOCK**：Firecracker 的 vsock 在宿主上是 Unix socket 加一行 `CONNECT <端口>` 的握手（客户机连宿主是 `<uds_path>_<端口>`），fc-manager 用标准库的 UnixStream。`unsafe` 只在 `src/sys.rs`（`kill`、`statfs`、`geteuid`），别处由 lint 禁止；D-28 的 `libs/rs-sys` 落地后挪过去。
- **部署件**：`deploy/images/firecracker`（linux 6.18.57，2026-10-15 发布；以 Firecracker v1.17.0 的 `microvm-kernel-ci-x86_64-6.18.config` 为底，叠上 `agentruntime.config`：无模块、串口控制台、没有 virtio-net、没有 vsock 回环；源码与配置都按 sha256 核对、先核对再解包，构建后逐项核对叠上的配置还在）；`versions.conf` 的 `FIRECRACKER_VERSION=v1.17.0`（2026-09-10）与它的 sha256；`deploy/cluster/fc-manager.service`（仿 `ar-firewall.service`，`ConditionPathExists=/dev/kvm`，排在防火墙与 k3s 之后，ExecStartPre 与 ExecStopPost 各清扫一次）与 `fc-manager.env.example`（只听 cni0 的 10.42.0.1:8097）；`install.sh` 第 3b 步（有 `/dev/kvm` 才装 Firecracker，给了 `FC_MANAGER_BIN` 与 `FC_MANAGER_SHA256` 才装 fc-manager）。

测试：`tests/acceptance.rs` 19 条对着 `fc-fake`（一个可执行文件按文件名扮演 jailer、firecracker 与 ip；firecracker 在 InstanceStart 之后扮演客户机：取令牌、应答 `CONNECT`、后面是假 agent），只要 Linux，不要 KVM 与 root；`tests/real_vm.rs` 是 FC-REAL，设 `AR_TEST_FIRECRACKER=1` 才编译；`tests/deploy/test_firecracker.py` 查部署件。CI 是单独的 `.github/workflows/rs-fc-manager.yml`，D-28 的 rust 一步落地后并进去。2026-11-04 在 GitHub 的 Linux runner 上：fmt、clippy（`-D warnings`）通过，单元测试 24 条、验收测试 19 条全过，`real_vm.rs` 不设变量时 0 条（没收集）；本机（Windows）跑可移植部分的单元测试，`AR_TEST_FIRECRACKER=1` 时的 `real_vm.rs` 只做过 clippy；部署测试 9 条，8 条在本机过（`bash -n` 那条只在 Linux 上跑，本机用 Git Bash 手工查过），`test_cloud.py` 里核对版本与脚本的 5 条也照旧通过。提交：`ac3f243`（测试）、`79ef29f`（实现）、`04e0e2c`（部署测试）、`3eb02c6`（部署件）。

| 验收 | 本段 |
| --- | --- |
| 1 一致性测试对适配器 | 没做：适配器是下一段（Go Sandbox Service） |
| 2 真 VM：内核不同、PID 1 是 agent、只有 lo | 写在 `real_vm.rs`，没跑（要 D-28 的 vsock agent 与 D-21 的 rootfs） |
| 3 令牌、非 root、只读根 | 宿主侧已测：令牌不进命令行、磁盘、日志、jailer 的环境，转发不带调用方的令牌；客户机里的那一半在 `real_vm.rs` |
| 4 内存、磁盘、CPU 的上限 | 已测参数与文件（`cpu.max`、`memory.max`、`pids.max`、vCPU、稀疏 scratch 的大小）；超限的效果在 `real_vm.rs` |
| 5 超容量 transient、已有的不受影响 | 已测（VM 数、内存、盘，并发建） |
| 6 销毁无残留、重复销毁、杀掉重启无孤儿 | 已测（进程、jail、cgroup、netns、记录、日志；SIGKILL 后重启、建到一半的三个崩溃点、`sweep` 子命令） |
| 7 杀掉 VM 记 lost | 宿主侧已测（fc-manager 发现并清理，之后 `unknown_env`）；记 lost、透明换新是适配器的事 |
| 8 冷启动与密度的数字 | `real_vm.rs` 打印，没跑 |

没做、没核对的：

- 真 VM 上一次都没跑过。按 Firecracker v1.17 的文档与二进制里的参数名写的，要在独服上核对：jailer 会不会自己建 jail 里的 `/run`（fc-manager 先建好并交给这台 VM 的 uid）、vsock 的握手与 `<uds_path>_<端口>`、`--level` 的输出去向。
- 要 D-28 在 Rust 版 agent 里做的（本段不改它）：`--init` 当 PID 1；`AGENT_LISTEN=vsock:700`（在客户机里监听 vsock 700 端口，小于 1024，非 root 的命令占不了）；`AGENT_TOKEN_VSOCK=701`（启动时连 CID 2 的 701 端口读一行令牌，不从环境变量、文件读）；根是只读的 `/dev/vda`，`/dev/vdb` 是 fc-manager 用 `mkfs.ext4` 格式化好的 scratch（挂成 overlay 的上层与工作目录）。这些参数来自内核命令行（`src/spec.rs` 的 `boot_args`），D-28 定了别的名字就改那一处。
- D-21 的 rootfs：现在 `AR_FC_IMAGES_DIR/<runtime>.ext4` 要人放（旁边的 `.sha256` 作镜像 digest）。D-22 的出网转发、预览没做：客户机现在完全不能联网。
- Worker 到 fc-manager 的 NetworkPolicy（只放 Worker）是 Chart 的事，随适配器一起做；现在靠令牌与 cni0 地址。
- 多宿主不在本期（`manager_url` 是单个地址）。
- D-28 的根 Cargo workspace 还没到 main：这个 crate 暂时自成 workspace（自己的 `Cargo.lock`）。落地后删掉 `Cargo.toml` 的 `[workspace]` 与本目录的 `Cargo.lock`，在根的 members 里加上，CI 并进 rust 一步，`cargo deny` 用根的 `deny.toml`。

#### D-20 第二段、D-21、D-22（2026-11-05，V1-FC）

v1 只写实现（all-plans-dag 第 0 节），下面的都没有新测试；已有的 fc-manager 验收测试（19 条，对着 fc-fake）、单元测试与 `tests/deploy` 照旧通过。

- **客户机里的 agent**（`services/rs-sandbox-agent/src/guest.rs`，系统调用在 `libs/rs-sys/src/guest.rs`）：`sandbox-agent --init` 当 PID 1：挂 /proc、/sys、/dev/pts、/dev/shm、/run；`/dev/vdb`（scratch）挂到 `/mnt/scratch`，里面的 `workspace`、`tmp` 绑定挂到 `/workspace`、`/tmp`（nosuid、nodev）；主机名 sandbox，lo 起来；连 CID 2 的 701 取令牌；再起 **agent 子进程**（同一个二进制），令牌经标准输入的管道给（`AGENT_TOKEN_STDIN=1`），子进程以 root 绑好 vsock 700 后降到 10001（`AGENT_UID`）并再设一次不可转储。PID 1 之后只 `wait4(-1)`：命令留下的孤儿过继给它、在这里回收（**SBX-ZOMBIE**）；agent 子进程退出就关机（令牌只交一次，VM 没用了，fc-manager 发现后清掉，Sandbox Service 记 lost、换新）。把 init 与 agent 分成两个进程，是为了 PID 1 的 `wait4(-1)` 抢不到 agent 等它自己的命令的退出状态。镜像的环境变量由 D-21 写进 `/etc/agentruntime/env`，PID 1 交给 agent 并全部列进 `AGENT_ENV_PASSTHROUGH`。
- **适配器**（`libs/go-core/adapters/fcsandbox`）：与 cfsandbox 同形，经 fc-manager 的 `/v1/envs`、`/v1/envs/<id>/agent/…`；只收 `network: none`（联网一律经网关的授权）、`workspace` 必须 archive、按项目定制的镜像 `unsupported_image`（envbuild 本来就只给 K8s 或 `custom_images: true` 的后端用，所以退回 universal）；`PreviewAddress` 调 fc-manager 的新路由。Sandbox Service 的登记收 `kind: firecracker`（`manager_url`、`runtimes`、`disk_mb`；令牌 `sandbox/fc-manager-token`；isolation 默认 microvm、workspace 默认 archive）。
- **D-21**（`deploy/images/firecracker/rootfs.sh`）：本机 Docker 里的平台镜像 `docker export` 展平、以 root 解开、注入 agent、建挂载点、写 `/etc/agentruntime/env`，`mkfs.ext4 -d` 打成盘（无日志）；缓存键是镜像 ID＋agent 的 sha256＋脚本格式版本，放 `<images>/cache/<键>.ext4`，复用前核对内容哈希；`<runtime>.ext4` 原子地换成硬链接，`.sha256` 写镜像 ID（环境表的镜像 digest）；链接数为 1 的旧盘删掉。**与计划不同**：没有复用 `oci_registry.read_layout` 的路径检查、也不是“不要 root”，因为本期只转平台自己构建的镜像（计划里同样这么限定）；租户镜像要进来时按计划另做。`install.sh` 第 3b 步给了 `FC_AGENT_BIN` 就转换 `FC_ROOTFS` 列的镜像。线上首次转换：python 389 MiB 6 秒、universal 2318 MiB 57 秒、node 514 MiB 13 秒、go 1221 MiB 48 秒。
- **D-22**（`services/rs-fc-manager/src/net.rs`）：客户机里 PID 1 的转发器 127.0.0.1:3128 → 宿主 vsock 1080、127.0.0.1:3129 → 1081；fc-manager 在每台 VM 的 `<uds>_1080`、`_1081` 上听，转给 `AR_FC_EGRESS_TARGET`、`AR_FC_MIRROR_TARGET`（ClusterIP:端口，写死在配置里，不解析客户机给的地址）；登记里 `egress_gateway_url: http://127.0.0.1:3128`、`pkg_mirror_url: http://127.0.0.1:3129`，代理变量与凭据由服务照旧注入。预览：`POST /v1/envs/<id>/preview {"port"}` 在 fc-manager 的监听 IP 上开一个端口，连接经 vsock 702 进客户机，第一行 `PORT <n>`，agent 转到 127.0.0.1:<n>；网关的 NetworkPolicy 加 `egressGateway.previewHostCidrs`（云上 10.42.0.1/32）。每台 VM 转发的连接数上限 `AR_FC_MAX_FORWARD_CONNS`（默认 128），预览端口每台最多 8 个；没做带宽上限（用量在网关按凭据记）。
- **线上第一次真 VM**（2026-11-05，本机，Firecracker v1.17.0、客户机内核 6.18.57）：建 python 的 VM 1.12 秒、universal 1.27 秒（建到 agent 就绪）；客户机里 `id` 是 10001、`uname -r` 是 `6.18.57-agentruntime`、PID 1 是 `sandbox-agent --init`、`/sys/class/net` 只有 `lo`、根只读、`/workspace` 与 `/tmp` 可写、读 PID 1 与 agent 子进程的 `/proc/<pid>/environ` 都是 Permission denied；经转发器连包镜像的 `/healthz` 是 ok，连出网网关得到它的 400（不是代理请求）；后台进程退出后没有僵尸；预览：客户机里 `python3 -m http.server 8000`，经 fc-manager 开的端口取到页面，客户机里没有服务的端口得到空回复；销毁后 jail、netns、VM 的 cgroup、监听都没了，重复销毁 204；手工 `kill -9` firecracker 进程，2 秒内 reaper 清掉，之后的请求是 `unknown_env`。

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

### D-28 sandbox-agent 改写为 Rust（强档）

**目标**：用 Rust 原样实现现有 sandbox-agent 的接口与行为，所有后端换过去，之后删掉 Go 版。2026-11-03 用户定。

**交付**：

- `services/rs-sandbox-agent`：与 `services/go-sandbox-agent` 同样的 HTTP 接口（`/healthz`、`/v1/exec`、`/v1/exec/cancel`、`/v1/files`、`/v1/archive`、`/v1/manifest`、`/v1/list`、`/v1/grep`）、同样的令牌校验、同样的环境变量白名单（`AGENT_*` 一个都不给命令）、同样的存档格式与路径规则（`archive.go`，以及与之对齐的 `agentruntime_core.dev.workdir`）。Linux 与 Windows 都能编译（Windows 只给本机开发测试，进程组的做法照 `proc_windows.go`）。
- 换过去的地方：`deploy/images/sandbox-python`、`universal` 等镜像的构建阶段；Cloudflare 桥的容器镜像（`deploy/cloudflare/sandbox-bridge`）；测试里本机编译 agent 的地方（`adapters/kindtest.py`、`adapters/cloudflaretest.py`、`tests/adapters/test_sandbox_exec_cancel.py`、py-worker 的 `test_interrupt_undo.py`）改成 `cargo build`；注释里指向 Go 版的路径（`processes.py`、`dev/workdir.py`、CLAUDE.md）。
- 一个发布周期内两版并存，之后删除 `services/go-sandbox-agent`。
- 本项只做原样改写。agent 按清单直接拉取、上传块（`/v1/restore`、`/v1/snapshot`）是之后的 WS-PULL-01（[workspace-read-latency-and-disk-2026-11-03.md](workspace-read-latency-and-disk-2026-11-03.md) 第 6 节），不混在本项里。

**难点与防错**

- **行为对齐靠黑盒测试**：`agentruntime_core.conformance.sandbox` 与 Go 版自己的测试（约 810 行）里的用例，先整理成对两版都跑的黑盒用例（起一个 agent 进程、经 HTTP 验证），两版都过才切换；Go 版的单元测试里测内部函数的，改写成等价的黑盒用例或 Rust 的单元测试。
- **存档格式不能变**：已有的快照（ObjectStore 里的清单与块）要能被 Rust 版导入；测试用 Go 版导出的 tar 与清单做 golden，Rust 版导入后清单一致，Rust 版导出的再被 Go 版导入一致。
- **取消与超时**：杀整个进程组、取消后确认已停（`ExecNotStopped` 的语义）、输出截断上限，与 Go 版一致。
- **依赖比 Go 版多**：Go 版只用标准库（含 `archive/tar`、`crypto/sha256`）；Rust 版至少要 tar、sha256、JSON 的库，它跑在每个沙箱里、握着 agent 令牌，依赖清单按第 10 节人审，tar 的解包路径校验（`..`、绝对路径、指出去的符号链接、设备文件）不交给库的默认行为，自己再核一遍。
- **令牌不落到命令看得到的地方**：沿用 D-05 的检查（`/proc/<pid>/environ`、命令行、磁盘）。Go 版只查了子进程的环境，`/proc/1/environ` 没查（[auth-tokens-2026-11-03.md](auth-tokens-2026-11-03.md) 的 AGENT-TOK-01）；Rust 版启动后 `prctl(PR_SET_DUMPABLE, 0)`，验收 4 要包含读 `/proc/<agent>/environ` 与 `cmdline`。

**验收**

1. 两版对同一套黑盒用例全部通过（Linux；Windows 跑本机开发用的子集）。
2. 存档互通：Go 版导出 → Rust 版导入、Rust 版导出 → Go 版导入，清单逐项一致。
3. 恶意 tar 套件（同 D-21 验收 3）导入失败，工作目录之外没有写出。
4. 命令读不到 agent 令牌；`AGENT_*` 不进命令的环境。
5. K8s（kind，`AR_TEST_KIND=1`）与 Cloudflare 假桥的测试用 Rust 版通过。
6. 记录两版的常驻内存与静态二进制大小（只记录，不设门槛）。

**依赖**：无（D-20 依赖它）。无契约变更。

**状态（2026-11-05）：代码与离线验收已完成，没部署。** 测试 `b71e578`、`65c0f74`，实现 `ded75be`，切换 `3cff64e`。

- 黑盒测试 `tests/sandbox-agent`（242 条）对 Go 版与 Rust 版都过：HTTP 与认证、命令、文件、取消与断开、存档（含 26 种恶意 tar）、令牌不可读，以及两版之间逐字节的对照（一组请求的状态码、响应体与关键的头）、随机拼的 400 个正则、Go 版导出的 golden（`golden/go-export.tar` 与清单，`make_golden.py`）与四个方向的交叉导入。Go 版的单元测试测内部函数的（Tail、PassThrough、mergeEnv 等）改成了 Rust 的单元测试与黑盒用例。验收 1～4 由它覆盖；`scripts/check.py` 的 `rust` 一步跑它。
- 验收 5：Cloudflare 假桥上的一致性测试（`libs/py-core` 的 `test_cloudflare_sandbox.py`、`test_sandbox_exec_cancel.py`）与 py-worker 用假桥的测试已经换成 Rust 版并通过。kind（`AR_TEST_KIND=1`）在这个云开发环境里跑不了：Docker 是 cgroup v1，kind 0.33 的默认节点起不来，换 `kindest/node:v1.31.12` 后 kubelet 也建不了 Pod 的沙箱（嵌套的容器运行时），留给有 cgroup v2 的机器或 CI 的夜间任务（`python-cross-tenant` 那个 job 跑 py-core 的全部测试，含 kind）。镜像的构建阶段在这里用 Docker 验过：`sandbox-agent` 镜像能构建（构建时要信任本环境代理的 CA，正式构建不用），拿出来的二进制过黑盒测试。
- 验收 6（2026-11-05，本机 Linux，Go 1.26.0 与 Rust 1.99.1）：静态二进制 Go 6,652,066 字节（`-trimpath -ldflags="-s -w"`），Rust 2,106,176 字节（musl、LTO、strip）。常驻内存：空闲 Go 6.4 MiB、Rust 1.4 MiB；跑一组负载（20 条各输出 100 KB 的命令、一条输出 8 MiB 的命令、对 225 个文件的清单、存档导出与搜索）之后，峰值 Go 53.6 MiB、Rust 31.1 MiB，结束后 Go 仍是 53.6 MiB（GC 没还给系统）、Rust 回到 1.8 MiB。
- 换过去的：`deploy/images` 里 sandbox-agent、sandbox-python（Cloudflare 桥的镜像）、universal 与单一语言镜像的构建阶段（`rust:1.99.1-slim-bookworm`，musl 静态）；`agentruntime_core.adapters.agentbuild`（kindtest、cloudflaretest 用，`AR_SANDBOX_AGENT_IMPL=go` 在两版并存期间换回 Go 版，`AR_SANDBOX_AGENT_BIN` 给现成的）；CI 的 `rust` job 与 py-core、py-worker、夜间任务装 Rust；`rs-fc-manager.yml` 并进 `rust` 一步；fc-manager 成为根 workspace 的成员。
- 没换的：SBX-01 新加的 `services/go-sandbox-service/internal/server/harness_test.go` 仍 `go build` Go 版 agent（那条线的文件，两版并存期间可用；SBX-01 那边空了时改成 cargo 或 `AR_SANDBOX_AGENT_BIN`）。Go 版 `services/go-sandbox-agent` 按计划保留一个发布周期，之后删掉它、`agentbuild` 的 go 分支与黑盒测试的 go 参数。
- 与 Go 版有意不同、或做不到一样的：命令的 PATH 里有相对目录（如 `.`）时 Go 会转去查 agent 自己的 PATH，Rust 版跳过这一项；JSON 里不合法的 UTF-8 Go 换成 U+FFFD、Rust 版拒绝（400）；HTTP/1.0 的 keep-alive 不支持（回完就关）；Windows 上不记 CPU 时间（Go 版记）。这几处 Runtime 的客户端都碰不到。
- Windows：`cargo clippy --target x86_64-pc-windows-gnu` 通过（编译得过），没在 Windows 上跑黑盒测试（这台机器没有 Windows），留给本机。
- **部署（不归本项）**：要换的镜像是 `sandbox-agent`（K8s 沙箱 Pod 的 initContainer，`worker.shell.backends` 的 `agent_image`、Chart 的 `sandbox-prepull`）、`sandbox-python`（Cloudflare 桥，`wrangler deploy` 重新构建）、`sandbox-universal`、`sandbox-node`、`sandbox-go`，都按新的 tag 重新构建、推送、改 values。上线后要在真沙箱里探：① gVisor 与 Kata 里以 10001 跑的命令读不到 `/proc/<agent>/environ`、`cmdline`、`mem`（gVisor 自己模拟 /proc，PR_SET_DUMPABLE 的效果要实测；kind 那条测试也在 gVisor 上跑一次）；② 经 pods/proxy 与 Cloudflare 桥的取消：断开与 `/v1/exec/cancel` 都杀掉整个进程组；③ 用 Go 版存过的工作区快照恢复进 Rust 版的环境，清单一致；④ 没有 IPv6 的 Pod 里 `:7070` 退回 0.0.0.0；⑤ initContainer 的 `install` 子命令、只读根文件系统下照常；⑥ 真实负载下的常驻内存。出问题时回退：values 里的镜像 tag 换回上一版（Go 版的镜像还在）。

## 6. 测试策略（没有 KVM 的机器）

沿用仓库已有的做法（`AR_TEST_KIND=1`、`AR_LIVE_CLOUDFLARE=1`）：

- **默认**：适配器与 Worker 的测试对**假 fc-manager**跑。假的 fc-manager 在进程里起一个真的 `sandbox-agent` 当“客户机”，走和真的一样的 HTTP 路由，所以一致性测试、环境表、对账、回退、配额都能在任何机器上测。
- **真 VM**：设 `AR_TEST_FIRECRACKER=1`。要 `/dev/kvm` 可读写、PATH 上有 `firecracker` 与 `jailer`，缺任何一个就失败（不静默跳过）；不设这个变量时这些用例不收集。夜间任务和生产独服上跑。
- vsock 的单元测试（`syscall` 手写部分）只在 Linux 上编译，Windows 开发机上用构建标签隔开。

## 7. 顺序与上线

| 步 | 内容 | 说明 |
| --- | --- | --- |
| 0 | D-28（sandbox-agent 改写为 Rust） | 与 D-19 并行；D-20 依赖它 |
| 1 | D-19 | 先做；它和 D-23 都改 `Sandboxes`/Broker，同一时间只由一个人改 |
| 2 | D-20（先用假 fc-manager 跑通） | 与 environment-config.md 的 D-23、D-25 可并行：它们不依赖 Firecracker |
| 3 | D-21、D-22 | D-20 之后；两者可并行 |
| 4 | 独服上跑真 VM 的验收 | 记录耗时与密度 |
| 5 | D-26 | 依赖 D-24 的 setup；放在最后 |

**切换是改配置，不是改代码**：上线时 `defaultBackend` 先保持 `gvisor`，firecracker `enabled: true` 但只给指定租户（租户策略 `backend: firecracker`）灰度；真 VM 验收通过后把 `defaultBackend` 改成 firecracker，gVisor 留作旁路。

## 8. 没有验证的、要实测的

- 单宿主上同时能跑多少台 VM、冷启动 P50 与 P95、快照恢复耗时：**都没有数字**。
- （已不适用：sandbox-agent 改用 Rust 后，vsock 两端都经 `libc`。）
- 选定的 Firecracker 与内核版本是否通知客户机“被克隆过”（影响 D-26 的重置方式）。
- ~~fc-manager 重启时接管还是终止~~：定为终止（D-20 第一段，理由见那里）。
- 多宿主调度不在本期（登记里 `manager_url` 先是单个地址）。

## 9. 与既有设计的出入

- cloud-deploy.md 第 4.2 节“公开注册的租户 → microVM（Kata 或 Cloudflare）”：建议加上 firecracker。陌生租户的默认仍应是 microVM 级，这一点不变。
- 同一节“完全访问只能落在 microVM 后端”：沿用 [design-changes-workspace.md](design-changes-workspace.md) 已经建议的口径（按租户策略决定）；firecracker 属于 microVM。
- 设计书 4.10、附录 L.11 里“Sandbox 的后端”要加 firecracker 一种；这是设计书的改动，列在 [environment-config.md](environment-config.md) 第 7 节，由人更新原文档。

## 10. Rust 的依赖与工具链（fc-manager 与 sandbox-agent，2026-11-03）

用户定 fc-manager 与 sandbox-agent 都用 Rust（第 1 节第 5 条）。**2026-11-04 用户按清单批准**（原话“按清单批准”）：下面列的库可以用；HTTP 与 TLS 的两处“实现前比较后定”仍由实现的会话比较后写进本节，不用再等人审，除非要加清单之外的库。D-28 可以开工。

- **布局**：仓库根一个 Cargo workspace（与 `go.work` 并列），成员 `services/rs-sandbox-agent`、`services/rs-fc-manager` 与共用的 `libs/rs-sys`（vsock、进程组、cgroup 等系统调用的薄封装；`unsafe` 只允许在这里）。

- **设计书与 CLAUDE.md**：设计书 4.15 的技术栈与仓库结构加 Rust、`services/rs-sandbox-agent`、`services/rs-fc-manager` 与 `libs/rs-sys`，由人更新原文档后重新导出；CLAUDE.md 的仓库结构与常用命令同步加上（实现时改）。
- **依赖**：`Cargo.lock` 提交；只收发布满两周的版本（与 Python 的 `exclude-newer` 同一原则）；`cargo deny` 检查许可证、安全公告与重复版本，进 CI。拟定的最小清单：两者共用 `libc`（vsock、cgroup 与进程相关的系统调用）、`serde` 与 `serde_json`；sandbox-agent 另要 `sha2` 与一个 tar 库（或自写只支持所需子集的 tar 读写，实现前比较后定）。HTTP：fc-manager 的对外 API 与连 Firecracker 的 API（UDS 上的 HTTP）要么用一个小的 HTTP 库，要么自己写最小的 HTTP/1.1；TLS 要么用 `rustls`，要么让 fc-manager 只听宿主内的 UDS 或回环地址、由已有的入口做 TLS。**这两项在实现前比较后定，写进本节**，不默认引入异步运行时。
- **fc-manager 的 HTTP 与 TLS（D-20 比较后定，2026-11-04）**：
  - HTTP：自己写最小的 HTTP/1.1（`services/rs-fc-manager/src/http.rs`：从严解析、Content-Length 与 chunked、一个连接一个请求），不引入 HTTP 库与异步运行时。fc-manager 只有四个路由加一个按字节转发的代理，连 Firecracker 的 API 是 UDS 上的几个 PUT；小的 HTTP 库也要多审一套依赖，而转发要原样流过请求体并把断开传给 agent，用库反而绕。
  - TLS：不做，不引入 `rustls`。fc-manager 只听 k3s 的 cni0 地址（`10.42.0.1:8097`），Pod 到它的流量不离开宿主；公网网卡上 `ar-firewall` 只放 22；认证靠 SecretStore 平台作用域的令牌。多宿主（第 8 节，本期不做）时再换 `rustls` 或经已有的入口。
  - 实际的依赖：`libc` 0.2.191、`serde` 1.0.229（derive）、`serde_json` 1.0.154，以及它们带来的 `itoa`、`memchr`、`proc-macro2`、`quote`、`syn`、`unicode-ident`、`zmij`、`serde_core`、`serde_derive`；2026-11-04 核对 crates.io，都是 2026-10-16 或更早发布的。`cargo deny` 等 D-28 的 `deny.toml`。工具链 1.99.1（2026-10-13）。
- **sandbox-agent 的 HTTP、TLS 与依赖（D-28 比较后定，2026-11-05）**：
  - HTTP：自己写最小的 HTTP/1.1（`services/rs-sandbox-agent/src/http.rs`），行为照 Go 的 net/http：keep-alive、Content-Length 与 chunked 的请求体、`Expect: 100-continue`、请求头 10 秒内读完、2 KiB 以上的响应分块、HEAD；路由与路径规范化照 Go 的 ServeMux（非规范路径 307、方法不对 405）。不引 HTTP 库与异步运行时：一个连接一个线程，与 Go 版的 goroutine 对应；断开检测（`peer_gone`，peek 套接字）要拿到底层连接，用库反而绕。
  - TLS：不做，与 Go 版一样是明文 HTTP。agent 只在沙箱里听，经 K8s API Server 的 pods/proxy、Cloudflare 桥或（D-20）vsock 到达，认证靠 `X-Sandbox-Token`（恒定时间比较）。
  - tar：用 `tar` 0.4.46（`default-features = false`，不带 xattr）。只用它读写头与流，解包不用它的 unpack：名字、类型、上级目录、链接目标都自己核（`archive.rs`），恶意 tar 套件在 `tests/sandbox-agent`。
  - **清单之外加了一个 `regex` 1.13.1（待人确认）**：`/v1/grep` 要与 Go 的 RE2 语法、语义一致（代审记录第 4 节的验收），自写一个正则引擎风险更大。只开 `std`、`unicode` 与几个不带新依赖的 perf 特性（不开 `perf-literal`，不拉 aho-corasick）；Go 写法到 regex 写法的改写与 Go 不收的写法在 `goregex.rs`，Go 1.26 收的 Unicode 类名表从 Go 生成。随机拼的 9000 个正则两版结果逐字节相同。人不同意时的替代：自写只支持 RE2 子集的回溯引擎，或 grep 退回固定字符串匹配（接口要改）。
  - 实际的依赖（agent，`cargo tree -p rs-sandbox-agent -e normal`）：`libc` 0.2.191、`serde_json` 1.0.154（带 `serde_core`、`itoa`、`memchr`、`zmij`）、`sha2` 0.11.1（带 `digest`、`block-buffer`、`crypto-common`、`hybrid-array`、`typenum`、`cpufeatures`、`cfg-if`）、`tar` 0.4.46（带 `filetime`）、`regex` 1.13.1（带 `regex-automata` 0.4.18、`regex-syntax` 0.8.11），共 19 个；2026-11-04 核对 crates.io，最新的是 `libc`（2026-10-16），都满两周。没用 `serde` 的 derive（只用 `serde_json::Value`，JSON 自己按 Go 的格式写）。`deny.toml` 在仓库根：只收 MIT、Apache-2.0、Unicode-3.0，重复版本、通配版本、未知来源一律拒绝，安全公告与没人维护的都报；2026-11-04 通过。
  - `unsafe` 只在 `libs/rs-sys`（prctl、kill、wait4、poll、faccessat、statfs、启动时删环境变量），其余 crate 由 workspace 的 lints 一律 deny；fc-manager 的 kill、geteuid、statfs 也挪了过去。
- **工具链**：固定 Rust 版本（`rust-toolchain.toml`，同样满两周）；`scripts/check.py` 加一步 `rust`：`cargo fmt --check`、`cargo clippy -D warnings`、`cargo test`、`cargo deny check`；目标 `x86_64-unknown-linux-musl` 静态链接；fc-manager 不需要镜像（systemd 服务），`deploy/cluster/install.sh` 装二进制并核对哈希；sandbox-agent 在 `deploy/images` 的各镜像里换成 Rust 的构建阶段，D-21 的 rootfs 转换注入同一个静态二进制。本机是 Windows：Linux 目标在容器里编译。
- **测试**：第 6 节的假 fc-manager 在 Python 测试里，与 fc-manager 的语言无关，照旧；fc-manager 自己的单元测试在 `cargo test` 里，真 VM 的用例同样由 `AR_TEST_FIRECRACKER=1` 控制。
