# 模型评测与调优：在 Agent Runtime 上怎么做、结果如何、踩过哪些坑

> 按 `Job Qualifications and Preferred Qualifications Summary.md` 里反复出现的要求整理（对照表在第 10 节）。
> 数据与脚本：`real-model-experiments-2026-10-28/`（报告、`scripts/`、`data/`）；可视化页面：`real-model-experiments-2026-10-28/dashboard.html`
> （由 `scripts/eval_dashboard.py` 生成，收评测报告、网关计量与 OpenTelemetry 的 span）。改动与待办：`docs/plan/eval-observability-and-runtime-fixes.md`。
> 截至 2026-10-28。真模型有随机性，数字都附运行次数与范围。

## 0. 一页结论

- **评测是决策级的，不只看端到端成功率**：Planner 的分诊（Triage）与 Stage 形状、Reviewer 的 Verdict、升级是否到位、上下文压缩后能否答对，各有门槛；
  再用真 Task 做端到端，并且不信 Agent 的自述，逐个工作区重跑测试核对。
- **Planner 过线，Reviewer 没过**：默认路由（strong 档 = claude-sonnet-5-5）干净的 3 次开发集，Triage 都是 100%（门槛 90%）；Reviewer 37.5%–50%（门槛 85%）。
- **最大的一个发现是控制变量**：Reviewer 从 10-22 的 75% “退步”到 50%，真正的原因是 Model Gateway 上线后按单价选档，把 strong 档从 opus 换成了 sonnet。
  只换 strong 档的对照里 opus 81%（每次运行贵约 30%）、gemini-3.5-flash 62.5%（便宜 7 倍）、gpt-5.5 50%。
- **离线评测与线上 trace 指向同一个病根**：评测里排第一的失败原因“缺 deliver、缺 tool_loop”，在真 Task 的 trace 里就是 Planner 把读文件、跑测试拆成了
  没有工具的 Atomic 模型节点（6/11 个 Task、17% 的花费）。已改成 Stage 校验拒绝（第 2 节）。
- **能由结构判定的写成代码底线，并用对抗模型测**：附录 B.6、B.7 里四条（同一错误在重试后再现、扇出后合并失败、质量包装没过 judge、Progress Check 自相矛盾）
  现在由代码判定，模型怎么说都改不了；对抗模型（永远原地重试）下这些样本照样判对。
- **花费 $21.35 跑完四组实验**（1267 次模型调用），另 $4 左右做修复后的验证（第 2 节）。

## 1. 指标体系：看什么、门槛与口径

| 维度 | 指标 | 口径 | 门槛 / 用途 |
| --- | --- | --- | --- |
| 规划（S-09） | Triage 准确率 | 分诊级别（fast、light、full、replan）与人确认的标注一致的比例 | ≥ 90%（中位数） |
| | Stage schema 通过率、规则违例、keep_nodes 违例 | 规划结果合 schema；不越权；replan 不重做已验证的 Node | 100%、0、0 |
| | 形状匹配率 | 第一个 Stage 的形状（deliver、Goal Node 的 mode、质量包装、单一写者……）与标注一致 | 诊断用 |
| 裁决（S-10） | Reviewer Verdict 准确率 | 专门的 Reviewer 样本（检查点）上判对的比例 | ≥ 85% |
| | 该升级却原地重试 | 低于标注的最低升级步骤（附录 B.8）的次数 | 0 |
| | Round Verdict 准确率 | Task 样本里每个 Round 的判定 | 诊断用 |
| 上下文（S-11） | Compaction Probe 通过率、答案留存率、重复操作率 | 长任务压缩后问关键问题能否答对；答案是否还在上下文里；有没有重复已做的操作 | 完整阶梯不劣于只遮蔽 |
| 真任务 | 成功率（独立核对）、每次成功成本、Round 数、浪费的 Round | 测试自己重跑；按网关计量算钱 | 端到端 |
| 可靠性 | 结构化输出失败率、回退率、供应商不可达 | `bad_structured_output`；`models_exhausted` 与超时（Planner、Reviewer 回退到规则） | 回退 > 5% 的运行作废 |
| 成本 | 每例成本、每次成功成本、规划成本占比 | 网关的 `cost_usd_micros` | 规划成本 ≤ Task 预计成本的 5% |
| 延迟 | 按模型、按 role 的 chat p50 / p95 | 网关打的 `chat` span 的时长 | 诊断用 |
| 数据闭环 | 任务族覆盖率；Learning 的门槛 | trace 里有任务族的 Task 比例；Wilson 下界与成本上界（附录 J.3） | 非劣效：δ = 2pt、ε = 5%、每组 ≥ 100 |

统计口径：真模型的分数每次都不同，**看多次运行的中位数与范围**；差异小于一次运行内的摆动（Task 样本 ±2 条）不下结论；
回退成规则超过 5% 的运行不代表模型，作废重跑；比较两次运行之前先核对模型、提示、评测集有没有变。

## 2. 与项目结合的调优过程

| 日期 | 做了什么 | 指标怎么变 | 学到什么 |
| --- | --- | --- | --- |
| 10-20 | S-09 Planner 与 Triage、S-10 Reviewer：先写验收测试再实现 | 离线（桩模型）全过 | 门槛与规则先于模型写下来 |
| 10-21 | 评测“站稳”（T-01–T-04）：样本必触发摘要、重复跑与按中位数判门槛、评测用的临时网关按标价计费 | — | 单次分数不可信，要重复跑 |
| 10-22 | 第一次真模型（直连供应商，strong = opus）：S-09 开发集 3 次 Triage 84.6%–92.3%（中位 88.5%，差 1.5pt）；S-10 开发集 Reviewer 37.5%–75%，留出集 37.5%–87.5% | 波动很大 | 供应商额度用完后每次 403，评测照样“跑完”：加 `CountingGateway` 把失败的模型调用记进报告 |
| 10-22 | Model Gateway 上线（T-05、T-06）：同档按单价选最便宜的 | strong 档悄悄换成 sonnet | 路由策略本身是质量决策，当时没人察觉 |
| 10-24 | R-01 修复；Reviewer 避开生成者那一家（`avoid_vendor`） | 留出集 Reviewer 3 次都是 75% | 判断与生成不同家，减少自评宽容 |
| 10-28 | 四组实验（E1–E4，$21.35）：基线 3 次、只换 strong 档、bad case 归因、真 Task 的 trace 闭环 | Triage 100%；Reviewer 37.5%–50%；opus 81%；真 Task 7/8 | 第 0 节的发现 |
| 10-28 | 修复（提交 d059029）：Planner 拒绝无工具的 fetch/act、桩模板不给真 Planner、预算压到剩余、任务族兜底、用户的语言；Reviewer 四条代码底线 | 见下面的验证 | — |
| 10-28 | 修复后验证（E5，开发集 2 次，默认路由） | 待补 | — |

## 3. 专项能力测试

### 3.1 规划（Plan-and-Execute 的“Plan”）

- 分诊：干净的运行 Triage 96%–100%（sonnet、opus、gemini-3.5-flash、gpt-5.5 都过 90%）。10-22 的 88.5% 之后，规划提示只列存在的 Tier（10-25）等改动让 Triage 稳定下来。
- 形状：只有 35%–50% 的 Task 样本第一个 Stage 的形状与标注一致；各模型、各次运行排第一的失败原因都是“期望有 deliver 节点，实际没有”，其次是缺 Goal Node 的 tool_loop、template。
- 真 Task 里看得更清楚（E4 的 trace）：第 1 轮常是 `run_tests`、`read_test_file`、`fix_pricing` 这样的 Atomic 链，模板是 `answer@v1`，甚至是测试专用的 `stub_answer@v1`；
  这些节点没有工具，照样 succeeded，内容是“工具不可用”。11 个 Task 里 6 个有这样的 Round，共 7 个 Round、$0.67（17%），1 个 Task 因此失败。
- 改法（d059029）：Stage 校验拒绝 executor 为 model 的 fetch、act 节点（人确认的标注里 56 个 fetch、25 个 act 全是 program 节点，规则与标注一致）；
  真 Planner 的 Node Catalog 不列 `stub_` 模板；提示写明只有 tool_loop 有工具。

### 3.2 裁决与失败恢复（Reviewer、升级顺序、Human-in-the-loop）

- 准确率：sonnet 37.5%–50%（干净的 3 次），opus 81.3%，gemini-3.5-flash 62.5%，gpt-5.5 50%；留出集 sonnet 75%。
- 逐条看（`scripts/e3_reviewer.py`）：sonnet 与 gpt-5.5 偏宽松（该修的判 done、该 replan 或 rollback 的原地重试），opus 偏谨慎（不假完成，但偏向 replan，
  Task 样本里 6 次把该 done 的判成 repair）。
- 能由结构判定的写成代码底线（附录 B.6、B.7，d059029）：本轮已在 Node 里重试、又是上一轮的同一错误 → replan；扇出后合并失败 → replan，不保留冲突的分支；
  质量包装用满轮数没过 judge 的产出（Stage Runner 记 succeeded、`quality.passed = false`）→ 不能 continue 或 done，换方法；
  Progress Check 自己答了没满足验收却判 done → 不收。
- 对抗测试：脚本模型永远原地重试时，`rs_revise_same_error`、`rs_fanout_conflict` 与原有的四条（同一 Node 第 2 次失败、两次 replan 没进展、预算用尽、写被拒）照样判对。
- 判不出来的留给模型并记下：`rs_rollback_corrupted_accounts` 的证据只在 judge 的错误文字里（要一个结构化的 `regressions` 字段，设计稿第 7 节）。
- 真 Task 里的升级给人：选项限定三种（原样继续、带指导重规划、停止并收尾），回复后按选项收尾。

### 3.3 结构化输出的可靠性

| 模型 | 调用 | `bad_structured_output` | 失败率 |
| --- | --- | --- | --- |
| claude-sonnet-5-5 | 634 | 39 | 6.2% |
| gpt-5.5 | 166 | 9 | 5.4% |
| gemini-3.5-flash-lite | 281 | 4 | 1.4% |
| gemini-3.5-flash | 82 | 1 | 1.2% |
| claude-opus-5-5 | 86 | 0 | 0% |

不合 schema 的输出多半重问一次就好（Planner 问两次、Reviewer 两次都不合格才回退），所以它不算回退，但花钱、加延迟；同档切换（sonnet → gpt-5.5 → opus）在计量里看得到。

### 3.4 上下文工程（Compaction Ladder）

完整阶梯（遮蔽 → 增量摘要 → reset，带 Compaction Probe）对只遮蔽：开发集 78.3% 与 91.3% 对 69.6%，留出集 83.3% 对 33.3%，门槛都过；
重复操作率两种都是 20%（一条写作样本），是摘要之外的问题。评测本身的坑：回答步骤遇到一条不合法的 JSON 就整轮崩溃（开发集 4 次里 2 次）。

### 3.5 工具使用与真任务（E4）

- 11 个真 Task（8 个修 CI、3 个故意做不到：测试互相矛盾、又不许改测试），$4.10、237 次计费调用、Round 数中位数 3。
- 独立核对：正常的 8 个里 7 个真修好了，测试文件没动；1 个被无工具的 Atomic 链拖到升级后终止（驱动脚本对所有提问都回“停止”，也有份）。
- 做不到的 3 个：1 个正确识别矛盾后终止；1 个判 done 但交付里写清了矛盾（结局可以接受，但它说“没有工具”，其实是规划成了没工具的节点）；
  1 个**假完成**：判 done，交付里自己写着“修复未生效”。
- 工具调用（`execute_tool` span）：repo_read 28 次、shell_run 26 次（p50 128 ms）、repo_edit 9 次；`context_read` 16 次全失败（要查）。

### 3.6 失败归因（Bad case 自动归因）

- Learning 的 MAST 三类归因（small@low，gemini-3.5-flash-lite）：2 次调用 $0.00025，对 1 错 1。错的那条真正的原因是协作失配（无工具的节点），
  但归因的输入按设计只有结局、各 Round 的 verdict 与 Stage 形状，看不到节点返回了“工具不可用”。
- 人工归因（E3）：把离线评测的失败原因、开发集上逐条样本的对错（三次都错 vs 时对时错）、真 Task 的 Stage 形状放在一起看，比单看一个分数可靠。

### 3.7 成本

- 每例成本：sonnet $0.06、opus $0.086、gpt-5.5 $0.042、gemini-3.5-flash $0.008（只换 strong 档）。
- 真 Task 每个约 $0.36；其中 17% 花在无工具的 Atomic 链上。
- 预算：线上日志里 Planner 给一个 Node 0.9 美元，Task 只剩 0.8242，整个 Node 被拒、白花一轮；Planner 原来根本不知道剩多少（已修）。

### 3.8 延迟（OpenTelemetry span）

按请求的模型（只取网关打的 `chat` span，E4）：sonnet p50 19.5 s、p95 91 s；gpt-5.5 p50 17 s、p95 40 s；gemini-3.5-flash-lite p50 2.8 s；deepseek-v4-flash p50 6.5 s。
带思考的强档一次规划 20–90 秒；决策 activity 的超时是 5 分钟、最多 5 次，供应商出问题时“规划下一轮”可以静默很久（线上日志，设计稿第 4 节）。

### 3.9 鲁棒性

- 供应商故障：08:23–08:54 与 09:38–09:41 两段大面积 `unreachable`（所有模型），E4 里 87 次调用把同档的候选都试完了（`models_exhausted`，规划的中位数 91 秒），
  **这些失败不在计量记录里**（没计费），只有 span 看得到。
- 同档切换生效（sonnet → gpt-5.5 → opus）；判断与生成不同家生效（25 次 Reviewer 调用带 `avoid_vendor=anthropic`、落到 gpt-5.5）。

## 4. 模型对比与择优

| strong 档 | Triage | Reviewer | 原地重试 | Task 样本通过 | 每次运行 | 结构化失败率 | 调性 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| claude-sonnet-5-5（默认，干净 3 次） | 100% | 37.5–50% | 3–4 | 11–13/26 | $1.81–2.01 | 6.2% | 宽松：假完成、不肯升级 |
| claude-opus-5-5 | 96.2% | **81.3%** | 1 | 6/26 | $2.53 | 0% | 谨慎：不假完成，偏向 replan |
| gemini-3.5-flash（mid 顶替） | 96.2% | 62.5% | 4 | 10/26 | **$0.26** | 1.2% | 便宜稳定，但不肯升级 |
| gpt-5.5（干净重跑） | 100% | 50% | 4 | 7/26 | $1.30 | 5.4% | 像 sonnet，Round 判定最低（77%） |

**按 role 定档**：Planner 是生成，Reviewer 是判断，要的能力不同。按单价选最便宜的 strong 档，对 Planner 没损失，对 Reviewer 损失 31 个百分点。
建议 `plan` 用便宜的强档（或 gemini-3.5-flash，省 7 倍），`judge` 钉到 opus（`pinned_model_profile` 已支持），每次运行多花约 $0.7；
这是设计书附录 H.5“Model Profile 的内部分数”要做的事。要人定。

## 5. 主流评测方案综合（与本项目的对照）

| 方案 | 测什么 | 判定 | 本项目的对应 | 借鉴 |
| --- | --- | --- | --- | --- |
| SWE-bench（Verified、Pro、Multilingual） | 真实仓库的 issue 修复 | 跑仓库自己的测试 | E4 的修 CI：测试当 judge，测试自己重跑核对 | 用题目自带的验收；防“改测试作弊”（我们核对测试文件的哈希） |
| Terminal-Bench | 终端里的多步任务 | 容器里的检查脚本 | 沙箱里的 shell_run | 环境可重放；按任务类型统计 |
| τ-bench / τ²-bench | 带策略约束的工具使用与对话 | 终态比对，pass^k（k 次都对） | 升级给人、effect 上限、预算 | **pass^k**：同一题多跑几次全对才算，比 pass@k 更看稳定性 |
| BFCL | 函数调用是否正确 | AST 比对与执行 | 结构化输出、工具参数（`invalid_arguments`） | 按模型统计结构化与参数错误率 |
| GAIA、BrowseComp | 带工具的检索与研究 | 精确答案 | 评测集的 research 类 | 答案可核对的题目设计 |
| WebArena、OSWorld | 网页与桌面操作 | 环境状态检查 | 没覆盖 | — |
| Aider Polyglot、LiveCodeBench | 代码编辑与生成 | 测试；按时间切分防污染 | 代码修复样本 | **按发布时间切分**防训练集污染 |
| LMArena | 人类偏好 | 两两比较的 Elo，带风格控制 | 没有 | judge 的位置偏差与长度偏差（附录 B.7 的“judge 总选第一个”） |
| Artificial Analysis Index | 多个评测的综合 | 独立重跑 | — | **独立重跑**往往比厂商自报低（见第 6 节） |
| 评测框架（Inspect AI、OpenAI Evals、promptfoo、DeepEval、Ragas、Langfuse/LangSmith 的评测） | 编排、打分、追踪 | 规则、LLM-as-judge、人工 | `agentruntime_learning.evals`（自写）、Langfuse 收语义 span | trace 与评测打通；数据集版本化 |

统计方法：pass@k 与 pass^k；Wilson、bootstrap 置信区间；非劣效检验（本项目附录 J.3：δ = 2pt、ε = 5%、每组 ≥ 100）；
LLM-as-judge 要 rubric、交换顺序、独立于生成者；榜单饱和（Vals 已不再在新模型上跑 SWE-bench Verified）。

本项目的不同之处：**决策级**的评测（Triage、Verdict、升级）而不只是端到端成功率；**代码底线 + 对抗模型**的测试；运行的**有效性判定**（回退超过 5% 作废）；
从线上 trace 做**观测式评测**（Learning Pipeline，按任务族、非劣效门槛）。

## 6. 主流榜单的跑分（截至 2026-10-28，附来源）

| 模型 | Terminal-Bench | SWE-bench | 综合指数 | 来源与说明 |
| --- | --- | --- | --- | --- |
| claude-opus-5-5 | 4.0：66.4%（xhigh，Anthropic）；独立 60%（Artificial Analysis） | Pro 89.9%、Multilingual 93.9%（Anthropic 系统卡，max） | AA Intelligence Index v5：58（表格里第 1；同一页的导语另说 Gemini 4 Carbon 第 1，以官网为准） | [Anthropic](https://www.anthropic.com/claude-sonnet-5-5)、[mixed-news](https://mixed-news.com/en/claude-sonnet-5-5-terminal-bench-70-6-percent-half-opus-price/)、[AA v5](https://artificialanalysis.ai/articles/artificial-analysis-intelligence-index-v5) |
| claude-sonnet-5-5 | 4.0：70.6%（Anthropic）；独立 64%（AA） | Pro 81.3%、Multilingual 90.3% | 聚合站约 56.0（v5 之前） | 同上；[benchlm](https://benchlm.ai/compare/claude-opus-5-5-vs-claude-sonnet-5-5) |
| gpt-5.5 | 2.0：82.7%（OpenAI） | Pro 58.6%（OpenAI） | — | [OpenAI](https://openai.com/index/introducing-gpt-5-5/)；τ²-bench Telecom 98.0% |
| gemini-3.5-flash | — | Verified 78.8%（Vals） | — | [Vals](https://www.vals.ai/benchmarks/swebench) |
| gemini-3.5-flash-lite | — | Pro 54.2%（Google 模型卡）；Verified 75.0%（Vals） | — | [DeepMind 模型卡](https://deepmind.google/models/model-cards/gemini-3-5-flash-lite/)、[Vals](https://www.vals.ai/models/google_gemini-3.5-flash-lite) |
| deepseek-v4-flash | 2.0：56.9（Max，模型卡） | Verified 79.0（Max，模型卡） | AA：34（0731 Max） | [Hugging Face](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash)、[AA](https://artificialanalysis.ai/models/deepseek-v4-flash) |

LMArena 文本榜（10 月）：Gemini 4 Argon 约 1532 居首，Claude Opus 5.5 紧随（[metatext](https://metatext.io/benchmarks/lmarena-elo)、[sophon](https://sophon.at/leaderboards/lmarena-text)）。

怎么读：
- **版本不同不能比**：Terminal-Bench 2.0 与 4.0 不是一个题集；SWE-bench Pro、Verified、Multilingual 也不是。
- **厂商自报偏高**：Sonnet 5.5 的 Terminal-Bench 4.0 自报 70.6%、独立测 64%；Opus 66.4% 对 60%。
- **effort 设置不同**：Opus 的成绩是 xhigh 或 max；我们用的是 strong@medium（思考预算 8192）。
- **榜单不能替代领域评测**：通用榜单上 Sonnet 5.5 与 Opus 5.5 在 Terminal-Bench 上几乎持平，但在本项目的 Reviewer 判定上差 31 个百分点。
  榜单用来选候选，最后靠自己的评测集定。

## 7. 踩坑记录

| # | 现象 | 原因 | 处理 / 教训 |
| --- | --- | --- | --- |
| 1 | 评测“跑完”了，分数却是回退的 | 供应商额度用完，每次 403；Planner、Reviewer 回退到规则 | `CountingGateway` 记失败的模型调用；回退超过 5% 的运行作废 |
| 2 | Reviewer 从 75% “退步”到 50% | 网关按单价选档，strong 从 opus 换成 sonnet | 比较前核对模型；报告记每个 role 的 `model_profile_id`（待做） |
| 3 | 两次运行的分数明显偏低 | 08:23–08:54 供应商大面积 `unreachable` | 把网关告警按分钟对齐运行时间，作废重跑 |
| 4 | 计量记录里看不到失败 | 没计费的上游失败不写计量；E4 里 87 次 `models_exhausted` 只在 span 里 | 失败也要计数：指标走 OTel，计量只管钱 |
| 5 | 延迟图把失败算到了 opus 头上 | 网关的 `gen_ai.response.model` 是同档切换后最后试的那个 | 按请求的模型分组 |
| 6 | 模型调用数翻倍 | Worker 的 model 节点与网关各打一个 `chat` span | 只取网关的 |
| 7 | 上下文评测整轮崩溃 | 一条回答不是合法 JSON 就抛异常 | 单条记失败、继续（待做） |
| 8 | 判 done 但测试没过 | Stage Runner 把用满轮数没过 judge 的 revise 记成 succeeded | Reviewer 的代码底线读 `quality.passed`（已修） |
| 9 | 真 Planner 用了测试专用模板 | 开发 Registry 的种子把 `stub_answer` 列给了它 | 真 Planner 的 Catalog 不列 `stub_`（已修） |
| 10 | Node 被整个拒绝、白花一轮 | Planner 不知道 Task 剩多少预算（字段一直没人填） | 由 Task State 算出剩余，压到剩余（已修） |
| 11 | Learning 看不到失败的 Task | 任务族为空（分诊判断不了、规划模型也没写），且同类请求有 4 个名字 | 兜底的 `general.unclassified`（已修）；受控词表（设计稿第 6 节） |
| 12 | 线上 Task 超过 20 分钟还在跑 | 租户策略的时间与工具调用预算传进了工作流，没有任何地方执行 | 设计稿第 3 节（核心链路，要版本分支） |
| 13 | 新租户的工作副本是空的 | 待诊断：新租户走 Kata（microVM），上线时只验过 gVisor | 设计稿第 2 节；诊断要读生产数据，按规矩由人批准 |
| 14 | 英文界面出现中文 | 提示是中文、代码生成的说明是中文 | 提示要求用用户的语言（已修）；代码说明按 code 翻译（设计稿第 5 节） |
| 15 | KEDA 的 Job 永远不起来（设计时发现） | kafka 触发器默认 `offsetResetPolicy: latest`，没提交过时延迟只算 1 | 改 `earliest`，并用激活阈值（提交 9ab2f7b） |
| 16 | 真模型评测很慢 | 带思考的强档一次 20–90 秒；评测按样本串行 | 多个进程并行，但会放大供应商的问题；看清楚是否在故障窗口里 |
| 17 | 测试里 Kafka 建主题失败（`_DESTROY`） | AdminClient 在拿到结果前被回收 | 保留引用直到结果返回 |
| 18 | Windows 上 pytest 大面积报错 | 临时目录路径超过 260 字符；默认临时目录被沙箱拒绝 | `--basetemp` 用短路径 |
| 19 | 结局被驱动脚本污染 | 自动回复对所有升级都选了“停止” | 驱动脚本只对预期的提问回复，其余交给人 |

## 8. 经验与方法论

1. **先定口径再跑**：门槛、有效性规则、样本与切分写在前面；提示只在开发集上调，留出集只看汇总。
2. **一次只换一个变量**：只换 strong 档的模型清单，其余档与提示不动。
3. **多次运行看范围**：差异小于运行内的摆动不下结论；单次结论不外推。
4. **不信自述**：Agent 说修好了，就重跑测试、核对测试文件没被改。
5. **离线评测与线上 trace 互证**：评测里的形状失败，在真 Task 的 trace 里找到了原因（无工具的 Atomic 链）。
6. **规则先于模型**：能从结构判定的写成代码底线，用对抗模型测；判不出来的才交给模型，并记下缺什么结构化信号。
7. **全体模型都错的样本先复核标注**，不拿它调提示。
8. **失败也要可观测**：计量只管钱，质量与可靠性看 OTel 的 span 与指标。
9. **按 role 定档、看成本质量前沿**，不按单价一刀切。
10. **榜单只作先验**，领域评测定胜负。

## 9. 复现与页面

- 实验步骤、脚本与数据：`real-model-experiments-2026-10-28/README.md` 第 7 节。
- 页面：`uv run --no-sync python scripts/eval_dashboard.py [--reports …] [--metering 名字=文件 …] [--spans 文件 …] [--out …]`；
  快照 `real-model-experiments-2026-10-28/dashboard.html`（数据嵌在页面里，离线可看）。
- 与 OpenTelemetry 的结合分三期（设计稿第 9 节）：静态页面（已做）→ 在现有 Tempo 上用 TraceQL 指标画 Grafana 面板 → 选型指标后端（要人定）。

## 10. 岗位要求对照

| 岗位里的要求（摘） | 证据 |
| --- | --- |
| 实验设计、控制变量、识别结果波动、环境异常与评测方法偏差；任务成功判定、执行成本、可复现（Code Agent 算法工程师） | 第 1、2 节；第 7 节第 1–7 条 |
| Agent Eval、LLM-as-a-Judge、执行轨迹分析、Bad Case 自动归因（AI Agent 开发工程师） | 第 3.2、3.6 节；第 5 节 |
| 区分不同层级模型的适用场景、模型自动择优、锐评各模型的调性 | 第 4 节 |
| Planning、失败恢复、长任务、Human-in-the-loop、上下文工程（Agent Harness 开发/优化） | 第 3.1–3.4 节 |
| 熟悉 SWE-bench、Terminal-Bench 等 Benchmark，有评测适配与复现经验 | 第 5、6 节；设计稿第 10 节 |
| 可观测：Log、metrics、trace 的采集链路（弹性计算平台方向） | 第 3.8、3.9 节；第 7 节第 4–6 条；页面 |
| 能从用户反馈、trace 与评测结果中发现问题、验证改进（Coding Agent 研发） | 第 2 节；线上日志的处理（设计稿第 1 节） |
| 能判断 AI 产出的质量 | 第 3.5 节：逐个工作区核对；第 7 节第 19 条 |
