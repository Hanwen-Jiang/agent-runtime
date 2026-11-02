# 能力矩阵评测（设计稿，2026-11-03 人审通过）

多因素选档（`8d49b3b`，见 [实施记录](model-routing-implementation-2026-11-03.md)）的能力分 `capability_scores` 按
（任务族 × 角色 × Tier）记，现在一条真实数据都没有，全部按 0.5 的先验算。本稿说明这些分数怎么测出来、怎么写进 Profile。
**不改契约**：字段已经在 `model_profile.schema.json` 里；改的只有评测代码、评测集与 Profile 的数据。

## 0. 人定的（2026-11-03）

1. 工具调用两个都接：BFCL 与 τ²-bench（见第 3 节怎么分工）。
2. gpt-6-astra、fable-5.1 很贵，测它们要省（第 7 节）。
3. 评测集优先用权威的公开数据，没有合适的就拿权威数据改编；测的是“系统在各种场景下的表现”，不是只测我们自己的场景。
4. **embedding 模型不进矩阵**：选定当前最新最好的一个，固定不变；换 embedding 就是重建整个向量库（`embedding_version` 等于
   Profile id，本来就是这么设计的），不是选档的事。
5. 有裁判的评测先用 Opus 5.5 当裁判，**全程留记录**，再由 gpt-6-astra 抽查复核（第 5 节）；预算先不设上限，但单条有上限、花费实时记。

## 1. 要产出什么

每个格子（task_family × role × tier）每个候选模型一个分数，写成 Profile 的一条：

```json
{"task_family": "general.unclassified", "role": "plan", "tier": "medium", "score": 0.72,
 "source": "capmatrix:s09+travelplanner@2026-11-10:n=60,r=3", "at": "2026-11-10"}
```

- `score` 在 0～1，是**收缩后的估计**（第 4 节），不是原始通过率。
- `source` 写清评测集、版本、样本数与重复次数，能追溯到报告文件。
- 只写进“同一格里所有候选都在同一批题上测过”的分数：路由比较的是同一格里的模型，跨格的分数不必可比。

**一个要先处理的现实**：路由按任务族**精确匹配**，而运行时大多数 Task 的任务族是 `general.unclassified`（只有命中 Recipe 的才有
具体任务族）。所以第一版把各场景合起来的分数写在 `general.unclassified` 下，马上就能生效；分场景的分数（例如 `coding.patch`）
等 Planner 能可靠地给 Task 打上任务族之后再写（那是 Planner 的分诊改进，另列工作项，不在本稿）。

## 2. 怎么把一个角色钉在一个模型上

**组件级**（只调 Worker 的 Planner、Reviewer、压缩等代码，不经 Temporal；`evals.worker_subject` 已经这样做）：

- 加一层 `PinningGateway`，包在评测用的 Model Gateway 外面：调用的 `call_context.role` 是被测角色时，填上
  `pinned_model_profile = 候选模型`；其他角色不动，照常选档。这个字段在 `model_gateway.proto` 里本来就有，网关也已支持
  （pin 排第一、availability 为 false 时不能绕过）。
- 现有的 `CountingGateway` 记下每次调用实际用的 Profile。**被测角色的调用只要有一次没落在钉的模型上**（例如钉的模型出错、网关
  切到同档别的模型），这一条用例记为“被污染”，不计分、单独列出；污染率超过 5% 整次运行无效（同现在回退率的规则）。

**端到端**（经 Task API 跑真系统：SWE-bench、Terminal-Bench、τ²-bench）：Task API 没有按角色钉模型的口子。做法是为每个候选建一个
评测租户，用租户的模型白名单把**一个模型档**限定成只有候选模型、其他档固定为基准模型。这只能做到“按档”钉，不能把同属强档的
规划与验收分开；所以端到端的分数只写“这个档的总体表现”，按角色拆开的分数以组件级为准。

## 3. 每个角色用什么评测

| 角色（附录 A） | 默认 Tier | 评测 | 来源 | 判分 |
| --- | --- | --- | --- | --- |
| plan（Planner、replan） | strong@medium | S-09 现有 40 条 + 改编 | 改编 TravelPlanner、NATURAL PLAN 的任务，与 SWE-bench Verified、Terminal-Bench、τ²-bench 的任务描述，按我们的分诊与 Stage 形状标注 | 规则（现有 scoring），不要裁判 |
| route（分诊） | small@low | S-09 的分诊标签 + 改编（同上，只标分诊级别） | 同上 | 规则 |
| judge（Reviewer、裁判） | strong@medium | S-10 现有 24 条 + 公开的裁判基准 | RewardBench 2、JudgeBench（有标准答案的成对比较） | 规则：和标准答案比，不要裁判 |
| act、transform（带工具） | mid@low | **BFCL v4**（主）+ **τ²-bench**（辅） | BFCL：单轮、多轮、并行的函数调用，按 AST 判对错，便宜、确定；τ²-bench：带用户模拟器的多轮工具交互（航空、零售、电信），测“工具 + 和人来回”，贵 | BFCL 规则；τ²-bench 按它自带的数据库终态判，用户模拟器固定一个模型 |
| transform（压缩、摘要） | small@low 或 mid@low | S-11 现有 7 条长任务 + LongBench v2 的子集 | LongBench v2（长上下文理解，有标准答案） | S-11 规则；LongBench 选择题 |
| deliver（回答、交付） | mid@low | 改编的公开题：SimpleQA（事实与不编造）、FRAMES（要查资料的多跳问题）；代码仓库问答从 SWE-bench Verified 的仓库与提交改编 | 有标准答案的按答案判；开放题用 DeepEval 的 G-Eval，裁判 Opus 5.5 | 规则优先，开放题才用裁判 |
| 整体（写代码、终端运维） | — | SWE-bench Verified、Terminal-Bench 2.0（已有适配器） | 公开 | 官方判分 |

说明：

- **DeepEval、Ragas 是打分框架，不带我们要的数据集**。DeepEval 自带的 MMLU、GSM8K、HumanEval 等是测裸模型的，不测“系统在场景里的
  表现”，不用。所以上表都是“公开数据集 + 框架打分”：能按标准答案判的就不用裁判（便宜、稳定），只有开放题才用 DeepEval 的 G-Eval。
- 之前我写的 `evalsets/deliverables`（8 条）与 `evalsets/memory-rag`（10 条）降为**链路冒烟**，不进能力分。
- 记忆检索（Ragas）不进矩阵（embedding 固定）；它继续测 Memory Service 的检索流水线本身，数据换成公开的 LongMemEval（跨会话的
  长期记忆问答），作为 Memory 上线的验收，不影响选档。
- “改编”的意思：取公开数据集的原题与标准答案，套上我们的输入格式（Planner 的消息、Reviewer 的检查点）。标签只在原数据给不出的
  地方补（例如分诊级别），补的标签走现有规矩：`confirmed: false`，人确认后才算。

## 4. 样本数、重复次数与分数折算

**每格的样本数**：组件级每格至少 **n = 40 道题**、**r = 3 次重复**（模型有随机性；S-09 实测同一模型三次的分诊准确率在 88%～96% 之间跳）。
BFCL、LongBench 这类便宜的题取 n = 100。端到端贵：SWE-bench 50 题、Terminal-Bench 40 题、τ²-bench 每个领域 20 题，r = 1。

**这样能分辨多大的差距**（这一点要事先说清）：n = 40、r = 3 时，95% 置信区间的半宽大约 ±0.12～0.15。所以矩阵只能分出**明显**的差距
（十几个百分点以上）；差几个百分点的模型应当视为一样，由新旧、价格去决定。这正好和路由的权重配合：能力只占 45%。

**折算成能力分**：

1. 题级结果：每道题 r 次的平均（通过记 1、不通过记 0；有分数的题取 0～1 的分数）。
2. 原始分 `p`：题级结果的平均。置信区间按**题**做 bootstrap（2000 次重抽样）；同一道题的 r 次不是独立样本，不能当成 n×r 道题。
3. **向 0.5 收缩**：`score = (n·p + k·0.5) / (n + k)`，k = 10。样本少时分数自动靠近先验 0.5，不会因为几道题的运气拉开差距。
4. 多个评测合成一个格子时，按题数加权平均（同一格里的所有候选必须用同一组评测、同一批题）。

**置信区间不够时**：

- 半宽 > 0.15：**不写分数**，路由照旧用 0.5 的先验，报告里写明“样本不足”。
- 两个候选的区间重叠：照写各自的收缩分（收缩后差距已经很小），报告标“不可分”。
- 想分清时，只对“接近领先者、区间重叠”的候选追加样本（每次加 n = 20），最多加到 n = 120；领先者遥遥领先的格子不再加。

## 5. 裁判与复核

- 只有开放题（deliver 的 G-Eval、少数改编题）用裁判，裁判 **Opus 5.5**，经 Model Gateway 调用（钉 `claude-opus-5-5`），温度 0。
- 每次判分全部落盘：题目、被测输出、裁判看到的完整输入、裁判的分数与理由、被测一侧实际用的模型、花费。
- **同家冲突**：被测输出里有 Claude 模型参与的，Opus 当裁判违反“裁判不与被测同家”的规矩（S-08）。这些题照判，但单独标出；复核时优先看。
- **复核**（人定：gpt-6-astra，要省）：只复核 (a) 全部同家冲突的题、(b) Opus 的分数在及格线 ±0.1 之内的题、(c) 其余随机抽 10%。
  两个裁判差距超过 0.2 的题交人看。复核结果算出两个裁判的一致率（Cohen's kappa）写进报告；kappa < 0.6 时这一格的裁判分数不发布。

## 6. 多久刷新一次

| 触发 | 测什么 |
| --- | --- |
| Profile 里新加一个模型 | 它所在档的组件级格子（影子运行，附录 H.5）；测完之前它的能力分是 0.5，靠新旧分与价格参与排序 |
| 每月一次 | 全部组件级格子（便宜：一次全量估计 30～60 美元，见第 7 节） |
| 每季度或重大版本 | 端到端（SWE-bench、Terminal-Bench、τ²-bench） |
| 每周 | 线上信号：Learning Pipeline 从 trace 统计各任务族、各模型的 Reviewer 判定通过率与花费，作为第二来源（附录 H.1 的可信度：内部线上 > 内部离线 > 公开） |
| 每天 | 价格与发布日期（已有 `sync_model_profiles.py`） |

过期：`at` 超过 90 天的分数下次刷新时删掉（回到 0.5），不让老数据一直占着。

## 7. 预算怎么估

公式：`每格花费 ≈ 候选数 × n × r × 每题花费`，裁判另算 `开放题数 × r × 每次判分花费`。

已有实测的每题花费（2026-11-02，线上价格）：S-09 规划约 0.008 美元/题，S-10 约 0.017 美元/题（含 Reviewer），SWE-bench 约
0.55～0.6 美元/题。BFCL、LongBench 的题短，按 1.5k 输入、200 输出 token 估。

| 部分 | 估算（每个普通候选模型） |
| --- | --- |
| 组件级：plan、route、judge、compress，各 n = 40、r = 3 | 约 4～8 美元 |
| BFCL n = 100、r = 1 | 约 1～3 美元 |
| deliver 开放题 n = 40、r = 3，含 Opus 裁判 | 约 5～10 美元 |
| τ²-bench 3 个领域各 20 题，含用户模拟器 | 约 20～40 美元 |
| SWE-bench 50 题 / Terminal-Bench 40 题 | 约 30 / 25 美元 |

**贵的模型（gpt-6-astra、fable-5.1）省着测**：

1. 只测它们可能当默认的档（强档的 plan、judge），不测 route、压缩这类便宜角色。
2. 先跑 n = 20、r = 1 的**淘汰赛**：区间上沿都够不着当前领先者的，直接停，写“不领先”；只有可能领先的才补到 n = 40、r = 3。
3. 端到端只在它们进入组件级前列之后才跑，而且 SWE-bench、Terminal-Bench 先跑 10 题的子集。
4. 每次运行前打印估算花费；预算不设上限，但单题有上限、花费实时写进评测看板。

## 8. 要做的（实现，按顺序；全部不改契约）

1. `PinningGateway` 与污染检测；评测命令加 `--pin role=profile`。
2. 矩阵调度：读计划（候选、格子、n、r）→ 跑评测 → 汇总（bootstrap、收缩、区间判断）→ 生成 Profile 的 `capability_scores` 补丁；
   发布走现有流程（改 `deploy/dev/model-profiles.json` 与 Registry 种子，人审后经 Registry 发布）。复用 `benchmarks suite` 做并发与拆预算。
3. 适配器：BFCL v4、τ²-bench（新）；RewardBench 2 / JudgeBench、LongBench v2 子集、SimpleQA、FRAMES 的加载与改编（新）。
4. 裁判记录与 gpt-6-astra 复核（第 5 节）；gpt-6-astra、fable-5.1 先加进 Profile（单价与日期从 models.dev 取）。
5. 第一轮：现有候选（Sonnet 5.5、Opus 5.5、gpt-6.1-sol、gpt-5.5、Haiku 5.5、deepseek-v4-flash、gemini-3.5-flash 等）跑组件级与 BFCL；
   贵的两个按第 7 节淘汰赛；结果出来后给人看再发布。

## 9. 要人定的（2026-11-03 人审通过：以下按稿执行）

1. 第 3 节的数据集选择（尤其“改编”的范围与谁确认补的标签）。
2. 第 4 节的阈值：n = 40、r = 3、k = 10、半宽 0.15。
3. 第 5 节复核的抽样比例（10%）与 kappa 门槛（0.6）。
4. embedding 固定选哪一个：`memory-live.md` 的探测里 `text-embedding-3-large` 区分度最好（同义 0.629、无关 0.044）；
   要换更新的，先做一次同样的探测再定，定了就不再变。
