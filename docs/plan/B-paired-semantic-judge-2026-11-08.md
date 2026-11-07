# B：N16-R/EV-REAL 固定语义评分执行器

状态：实现与离线回归交付，不代表真实22项评分完成。A仍是唯一真实Task/裁判执行者，B没有模型调用、部署或冻结环境变更。

既有paired_completion.review_export在11题×off/rules均有有效终态及规划证据后，导出去掉arm标签的22项材料。新模块paired_semantic_judge直接消费这份导出与原paired-manifest，不提交Task、不重跑答案、不删题、不修改五维rubric/0、0.5、1锚点。输入的题ID集合、证据摘要、两臂裁判Profile及Registry版本/digest必须一致。保留全部提供的上下文，不以截断难题使费用通过。

prepare只写本地不可变执行清单，原计划semantic_judge_usd预留是唯一总限；当前正式plan为7美元、22次预计6.336美元。模型从两臂semantic_judge_profile固定（当前Opus5.5），不使用Task内部judge角色的Sonnet配置替代。显式tier在付费前冻结，默认strong@medium；生成上限沿原judge_assumptions.output_tokens。profile/tier返回失配立即停止且保留费用。

执行只支持真实grpc_gateway经AuditedGateway逐RPC身份记录，再由BudgetGateway约束累计额度。默认每项调用前fsync pending，返回原始响应先落0600文件，再校验完整STOP、全部维度、分值和证据理由。逐次读取model_usage，核所有attempt的node身份、Profile、Registry和费用；任何pending、额外节点、错Profile、缺摘要或账本与返回费不一致均停止后续调用。Gateway内部重试产生多个attempt仍全部计费；本执行器不额外重试。

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

## 首个未返回请求的唯一显式恢复

原22任务和工具轨迹已完整，实际材料估价6.511312美元。首个Opus RPC只得到ConflictError，没有response或score；权威账本唯一`mbr_WO3FHOU4DZAELVWGLO3BNQOPXX`保留298752微美元，实际用量/费用未知。现有日志遮蔽原provider error，不能推断是timeout/免费；同实际凭据只读GET /v1/models返回200且含Opus，没有发现配置缺口。

经A/B逐项核账，仅允许该未评分首项显式恢复一次，然后原余21项各一次：仍同runID/TaskID/item、模型/Registry/tier/12k总completion、评分材料与规则。原目录7份原件逐字节保持；独立恢复目录引用SHA，从audit序号2继续。原7美元减298752微美元保留额，新执行硬限6.701248美元；完整预计6.511312加保留额为6.810064，不增预算。保留额不计作已结算spent，不修改model_usage；新增未知、原row变化或有效旧score均拒绝恢复/继续。

paired_semantic_recovery只接受该一次已核定失败，不是任意unknown开关。恢复器同时锁原目录及其usage目录，未执行完的下一次失败仍禁止盲重试。最终review记录known_cost_usd与retained_pending，actual_cost_usd=null/cost_known=false；完整报告也不通过费用门槛。140项恢复/裁判/账本/预算/报告回归通过（有限恢复、原件保护、22seq2..23、续跑0RPC、篡改/新pending拒绝），末次金额非有限值检查另53项回归通过。

## 已结算格式失败与统一strict配置

首恢复的新seq2/3有效评分分别34257/35024微美元，合计69281；seq4实际生成了9457输入/739输出，因bad_structured_output失败，已结算47706微美元。原budget已知116987包含这笔失败，不能回退为仅成功费用。适配器在非法JSON时仅保留Usage，现Trace无法还原原文，故不伪造评分。

确定的兼容缺口是reason.minLength触发冻结Go strictUnsupported，导致wire strict:false。schema_transport_version2移除传输层minLength并给score显式number，使wire可strict:true；本地validate_scores仍要求非空理由、全部五维及0/0.5/1，评分标准不放宽。版本1的原22请求摘要仍完全兼容。

为避免混合实际配置，新正式对照集统一22项全部使用v2，旧两项有效评分/原响应原样单列，绝不与新20混均值或择高。仍同runID/TaskID、同22个既有答案、同模型/Registry/tier/12k；未重新执行受评Task。旧4attempt全部保留：原298752未知占用，加116987已知；新audit从seq5开始，原7美元下执行cap6.701248、prior spent0.116987，准入余6.584261。实际完整新估价6.508232，加原已知及未知保留额6.923971≤7。

paired_semantic_format_recovery仅接受本次已核定原数据/金额，保护父目录及原首失败目录，核父输入与manifest摘要、旧响应/有效评分、所有旧row、新22模板及重算费用。新未知或再次失败停止，不自动循环；本轮不再新增实验配置。格式恢复17项通过、邻接schema/judge/首恢复106项通过，旧原件/费用/评分标准保持。

## 同wire v2的原始评分证据提取

v2原四项有效，第五项返回STOP、同模型/tier及已知57578微美元，五维分值与理由均符合原标准，但根级多overall_assessment字符串，违反原additionalProperties=false。原validate_scores(raw)仍拒绝；原raw、failed事件、费用和raw schema不合规事实全部保留，不重发该已返回请求。

新增独立paired-score-extraction-v1，仅对恰好多这个非评分字符串的响应提取原scores，值/理由逐字不变；其他根键、非字符串metadata、缺维度、非法锚点或空理由继续拒绝。该提取对全部22统一，0/0.5/1不区分，旧四项是恒等映射；五维标准与请求配置都不变。每项记录raw_schema_valid及ignored_root_fields，review/报告保留失败计数；原payload不因此变合规，整体验收也不因此通过。

独立semantic-extracted目录复用五份原响应与四原评分、派生第五份评分；同manifest/模型/schema2/run/Task，原strict目录不修改。九行账本严格核对，已知332277微美元包含全部旧费；原298752未知仍保留，执行cap6.701248、prior spent0.332277。offset9，从seq10仅续原余17；实际余17估价5.038352，加已知与未知预占为5.669381≤原7。恢复动作模型调用0；原件/seed/账单篡改或新pending拒绝，未知实际总额仍null。
