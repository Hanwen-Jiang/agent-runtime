# STOR-SEC 定向真实存储验收（2026-11-07）

对象存储配置取自正在运行的 `ar-agent-runtime-sandbox-service` Deployment 及其引用的 Secret；所有凭据仅传入临时 0600 env 文件，执行后删除，不进日志或仓库。

运行 `libs/go-core/adapters/s3objects/signed_upload_live_test.go` 的 `TestLiveSignedUploadSecurity`，仅在随机 `stor-sec/<128-bit-id>/` 前缀创建对象并逐个清理，不调用桶生命周期 API。

结果：10 个子用例全部通过，测试耗时 5.36 秒：

- 正确内容可以写入，原对象内容核验正确；再次写同键被拒绝。
- 同长度错误内容被拒绝（校验和）。
- 错误 Content-Length 被拒绝。
- If-None-Match、x-amz-checksum-sha256 分别缺失、篡改均拒绝。
- 改对象键、改 HTTP 方法均拒绝。
- 一秒签名地址在三秒后拒绝。

复跑（凭据由当前部署注入，禁止把值写到命令行）：

```sh
AR_LIVE_S3=1 go test ./adapters/s3objects -run '^TestLiveSignedUploadSecurity$' -count=1 -v
```

补验真实 Rust agent 的两阶段 PUT 与分批 GET 恢复：11 个子用例全过，6.27 秒，其中真实 agent 流程 1.25 秒。Prepare 后修改源文件仍上传原字节；重复 Commit 成功且不重复覆盖；下载后恢复文件的原始字节一致。测试对象均已清理，未调用生命周期 API。

代码验收：Rust 共 50 个单元用例通过；真实 agent HTTP 字节黑盒 4 项通过；Go Service 直传、独立 stdout/stderr 与故障回退、去重快照/恢复 3 项通过；Python 预占/交付 19 项和 Sandbox Service/平台工具 39 项通过。节点缓存覆盖同 hash 单次并发拉取、损坏后重拉、LRU 上限、重启复用以及 VM/method/expiry 拒绝。

当前交付直传保留 25 MiB 文件配额，输出直传单流最大 64 MiB。工作区大文件分块正在后续提交补齐。部署时需在 agent 运行镜像与 fc-manager 宿主提供 /usr/bin/curl 和 CA 根证书，Firecracker guest 3130→vsock1082 接线由 D26 集成提交提供。
