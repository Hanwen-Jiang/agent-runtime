# 真实评测费用上限：Gateway未知用量回归（2026-11-06）

基线为评测集成分支 `goal/evaluation-20261106` 的 `decdfd2f`。本分支 `goal/gateway-budget-eval-20261106` **只新增测试与本说明**，不修改 `budget.go`、`gateway.go`、账本、proto或既有断言；核心修复归主集成负责人。

## 必须保持的行为

付费评测带 `CallContext.max_cost_usd_micros`。某个provider attempt的用量未知时：

1. 原始reservation保持pending（actual金额、token为NULL），不能退款为0。
2. 本次RPC立即失败关闭，不能继续provider retry、fallback，也不能发成功的流式final。
3. 错误必须非retryable，并明确表达账本用量不确定（`model_usage_*`），使调用方能保留预占；本测试不锁死pending/unsettled的具体后缀，由核心实现决定。
4. 前面已知的实际费用仍通过错误details返回，不把pending估算当作已结算费用；原有冻结价格和usage行保留。
5. 真正明确未计费的NotFound拒绝仍可结算0并按原规则fallback。已知费用仍消耗请求剩余额度。

无request cap的旧兼容行为不是这些新断言的范围，不删除或放松既有测试。

## 新测试与当前真实结果

新文件：`services/go-model-gateway/internal/gateway/eval_budget_pending_test.go`。

复用既有 `fixedBudget`、`budgetTime`、`usageRows`、`fakeProvider`、`testProfiles`、错误details和计价常量。用真实 `gateway.Server` 和真实SQLite账本；供应商替身模拟计费故障，不发真实模型请求。普通调用直接执行真实Server方法；流式用真实Connect/gRPC handler、编码、trailer经内存HTTP RoundTripper连接，避免当前运行沙箱禁止TCP监听的问题。不把内存传输称为真实网络验收。

基线执行结果为 **8个缺陷场景失败、2个控制场景通过，整体exit 1**：

| 场景 | 当前行为 | 期望 |
| --- | --- | --- |
| 文本回答无usage | 返回成功，账本pending | 返回用量未知错误 |
| transient无usage | mid-a调用3次再mid-b成功，留下3个pending | 首个未知attempt后停止 |
| 未提供的tool调用且无usage | fallback到mid-b后成功 | 不再调用其他provider |
| 无usage的invalid output | 只返回模型输出错误 | 显式返回账本用量不确定 |
| 已知435µUSD后下一次unknown | 共调用4次，2个pending，最终成功 | 调用2次后停止，错误保留435µUSD |
| Score零usage | 返回成功，账本pending | 失败关闭 |
| Stream首chunk前unknown | 重试后发成功final | 不重试、不发成功final |
| Stream final零usage | 发成功final | 保留pending并以错误结束 |
| 明确NotFound未计费拒绝 | **通过**：0结算后fallback，实际1496µUSD | 保持 |
| 已知435µUSD与500µUSD请求上限 | **通过**：拒绝下一attempt，保留实际费用 | 保持 |

这些红测试是待核心修复的证据，不标为已修复。不得通过skip、删除有效断言或把未知当免费来变绿。

## 可复现命令与工具

`/tmp/go127/bin/go version`：`go1.27.2 linux/amd64`。已有模块缓存可直接只读使用；没有启动Docker、没有提权、没有联网下载。

```bash
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off \
GOMODCACHE=/var/lib/docker/volumes/goal-root-gomod/_data \
GOCACHE=/tmp/goal-gateway-budget-gocache \
/tmp/go127/bin/go test ./services/go-model-gateway/internal/gateway -run '^TestEvalBudget' -count=1 -v
```

编译检查 `go test ... -run '^$' -count=1` 通过；`gofmt` 与 `git diff --check` 通过。完整红回归日志 `/tmp/goal-gateway-budget-validation/regression.log`。本轮真实模型调用0，实际费用 **$0**。

主集成修复后，应先复跑本文件至全部通过，再在有TCP能力的环境跑原Gateway预算、retry、stream、usage-guard套件，确认无cap兼容性和真实RPC trailer保持。
