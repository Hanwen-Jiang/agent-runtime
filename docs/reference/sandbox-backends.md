# 沙箱后端：控制面、VMM 与取舍（2026-10-27）

对话里几轮讨论的结论，供 [设计书改动稿](../plan/design-changes-workspace.md)（W1、W5～W11）与 [云上部署计划](../plan/cloud-deploy.md)（D-05～D-17）引用。版本与价格按 2026-10 核对。

## 1. 先分两层

沙箱牵涉两层，常被混为一谈：

| 层 | 管什么 | 例子 |
| --- | --- | --- |
| 控制面 | 调度到哪台机器、建与删、配额、网络规则 | K8s（调度器、kubelet、containerd）、Cloudflare 自己的调度（Worker、Durable Object）、E2B 的 api 与节点进程 |
| 隔离层 | 进程在什么边界里跑 | runc（普通容器）、gVisor（用户态内核）、microVM（KVM 虚拟机，由 VMM 跑） |

- **Kata** 不是虚拟机，是 containerd 下面的一个运行时：把一个 Pod 装进一台 VM，VM 由它下面的 VMM 跑。kata-deploy 4.2 带的 VMM：Cloud Hypervisor、QEMU、Dragonball（Kata 4 默认，内嵌在 Rust 运行时的进程里）、Firecracker（只有 Go 运行时的版本）。
- **Firecracker** 是 VMM（AWS 为 Lambda、Fargate 做的）。
- **microVM** 是一类东西的统称：设备极少、启动快、内存开销小的 KVM 虚拟机。

## 2. 三种组合

| | K8s + Kata（main 用） | Cloudflare Containers（main 用） | 自托管 E2B（只在 e2b 分支） |
| --- | --- | --- | --- |
| 跑在什么上 | 带 `/dev/kvm` 的 Linux 节点：独立服务器，或开了嵌套虚拟化的云主机（AWS C8i/M8i/R8i 2026-02 起、GCP、Azure；嵌套有性能损失） | Cloudflare 的机器，看不到节点 | 专用的 KVM 节点：内核 6.8+、预留 2 MiB 大页、nbd 模块；节点进程要 root 与宿主网络 |
| 一条命令的路径 | kubelet → containerd → Kata 的 shim → VMM → 客户机内核 → kata-agent → 进程 | Worker（桥）→ Durable Object → Container | E2B api → 节点进程 → Firecracker；命令经 client-proxy 到 VM 里的 envd |
| 运维 | 节点、升级、容量都归我们 | 不用运维 | 第二套控制面（自己的 Postgres、Redis、ClickHouse） |
| 规格上限 | 看机器 | standard-4：4 vCPU、12 GiB、20 GB 盘 | 看机器 |
| 新建 | 冷启动一台 VM，秒级 | 1～3 秒 | 从模板的内存快照恢复，不走内核启动 |
| 暂停 | 没有（Pod 只有在跑和删掉） | 休眠即停机，不留内存 | 带内存暂停与恢复、fork |
| 文件 | virtio-fs 直接挂节点上的工作区 | 临时盘，休眠清空 | 写时复制根盘，暂停时进快照 |
| 出网 | NetworkPolicy 只能按 IP；按域名靠出网网关（D-16） | 全断或按域名白名单，只有 80、443 | 按域名允许与拒绝；可经 SOCKS5 交给我们的网关 |
| 进网 | Pod IP 直连（agent 的 7070） | 只经 Worker（HTTP），要写桥 | 经 client-proxy，要 traffic 令牌 |
| 计费 | 机器的固定成本 | CPU 按 10 毫秒、内存按配置，休眠不收钱 | 机器的固定成本 |

## 3. VMM 的功能差别

| | Firecracker | Cloud Hypervisor | QEMU |
| --- | --- | --- | --- |
| 文件怎么进 VM | 只有块设备（Kata 要配 devmapper；E2B 用 NBD 写时复制） | virtio-fs | virtio-fs、块设备 |
| 带内存的快照与恢复 | 一等功能（E2B 靠它） | VMM 支持，Kata 不开放给 Pod | 同左 |
| CPU、内存热插拔 | 没有，只有 balloon | 有（Kata 按 Pod 的 limits 调） | 有 |
| GPU 直通 | 没有 | VFIO | 有（Kata 的 NVIDIA 变体都基于 QEMU） |
| 机密计算（TDX、SEV-SNP） | 没有 | 部分 | 有（Kata 的 snp、tdx 变体） |
| 攻击面 | 最小 | 小 | 大 |

隔离强度对我们是一档（KVM 硬件虚拟化，各自的客户机内核）。实际差别在三件事：文件系统怎么进 VM、能否带内存暂停、能否调大小与直通设备。

## 4. 取舍（已定）

1. **主后端 K8s + Kata**（Cloud Hypervisor，跑在 Kata 4 的 Rust 运行时上，handler `kata-clh-runtime-rs`）：一套控制面，网络策略、配额、指标、环境表都在一处；工作区经 virtio-fs 挂节点盘。邀请制租户用 gVisor（不需要 KVM、启动快）。
2. **溢出后端 Cloudflare Containers**：机器不够时零运维地扩，按域名的白名单现成；代价是临时盘、规格小、只能经 HTTP 进。
3. **E2B 不进 main**（2026-10-27 用户定）：自托管的 E2B 只在要“带内存暂停”的长寿命沙箱、fork 或高密度快速新建时值得多养一套控制面，放在可插拔的 `e2b` 分支。不自己直接驱动 Firecracker：那等于重写 E2B 的节点进程（NBD、userfaultfd、网络槽位、模板构建）。
4. 后端由租户策略选（W4），暂时不可用时按回退列表换，只能平级或升级隔离强度（W11）；配额与预算按环境表跨后端算（W9、W10）。

## 5. 其他几轮的结论（细节在改动稿）

- **语言环境**：所有后端用同一份 Dockerfile 打的 `universal` 镜像（Python、Node、Go、JDK、Rust 等），按 digest 固定；换后端不涉及迁移语言环境。不是“每次换容器都重装”：基础镜像预拉在节点上，会话里复用同一个环境（W5、W6）。仓库自带环境定义时在 microVM 里构建一次、按定义的哈希缓存（W6 第 3 条）。
- **装依赖**：集群里的包镜像按需拉取、内容寻址，有高低水位与两段 LRU、单文件大小上限、按租户的未命中配额，被曝恶意的版本可以全局清除（W7）。
- **联网测试**：默认断网；按任务申请联网授权，人批准后经出网网关出去，网关按域名、端口、字节、连接数、时长限制并逐连接审计，到期或 Task 结束即收回；进网只有带签名短期令牌的预览地址（W8）。
- **要买的**：一台带 KVM 的独立服务器（64 GB 内存、NVMe）、一个域名、Cloudflare（免费版的 DNS 与代理、R2、Workers 付费版给 Containers）；见云上部署计划第 2 节。
