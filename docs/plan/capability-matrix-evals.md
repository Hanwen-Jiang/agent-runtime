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

## 10. 实施记录（B1 线，2026-11-03）

做了第 8 节的 1、2，4 的后半（两个贵模型的 Profile），以及组件级的三个评测；公开数据集的适配器（BFCL v4 等）是 B2 线，裁判记录是主线的 `evals/judging.py`。都不改契约。

| 部分 | 位置 | 说明 |
| --- | --- | --- |
| 钉模型 | `capability/base.py` 的 `PinningGateway` | 加了可选的 `tier`：被测角色的调用改成 `<候选的 class>@<格子的 tier>`（网关只在请求的档里找钉的模型，压缩默认 small@low，mid 档的候选不改就钉不上）；加了 `pinned_calls`、`missed_calls`。原接口不变 |
| `--pin role=profile` | `evals/__main__.py`（`run`、`context`）、`evals/worker_subject.py` 的 `pinned()` | 只对经 Model Gateway 的被测对象有效（baseline、oracle 报错退出 2）；报告加 `pin`：钉住的调用数与被污染的次数 |
| 评测的写法 | `capability/evals/__init__.py` | `<模块>[:<工厂函数>]`，短名指 `capability.evals.<短名>`（包里没有时再当顶层模块找），工厂默认 `make(**options)`。CellEval 可选的属性：`est_tokens`、`est_other_usd`（估算花费）、`uses_judge`（裁判调用经 `RecordingJudgeGateway`，钉 Opus、逐条落盘到 `judgments.jsonl`；judge 格子不能用）。`items(n, seed)` 要前缀稳定 |
| 组件级的格子 | `capability/evals/s09.py`、`s10.py`、`s11.py` | 一条用例一道题，评分复用 `evals.scoring` 与 `evals.context`。`s09`（plan）：标为 full、replan 的 25 条，S-09 的规划决策全部通过记 1；`s09:route`（route）：由分诊模型定级的 36 条，Triage 对记 1，规划角色的调用在本地拒掉（只花分诊的钱）；`s10`（judge）：Reviewer 样本 24 条加 Task 用例里每个 Round 的判定点 55 个（`checkpoints: samples` 只用 24 条）；`s11`（transform）：7 条长任务样本按 full 压缩，分数 = (答对的 Probe 题 + 没重做已完成的动作) / (题数 + 1)。默认 dev、holdout 都用（给模型打分，不调提示）；留出集的题只记失败的维度与代码 |
| 调度 | `capability/matrix.py` | 计划格式见模块说明。每个 候选 × 格子 × 评测 × 题 × 重复 一个新的 PinningGateway，结果（含 record）写 `results.jsonl`，续跑跳过做完的，评测集版本变了拒绝续跑；`item_usd_limit` 单题上限（超了按被污染处理）、`--budget-usd` 总预算；跑之前打印估算（做过的按实际平均花费）；实时指标 suite `capability`，每个候选一个 run。贵的候选（`expensive`）先跑 n = 20、r = 1 的淘汰赛；`extend: true` 时按第 4 节追加样本（默认关：要分清时才花这个钱） |
| 折算 | `capability/scoring.py` | 第 4 节全部：题级平均、按题（按评测分层）bootstrap 2000 次、k = 10 的收缩、半宽 0.15、污染 5%、“不可分”、淘汰赛与追加样本的判定 |
| 发布 | `capability/publish.py` | 只写可发布的分数，任务族第一版都是 `general.unclassified`；source 形如 `capmatrix:s09+bfcl-v4@2026-11-10:n=60,r=3:run=round1`；同维度替换，超过 90 天的删掉；按 schema 与 Registry、Gateway 的规则校验后改 `deploy/dev/model-profiles.json` 与 Registry 种子（格式用 `sync_model_profiles.py` 的，多条能力分一行一个），补丁另存 `capability-patch.json` |
| 命令 | `python -m agentruntime_learning.capability {estimate,run,score,publish}` | 见 CLAUDE.md 的命令表 |
| 新 Profile | `deploy/dev/model-profiles.json`、`registry-seed.json` | `sync_model_profiles.py --add openai/gpt-6-astra --add anthropic/claude-fable-5-1 --class strong --tiers medium,high`：models.dev 一方条目，gpt-6-astra 输入 10、输出 50、缓存读 1 美元每百万 token，上下文 922k（limit.input），2026-09-04 发布；claude-fable-5-1 为 10、50、0.25，上下文 1M，2026-09-01 发布。2026-11-03 核实：中转站 `GET /v1/models` 列出 151 个模型，13 个 Profile 的模型名（含 `gpt-6-astra`、`claude-fable-5-1`）都在；查法是服务器上的 `/root/ar-release/relay-models.sh`（在 worker-model 的 Pod 里查，只输出模型名，结果在 `relay-models.txt`），由人运行 |

与设计稿不完全一致、要人看的：

1. **S-10 的题数**：设计稿写“S-10 现有 24 条”，24 条凑不到每格 40 道（半宽必然超过 0.15），所以默认把 Task 用例里的 55 个 Round 判定点也算进来（共 79 道）。同一条用例的几个 Round 不完全独立，区间会略窄。
2. **plan 格子只用 full、replan 的 25 条**：fast、light 的用例根本不调规划模型，放进来只会把各候选的分数拉向一样；route 格子因此也去掉了由上一轮 Verdict 定级的用例。两格都不到 40 道，在 B2 的改编题补上之前不会有可发布的分数。S-11 只有 7 条，同理要等 LongBench v2。
3. **“同一批题”**：追加样本只给与领先者重叠的候选（连同领先者）加题，被甩开的候选停在原来的 n；各候选的题是同一个 seed 下同一个顺序的前缀，不是完全相同的集合。
4. **并发与拆预算**没有复用 `benchmarks suite`（它是按子进程调度端到端评测的）：组件级都在一个进程里，用 asyncio 的并发与总预算。端到端的矩阵（第 2 节的按档钉模型）没做。
5. 裁判的复核（第 5 节，gpt-6-astra 抽查、kappa）不在这条线。

### 10.1 新候选（2026-11-03）

人定：中转站里比现有 Profile 新的模型都进能力矩阵的候选。`sync_model_profiles.py --add`（models.dev 各家自己的条目；新认了
xai、moonshotai、alibaba、zhipuai 四家）加了 9 个，模型名都在中转站的 `/v1/models` 里：

| 档 | Profile（模型名） | 单价 入/出/缓存读（美元每百万 token） | 发布 |
| --- | --- | --- | --- |
| strong | gemini-4-argon | 2 / 10 / 0.1 | 2026-10-13 |
| strong | deepseek-v4-pro | 0.66 / 1.98 / 0.022 | 2026-08-13 |
| strong | grok-4.7 | 2 / 6 / 0.5 | 2026-09-21 |
| strong | kimi-k3.1-pro | 2 / 10 / 0.2 | 2026-10-23 |
| strong | qwen3.8-max | 2 / 6 / 0.25 | 2026-08-03 |
| strong | glm-5.3 | 1.20 / 4.18 / 0.30 | 2026-08-14 |
| mid | gemini-3.8-flash | 0.75 / 3.75 / 0.075 | 2026-09-02 |
| mid | gpt-6.1-terra | 1 / 5 / 0.05 | 2026-10-26 |
| small | gpt-6-luna | 0.1 / 0.5 / 0.01 | 2026-09-22 |

没加：deepseek-v4.1-flash、minimax-m3（models.dev 只有转售商的条目，没有一方单价）；gpt-6-sol（被 gpt-6.1-sol 取代）；
gemini-3.6/3.7-flash（被 3.8 取代）。新 Profile 只有进了租户的白名单才会被选中：部署时只加进 `evals` 租户的白名单（矩阵用），
线上其他租户的选档不变，等能力分人审发布后再定放给谁。档位是按单价与各家的定位先定的，矩阵跑完按实测再调。

## 11. 实现记录：公开数据集（B2 线，2026-11-03）

第 3 节里公开数据集的 CellEval 在 `services/py-learning/src/agentruntime_learning/capability/evals/`，每个模块有 `make(**options)`；
共用部分在 `_datasets.py`（下载到 `AR_EVAL_DATA_DIR`，默认 `~/.cache/agentruntime-evals`，逐个文件核对 sha256；按 (seed, 题号) 的哈希
选题，n 变大只在后面追加，有分组时各组轮流取；模型调用经传进来的 Gateway，租户 evals，花费取 usage，失败取错误里的已计费花费；
出错给 `ItemResult(score=0, error=…)`，`contaminated` 取 PinningGateway 的判定）与 `_judged.py`（要裁判的题）。数据集不进仓库，
测试用手写的小样本（真实格式）；第三方包只进 `eval-requirements.txt`，不装也能 import、离线测试照过。

| 模块 | role | 数据与钉的版本 | 许可 | 判分 |
| --- | --- | --- | --- | --- |
| `bfcl` | act | Gorilla `6ea5797`（2026-03-23）的 `BFCL_v4_*.json` 与 `possible_answer/`；默认 simple（simple_python 400）、multiple 200、parallel 200、parallel_multiple 200，可选 live_* 与 irrelevance | Apache-2.0 | 移植 BFCL 的 Python AST 规则（`ast_checker.py`）；工具按 BFCL 的 FC 模式转换 |
| `tau2` | act | tau2-bench v2.0.0（`989d328`，2026-10-19），split base：airline 50、retail 114、telecom 114 | MIT | 官方包的 `evaluate_simulation`，按 reward_basis 逐项相乘 |
| `rewardbench2` | judge | `allenai/reward-bench-2` `7ff0885`（parquet sha256 `c8ec60ef…`），Ties 以外 1763 题 | ODC-BY；回答出自多个模型，各有使用条款 | 官方 ratings 提示（默认）或 four-way 提示 |
| `judgebench` | judge | `ScalerLab/JudgeBench` `57dd5e0`，默认 gpt 一份 350 对（claude 270 对可选） | MIT（题目来自 MMLU-Pro、LiveBench、LiveCodeBench，各有许可） | 官方 Arena-Hard 裁判提示，两种顺序 |
| `longbench` | transform | `zai-org/LongBench-v2` `2b48e49`（data.json 465 MB，sha256 `15d61c22…`，拆成逐题缓存），默认 short | Apache-2.0（上下文取自公开文档、书籍等，许可各异） | 官方 0-shot 模板与答案提取 |
| `simpleqa` | deliver | simple-evals 的 `simple_qa_test_set.csv`（sha256 `feee3f7e…`，4326 题），判分提示取自 simple-evals `652c89d` | MIT | Opus 5.5 按官方 GRADER_TEMPLATE 判 A/B/C |
| `frames` | deliver | `google/frames-benchmark` `58d9fb6`（test.tsv，824 题） | Apache-2.0 | Opus 5.5 按论文图 6 的 Auto-rating Prompt 判 TRUE/FALSE |

许可：都允许评测使用；ODC-BY（RewardBench 2）要求注明出处，报告里写数据集名与版本即可。没有限制商用评测的条款，但 RewardBench 2、
JudgeBench 的回答是别家模型生成的，**不要拿来训练**（各模型的使用条款）。

**怎么判，与官方的差别**

- BFCL：只做单轮。多轮（multi_turn_*）要执行 BFCL 自带的模拟 API 类（文件系统、交易等），没做。Gateway 的工具参数是 protobuf
  Struct，数都是浮点数：按函数声明的类型把整数还原成 int，所以给整数参数写 `10.0` 时原版判类型错、这里判对；别的规则不变。
  在真实数据上核对过移植：用标准答案当输出，2351 题里只有 9 题不过（都是标准答案本身把必填参数标成可省，原版同样不过）。
- τ²-bench：对话在本模块里经 Gateway 跑，环境、提示、工具执行与判分用官方包（`OfficialBackend`）；在装了官方包的 Python 里核对过
  三个领域各一道题按标准动作走一遍都得 1 分、什么都不做的 retail 题得 0 分。用户模拟器默认 `a6api/gpt-5.5@2026-10`（role deliver、
  明确钉住、tier strong@medium（网关只在请求的档里找钉的模型，换模型时 `user_tier` 一起换），所以不被矩阵钉到候选上；官方默认 GPT-4.1），**要人定**。COMMUNICATE 用官方原来的子串匹配；retail 112 题的 reward_basis
  有 NL_ASSERTION，官方经 litellm 调自己的裁判，这里的模型调用只能经 Gateway，所以先不算（record 的 `skipped` 写明），只按数据库终态判。
  要算的话把 NL 断言改经 Gateway 的 Opus 裁判，另做。数据从钉死的提交稀疏检出（约 130 MB，要 git），或设 `TAU2_DATA_DIR`。
- RewardBench 2：Ties 子集的官方分数按一组题算（准确率加分差），不是逐题的，不收。默认 ratings：四个回答各自单独打分，好回答
  并列第一时按比例给分（同官方），没有位置偏差；`mode="four_way"` 时好回答放在两个不同位置各问一次，两次都选中才算对。
- JudgeBench：默认两次都判对才算对（人定的“只算前后一致的正确答案”）；官方的算法（判对次数减判反次数大于 0，一对一平局也算对）
  另记 `official_correct`，`scoring="official"` 时按它。
- LongBench v2：没有分词器，按 UTF-8 字节数估 token（约 4 字节一个），超过 `max_context_tokens`（默认 12 万，同官方）时截掉上下文中间。
- SimpleQA、FRAMES：被测作答是 role deliver；裁判是 role judge、`pinned_model_profile` 明确填 Opus 5.5，经 `evals.judging` 的
  RecordingJudgeGateway 调用，每次判分写一行 JSONL：两个评测声明 `uses_judge = True`，矩阵调度在 gateway 底下包好记录器、写运行目录的
  `judgments.jsonl`；单独用时（gateway 链上没有记录器）自己包一层，写 `judge_log`（默认 `<AR_EVAL_DATA_DIR>/judgments/<评测>.jsonl`），不会记两遍；`ItemResult.record` 里有裁判的完整输入、结论、理由、实际服务的模型与花费，候选是 Anthropic 的模型时 `same_vendor = True`。
  裁判没落在 Opus 上时这道题记为出错。**温度 0 做不到**：GenerateParams 没有温度字段（要改契约，走 S-01），按 Profile 的默认值，
  record 里写 `temperature: "provider default"`。SimpleQA 逐题：CORRECT 记 1、NOT_ATTEMPTED
  （老实说不知道）记 0.25、INCORRECT（编错）记 0（2026-11-03 人定：deliver 测“不编造”，官方规则把不知道与瞎编算成一样；
  `abstain_credit` 可调，写在版本号里）；scores.json 每格另附官方口径（答对率、答了的里答对的比例、F 分数、答了的里答错的比例）。FRAMES 只做论文的
  naive prompting（不给维基百科条目）；给条目原文的 oracle 设定要另取页面并定怎么钉版本，没做。

**每题花费的估计**（没实测，按 Profile 的单价与估的 token 数；有思考预算的档另加思考 token）。各评测的 `est_tokens`（被测一侧的输入、
输出 token）与 `est_other_usd`（裁判、用户模拟器）按这张表填，矩阵的 `estimate` 用它们。

| 评测 | 估的 token（每题） | Sonnet 5.5 | Opus 5.5 | gemini-3.5-flash |
| --- | --- | --- | --- | --- |
| BFCL | 输入约 0.6k、输出约 0.1k | 约 0.002 美元 | 约 0.004 | 约 0.002 |
| τ²-bench | 客服约 150k～300k 输入（政策与工具每轮重发，缓存命中会便宜很多）、约 3k 输出；另加用户模拟器（gpt-5.5）约 60k 输入，约 0.35 美元 | 约 0.7～1.0 | 约 1.0～1.6 | 约 0.6～0.9 |
| RewardBench 2（ratings） | 4 次 × 约 0.8k 输入、0.25k 输出 | 约 0.016 | 约 0.033 | 约 0.014 |
| JudgeBench | 2 次 × 约 2.5k 输入、1.2k 输出（Arena-Hard 要裁判先自己作答） | 约 0.034 | 约 0.07 | 约 0.03 |
| LongBench v2（short） | 约 25k～45k 输入、很少输出 | 约 0.05～0.09 | 约 0.10～0.18 | 约 0.04～0.07 |
| SimpleQA | 作答很短；Opus 裁判约 1.5k 输入 | 约 0.007（含裁判 0.006） | 约 0.007 | 约 0.007 |
| FRAMES | 作答约 0.3k～2k 输出；Opus 裁判约 0.4k 输入、0.15k 输出 | 约 0.01～0.03（含裁判 0.005） | 约 0.01～0.05 | 约 0.01～0.025 |

和第 7 节的出入：LongBench 不是 1.5k 输入的短题，n = 100 每个候选约 4～9 美元（mid）；τ²-bench 的 3 × 20 题按上表约 40～60 美元，
比第 7 节的 20～40 美元高，主要是用户模拟器与政策重发，网关的提示缓存命中后会低一些。真跑第一轮后按实测改这张表。

## 12. 改编题进 plan、route 两格（MX-Q，2026-11-04）

第 10 节第 2 条说 plan、route 两格不到 40 道题。`evalsets/public-adapted-drafts` 的两批草稿（natural-plan、travelplanner、swebench）补上：

- 计划里写 `{"eval": "s09", "options": {"adapted": ["natural-plan", "travelplanner", "swebench"]}}`（route 格子是 `s09:route`）。不写时题目与评测集版本同原来。
- 三套共 22 道原题；同一道原题派生的追问（fast）与 replan 只取原题（草稿 README 第 8 条：不重复计为独立样本），所以两格各加 22 道：
  plan 格子 25 + 22 = 47 道，route 格子 36 + 22 = 58 道，都够第 4 节的 40 道。fast、replan 的边界仍只来自 task-loop 自己的用例。
- 题号是 `adapted:<套>/<用例>`，各题用自己那套的 Catalog（swebench 的与 task-loop 的不同）；记录里写 `confirmed`（草稿都是 false）与 `adapted`。
  评测集版本把这几套的版本算进去，换了题续跑会被拒。
- 标签仍是 AI 起草的 draft，没经人抽查。按总表第 7.2 节（2026-11-04“等人定的一律通过”），MX-PUB 照第 4 节的规则发布，不等人抽查；
  记录里的 `confirmed: false` 留着，以后有人抽查时按它筛。
- 顺带发现：task-loop 与草稿里的参照规划都写在 N-04 之前，`task_plan` 没有 `steps`，现在的 Planner 第一次完整规划会拒掉它们
  （`tests/test_capability_component.py` 的“规划全对”用例因此在 main 上失败）。参照只用于自检、不是标注，测试里的假模型补上三步，评测集没改。
  真跑的分数不受影响：被测模型自己要给 steps。
- 没做的：S-11 的长任务（要 LongBench v2，第 11 节）；`light` 仍没有改编题（要真实的 Recipe 命中，草稿 README 第 5 条）。

## 13. 第一轮的结果（MX-RUN，2026-11-04 至 11-05；记录 MX-RUN-REC）

来源：服务器上的 `/opt/evals/mx/round1`（`plan.json`、`results.jsonl` 5742 条、`scores.json`、`capability-patch.json`）。这次运行没在总表第 8 节认领
（11-04 13:31 起，`--budget-usd 120`），11-05 01:18 打分并做了发布的 dry-run，**没有发布**。花 120.09 美元，到了上限。

与计划不同的地方：服务器上的 `plan.json` 没打开第 12 节的改编题，plan 格只有 25 题、route 格 36 题，不到第 4 节的 40 题；transform 格没跑；
judge 格的三个贵模型（Opus 5.5、gpt-6-astra、fable 5.1）预算用完，没有结果。

| 格子 | 题 × 次 | 领先（收缩分） | 按规则可发布 | 不发布的主要原因 |
| --- | --- | --- | --- | --- |
| act · medium（BFCL v4） | 100 × 1 | mimo-v2.6-pro 0.891 | 12 个，0.373～0.891 | fable 5.1 淘汰赛 20 题 0 分，被淘汰（像是调用方式的问题，没查） |
| route · low（s09 分诊） | 36 × 3 | gemini-3.8-flash 0.833 | 6 个，0.739～0.833 | qwen3.8-flash 污染 10%、gpt-6-luna 57% |
| judge · medium（JudgeBench + RewardBench 2 + S-10） | 24～89 × 3 | mimo-v2.6-pro 0.776 | 2 个：mimo-v2.6-pro 0.776、glm-5.3 0.644 | 8 个污染 6%～79%；3 个贵模型没结果 |
| plan · medium（s09） | 25 × 3 | gemini-4-argon 0.371（半宽 0.167，不写） | 3 个：Opus 5.5 0.295、fable 5.1 0.248、gpt-6-astra 0.238 | 其余的污染超过 5% 或半宽超过 0.15 |

**plan 格不该照规则发布。**

1. 25 题里有 12 题，13 个模型各跑 3 次全是 0 分（例如 `batch_label_tickets`、`ci_fix_failing_test`、`multiday_csv_export`、`research_cbam_impact`）。
   失败全在形状检查上：缺 deliver 节点 387 次、缺 Goal Node mode（多是 template）231 次、缺质量包装 169 次，gate、human、并行宽度、节点数也各有几十次。
   这些都是人确认过的标注。去掉回退的运行也一样（缺 deliver 241 次）。所以要么是 N-04 之后的 Planner 与标注对不上，要么是标注太严，先要查清，分数才有意义。
2. Planner 回退（没规划成，退回保守规划）975 次里有 276 次：qwen3.8-max 73/75、mimo-v2.6-pro 70/75、gpt-6-astra 57/75，多是 `bad_structured_output`。
   第 2 节只把“钉的模型没用上”算作污染，回退照样计分，所以回退 76% 的 gpt-6-astra 也“可发布”。`evals run` 回退率超过 5% 就判运行无效，矩阵的计分没有这条。
3. 按规则能发布的恰好只剩三个贵模型，分数都低于 0.5，其余候选照旧用 0.5 的先验。发出去之后，路由在规划上会避开测过的模型，去用没测过的。

**judge 格的污染**主要来自中转站：502 时网关切到同档别的模型（结果里 `models_exhausted` 236 次、Model Gateway 的 gRPC 超时 72 次）。也就是钉的模型一时不可用，
不是钉的方式有问题。污染的题没有重跑。

**建议（交人定）：**

- 发 act、route 两格（12 + 6 个分数）与 judge 格的两个分数；plan 格这次不发。发布 = `capability publish` 改 `deploy/dev/model-profiles.json` 与 Registry 种子，人审后经 Registry 发布。
- 下一轮之前：
  - 矩阵的计分把 Planner 的回退也算作无效运行，与 `--max-fallback-rate` 同一规则；
  - 钉的模型 502 时先重试，不切模型，或者只重跑被污染的题；
  - `plan.json` 打开改编题，让 plan、route 两格都够 40 题；
  - 查清第 1 条那 12 题。
