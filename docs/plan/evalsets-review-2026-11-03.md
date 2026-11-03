# 评测集评审与首批改编（2026-11-03）

范围：用户指定的 4 条 session 草稿、8 条 deliverables、10 条 memory-rag，以及公开题改编规则与首批编写。原有评测样本、评分代码和已通过设计均未改动；以下“通过”是 AI 核对意见，不是人工 confirmed。

## 结论与待处置项

| 优先级 | 发现 | 最小处置建议 |
| --- | --- | --- |
| P1 | 两条 Session full 样本把占位“说明正文”判 done，证据不足 | 补齐合成读取事实与满足验收条件的示例交付；保留 full/read 和 draft。补齐前不合入 task-loop |
| P1 | deliverables honesty 规则明确禁止承认之前答案编造，误罚主动纠错 | 删除该惩罚，改成不得虚构已执行操作；主动纠错不扣分。这是评分文字修订提案，未改代码 |
| P1 | `how-did-you-read` 无实际工具轨迹供裁判对照，静态答案不能核实执行真实性 | 暂缓确认该题为真实性评测；现有分数仅视为文本冒烟观察。需要真实性时另用已有 trace 对照 |
| P2 | task-loop 样本 runner 不执行交付节点，不能单独验证上下文实际传到最终回答 | 将这 4 条用途写为“上下文相关分诊回归”；上下文交付链路由对应端到端回归验证 |
| P2 | 未确认 deliverables 分数会进入看板，实时 score 标签不含确认状态 | 解释为未确认的链路冒烟结果；不能称为已人审能力分 |
| P2 | capability 的 S09/S10 自定义 eval-set 不过滤 draft | 新样本保留独立草稿目录，不加入矩阵运行/发布计划；现有代码问题单列，未借评审扩大修改范围 |

以上发现是提案。遵守仓库“审计只读”和冻结设计规则，没有顺手修订原集、指标或发布流程。

## Session：4 条逐题意见

目录：`services/py-learning/evalsets/task-loop-session-drafts/task_cases/`。

| ID | 期望 | 评审意见 |
| --- | --- | --- |
| session_followup_answerable_from_earlier_reply_fast | fast / none | 通过。前文已列出三个模块，追问可直接复述，无须读取工作区 |
| session_general_question_stays_fast | fast / none | 通过。HTTP 429 的独立常识问答不因有工作区而需要完整规划 |
| session_followup_term_needs_workspace_full | full / read | 分诊通过；Reviewer 检查点暂不通过。仅有模块名称不足以说明实现，须读仓库；无 Recipe 命中 |
| session_deep_dive_needs_workspace_full | full / read | 分诊通过；Reviewer 检查点暂不通过。通知接口、认证与失败处理无法从前文得出 |

运行时依据：`services/py-worker/src/agentruntime_worker/planner/prompts.py:46` 区分“仅凭对话可答”与“需要读仓库”；`planner/triage.py:139` 对 follow_up/question + none 给 fast，`:141` 对未命中 Recipe 的任务给 full。

两个 full 文件均在第 86 行定义实际说明要求，第 134 行只给出 `files_read: 3`，第 141 行交付为 `说明正文`，第 146 行只接受 done。按 `reviewer/prompts.py:46`，这些结果不能证明满足 done_when。最小修正是补足具体内容，不改变分诊方向；并入正式集前须处理，因为 `capability/evals/s10.py:44` 默认收集任务中的全部 Round。

`evals/runner.py:197` 调 Planner，`:214` / `:423` 把样本预填结果交给 Reviewer；并未执行回答节点。故本次通过不等于验证了最终答复的上下文传递，也没有真实仓库读操作的证据。

## Deliverables：8 条逐题意见

文件：`services/py-learning/evalsets/deliverables/cases.jsonl`；表中顺序即行号。

| ID | 结论 | 核对证据 |
| --- | --- | --- |
| structure | 参考正确 | 目录和 core/cli/__main__ 职责均吻合。可选文案澄清：“5 行”改为“5 条数据记录”，CSV 另有表头 |
| version | 参考正确 | pyproject.toml 与 inventory/__init__.py 均为 0.3.1 |
| total-units | 参考正确 | 120 + 40 + 15 + 8 + 60 = 243，5 项 |
| low-stock-rule | 参考正确 | README 规定 ≤ 补货线：A101/A102/B200；现有 `<` 漏掉 A101 |
| run-tests | 参考正确 | 实跑 4 条测试，1 条失败，失败项和原因与答案一致 |
| fix-low-stock | 修法与预期正确 | 在内存副本仅将 `<` 改成 `<=`，四条原测试均通过；未修改 fixture |
| cli-usage | 参考正确 | `python -m inventory report data/stock.csv` 与安装后的 inventory 入口均有实现 |
| how-did-you-read | 暂缓确认 | 文件清单取决于本次真实操作，不能用固定参考证明；题目未启用 correctness，参考答案不参与 honesty/relevancy 的 evaluation_params |

关键代码：`evals/deliverables.py:42-49` 的 honesty 只看 input 和 actual_output；`:144-153` 的输入未附工具证据。`:45-47` 的 “must not confess that earlier answers were made up” 应作为指标缺陷处理。诚实承认错误与虚构执行经历不能按同一种行为扣分。

每条 deliverable 独立建 Session，不能预设第 8 条继承前 7 条的阅读历史。`fix-low-stock` 的文本参考正确，也不能据此证明被测系统实际改过文件、跑过测试；本次内存验证只核对参考修法。

看板边界：`deliverables.py:239-242` 会发送这些分数，confirmed 状态只记在 run/report（`:135`、`:277`、`:286-287`）；`evals/live.py:79-83` 的实时标签不含该状态，`dashboard_server.py:151-155` 直接接收 score。因此“会上看板”确实成立，但不代表可发布为能力分。

## Memory-rag：10 条逐题意见

文件：`services/py-learning/evalsets/memory-rag/questions.jsonl`，以同目录 corpus 为依据；并非对生产环境操作说明的确认。

| ID | reference_ids | 结论 |
| --- | --- | --- |
| q-tests | m01 | 一致：uv run pytest、src 布局说明 |
| q-port | m02 | 一致：8443、TLS、/healthz |
| q-migrate | m03 | 一致：make migrate、路径、已发布迁移不改 |
| q-node | m04 | 一致：ci-node20 镜像 |
| q-language | m05 | 一致：中文、英文标识符、先摘要 |
| q-flags | m06 | 一致：启动读取，修改后重启 |
| q-flaky | m07 | 一致：共享临时目录及隔离修法 |
| q-key | m09 | 一致：secondary → 部署 → 24h 后 primary |
| q-old-logs | m15 | 一致：14 天、冷归档、恢复一天 |
| q-merge | m14、m13 | 一致：合并条件与评论规范两个事实 |

`q-language` 的中文查询对应英文记忆，lexical 基线失败不等于标注错。`q-merge` 要看 recall=1；`evals/rag.py:142` 的实时 passed 只代表至少命中一条，不能代表两个事实已找全。保留此集作冒烟即可，不为迁移 LongMemEval 扩充本集。

## 改编规则与首批交付

见 `services/py-learning/evalsets/public-adapted-drafts/README.md`。本次直接采用可逆的草稿编写选择，无须改变已冻结设计：AI 补标、逐题给理由、保留来源与答案，人工决定确认，未确认不进入发布计划。

首批为 13 条规划草稿（NATURAL PLAN 6、TravelPlanner 3、SWE-bench 4）和 2 条 SWE 衍生代码问答。原题和原答案来自官方来源；本地分诊、Stage、模拟 Round、QA 问题和 rubric 是新增内容，明确区分。各规划集能复用现有 loader；QA 为带来源的草稿，还不是新接好的执行适配器。

三点限制：

- 本批主要是 full，不能单独测出 fast/light/replan 的完整边界能力；后续选择要覆盖有真实上下文依据的边界，不能凭难度补 light 标签。
- S-09 只检查规划结构；旅行方案正确率和 SWE 官方通过率需各自执行/判分。模拟 Round 不是官方轨迹，不能拿来增加独立 S-10 样本数。
- 这些开发样本不构成留出集，也不使 plan/route/S-11 自动达到发布条件。重复次数、确认状态、同批候选与置信区间等仍按已接受设计执行。

用户清单中两处与当前仓库不同：矩阵设计第 3 节已将 deliverables / memory-rag 降为冒烟；S-10 默认不是仅 24 条，而是 24 条 reviewer samples + 55 个任务 Round 检查点，共 79 个检查点，同一任务内有相关性，不能当作 79 道独立任务。S-11 后续补充按设计使用 LongBench v2，不由本次规划草稿补齐。

## 本次验证

- 原 deliverables fixture：实际运行 unittest，4 条 / 1 失败；仅在内存副本应用参考修法后 4/4，通过后丢弃副本。
- Memory：逐题核对原文、参考答案与 reference_ids，10/10 一致。
- 新规划草稿：13/13 通过现有 loader 的 schema、Catalog、DAG 校验；13 个 Planner 参照和 13 个合成 Reviewer 检查点均通过 Oracle 自检。2 条 QA 的输入补丁与保留上游原行一致。Oracle 自检不代表人审通过或被测模型能力。
- 新题来源：对所保留上游字段、Planner 输入与答案侧内容进行一致性核对；版本和许可证归属见各子目录来源文件。
- 未调用在线模型，未跑付费基准，未改 Profile，未发布能力分。原有样本和冻结设计保持原状。

## 复核（2026-11-03，Claude）

逐题对照钉住的上游文件核对第一批 13 条规划草稿与 2 条问答：NATURAL PLAN 6 条、TravelPlanner 3 条的原字段与上游一致，Planner 输入就是原题原文；SWE-bench 4 行与服务器上的 Verified 数据逐字段一致（FAIL_TO_PASS / PASS_TO_PASS 一边是列表、一边是 JSON 字符串，解析后相同）。标准答案、gold patch 与 test patch 都没有进 Planner 输入。现有加载器与 Oracle 自检 13/13 通过。上面 P1、P2 各项我同意，`deliverables.py` 的 honesty 规则确实写着 “must not confess that earlier answers were made up”。

另记两点：

- NATURAL PLAN 与 TravelPlanner 的 `category` 多数标为 `booking_payment`，题目本身只生成文字、risk=none。它只是报告里的分组，不影响判分；按“类别”看报告时，这一组的成本与成功率会和真正要付款的订票题混在一起。要分开就新加一个分组（例如 `planning`），由人定。
- 第二批的 14 条见 `public-adapted-drafts/README.md` 末尾；补上了 fast 3 条与 replan 2 条，light 仍缺。
