# 公开题改编：第一批草稿（2026-11-03）

按已人审的 `docs/plan/capability-matrix-evals.md` 第 3 节开始编写。这里是独立草稿，未接入默认 task-loop、矩阵计划或 Profile 发布。AI 评审不等于人工确认。

## 第一批选题

| 子目录 | 数量 | 选择方式 | 本地用途 |
| --- | ---: | --- | --- |
| `natural-plan` | 6 | trip 的 3/5/8 城各第一题，meeting 的 3/6 人各第一题，calendar 的 3 天首题 | 分诊与规划草稿 |
| `travelplanner` | 3 | 官方 train 的 easy/medium/hard 各第一题（零基行号 0/15/30） | 分诊与规划草稿 |
| `swebench` | 4 + 2 | 4 个真实 Verified issue，另从其中 2 个生成代码问答；具体 ID 和选择理由见子目录 | 4 条规划，2 条交付问答草稿 |

这是方便逐条核对的试编批次，不是随机代表样本，也没有凑足可发布的能力分。所有已展示、编写和核对的题都放本地 `dev`；上游 train/test 标识另保留在来源记录，不能把公开 test 自动称作我们的未见留出集。

## 改编规则

1. **选题**：先选原题、答案/判分证据均可取得且与现有能力对应的题。后续按来源与难度分层固定题目清单，再运行模型，不能根据模型表现挑题。Terminal-Bench 只选已有可移植清单中的任务，并保留实际环境前提；τ²-bench 同时保留领域政策、可见用户请求与答案侧终态，不能把隐藏目标或数据库答案放进模型输入。两者尚未纳入本批。
2. **原题**：保留原文和原始约束，不把历史日期改成今天，不改预算、人数、测试目标或正确答案。TravelPlanner 的 reference_information 是题目给定的历史信息，随 query 放进输入，不上网替换成当前价格。NATURAL PLAN 使用官方 prompt_0shot，不包含 prompt_5shot 或模型示例预测。
3. **证据**：每题关联来源 URL、上游 revision、题号/零基行号、官方 split、保留的原字段及答案字段；见各子目录来源文件。原答案、gold patch、隐藏测试只供评审/评分，不能进入 Planner 输入。代码问答必须明确给的是 base 文件还是 patch，答案只对该版本成立。
4. **补标责任**：AI 先写分诊、风险、必要形状与理由，统一待确认。task schema 用 `review.status: draft`（现有 `TaskCase.confirmed` 因此为 false），不要为统一拼写修改 schema。JSONL 问答/来源记录可用 `confirmed: false`。人工确认者决定是否接受补标；未审或有争议的题保持 draft。抽查不自动使其余题变为 confirmed；若人工明确批准整批，才按该授权记录真实确认人和日期。
5. **分诊依据**：`fast` 需要仅凭已有材料即可直接答复/续改；`light` 需要有效 Recipe 命中与差量检查，不等于“简单题”；新规划任务未命中 Recipe 通常为 `full`；`replan` 需要真实在途 Task/上一轮证据。不能为凑齐四类而捏造 Recipe 命中。本批主要覆盖 full，其余边界留给后续有依据的样本。
6. **最小形状**：只约束完成任务必需的行为。示例 Stage 是一种可行组织方式，不要求匹配节点名、节点的确切个数或个人偏好的质量包装。旅行安排只生成文本时 risk=none，不标成付款或日历写操作；修代码按可回滚写处理。
7. **评分含义**：现有 S-09 规则检查结构、规则、分诊与标注的形状，不执行行程约束求解，也不运行 SWE-bench 官方测试。因此“规划结构通过”不能叫“官方基准通过”。原答案保留下来用于复核和后续明确的答案评分，不宣称已接好官方执行环境。
8. **Round**：现有 task schema 必须至少有一条 Round。本批因此提供明确标记的合成检查点；NATURAL PLAN / TravelPlanner 把官方答案放在模拟结果中，不声称这是实跑。SWE 的检查点见其 README。它们不得充作新增的独立官方 S-10 题。一个原题的规划、检查点、QA 变体按同一来源分组，后续不能跨 dev/holdout 或重复计为独立样本。
9. **停止条件**：本批交付是可核对、可加载的草稿和来源；不扩展评测运行框架，不补造 S-11 长任务，不修改已接受的矩阵设计。

## 怎么核对

每个规划子目录有独立 `catalog.json` 与 `task_cases/`，可由现有 `load_eval_set` 加载。catalog 复用当前 task-loop；不新增工具、模板或 Recipe。

离线自检可以调用现有 `OracleSubject` + `score_planner` / `score_reviewer`，它只验证参照输出与评分规则一致，**不证明标签正确或模型能力达标**。正式使用前须逐题处理本文和评审报告中的待确认项。

当前 capability 的 S09/S10 适配器加载自定义 eval-set 时不会过滤 draft；目录隔离和不接入矩阵计划是本批的具体使用边界。普通 evals 命令已有 `--require-confirmed`。适配器缺少确认检查已在评审报告单列，不在本次只读评审中修改运行时代码。

## 来源与归属

- NATURAL PLAN：Google DeepMind / Zheng et al., *NATURAL PLAN: Benchmarking LLMs on Natural Language Planning* (2024)。[官方仓库](https://github.com/google-deepmind/natural-plan)，Apache-2.0；原 LICENSE 保存在 `natural-plan/LICENSE`。
- TravelPlanner：OSU NLP Group / Xie et al., *TravelPlanner: A Benchmark for Real-World Planning with Language Agents* (2024)。[官方数据](https://huggingface.co/datasets/osunlp/TravelPlanner)，[CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/)。修改仅为本地输入包装与补标，原字段见 `travelplanner/sources.jsonl`。
- SWE-bench Verified：来源、许可证与改编范围见 `swebench/README.md`。

未执行在线评测、未调用付费模型、未发布任何能力分。

## 第二批（2026-11-03，Claude）

按上面的规则续写 14 条，同样是 `draft`，只放 `dev`。原题与标准答案原样取自第一批钉住的上游 revision；生成后逐题与上游核对过，标准答案没有进 Planner 输入（追问题除外，见下）。

| 子目录 | 新增 | 选法 | 分诊 |
| --- | ---: | --- | --- |
| `natural-plan` | 6 | trip 的 4/6/7 城、meeting 的 4/5 人、calendar 的 1 天，各取上游顺序的第一题（与第一批不重复） | full |
| `travelplanner` | 3 | easy/medium/hard 各取第二题（零基行号 1/16/31） | full |
| `natural-plan` | 3 | 追问：上一轮的问与答 = 上游原题与 golden_plan；追问只问回复里写明的第一站或会议时间 | fast |
| `natural-plan` | 2 | 上一轮交付了一份漏掉最后一个城市的方案（本地合成），Reviewer 判 replan；第二轮交付上游 golden_plan | replan |

追问题与 replan 题由同一道原题派生，`sources.jsonl` 里记 `derived_from` 与 `group_id`，以后划分 dev/holdout 时与原题放在一起，不算独立样本。追问题的 Planner 输入里有上一轮回复（即标准答案），这是题目本身要考的：答案已在对话里，不必重新求解。

仍然没有 `light`：它要真实的 Recipe 命中，规则 5 不允许捏造。Terminal-Bench 与 τ²-bench 的任务描述也还没改编。
