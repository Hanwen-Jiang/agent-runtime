# D26-REVIEW03：失效快照仍被活动 VM 引用时保留配额

基于现有真实验收线 `261450be`，复用已实现的实际 jail 镜像固定、API socket 路径限制、IO accounting、warm hardlink修复和真实rootfs，不重写这些实现。

失效快照先持久写入 `invalidated` 标记，立即禁止新恢复；state/memory/scratch仍有任何活动hardlink时保留整个缓存条目，并按实际文件逻辑长度计入容量。没有引用的失效条目才可删除。重启/reopen后仍从磁盘标记和nlink恢复计费。该key有旧代pin时只冷启动，不悄悄发布新代绕过容量；最后一个引用释放后可清理旧代并重新捕获。

本轮验收：

- `cargo test --offline -p fc-manager --lib`：**35 passed**，包括失效代保留配额、Cache reopen仍计费、最后pin释放才回收，以及原有通信/缓存用例；未skip。
- 显式启用真实KVM的 `memory_snapshot_clones_refresh_identity_and_keep_tenant_disks_private`：**1 passed，37.68s**。使用优化release manager、`/var/lib/agentruntime/fc/goal-final-img/python.ext4`，独立测试目录 `fcr-9120fb87`，结束后全部VM销毁、sweep 0残留、目录清理。验证machine-id/boot-id/hostname/随机状态、时间、跨租户拒绝、工作区隔离、真实memory恢复日志、活跃clone仍健康时另一restore损坏使cache失效、tombstone保留、最后pin释放后recapture。
- 证据 `.goal-tools/fc-pin-functional-release2.log`。前两次失败保留：debug哈希超过测试HTTP60秒（`.goal-tools/fc-pin-functional.log`，VM sweep成功）；第二次release还未链接完成就启动测试导致binary不存在（`.goal-tools/fc-pin-functional-release.log`，无VM创建）。未隐藏或删除失败记录。
- 生产manager/rootfs均未修改。费用 $0。性能测试另以固定提交、新目录、协调窗口采集，功能耗时不作为启动性能。
