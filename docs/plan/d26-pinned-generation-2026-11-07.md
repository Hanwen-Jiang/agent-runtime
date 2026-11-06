# D26-REVIEW03：失效快照仍被活动 VM 引用时保留配额

基于现有真实验收线 `261450be`，复用已实现的实际 jail 镜像固定、API socket 路径限制、IO accounting、warm hardlink修复和真实rootfs，不重写这些实现。

失效快照先持久写入 `invalidated` 标记，立即禁止新恢复；state/memory/scratch仍有任何活动hardlink时保留整个缓存条目，并按实际文件逻辑长度计入容量。没有引用的失效条目才可删除。重启/reopen后仍从磁盘标记和nlink恢复计费。该key有旧代pin时只冷启动，不悄悄发布新代绕过容量；最后一个引用释放后可清理旧代并重新捕获。

本轮验收：

- `cargo test --offline -p fc-manager --lib`：**35 passed**，包括失效代保留配额、Cache reopen仍计费、最后pin释放才回收，以及原有通信/缓存用例；未skip。
- 显式启用真实KVM的 `memory_snapshot_clones_refresh_identity_and_keep_tenant_disks_private`：**1 passed，37.68s**。使用优化release manager、`/var/lib/agentruntime/fc/goal-final-img/python.ext4`，独立测试目录 `fcr-9120fb87`，结束后全部VM销毁、sweep 0残留、目录清理。验证machine-id/boot-id/hostname/随机状态、时间、跨租户拒绝、工作区隔离、真实memory恢复日志、活跃clone仍健康时另一restore损坏使cache失效、tombstone保留、最后pin释放后recapture。
- 证据 `.goal-tools/fc-pin-functional-release2.log`。前两次失败保留：debug哈希超过测试HTTP60秒（`.goal-tools/fc-pin-functional.log`，VM sweep成功）；第二次release还未链接完成就启动测试导致binary不存在（`.goal-tools/fc-pin-functional-release.log`，无VM创建）。未隐藏或删除失败记录。
- 生产manager/rootfs均未修改。费用 $0。性能测试另以固定提交、新目录、协调窗口采集，功能耗时不作为启动性能。

## 真实测量结果与交付证据

固定实现提交 `17f1c7b2c8e1a661eb09b8cbec0a6c9ed090f0c6`，相同优化manager二进制、Firecracker、kernel与rootfs；精确SHA-256、规格、原始CPU/内存/IO计数、每次请求、失败分类及清理均在 [comparison.json](evidence/fc-pin-20261107/comparison.json) 和其引用的原始文件中。主机24逻辑CPU，VM 1 vCPU / 256 MiB / 256 MiB scratch，Python基础镜像。每个时延从创建请求开始至首条真实shell命令结束；p50/p95为预定nearest-rank。

| 模式 | 样本 | p50 | p95 | 失败 |
| --- | ---: | ---: | ---: | ---: |
| cold | 20 | 1025.29 ms | 1227.86 ms | 0 |
| warm pool | 20 | 24.53 ms | 52.36 ms | 0 |
| memory snapshot | 20 | 9654.47 ms | 10201.03 ms | 0 |
| memory snapshot 复核（同版本、全新目录） | 20 | 9649.91 ms | 10319.38 ms | 0 |

两组snapshot各另有一次真实捕获预填充，保留但不混入20次恢复分位数。每个快照恢复都有同env_id权威bootstrap path证据；没有以耗时猜启动来源。实际结果是快照约为cold的9.4倍耗时，**本实现/规格未观察到加速收益**。内容校验和恢复IO成本不能为了好看的分数被移除，生产默认继续保持`AR_FC_SNAPSHOT_CACHE_MIB=0`；功能可用不等于应该默认启用。

CPU/内存/IO：原始样本逐次包含宿主`/proc/stat`、memory、diskstats、PSI及VM cgroup `cpu.stat/memory.current/memory.peak/memory.events/io.stat`。四组平均宿主CPU忙碌率依次15.43%、18.29%、18.77%、13.96%；VM cgroup memory.peak中位数依次约52.6、52.9、7.2、7.1 MiB。后两值受到恢复文件映射/按需触页影响，**不据此宣称整机内存容量提高**。空io.stat表示该时间窗没有归属该cgroup的块设备IO记录；宿主背景IO仍单独保留，不能冒充VM私有IO。

共享宿主测量限制：主集成负责人为本次预留了窗口、停止已知构建，但既有线上服务和已经开始的Memory实验继续运行。首组snapshot窗口20:05:49Z观察到短暂Go编译；因此保留全部第一组并另跑20次复核。复核中仍观察到不属于本测量进程的Go/Postgres测试；[build-process-observations.jsonl](evidence/fc-pin-20261107/snapshot-repeat/build-process-observations.jsonl)保留每秒观察，首组干扰在`first/contention.json`。没有删除慢样本，没有称为完全纯净/排他宿主测量。两组在这些真实背景负载下均显示稳定负收益，最终部署决策维持快照关闭。

密度复用已完成且配置明确的先前真实数据（源码`5e1037e5`，单独保留版本，不混入上述启动对比）：96个idle VM同时在场且逐个首命令成功；MAX_VMS=96、MAX_VCPUS=96、内存/磁盘配置预算各32768MiB。记录在`density-earlier-source/{manifest,samples,summary,cleanup}`，它是**至少96台的观测下界与配置档位，未探测物理容量极限**，不把旧64或当前96配置数写作硬件极限。工作负载仅idle+一条shell，不推断编译/真实任务密度。

本轮结束后所有测试manager、VM、jail、cgroup、随机token、secret文件与gp9/gp10的基础快照缓存均已清理；[host-cleanup.json](evidence/fc-pin-20261107/snapshot-repeat/host-cleanup.json)记录0残留检查。原始测量产物保留并带摘要索引。生产manager、生产rootfs与其它会话原始产物未修改。新增模型费用 **$0**。
