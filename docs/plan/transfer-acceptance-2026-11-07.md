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

## 审查补强与可回退启用

补强位于独立 `goal/transfer-review-20261106` / `.worktrees/transfer-review/agent-runtime`；旧 transfer 工作树出现其他执行者的并发编辑后，不再用于这次审查验收。

- **事务结果不明**：交付登记的 commit ACK 丢失后先查询幂等记录，已登记对象不删除；查询也失败时保留对象和预占，不把未知结果当作回滚。清理失败保留预占至到期并保留原始错误。真实 SQLite 注入“成功 commit 后抛错”的回归验证对象仍可读且重试不重复上传。
- **暂存生命周期**：Service 在随机 ID/容量失败时不留下准备结果，过期记录同时释放 agent 暂存；同 VM 的过期槽先清理再准备新上传。agent 独立每 30 秒清理过期内存/恢复块，Service 崩溃不会导致暂存无限保留。
- **恢复发布**：先在同盘私有 `.agentruntime-restore` 中完整解包；失败不搬进工作区，发布中途失败回滚已搬入项。崩溃遗留该目录时，agent 拒绝工作区读写与执行（仍允许 health/cancel），需要销毁该环境，防止重启误复用半成品。目录本身可能是挂载点，不能依赖重命名工作区根。
- **可信字节核验**：`ObjectStore.SignedUpload` 明确保证一次写入、固定 checksum 和 size；diskobjects 实际核对内容，R2 的同长度篡改、签名头篡改和覆盖拒绝已有上面的真实证据。因此随机专用键的 `HEAD` 大小核对用于确认 PUT 完成，摘要信任来自存储端强制校验，不是 agent 回报。新增“PUT 已写入但返回失败”回归同时要求成功幂等与 Service 不 GET 全体字节。不能满足该保证的 provider 必须拒绝 SignedUpload。

`Archiver.ChunkedSnapshots` 默认关闭，仅控制新分块写入，读取始终支持旧整文件和新 chunks。关闭时沿旧清单写入；超过 64 MiB 的文件仍用完整 tar 回退。Service/env/Helm 由主集成接线：`AR_WORKSPACE_CHUNKED_SNAPSHOTS` / `sandboxService.chunkedSnapshots`。部署先使用新 reader、关闭 chunks，验收并保存这个兼容新格式的回退版本；随后启用 chunks 做最终验收。回退关闭写入即可继续读取新旧快照，禁止通过恢复旧快照记录丢弃用户后续改动。

审查树复测：Rust agent 25 项通过；`cargo clippy --offline --locked -p rs-sandbox-agent -p rs-transfer -p fc-manager --all-targets -- -D warnings` 通过；Go archive 2 项通过、Service 测试编译和 vet 通过；Python Worker 交付/分块复制 23 项通过、FileEntry 兼容 1 项通过。两个 Python 测试包需分别启动 pytest，合并调用有同名 `tests.conftest` 导入冲突（已拆开重跑）。新增 HTTP 失败 ACK、写入开关、崩溃标记路由端到端用例仍受当前 socket 权限限制，不能标记验收通过。无新增费用或部署。

## 网络权限恢复后的真实链路与审查收敛（2026-11-06 UTC）

审查 `GOAL-TRANSFER-REVIEW.json` 的 01/02/03 已实现：迁移 216 持久保存 tenant/task/工具幂等键对应的原始传输、稳定对象键、冻结摘要/大小和结果；相同幂等键改变路径或名字冲突；空 lease 恢复只查询指定 tenant/task 的既有身份，上传只能复用原 lease。Worker 在提交前已预占配额，丢响应或取消时保留预占，重建 Worker 与重启 Service 后仍恢复同一对象；登记失败不删除持久 journal 已拥有的对象。

命令输出在执行前预占两个 agent 暂存槽；容量不足时命令未执行。上传失败只返回有界摘录及 `stdout_pending`/`stderr_pending`，`context_read` 通过 `transfer://<task>/<transfer>` 恢复同一上传后按窗口取回，不重执行命令，不再通过 Worker 回传全体字节。超出单流捕获硬上限 `min(file_limit,64 MiB)` 仍明确标为 truncated；引用的大小和摘要描述保留的尾部，不宣称无限完整输出。

节点缓存持独占目录锁，在启动时仅清理本实现 `.pending-<数字PID>-<数字序号>` 常规文件；不删除无关文件，不清理活动写者的文件。04 的账号/桶专属主机校验已集成主负责人的 `50f476ac`。

真实验证：

- Rust agent HTTP 7 项通过（4.09 秒），含 immutable prepare、上传重试、缺块恢复、68 MiB Service 用例前置、恢复崩溃标记、满槽拒绝副作用命令。
- Go Sandbox Service 完整包 90 个顶层测试通过，0 skip；非 root CI 容器启动真实 Postgres、真实 Rust agent。68 MiB 写入开关/缺块/恢复用例 25.91 秒；实际丢弃 Commit HTTP 响应后 Service 重启恢复通过。日志：`evidence/transfer-service-20261106.log`。
- Worker→真实 Service→真实 agent→HTTP 存储两项端到端通过；连续丢三次 RPC 响应后重建 Worker/重启 Service 仅一次 PUT；失败输出只到 Worker 摘录，唯一一次 GET 来自显式 context_read，副作用命令只执行一次。相应回归与交付测试合计 26 项通过，7.31 秒。
- 真实 R2 定向验收 11 子例全部通过，6.53 秒，包含实际 agent 上传/恢复（1.40 秒）、同长度错误内容、checksum/If-None-Match/Content-Length/对象键/方法/到期拒绝；测试对象全部清理，无桶生命周期修改。日志：`evidence/transfer-r2-live-20261106.log`。凭据只在子进程环境中，未落盘。
- Firecracker 库 34 项和 rs-transfer 库 2 项（包含真实 TCP/Unix socket）通过；clippy 全 targets `-D warnings` 通过；Service vet 通过。Python 其他 Service/context/复制 12 项通过，平台/context/交付 43 项通过。

早先两个使用 debug agent 的重复 Service 测试进程已停止，改用 release agent 完整运行；未把中断当通过。旧 Go 与 Rust 共用黑盒首轮 240 通过、2 个 grep 逐字节兼容失败，定位为已替换 UTF-8 文本的 JSON 表示差异；保持有效断言，由独立兼容修复提交关闭。

本轮模型调用 0、模型费用 0；R2 使用既有账号的小对象安全测试，账单尚无可独立归因的本次存储费用，未声称已核得存储费用为零。未自行生产部署。部署必须同时更新 Service、Worker、Rust agent 镜像和 FC rootfs；分块写入先关闭保存兼容 reader 回退点，再启用验收。

最终共同黑盒兼容已修复并复跑：Go 1.27.2 与 Rust agent 的全部 242 项通过，0 skip，51.34 秒（`evidence/transfer-agent-compat-20261106.log`）。修复按固定 Go 工具链的实际 Unicode script alias/JSON 行为；旧的 Go 1.26 限制断言改为验证 1.27 接受别名，并继续验证不支持 Emoji、四个新增 script 的正反匹配。未删减固定或 400 个随机表达式的逐字节断言。Rust 输出转移改用 move，避免为大输出额外复制整个缓冲。

额外摘取 `31708ff0` 的有意义清单入口防护：Go/Python 读取时都拒绝未知版本、缺失 entries、非法摘要/长度/路径/非目录父项/来源；Python 复制前完成验证，不先发布损坏元数据。v1 保持整文件格式，实际含 chunks 时才写 v2；关闭新写入开关仍生成 v1，读取同时支持合法 v1/v2。未重复引入旧的上传未知结果处理，持久 journal 仍是唯一恢复来源。Go archive 3 项与 Python 原 archive/copy 加新元数据回归 14 项通过。
清单入口补强后，Service 的 Archive/Direct/Snapshot 14 项真实回归全部通过（`evidence/transfer-manifest-20261106.log`），含更新的 v1→v2→关闭写入仍能恢复用例，68 MiB 用例 22.86 秒。
