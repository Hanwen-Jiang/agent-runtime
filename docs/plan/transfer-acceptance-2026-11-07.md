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

## 续接：大文件分块与元数据一致性

保留上面的真实 R2/HTTP 证据；本段是之后的源码变更，不能用之前结果代替本段的上线验收。

`/v1/snapshot` 现在生成 4 MiB 有序内容块及整文件 sha256；`PrepareRange` 只暂存一个范围的不可变字节。Go Sandbox Service 按缺块签发上传，复读完整清单检测快照期间修改。恢复逐块验 sha256、按顺序重组并验证整文件 sha256，完成验证后才导入工作区；未改动的旧版整文件清单仍能读。可选 `chunks` 元数据同步进入 Python 的复制、引用保留和 GC，以及 Go 的继承块读取和 GC。原来超过 64 MiB 的文件现在走分块路径；交付配额和命令输出上限不扩大。

新增回归包括 68 MiB 文件按缺块存档/恢复、修改一个块只上传一个块、重复恢复批次不重传、错序和损坏拒绝、元数据复制后源删除仍保留活块、跨租户拒绝。未添加迁移或修改公开 proto/schema。

本次已执行：

- `cargo test --offline --locked -p rs-sandbox-agent --bin sandbox-agent`：22 通过。
- `cargo test --offline --locked -p fc-manager --lib blob_cache::tests`：3 通过，覆盖 LRU、损坏重取、并发去重、重启复用。
- `go test ./internal/archive -count=1`（Sandbox Service）：2 通过，使用真实本地 SQLite 与磁盘对象存储。
- `go test -c ... ./internal/server` 与 `go vet ./internal/archive ./internal/server`：通过；六个 Go provider/adapter 包编译通过，编译检查不计作行为验收。
- `pytest libs/py-core/tests/dev/test_file_entry_chunks.py`、`pytest services/py-worker/tests/test_chunked_workspace_copy.py`：各 1 项通过。

当前执行权限在创建 TCP/Unix socket 时返回 `EPERM`，也拒绝 Docker socket；因此完整 FC 库测试结果为 25 通过、3 个 socket 环境失败，rs-transfer 为 1 通过、1 个 HTTP 环境失败，未删除断言或加 skip。新大文件 HTTP/Service 用例尚待可联网执行环境运行，不能算通过。重新部署 agent、Service、Worker 及 FC 的 3130/vsock1082 接线后，必须重跑：

```sh
AR_SANDBOX_AGENT_IMPLS=rust pytest tests/sandbox-agent/test_direct_transfer.py
# services/go-sandbox-service 下，使用非 root 的 Postgres 测试环境：
go test ./internal/archive ./internal/server -count=1
cargo test --locked -p rs-transfer -p fc-manager --lib
```

本次没有付费模型调用、重复 R2 对象写入或部署，费用为 0；测试临时文件由夹具清理。
