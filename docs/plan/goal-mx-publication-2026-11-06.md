# MX-PUB Registry 发布准备（2026-11-06）

本线只准备，尚未调用 Publish 或 Promote。部署负责人在固定发布窗口执行；不使用 registry-seed 绕过评测证据。

最终材料：父目录 .goal-evidence/mx-publication-audited-20261106/bundle.json。每个候选目录包含 publish.json、baseline.json、report.json、promote-evaluation.json 和 promote-request-template.json。版本模板的占位值必须换成真实 Publish 返回或按相同正文找到的版本，不能预造版本号。逐候选路径和统计见 evidence/mx-publication-index-20261106.json。

原始5742条结果未改。核验BFCL八份题目/标准答案文件SHA256，从保存的模型输出重新做AST评分；在新目录复制全部观测，仅给BFCL行追加真实model_usage匹配的cost_known和reservation证据，再运行原冻结scorer。质量统计保持原值，不删除困难题、候选或失败记录。

原质量有效12项；加入费用完整性证据后最终可发布10项，候选总数仍为13，覆盖10/13。Sonnet的multiple_161有未结算预占0.051644美元，Qwen的simple_python_297有0.030988美元，两者被现有unknown_cost门槛拒绝。Fable仍因20题淘汰赛不领先/不足40题被拒。预占不是实际费用；没有退款或补写历史Registry版本。

十项专用Promote证据已按核心提供的registry_capability_evaluation.schema.json及defs校验：kind=capability_scores、policy=capmatrix-publication-v2，measurement与新Profile能力分一致，记录样本、重复完整性、原始分、置信区间、污染率、10/13覆盖、证据SHA和cost_known=true。完整报告保存原始/派生记录、数据集、评分代码和账本摘要。只有capability_scores变化，当前模型、价格和其他正文不变。

这是能力测量发布，不是模型更换或A/B非劣效实验，不构造baseline/candidate成本对照，不填0成本或waiver。历史调用未记录Registry修订，继续保持未知。当前Registry导出只用于发布前默认版本校验和回退点，不倒填旧调用来源。

scripts/mx_registry_publication.py的prepare子命令生成全套材料。publish-shadows和promote默认只读检查；负责人加--execute才分别发布影子版本或按专用证据晋升，令牌从AR_REGISTRY_PUBLISH_TOKEN_FILE读取，只放Authorization头，不输出。promote从真实Publish回执取版本，验证影子正文、当前默认、完整报告与bundle摘要；保存pending及previous_default_version，回执丢失后先读实际default确认，不盲目重发，服务端baseline_version另有事务CAS保护。

每次Publish前核对当前默认版本和完整正文，扫描后续版本，正文相同则复用。请求前持久化pending，回执丢失后只查询确认，不盲目重复发布。默认漂移、材料hash改变、未确认pending或未完成费用审计时停止。baseline.json保留旧默认版本、真实Registry摘要及完整正文；回退由部署负责人按项目流程执行。

旧.goal-evidence/mx-publication-20261106是12项质量草案，保留供审查；最终脚本拒绝其缺少最终费用门槛的bundle，不得执行旧草案。

验证：六条离线回归通过，覆盖只读预检、默认漂移、丢回执恢复、pending禁止重试、材料篡改、无关shadow保护和旧草案拒绝。生成流程已核验十项schema及原输出评分。新增模型费用0；线上发布、晋级、再导出及Gateway加载仍待主集成执行。

A/B最终分工：B统一部署专用Promote接口后通知，A作为唯一发布执行者按本bundle的10项Publish→Promote；Memory实验结束前B不更新Gateway冻结的Profile快照。源码model-profiles.json与registry-seed.json已同步最终10项新证据来源摘要；仅撤回本Goal早先准备的Sonnet/Qwen两条未结算能力分，其他已有字段不动。撤回的精确旧值与最终集合在bundle目录source-reconciliation.json；不是删除原始评测结果。脚本晋升丢回执/默认漂移/证据篡改回归和现有发布CLI合计24项通过。

## 实际 Registry 发布（2026-11-07）

A已完成10个Publish并逐项GET核对影子正文；第一次Opus晋升被服务端明确400拒绝，原默认仍v2。只读数据库比对确认原文数字4.0/20.0经protobuf转为4/20，数值完全相等，但旧UseNumber字符串比较误判。B以精确有理数比较修复1521aacc（不改历史正文/摘要），SQLite/Postgres回归通过并部署task-service:goal-1521aacc。A保留拒绝回执，重新读取默认及影子对账后恢复同一晋升，没有重复Publish。

随后10项Promote全部成功，重读确认default及完整正文：

| 模型Profile（a6api前缀） | 原默认 | 新默认 |
| --- | --- | --- |
| claude-opus-5-5@2026-10 | v2 | v3 |
| deepseek-v4-pro@2026-11 | v1 | v2 |
| gemini-4-argon@2026-11 | v1 | v2 |
| glm-5.3@2026-11 | v2 | v3 |
| gpt-6-astra@2026-11 | v1 | v2 |
| gpt-6.1-sol@2026-11 | v1 | v2 |
| grok-4.7@2026-11 | v1 | v2 |
| kimi-k3.1-pro@2026-11 | v1 | v2 |
| longcat-2.0@2026-11 | v1 | v2 |
| mimo-v2.6-pro@2026-11 | v2 | v3 |

证据：bundle目录publication-state.json、promotion-state.json、live-promote-after-numeric-fix.log；初次拒绝与精确数字诊断原件保留。私有临时发布凭据文件已删除。发布本身未调用模型，费用0。Gateway最终导出/加载由B统一完成后另记；此刻不能将Registry晋升称为Gateway已生效。plan/route/judge/transform以及2个费用未知act项的未发布原因仍全部保留。
