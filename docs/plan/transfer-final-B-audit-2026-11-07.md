# B 传输末轮独立审计（2026-11-07）

冻结基线 `dcf11ca2`，独立工作树 `.worktrees/B-audit-transfer-final-20261107/agent-runtime`；没有修改 A 工作树、线上服务或任何冻结评测配置。

## 确证缺口与修复

`CommitTransfer` 在持久日志已有 result 时直接返回旧回执；若对象已被生命周期或清理删除，Service 重启后的重试仍能把不存在的 URI 重新登记成交付。新增真实 Service/agent/HTTP ObjectStore 回归先成功上传、删除对象、重启 Service，再恢复同一 transfer，旧代码红灯。修复后 cached result 也需 ObjectStore HEAD 存在且尺寸与原 prepared 信息一致，缺失返回 NotFound；不 GET 文件字节、不重新上传源文件。

## 验证

- Release 静态 Rust agent 构建成功（Rust 1.99.1，56.74s）。
- `go test -count=1 ./services/go-sandbox-service/internal/archive ./services/go-sandbox-service/internal/server`：通过（0.038s / 41.530s），含68MiB大文件分块变化/重复快照/恢复、丢失回包后Service重启、原始准备内容固定、跨租户拒绝。
- 上述两包 `go vet`：通过。
- Worker `test_transfer_live_chain.py test_direct_delivery.py test_context_read.py test_chunked_workspace_copy.py`：24 passed / 63.59s，含真实Worker→Service→agent→HTTP ObjectStore链路。失败命令仅返回小摘录和pending引用，后续context_read上传原字节，命令只执行一次；PUT次数1，显式读取前GET次数0。
- 真实Rust agent `test_direct_transfer.py`：7 passed / 4.19s，含两阶段不可变内容、大文件有序块、重复缺块零下载、坏摘要/路径拒绝、不完整恢复挡住后续操作和输出容量预占在命令副作用之前拒绝。
- Cached receipt 缺失对象的精确NotFound断言单独复验，日志见下方green文件。

Go/Python持久manifest预检接受明确v1/v2，旧nil空清单可读；v1拒绝chunks；路径、重复/非目录父级、模式、块与整文件尺寸/摘要、来源workspace均在恢复/复制前检查。Rust恢复在完整块及整文件hash校验后通过staging发布；中断状态阻止其他工作区操作。

## 证据与资源

父目录 `.goal-tools/B-transfer-final-{build,go,python,http,receipt-red,receipt-green}.log`。`receipt-red`保留修复前失败，不冒充通过。测试仅使用独立临时本地HTTP对象服务器；无新真实R2调用、无模型调用，费用0。已启动必要测试后获知另有FC测量；未再启动额外全套，结束时间可用日志mtime与进程记录核对。所有测试进程已退出，测试夹具清理服务/agent/对象资源。A仍需将修复集成并部署后核验线上链路，不能把本报告当作全20项完成。

补充可信摘要边界：若ObjectStore.Head返回非空SHA256，所有确认/恢复路径均与prepared摘要核对，摘要矛盾即拒绝。已补同长度对象替换、HEAD返回真实新摘要时拒绝缓存回执的回归。正常R2 SignedUpload的ObjectInfo不提供摘要，此时依赖checksum签名绑定、If-None-Match不可覆盖和唯一对象key（STOR-SEC真实验收），不能声称仅长度验证内容哈希。
