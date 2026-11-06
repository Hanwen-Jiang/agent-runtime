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

这项验收确认当前真实 R2 的签名约束，不代表 agent 直传、精确主机授权与操作结束收回已经接通；这些由 WK-LITE/WS-PULL 另验。
