# N16-R / EV-REAL 完整配对准备补漏

新增实现不启动模型或 Task，不修改集群。先前十题与事实清单保留；`completion_cases()` 追加 `/root/ar-release/rev38/sub-walk.sh` 的完整 requests/httpx/aiohttp 原题（原 task `tsk_01M6Q70KK2A2NRVG2HYXDSN5DD`），总计11题/22个Task。没有把旧 $1.97 当作当前修复结论。

`evalsets/paired/completion-rubric.json` 在本次真实运行前固定：事实正确性、覆盖、证据及诚实不确定性、综合推荐、规划覆盖五维，0/.5/1 有固定锚点、等权；独立比较 rules 应出现三个独立 sub_agent 分支及依赖三者的综合节点，off/单事实不应拆子 Agent。既有 regex 分数仍是辅助，不能据此宣告完整验收。没有删除旧难题或放松评分。

`paired.py` 复用既有 DeepEval SSE 工具证据采集器，在同一付费 Task 保留证据；Temporal 只解码真实 `plan_round` 完成输出，并保存 id/kind/mode/role/tier/after、事件ID和摘要，不导出目标正文、参数或密钥。租户 memo 不同拒绝；claim-check 复用 Worker 的授权 ObjectStore/SecretStore 读回，无法读回标缺失，不伪造结构。第一份规划固定评分，后续 replans 全保留。

`ReviewRoundOutput.judge_usage` 为可选、默认 None，旧历史仍可解码。Reviewer 用调用上下文隔离记录每次 Jev attempt，返回 model、实际 input_tokens、usage_known、provider_attempted 和 veto_rule_disabled。未调用明确零；HTTP错误/超时/缺usage为未知；禁用后仍调用的检查照常计入。仅加观察字段，不改 Task Loop/Stage Runner/判定规则。活动失败后未返回的证据仍视为未知，不能据成功结果推断失败尝试免费。

Jev 按官方 $0.042/百万输入 token、输出免费核算：[价格依据](https://help.typesafe.ai/articles/5187202169-how-much-does-jev-cost)。没有请求最低费用/失败固定费假设。`estimate` 现在写 soft_task_target_total_usd，明确不是硬上限，并从已有40美元总额扣历史账单、语义判分与Jev预留。完整成本另用隔离租户model_usage前后差额，包含Memory embeddings和所有非Task调用，不只task_id。尚无本次新模型费用。

## 只读准备与执行命令

工具模块 `agentruntime_learning.benchmarks.paired_prepare` 有 scaffold / observe / prepare / scope-ledger / review-export / completion-report。现有部署 manifest 只是候选，不当作线上观察。下列路径使用运行者的新隔离输出目录：

```sh
python -m agentruntime_learning.benchmarks.paired_prepare scaffold \
  --manifest <当前release-manifest.yaml> --models <固定role到profile的JSON> \
  --runtime-commit <完整部署提交> --tenant <隔离评测租户> \
  --task-api https://api.cgent.link --judge-profile <已有批准裁判profile> --out collector.json
python -m agentruntime_learning.benchmarks.paired_prepare observe --spec collector.json --out observed-off.json
# 按已有部署流程切换同一隔离栈的 off/rules。此工具不做切换；排空任务并保持其它条件一致。
python -m agentruntime_learning.benchmarks.paired_prepare observe --spec collector.json --out observed-rules.json
python -m agentruntime_learning.benchmarks.paired_prepare prepare \
  --off observed-off.json --rules observed-rules.json --settings settings.json --out plan.json
python -m agentruntime_learning.benchmarks.paired estimate --plan plan.json
```

`models.json` 必填plan/act/judge/route/sub_agent；实际用到的transform/probe/embed应一并固定。它是待验证的角色模型选择，不是已观测到的实际调用；每次账本仍核实served profile，任何意外回退都会拒绝接受证据。两臂须同主模型、同Registry/Memory/其余配置。

`settings.json` 必填 `run_id, seed, budget_usd<=40, task_usd_limit, thresholds{minimum_quality_gain:0.1,max_cost_ratio:2}, cost_reserves{semantic_judge_usd,external_jev_usd}`；还需 `expected_usage_per_task{off:{role:{calls,input_tokens,output_tokens}},rules:{...}}`、`expected_judge_usage{calls,input_tokens,output_tokens}`、`expected_jev_usage{max_calls,input_tokens_per_call,usd_per_million_input_tokens:0.042,price_basis:<官方或账户记录>}`。历史支出用historical_runs及prior_external_cost_usd显式扣除。预留与输入/输出预估必须先写入，不可看结果后调整。

观察器只执行 kubectl get deployments/pods/configmaps；Secret仅查询resourceVersion（不读取data）；Registry/Memory/账本使用业务库只读事务与明确scope。它核对rollout完成、实际imageID、镜像提交、各Worker显式开关、ConfigMap更新时间早于进程启动、实际加载profile与Registry不可变正文/修订、当前租户Gateway额度。省略off、动态未观察envFrom、未重启的配置修改、内联敏感环境值、Memory写队列不空均拒绝。Memory语料摘要取平台/租户范围内容标识及ACL/状态，不导出正文；运行前后改变即drift。需在已授权隔离环境固定Memory corpus，不能跨臂偷偷带入写回。

在每臂开始和结束（包括候选写回已结算后）只读导出完整范围账本，并执行该臂：

```sh
python -m agentruntime_learning.benchmarks.paired_prepare scope-ledger --tenant <隔离租户> --out off-before.json
python -m agentruntime_learning.benchmarks.paired run --plan plan.json --out <新run目录> --arm off
python -m agentruntime_learning.benchmarks.paired_prepare scope-ledger --tenant <隔离租户> --out off-after.json
# rules同样导出rules-before/after，run --arm rules
python -m agentruntime_learning.benchmarks.paired_prepare review-export --out <run目录> --destination review-input.json
```

准备器生成的snapshot_command会再次现场观察，不回放静态JSON。运行账户需AR_RELATIONAL_DSN（只读、非超级用户）、AR_TEMPORAL_ADDRESS/NAMESPACE、AR_TASK_API_TOKEN_FILE，以及读claim-check所需既有Worker存储配置。鉴权不混用Memory内部token。

`review-input.json` 是去掉arm标签、按匿名ID排序的同批答案/规划/工具证据。由预注册人工或批准模型按五维给出`review.json`：`rubric_sha256, reviewer_version, actual_cost_usd, cost_evidence, items[{id,evidence_sha256,scores:{dimension:{score,reason}}}]`。人工明确0成本；模型判分必须复用现有BudgetGateway冻结裁判/逐次记账并限制在semantic_judge_usd，不可再次提交Task。遗漏题/维度、摘要失配均拒绝。

将四份范围账本整理为 `scopes.json={off:{before:[...],after:[...]},rules:{before:[...],after:[...]}}`，最后：

```sh
python -m agentruntime_learning.benchmarks.paired_prepare completion-report \
  --out <run目录> --export review-input.json --review review.json --scopes scopes.json --destination completion.json
```

完整报告分开输出N16-R质量/总费门槛、EV-REAL结构规划结论、原题Jev各轮否决/额外轮次、所有成本。任何Jev缺usage/未知失败、ledger pending或不含Task完整账单都不会标证据完整。

## 验证

本轮针对配对/prepare/规划/Jev新回归与既有评测、Reviewer用例：`pytest -o asyncio_mode=auto`，75 passed；现有 `AR_LIVE_JEV` 付费冒烟未启用，1 skip，不算真实验收。实际初次命令从仓库根执行未加载asyncio auto导致30个async用例框架失败；按项目asyncio配置复测后通过，没有修改断言。待统一集成后重放旧Temporal历史；本提交不声称已完成真实配对实验。
