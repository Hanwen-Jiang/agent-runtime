# B：N16-R/EV-REAL 固定语义评分执行器

状态：实现与离线回归交付，不代表真实22项评分完成。A仍是唯一真实Task/裁判执行者，B没有模型调用、部署或冻结环境变更。

既有paired_completion.review_export在11题×off/rules均有有效终态及规划证据后，导出去掉arm标签的22项材料。新模块paired_semantic_judge直接消费这份导出与原paired-manifest，不提交Task、不重跑答案、不删题、不修改五维rubric/0、0.5、1锚点。输入的题ID集合、证据摘要、两臂裁判Profile及Registry版本/digest必须一致。保留全部提供的上下文，不以截断难题使费用通过。

prepare只写本地不可变执行清单，原计划semantic_judge_usd预留是唯一总限；当前正式plan为7美元、22次预计6.336美元。模型从两臂semantic_judge_profile固定（当前Opus5.5），不使用Task内部judge角色的Sonnet配置替代。显式tier在付费前冻结，默认strong@medium；生成上限沿原judge_assumptions.output_tokens。profile/tier返回失配立即停止且保留费用。

执行只支持真实grpc_gateway经AuditedGateway逐RPC身份记录，再由BudgetGateway约束累计额度。每项调用前fsync pending，返回原始响应先落0600文件，再校验完整STOP、全部维度、分值和证据理由。逐次读取model_usage，核所有attempt的node身份、Profile、Registry和费用；任何pending、额外节点、错Profile、缺摘要或账本与返回费不一致均停止后续调用。Gateway内部重试产生多个attempt仍全部计费；本执行器不额外重试。

每项成功逐字节保持在journal，正常续跑只跳过已确认完成项；未知/失败/残缺尾部拒绝自动续跑，不因有已结算下界而清掉pending。恢复核原始响应摘要与评分一致。输出review.json直接符合paired_completion.semantic_review/completed_report，ledger-final.json可复查所有尝试。不会将BudgetGateway账单当作真实ledger替代。目录用进程锁，另一执行者不能同时使用。

## 调用

先等待A的完整22项证据导出，核裁判租户有批准的Opus权限和剩余7美元预留，Gateway与Registry和原观测匹配。真实hard-route配置仅匹配subject租户`gh-goalpair-20261107`；专用裁判租户`gh-goalpairjudge-20261107`不命中该覆盖，使用原Opus pin。配置读取已核实，不为裁判改变冻结的Task角色模型。工具不创建策略或改预算。

```sh
python -m agentruntime_learning.benchmarks.paired_prepare review-export \
  --out <完整配对run目录> --destination <blinded-export.json>
python -m agentruntime_learning.benchmarks.paired_semantic_judge prepare \
  --plan <paired-manifest.json> --export <blinded-export.json> \
  --tenant <已批准裁判租户> --gateway <实际grpc-host:port> \
  --tier strong@medium --max-output-tokens 12000 --thinking-budget-tokens 8192 \
  --out <全新judge目录>
# 默认只预览，不发生RPC
python -m agentruntime_learning.benchmarks.paired_semantic_judge run --out <judge目录>
# 由A执行；既有AR_RELATIONAL_DSN只用于租户作用域只读账本，不放argv/产物。
AR_EVAL_TENANT=<同上> AR_MODEL_GATEWAY_ADDRESS=<同上> \
python -m agentruntime_learning.benchmarks.paired_semantic_judge run --out <judge目录> --execute
```

每个judge输出目录独立的usage子目录以plan.run_id+-semantic-judge固定task身份。不要为失败调用换run_id或目录重问；先用实际model_usage、calls.jsonl、budget.json和原响应核对，恢复策略需明确且不能重复扣费。语义判分仍可能未达到原质量门槛；执行完成不等于N16-R/EV-REAL通过。

本轮无模型支出。回归覆盖完整cohort与不可变身份、评分锚点/空理由、正常22项执行和零新增调用续跑、未知/格式错/截断响应拒绝、Ledger节点/Profile/Registry/费用失配、原响应篡改和错Gateway。精确命令与结果在父目录.goal-tools/B-semantic-judge-tests.log及最终针对套件日志。

最终回归：新执行器30passed/0skip（0.90s），既有paired_completion、paired_evaluation、eval_budget、audited_gateway64passed/0skip（2.11s）。日志.goal-tools/B-semantic-judge-tests-final.log和B-semantic-judge-compat.log。

A接续整合：manifest还固定SYSTEM、材料转换版本与全部22请求模板摘要，恢复重建核对。付费前核实真实适配器：显式MaxOutputTokens直接写入max_completion_tokens；ThinkingBudgetTokens仅映射reasoning_effort。因此使用原计划的总completion cap12000、effort hint8192（medium），不把两项相加或解释为独立可见/思考额度。此前未执行准备曾误写3000+8192，已在首笔调用前纠正并保留旧准备版本；模型、评分及7美元上限不变。只投影所有真实plan图与triage_level，剔除运行内Jev评分提示；原export不改，附加材料单独保存。scripts/prepare_paired_semantic_review.py完整保留22份答案，加入原fixture路径、已冻结HTTP参考资料及完整真实工具轨迹摘要；参考摘要/跨Task或租户/不完整trace拒绝。全部材料准备后再核算完整输入及总completion预计费用，7美元原裁判限不变，不截断难题。

新版执行器48测试及材料准备4测试通过，邻接兼容79测试通过，均未调用真实模型。旧/opt/evals/.../score_semantic.py仅保留未执行的准备记录；本模块是唯一最终评分路径，不运行两套循环。

输出限制依据：services/go-model-gateway/internal/gateway/budget.go的outputLimit及libs/go-core/adapters/openaicompat/wire.go的chatRequest、provider.go的EffortForBudget。该次修正只涉及尚未付费的裁判准备，不改变已执行Task或线上环境；实际请求摘要将在完整22份材料准备时冻结。
