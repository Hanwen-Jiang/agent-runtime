# 真模型实验（2026-10-28）：对照岗位要求看 Agent Runtime

> 目的：按 `../Job Qualifications and Preferred Qualifications Summary.md` 里反复出现的要求，用真模型跑一组有明确问题的实验，
> 拿出能核对的证据，而不是只看测试“过没过”。四组实验：E1 基线与波动、E2 模型对比、E3 Bad case 归因、E4 trace 闭环。
> 环境：本机整套（`scripts/dev_stack.py`），Model Gateway 经 a6api（OpenAI 兼容）接 9 个模型（`deploy/dev/model-profiles.json`），
> 评测集 `services/py-learning/evalsets/task-loop`（标注经人确认）。报告在 `services/py-learning/.eval-reports/exp-2026-10-28/`（不提交），
> 本目录的 `scripts/` 是实验用的脚本，`data/` 是 E4 的原始结果与汇总。总花费见第 7 节。

## 0. 结论

1. **Planner 过线，Reviewer 没过**。默认路由（strong 档 = claude-sonnet-5-5）开发集干净的 3 次：Triage 准确率都是 100%（S-09 门槛 90%，过）；
   Reviewer 的 Verdict 准确率 50%、50%、37.5%（门槛 85%），“该升级却原地重试”3、3、4 次（门槛 0）。留出集：Triage 100%，Verdict 75%，原地重试 0。
2. **看起来的“退步”是路由换了模型**：10-22 的 Reviewer 是 75%，今天 50%。10-22 那次在 Model Gateway 上线之前，直连供应商时 strong 档默认是
   claude-opus-5-5；网关在同一档里按单价选最便宜的，选中了 sonnet。“退步”的 3 条样本正是今天 opus 做对的 3 条。opus 把 Reviewer 拉到 81%，
   每次运行多花约 30%（$2.53 对 $1.8–2.0）。gpt-5.5 与 sonnet 差不多（50%），倾向也一样。
3. **换小模型守得住一部分**：gemini-3.5-flash 顶替 strong 档，花费是 sonnet 的 1/7（$0.26），Triage 96%，Task 样本通过 10/26（sonnet 干净的三次是
   11–13，差一两条，在单次运行的波动之内）；Reviewer 62.5%，比 sonnet 高，但同样不肯升级。
4. **真 Task 上 7/8 修好了 CI**（我自己在工作区里重跑测试核对，测试文件都没动）。但暴露了三个 Harness 问题：Planner 把读文件、跑测试拆成
   不带工具的 Atomic 模型节点（6/11 个 Task、17% 的花费浪费在这上面，1 个因此失败）；一个做不到的 Task 被判 done，交付里自己写着“修复未生效”；
   11 个 Task 里 6 个没有任务族，其余 5 个用了 4 个名字，Learning 因此看不到失败的 Task。
5. **trace 闭环在真模型下跑通**：Worker 收尾的 span → Collector（与线上同样的脱敏、语义过滤）→ Redpanda → Learning 用 `--topic` 消费 →
   归纳候选 → 门槛（样本不够，发布被拒）→ 提交消费组的位置（延迟归零）。真模型的失败归因 2 次花 $0.00025，对 1 错 1。
6. **评测本身要防的偏差都碰到了**：供应商在 08:23–08:54 大面积 `unreachable`，那段时间的两次运行有 22 和 53 次决策回退成规则，作废重跑；
   上下文评测遇到一条不合法的 JSON 就整轮崩溃（开发集 4 次里 2 次）；有 2 条 Reviewer 样本四个模型全错，应先复核标注。

## 1. 评测方法：控制变量、波动、环境异常、可复现

对应要求：“控制实验变量，识别结果波动、环境异常及评测方法偏差”；“关注任务成功判定、运行稳定性、执行成本及结果可复现性”（Code Agent 算法工程师）；
“做过 A/B 实验或统计显著性分析”（AI Coding 项目实习生）。

**做法**

- 同一评测集、同一提示、同一网关，开发集跑多次看波动；只换 strong 档的模型（给网关换一份只留一个 strong Profile 的清单，其余档不动），变量只有
  Planner 与 Reviewer 用的模型（`scripts/variants.py`）。
- 每次运行的报告记下模型调用失败（`model_call_failure_codes`）：Planner、Reviewer 调用失败时会回退到规则，分数就不再代表模型。网关的告警日志按分钟
  统计 `unreachable`、`timeout`，与运行时间对齐（第 1.2 条）。

**1.1 基线的波动**（sonnet，开发集，26 条 Task 样本 + 16 条 Reviewer 样本）

| 运行 | Triage | 形状匹配 | Reviewer Verdict | Round Verdict | 原地重试 | Task 样本通过 | 花费 | 回退成规则的调用 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| e1 | 92.3% | 35% | 50% | 92.3% | 3 | 9/26 | $1.78 | 22（作废，见 1.2） |
| e1b | 100% | 50% | 50% | 94.9% | 3 | 13/26 | $1.99 | 4 |
| e1c | 100% | 42% | 50% | 94.9% | 3 | 11/26 | $1.81 | 4 |
| e1d | 100% | 42% | 37.5% | 87.2% | 4 | 11/26 | $2.01 | 1 |
| 留出集 | 100% | 50% | 75% | — | 0 | — | $0.92 | 0 |

读法（看干净的 e1b、e1c、e1d）：Triage 三次都是 100%；Reviewer 37.5%–50%、原地重试 3–4 次，离门槛很远，是稳定的差距，不是波动；
Task 样本通过 11–13 条、Round Verdict 87%–95% 在摆动，单次运行差 ±2 条以内的比较没有意义（E2 每个模型只跑了一次，第 2 节的比较按这个尺度读）。
受环境影响的 e1 Triage 掉到 92%，正是回退成规则的那几次决策。

**1.2 环境异常**：网关日志里 `unreachable` 在 08:23–08:54（每分钟 5–27 次，所有模型、包括 small 档都有）与 09:38–09:41 两段集中出现，同时段
E4 的 11 个 Task 正在跑。受影响的 e1 第一次与 gpt-5.5 的第一次运行各有 22、53 次决策回退成规则，**作废**，另跑干净的 e1d 与 gpt-5.5 重跑。
原因没有定位（供应商或本机出网的代理），记为环境问题。

**1.3 评测方法偏差**

- 控制变量没控住的“退步”（结论第 2 条）：比较前后两份报告，先核对模型有没有换。报告的成本里只有档位与金额，没有记模型 id，这次是靠提交时间
  与网关上线时间对出来的。改进：报告里记每个 role 实际用的 `model_profile_id`。
- 上下文评测（S-11）的回答步骤遇到一条不合法的 JSON 就抛异常，整轮没有报告（开发集 4 次里 2 次）。改进：单条样本记失败、继续跑。
- 2 条 Reviewer 样本四个模型全错（`rs_debate_no_convergence`、`rs_judge_bias`，期望 repair，各模型给 replan、done 或 continue），先请人复核标注，
  不拿它们调提示（提示只在开发集上调的规矩之外，再加一条：全体模型一致不同意的样本先复核）。

## 2. 对模型的判断与分层择优

对应要求：“能区分不同层级模型的业务适用场景”（AI Agent 全栈实习生）；“模型自动择优”；“能够锐评各主流大模型的调性”；“对模型行为有品味有判断力”。

**2.1 只换 strong 档**（开发集各一次；Planner、Reviewer 用 strong@medium，其余档不变）

| strong 档 | Triage | Reviewer Verdict | 原地重试 | Task 样本通过 | 每次花费 | 每条样本 | 回退成规则 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| claude-sonnet-5-5（默认，按单价选中；干净的 3 次） | 100% | 37.5–50% | 3–4 | 11–13/26 | $1.81–2.01 | $0.06 | 1–4 |
| claude-opus-5-5 | 96.2% | **81.3%** | 1 | 6/26 | $2.53 | $0.086 | 0 |
| gemini-3.5-flash（mid 档顶替） | 96.2% | 62.5% | 4 | 10/26 | **$0.26** | $0.008 | 1 |
| gpt-5.5（重跑；第一次 53 次回退，作废） | 100% | 50% | 4 | 7/26 | $1.30 | $0.042 | 6 |

**2.2 各家的调性**（Reviewer 样本逐条对比，`scripts/e3_reviewer.py`）

- **sonnet 偏宽松**：该修的判 done（`rs_cosmetic_reflection`、`rs_judge_bias`：judge 的意见只是表面的或有偏，期望 repair，sonnet 给 done）；
  该升级的原地重试（`rs_revise_same_error` 期望 replan、`rs_rollback_corrupted_accounts` 期望 rollback，都给 repair）；该换路的继续（`rs_reroute_batch`
  期望 reroute，给 continue）。E4 里“做不到却判 done”的那个 Task 是同一种倾向。
- **opus 偏谨慎**：上面 sonnet 错的 rollback、reroute、同一错误重复三条都对，从不假完成；但偏向 replan（期望 repair 的给 replan），在 Task 样本的
  Round 上 6 次把应当 done 的判成 repair，所以 Reviewer 样本最好、Task 样本通过反而最少（6/26）。
- **gemini-3.5-flash**：不假完成、reroute 判对，但和 sonnet 一样不肯升级（rollback、replan 都给 repair）；结构化输出很稳（只失败 1 次）。
- **gpt-5.5 像 sonnet**：`rs_judge_bias` 判 done，同一错误重复、账目写坏、并行冲突都原地重试，该升级给人的 `rs_escalate_nonrefundable` 判 continue；
  Task 样本里 Round 级的 Verdict 只有 77%（sonnet 87%–95%），四家里最低。
- **结构化输出**：全天网关记的 `bad_structured_output`：sonnet 39/634 次（6.2%）、gpt-5.5 9/166（5.4%）、gemini-3.5-flash-lite 4/281（1.4%）、
  gemini-3.5-flash 1/82（1.2%）、opus 0/86。同档切换（sonnet 失败后换 gpt-5.5、opus）在 E4 的计量里看得到。

**2.3 择优的结论**：Reviewer 是判断，Planner 是生成，两者对模型的要求不一样。按单价选最便宜的 strong 档，对 Planner 没损失（干净的运行 Triage
都在 96% 以上），对 Reviewer 损失 31 个百分点（opus 81% 对 sonnet 的中位数 50%）。建议按 role 定档：`plan` 留在便宜的 strong（或 gemini-3.5-flash，省 7 倍），`judge` 钉到 opus（`pinned_model_profile`
已经支持），每次运行多花约 $0.7。这是设计书 H.5“Model Profile 的内部分数”要解决的事，现在的单价排序是第一期规则。是否采纳要人定。

## 3. Bad case 归因：从轨迹与评测结果里找原因

对应要求：“Agent Eval、LLM-as-a-Judge、执行轨迹分析、Bad Case 自动归因”（AI Agent 开发工程师）；“能从用户反馈、Agent trace 和评测结果中发现问题、
验证改进”（Coding Agent 研发实习生）。

**3.1 Planner：要用工具的步骤放在了没有工具的节点上**（离线评测与真 Task 指向同一个原因）

- 离线：各模型、各次运行排第一的失败原因都是“期望有 deliver 节点，实际没有”（每次 5–11 条），其次是“缺少 Goal Node mode：tool_loop / template”。
  例：`ci_fix_failing_test` 的标注期望第一个 Stage 是一个可写的 tool_loop 加 deliver，三次都没做到。
- 真 Task（E4 的 trace，`scripts/e4_shapes.py`）：第 1 轮常是 `run_tests`、`read_test_file`、`read_pricing_file`、`fix_pricing` 这样的 Atomic 链，模板是
  `answer@v1`，甚至是测试专用的 `stub_answer@v1`。这些节点没有工具，照样 `succeeded`，内容是“工具不可用”。Reviewer 接着 replan，第 2 轮才换成 tool_loop。
  11 个 Task 里 6 个有这样的 Round，共 7 个 Round、$0.67（占 17%）；`e4-ok-8` 两轮都这样，升级后被终止。
- 原因与改法：Stage 校验只看结构，不看“这一步要不要工具”；Registry 的开发种子把桩模板暴露给了真 Planner。改法：校验拒绝“目标是读写文件或跑命令、
  却是 Atomic 模型节点”的 Stage（或改写成 tool_loop）；开发种子里的桩模板只给桩 Planner；提示里写明只有 Goal Node 的 tool_loop 能用工具。

**3.2 Reviewer：假完成与不肯升级**

- `e4-bad-1`（测试互相矛盾、又不许改测试）被判 done，交付里自己写着“修复未生效，当前状态不通过”。离线的 `rs_cosmetic_reflection`、`rs_judge_bias`
  也是 done。改法：done 之前核对验收条件的证据（有跑测试的 program judge 时以它的结果为准），不是只看 deliver 节点 `succeeded`。
- 原地重试的 3 条（同一错误重复、账目已被写坏、前提被推翻）都能从结构化输入里判出来：附录 B.8 的代码底线（`RULE_DECIDED` 那几条）对它们没覆盖。
  改法：代码底线加“同一错误第二次出现至少 replan”“检测到写坏的状态至少 rollback”。

**3.3 自动归因的边界**：Learning 的失败归因（MAST 三类，small@low）对 E4 的两个失败 Task 都给 `specification`。`e4-bad-3`（需求自相矛盾）对；
`e4-ok-8` 错，真正的原因是 3.1 的协作失配。归因的输入按设计只有结局、各 Round 的 verdict 与 Stage 形状（trace 不带用户数据），看不到节点返回了
“工具不可用”。改法：在 span 上加不含用户数据的节点级信号（例如“模型节点声称缺工具”的计数），归因再用它。

## 4. Harness：规划、失败恢复、上下文工程

对应要求：“对 Agent Harness 或 Agent Runtime 有系统性理解，能够处理长任务执行、状态持久化、并发调度、失败恢复及 Human-in-the-loop”
（Agent Harness 开发/优化工程师）；“熟悉 Agent Loop、Tool Use、Reasoning、Planning、Memory、Subagent……”。

- **规划（S-09）**：干净的运行 Triage 准确率 96%–100%，schema 通过率 100%，规则违例 0，replan 不重做已验证的 Node。Stage 形状只有 35%–50%
  与标注一致（第 3.1 条）。
- **失败恢复（S-10）**：Round 级 Verdict 准确率 87%–95%（sonnet），Reviewer 样本 37.5%–50%（第 3.2 条）。同档切换生效：sonnet 输出不合法时换到 gpt-5.5、opus（E4 计量）。
  **判断与生成不同家**生效：E4 里 25 次 Reviewer 调用带 `avoid_vendor=anthropic`、落到 gpt-5.5（R-02 第 1 条）。
- **Human-in-the-loop**：E4 里 2 个 Task 升级给人（`_review`，选项 continue_as_is、replan_with_guidance、stop_and_wrap_up），回复后按选项收尾为 aborted。
  注意：驱动脚本对任何提问都回复“停止”，`e4-ok-8` 的终止有一部分是这个脚本造成的。
- **上下文工程（S-11）**：完整的压缩阶梯（mask → summarize → reset，带增量摘要与 Compaction Probe）对只遮蔽：

| 集 | full：Probe 通过 | mask_only：Probe 通过 | full：答案留存 | 门槛 |
| --- | --- | --- | --- | --- |
| 开发集 dev2 | 78.3% | 69.6% | 78.3% | 过 |
| 开发集 dev3 | 91.3% | 69.6% | 87.0% | 过 |
| 留出集 | 83.3% | 33.3% | 83.3% | 过 |

  两种做法的“重复操作率”都是 20%（`ctx_writing_handbook_revision` 一条），是摘要之外的问题。开发集另有 2 次在回答步骤遇到不合法的 JSON 崩溃（第 1.3 条）。

## 5. 可观测链路与数据闭环

对应要求：“建设过 Log / metrics / trace 等数据采集与可观测链路”（Agent 弹性计算平台）；“熟悉向量数据库、语义缓存……数据合成或训练数据闭环”；
“Learning Pipeline / 离线评测 → 发布”。

**E4 的链路**（`scripts/e4_collector.py` 从 Chart 的 Collector 配置派生，只留 Learning 那一路；`scripts/e4_tasks.py` 跑 Task）：

1. 本机第二套整套，Worker 设 `OTEL_EXPORTER_OTLP_ENDPOINT` 指向本机的 otelcol-contrib；Collector 的处理器与线上相同（memory_limiter、脱敏、语义过滤、batch），
   kafka 导出器写 Docker 里的 Redpanda（v26.2.4，与集群同版）的 `telemetry.spans`。
2. 11 个真 Task（8 个 CI 修复、3 个做不到的），花费 $4.10、237 次模型调用。11 个 Task 的 `invoke_workflow` 全部到了主题里（197 条记录、479 个 span）。
3. Learning 按集群里 Job 的方式跑（`--topic telemetry.spans --group learning-e4`，真 Registry、真网关）：读快照 `telemetry.spans#0:0-197`，审计记一条
   `learning.read_traces`；归纳出候选 `learned_dev_code_fix`；门槛判“样本不足（候选 1 条、现行 2 条，至少 100 条）”，发布被拒，退出码 0；
   消费组提交到 197，KEDA 看到的延迟是 0。
4. **任务族是闭环的短板**：6/11 个 Task 的 `runtime.task_family` 是空的（fast、light 分级的规划不给任务族），Learning 跳过它们，失败的 Task 全在里面；
   另外 5 个用了 4 个名字（`dev.code_fix`、`coding.fix_bug`、`engineering.bugfix`、`software.debug`）。按任务族归纳 Recipe（附录 J.4）因此攒不起样本。
   改法：任务族一律由服务端给，取自 Registry 里登记的词表（small@low 的 Classify 调用，附录 H.3 已有这个默认档），所有分级都给。

## 6. 发现的问题与改进（按优先级）

| 优先级 | 问题 | 证据 | 改法 | 挂在 |
| --- | --- | --- | --- | --- |
| P0 | 要用工具的步骤放在无工具的 Atomic 节点上，节点照样 succeeded | 3.1：6/11 个 Task、17% 花费、1 个失败；离线首要失败原因 | Stage 校验拒绝；桩模板不给真 Planner；提示写明 | S-09 的后续、T-08 的开发种子 |
| P0 | Reviewer 假完成；该升级却原地重试 | 3.2：`e4-bad-1`；Reviewer 样本 50%、原地重试 3 | done 前核对验收证据；代码底线补两条 | S-10 的后续 |
| P0 | 任务族缺失与不一致，Learning 看不到失败 | 第 5 节第 4 条 | 服务端按词表分类、所有分级都给 | T-16 的后续 |
| P1 | 按单价选档让 Reviewer 掉 31 个百分点 | 第 2 节 | `judge` 钉 opus 或引入 H.5 的内部分数（要人定） | H.5、T-05 |
| P1 | 评测在模型失败多时不自动作废；报告不记模型 id；S-11 遇坏 JSON 崩溃 | 第 1.2、1.3 条 | 报告记每个 role 的 `model_profile_id` 与回退次数，超阈值标无效；单条失败不崩 | S-08 的后续 |
| P2 | 失败归因输入太薄 | 3.3 | span 上加不含用户数据的节点级信号 | T-16 的后续 |
| P2 | 2 条 Reviewer 样本可能标错 | 1.3 | 人复核 | S-08（标注经人确认） |

## 7. 花费与复现

花费（各网关的计量记录，按 `deploy/dev/model-profiles.json` 的标价；`scripts/total.py`）：

| 部分 | 调用次数 | 花费 |
| --- | --- | --- |
| E1（基线开发集 4 次、留出集 1 次）与 S-11（上下文 5 次，含 2 次崩溃） | 644 | $11.90 |
| E2 gpt-5.5（作废的一次与重跑） | 177 | $2.55 |
| E2 opus | 103 | $2.53 |
| E2 gemini-3.5-flash | 106 | $0.27 |
| E4（11 个真 Task 与失败归因） | 237 | $4.10 |
| 合计 | 1267 | **$21.35** |

按模型：sonnet $15.05（634 次）、gpt-5.5 $3.07、opus $2.87、gemini-3.5-flash $0.27、gemini-3.5-flash-lite $0.08（281 次，Triage 路由与摘要）、
deepseek-v4-flash $0.01。作废的两次运行约 $2.9。

复现（都在仓库根目录，先按 CLAUDE.md 设好 `UV_PROJECT_ENVIRONMENT`）：

```powershell
uv run --no-sync python scripts/dev_stack.py up                     # 默认路由的网关（E1）
uv run --no-sync python docs/job-qualifications/real-model-experiments-2026-10-28/scripts/variants.py deploy/dev/model-profiles.json <目录>
# 每份清单起一个网关：AR_MODEL_PROFILES_FILE=<目录>/profiles-<名>.json model-gateway serve（其余环境变量同 dev_stack.py env 的 model-gateway 一节）
$env:AR_EVAL_GATEWAY_FACTORY = "agentruntime_worker.executor.grpc_gateway:from_env"; $env:AR_MODEL_GATEWAY_ADDRESS = "<网关地址>"
uv run --no-sync --package agentruntime-learning python -m agentruntime_learning.evals run --subject agentruntime_learning.evals.worker_subject:worker_subject --split dev --gate s10 --require-confirmed --compare none --reports-dir <目录>
uv run --no-sync --package agentruntime-learning python -m agentruntime_learning.evals context --subject agentruntime_learning.evals.worker_subject:context_subject --split dev --require-confirmed --reports-dir <目录>
```

E4：Redpanda 容器（`redpandadata/redpanda:v26.2.4`，`--mode dev-container`），`scripts/e4_collector.py` 生成 Collector 配置并用 otelcol-contrib 起，
`OTEL_EXPORTER_OTLP_ENDPOINT` 设好后 `dev_stack.py up --root <另一个目录> --tenant-effect t13=write_reversible`，再跑 `scripts/e4_tasks.py`、
`scripts/e4_verify.py`，最后 `python -m agentruntime_learning.pipeline run --topic telemetry.spans --group <组> …`。

**局限**：E2 每个模型只跑了一次开发集，比较要按第 1.1 条的波动读；E4 的 11 个 Task 只有一种任务（CI 修复）；驱动脚本对所有提问都回复“停止”；
真模型有随机性，单次结论不要外推。

## 附：岗位要求与证据对照

| 岗位里的要求（摘） | 这份文档里的证据 |
| --- | --- |
| 实验设计、控制变量、识别波动与环境异常、评测方法偏差 | 第 1 节：多次运行的范围、作废两次受环境影响的运行、识别出“退步”是换了模型、崩溃与标注问题 |
| 区分不同层级模型的适用场景、模型择优、模型调性 | 第 2 节：只换 strong 档的对照、按 role 定档的建议、各家在 Reviewer 样本上的倾向 |
| Bad case 归因、轨迹分析、从 trace 与评测结果发现问题 | 第 3 节：离线失败原因与真 Task 的 Stage 形状对上同一个原因；自动归因的对错与边界 |
| Harness：规划、失败恢复、长任务、Human-in-the-loop、上下文工程 | 第 4 节：S-09、S-10、S-11 的结果；同档切换、判断与生成不同家、升级给人 |
| 可观测链路、数据闭环、Learning | 第 5 节：Collector → Redpanda → Learning → Registry 跑通，以及任务族这个短板 |
| 能处理异常、日志、配置，能判断 AI 产出的质量 | 第 4 节 E4：不信 Task 的自述，逐个工作区重跑测试核对 |
