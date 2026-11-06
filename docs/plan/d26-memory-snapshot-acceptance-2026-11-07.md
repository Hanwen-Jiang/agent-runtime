# D-26 内存快照实现与验收记录

状态：实现候选与离线回归已提交，**真实快照恢复未验收、未部署，FC-REAL 未完成**。旧预热池不计入本项完成。来源为本轮中断前保留在 `.worktrees/root-test/agent-runtime` 的实现；该目录原始修改未覆盖。

## 实现

- `rs-fc-manager/src/snapshot.rs` 在客户机 PID 1 完成空白文件系统初始化、尚未收到任何凭据或租户输入时执行 Firecracker `PATCH /vm Paused`、`PUT /snapshot/create`，持久保存真实 state、memory 与一致的 blank scratch。恢复执行 `PUT /snapshot/load`、`PATCH /vm Resumed`，不是预热池分配。
- `AR_FC_SNAPSHOT_CACHE_MIB` 默认为 0；缓存只在宿主本地。缓存键包含内核、rootfs、Firecracker 二进制的真实 SHA-256、宿主内核、CPU/microcode/flags、VM 规格、init 与快照格式。公开镜像路径变更后还核验 jail 内实际硬链接内容，防止恢复内存与磁盘版本错配。
- 创建临时目录后 fsync 文件、manifest 与目录，再原子发布；启动清理本缓存的未完成目录。恢复逐文件验证类型、长度、不可写权限与 SHA-256。发现不兼容或损坏会清除缓存、完全拆掉尝试中的 VM，再按同一容量预占冷启动；不向调用方交付半启动的 VM。
- 按配置容量执行 LRU；活动 VM 对内存文件的硬链接阻止普通 LRU 淘汰。内存和 state 为 root 所有的只读文件；可写 scratch 是逐 VM 独立副本。ObjectStore 工作区恢复与此基础内存快照不同，不在快照中保存租户工作区。
- 每次创建生成独立 env_id、宿主 uid/jail/netns/cgroup/vsock UDS 与 agent token。guest CID 3 在各 VMM 私有 UDS 内复用，不是跨 VM 路由标识。快照中的 vsock 连接不复用；客户机重连后才交付新的 token、512 bit 宿主熵、machine-id 与宿主时间，收到 ACK 后才探测 agent 健康。
- 客户机在任何用户进程启动前给内核 CRNG 注入并重播种新熵、校准时钟、挂载新的 machine-id/boot-id/hostname，随后启动降权 agent。token 只经 vsock 与 stdin，未写入磁盘、环境或日志。

## 本轮修正

旧真实日志 `.goal-tools/d26-real-release2.log` 显示 snapshot bootstrap 后 agent 未就绪，随后冷回退成功，因此不能当作快照验收成功。源码显示保存和校验快照时仍持有客户机仅等待 5 秒的旧连接；现已在保存后关闭旧连接，重新握手并要求 ACK，清理过期连接积压，再开始独立健康检查期限。此修正仍需真实 KVM 复测才能确认解决该失败。

离线回归另复现了同大小快速写入可保持相同时间戳的问题。原先仅依赖 inode/mtime/ctime 的 hash memo 可能返回旧摘要；已改为按实际字节重新校验，不以元数据代替内容完整性。这会增加恢复 IO，**实际收益未测定**。

真实隔离回归检查：真实 memory 文件、恢复路径日志、无重新内核启动日志，machine-id、boot-id、hostname、随机 UUID、时钟新鲜度、两租户文件隔离与权限边界，以及篡改缓存后的冷启动回退。旧容量测试明确设置 VM/vCPU 两种配置上限，记录为 idle VM 观测下界，不称物理极限。

## 本轮验证与阻塞

工具链：Rust 1.99.1，离线 Cargo registry；系统 PATH 无 C 编译器，使用现有容器镜像只读文件中的 GCC 12 与动态库，不启动容器。命令共同环境如下（路径为本机现有缓存）：

```sh
export PATH=/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/108/fs/usr/bin:/var/lib/docker/volumes/transfer-rustup/_data/toolchains/1.99.1-x86_64-unknown-linux-gnu/bin:$PATH
export LD_LIBRARY_PATH=/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/108/fs/usr/lib/x86_64-linux-gnu
export CARGO_HOME=/root/proj/agent-projs/.goal-tools/fc-cargo
export RUSTFLAGS='-C linker=/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/108/fs/usr/bin/gcc'
cargo test --offline -p fc-manager --lib
cargo test --offline -p rs-sandbox-agent --bins
AR_TEST_FIRECRACKER=1 cargo test --offline -p fc-manager --test real_vm memory_snapshot_clones_refresh_identity_and_keep_tenant_disks_private -- --exact --nocapture
```

- 客户机 20 条单元回归通过；其中新增 bootstrap 完整性及 boot-id 新鲜度验证。
- 缓存专项 3 条通过：同大小内容修改、LRU/pin/非法项、逐 VM 可写盘隔离和损坏拒绝。
- `fc-manager --lib` 完整执行：25 passed / 4 failed，四项均为现有运行沙箱禁止 Unix listener 或 setsockopt（EPERM），包括新增缓存隔离测试通过。未跳过、删除断言或标记整套通过。日志 `.goal-tools/d26-current-unit.log`。
- 真实 KVM 测试已显式启用并执行，环境缺少 `/dev/kvm`，不得以本轮编译或旧预热结果替代真实验收。日志 `.goal-tools/d26-current-real.log`。
- `cargo clippy --offline -p fc-manager -p rs-sandbox-agent -p rs-sys --all-targets -- -D warnings`（`AR_TEST_FIRECRACKER=1`，包含真实测试编译）通过。
- FC-REAL 测量 runner 原有 9 条回归通过。真正的 cold/warm/snapshot、密度及 CPU/内存/IO 采样尚未运行，无 p50/p95 或收益结论。

## 部署与下一步真实验收

需恢复宿主 KVM、网络监听和部署工具访问，保持单独资源窗口。先保存当前 manager、配置、rootfs 的回退副本（rootfs 用独立硬链接，防止转换器清理旧镜像）；排空 Firecracker 环境后部署新 manager 与包含新 guest bootstrap 的 rootfs，再启用本项配置。无需模型调用，模型费用为 $0。

依次运行上述真实隔离测试、已有 Firecracker 端到端验收，再使用 `scripts/fc_real_measure.py` 在相同规格和相同提交的独立实例采集 cold/warm/snapshot/density。runner 的窗口锁、原始样本、配置上限、资源余量、失败分类和清理审计见 `goal-fc-real-2026-11-06.md`。若真快照不能创建或恢复，必须保留失败日志并恢复服务，不能发布收益或宣告 D-26 完成。

## 跨线集成

D-26 提交 `5726d1f1` 已与主线 `f2daba6f`（含 WK-LITE/WS-BLOB 与 D-27）在独立分支合并；保留 blob 与 snapshot 模块，共用 `sha2` 一次。合并后 fc-manager 全目标 clippy 通过。全包 clippy 发现 transfer 旧代码的两项 lint（exec 嵌套 if、archive 测试模块后有函数），已交回该文件负责人修复；没有降低 lint 标准。
