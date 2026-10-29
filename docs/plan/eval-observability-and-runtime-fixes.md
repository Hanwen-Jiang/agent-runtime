# TMP 计划：线上日志的问题、真模型实验的遗留与评测可观测（设计稿）

> 状态：2026-10-28 人确认开工，要人定的几项已定（第 11 节）。第 3～7 节的 S-01 草案人审通过（兼容性基线已更新）；
> 第 2～4 节已实现（11.7）。第 5～10 节未做。来源两处：
> - 线上日志（`Z:\agentruntime\1028137pm.log`，code.cgent.link，租户 `gh-nsnzd-kimberlyxie343178`，“读仓库并讲清结构”跑了 5 轮、$0.62，最后卡在“规划下一轮”）；
> - 真模型实验报告（`docs/job-qualifications/real-model-experiments-2026-10-28/README.md` 第 6 节的 P0、P1）。
> 已经在本仓库里改掉的列在第 1 节；第 2 到 9 节是重的或要先定的，每节写清问题、证据、做法、要人定的、验收。

## 1. 已经改掉的（2026-10-28，提交见 git log）

| 问题 | 证据 | 改法 | 测试 |
| --- | --- | --- | --- |
| Planner 把读文件、跑命令放在无工具的 model 节点上 | 实验 3.1：6/11 个 Task、17% 花费；线上日志第 5 轮的 `list_repo_files`（fetch） | Stage 校验拒绝 executor = model 的 fetch、act 节点（节点或模板是 model 都算）；提示写明只有 tool_loop 有工具；真 Planner 的 Catalog 不列 `stub_` 模板 | `test_planner_full.py`、`test_registry.py` |
| Reviewer 原地重试与假完成 | 实验 2.2、3.2 | floor 加三条（附录 B.6、B.7）：本轮已重试仍是上一轮的同一错误 → replan；扇出后合并失败 → replan（不保留冲突的分支）；质量包装用满轮数仍没通过 judge（Stage Runner 记 succeeded、`quality.passed = false`）→ 不能 continue 或 done，换方法；Progress Check 自己答了没满足验收条件却判 done → 不收 | `test_reviewer_rules.py`；py-learning 的 `test_worker_subject.py`（对抗模型下 `rs_revise_same_error`、`rs_fanout_conflict` 由代码判对） |
| 任务族为空，Learning 看不到 | 实验第 5 节：11 个里 6 个空 | 分诊与规划模型都没给时记 `general.unclassified`（full、replan、保守规划都给；fast 仍没有 Task Plan，见第 6 节） | `test_planner_full.py` |
| Node 的预算超过 Task 剩余，被整个拒绝 | 日志第 2 轮：0.9 美元 > 剩余 0.8242 | Planner 由 Task State 算出剩余（与 Reviewer 台账同一口径），提示里写明，代码把超出的 Stage、Node 预算压到剩余（`session_budget_remaining_usd` 一直没人填） | `test_planner_full.py` |
| 英文界面里 Reviewer 的文字是中文 | 日志：理由、问题、已验证的事实 | Reviewer 与 Planner 的提示要求给人看的文字用用户的语言（goal 的语言）；代码生成的说明见第 5 节 | `test_reviewer.py` |
| “已验证的事实”里出现 Reviewer 的内部材料 | 日志：“代码计算出的硬信号为空，floor 为 none” | 提示要求 facts 只写任务与产出物本身的事实，不提 floor、硬信号、规则 | `test_reviewer.py` |

## 2. 新租户的工作副本是空的（P0，要先在线上诊断）

**现象**：GitHub 上的 `nsnzd-kimberlyxie343178/test-cgent` 有 LICENSE 与 README.md（提交 432ef9a），会话的工作副本 `test-cgent-f666--s01e2e6`
在“浏览文件”里是空的；Task 里 `repo_list .` 成功但为空，`repo_read README.md` 报 `file_not_found`。Agent 在空目录上跑了 5 轮。

**线索**：开放注册的新租户走平台默认策略（`docs/plan/open-signup.md`）：沙箱后端 **kata（microVM）**、`min_isolation: microvm`、不回退到 gVisor。
上线时验过的工作区与真模型 Task 都在 **gVisor** 上。会话副本由 Workspace API 的 `from_workspace_id` 建（`operations._copy`：在源的只读环境里
`export_archive`，在新的可写环境里 `import_archive`）。

**假设**（按可能性）：
1. Kata 环境没挂上工作区卷（或挂到了别处）：克隆写进了 microVM 的根文件系统，环境销毁后丢了，卷上的基线工作区是空的，副本从空的基线复制。
2. 基线有内容，但只读的 Kata 环境里看不到（virtio-fs 与 subPath、只读挂载的组合），导出是空的。
3. 克隆失败被应用吞掉，退成了空的初始化（coding-agent 一侧）。

**诊断（只读；按“生产数据要人批准”的规矩，由人执行或批准后执行）**：

```bash
kubectl -n agent-runtime logs deploy/ar-agent-runtime-worker-task --since=6h | grep -E 'test-cgent|kimberlyxie' | tail -80
temporal workflow list --namespace agent-runtime --query "WorkflowType='WorkspaceOperationWorkflow'" --limit 20
kubectl -n sandboxes get pods -o wide; kubectl -n sandboxes get pod <kata 环境的 Pod> -o yaml | grep -B2 -A8 volumeMounts
ls -la <workspaces 卷的路径>/gh-nsnzd-kimberlyxie343178/   # 基线 test-cgent 与副本各有什么
```

**做法**（定位后）：假设 1、2 修 K8s 后端的 Kata 挂载（或 Kata 后端改用 `workspace: archive`，与 Cloudflare 一样从 ObjectStore 恢复，D-06）；
假设 3 修 coding-agent。另外两件不论原因都要做：
- Workspace API 建完核对：克隆或复制之后，工作区里文件数为 0 而源不为 0 时报错（`workspace_empty_after_copy`），不留空的副本。
- Agent 侧早停：工具在工作区根上列出空目录、读不到任何文件时记一个硬信号（`environment`，附录 B.6 的扩展），Reviewer 第一轮就问人“工作区是空的”，
  不在空目录上反复规划。

**验收**：kind 里装 Kata（`AR_KIND_KATA`，参照 `AR_KIND_GVISOR`）跑 Workspace API 的建、复制、浏览与一个只读 Task；新租户在线上复测。

## 3. Task 的时间与工具调用预算没有执行（P0）

**现象**：默认策略写着每个 Task 20 分钟、50 次工具调用；task-service 把 `budget.wall_clock`、`budget.tool_calls` 传给了 Task 工作流，
Worker 只用了美元预算（`constraints.budget_usd`），另外两项没有任何地方读。日志里的 Task 跑到 17 分 29 秒还在“规划下一轮”，到了 20 分钟也不会停。

**做法**（核心链路，按 CLAUDE.md 的规矩一个 agent 改、加版本分支、补重放测试）：
- Task 工作流在每个 Round 开头与每个决策 activity 前比较 `workflow.now() - started` 与 `wall_clock`；超了按 abort 收尾（交付现有结果，说清原因），
  进度事件带 `code = budget_exhausted`、`kind = wall_clock`，应用按 code 显示“时间预算用完”。
- 长的决策 activity 与超时赛跑：用 `workflow.wait_condition` 加剩余时间的超时，超了取消 activity。
- 工具调用数由 Stage Runner 与 tool_loop 计数（与美元预算同样写进 Task State 的 constraints），超了 budget_check 拒绝，Reviewer 的 floor 只剩问人或收尾。
- 版本分支 id 追加到 `patches.AT_START` 末尾；golden 历史里补一份“超时收尾”。

**要人定的**：超时是 abort（交付部分结果）还是先问人（“时间到了，继续吗？”，问人会占用 Worker 外的等待时间）。建议 abort，与美元预算用完一致。

## 4. 规划与评审调用卡住时看不见（P0）

**现象**：日志里人回复之后，界面停在“规划下一轮”11 分钟以上，没有任何进度。决策 activity 的超时是 5 分钟、最多 5 次、间隔到 30 秒（`timeouts.py`），
网关又在同档里依次切换、每次 120 秒：最坏约 27 分钟没有任何事件。当时网关在报 `grpc_deadline_exceeded`（第 5 轮），与真模型实验里
08:23–08:54 供应商大面积 `unreachable` 是同一类问题。

**做法**：
- 决策 activity 的第 2 次及以后的尝试开头发一个进度事件（`decision_retry`：哪一步、第几次、上次的错误 code），应用显示“规划调用超时，正在重试（2/5）”。
  事件经现有的推送 activity；activity 侧拿 `activity.info().attempt`，不改工作流的命令序列。
- 给 plan_round、review_round 加 `schedule_to_close`（例如 8 分钟）：重试用完之前先到总时限；到了 Planner 回退到保守规划（一个 tool_loop 的探索节点），
  Reviewer 按规则判定（`enforce(None, …)`），不让 Task 悬着。
- 网关的 `models_exhausted` 带上次的错误 code 回给 Worker，事件里显示“模型供应商暂时不可用”。

## 5. 代码生成的说明是中文（P1，契约改动）

**现象**：英文界面里出现规则改写的理由（“按规则改为换更强的 Tier：换 Stage 形状已用满上限（1/1）：升一级”）、问人的模板（“原样继续、带指导重规划，
还是停止并用现有结果收尾？”）、工具的错误（`file_not_found：工作区里没有文件 …`）、fast 规划的 Stage 目标（“回复用户这条消息”）。

**做法**：与 task-service 的错误一样按 code 出：规则的每条理由给 `reason_code` 与参数（`cap_used`、`same_error_after_retries`…），问人的问题给
`question_code`，工具错误本来就有 code；进度事件的载荷加 `codes`（`task_event_payload.schema.json`，走 S-01），中文说明留作后备。
coding-web 按 code 翻译。fast 规划的 goal 改用用户消息的语言或只给 code。

## 6. 任务族的受控词表（P0 的剩余部分，契约改动）

**现象**：同一类请求被起了 4 个名字；fast 的 Task 没有 Task Plan，所以仍没有任务族。

**做法**：Registry 按租户登记任务族词表（平台默认一份，例如 `engineering.ci_fix`、`engineering.code_explain`、`analytics.attribution`…），
分诊的 Classify 调用（small@low，附录 H.3）只能从词表里选，选不出记 `general.unclassified` 并计数；服务端定任务族，Planner 写的不算数；
Task 工作流从分诊结果取任务族交给收尾写回（fast 也有）。词表的增补由 Learning 的报告提出（未分类里的高频请求），人审后发布。
要改 proto（Registry 的种类）与 schema，走 S-01。

**验收**：E4 那批 CI 修复请求（`docs/job-qualifications/real-model-experiments-2026-10-28/scripts/e4_tasks.py`）全部落在同一个族；
Learning 不再因任务族跳过 Task。

## 7. rollback 需要结构化的信号（P1，契约改动）

`rs_rollback_corrupted_accounts`（共享产物被改坏，期望 rollback）的证据只在 judge 的错误文字里（“账户文件被清空”），代码判不出来。
做法：judge 类模板的输出加 `regressions`（被破坏的既有产物列表，`node_output.schema.json`），非空时 floor 至少 rollback（有检查点时）。

## 8. 评测本身（P1）

- 报告记每个 role 实际用的 `model_profile_id`（这次的“退步”是靠时间线对出来的），以及回退成规则的次数；超过阈值（例如 5%）的运行标为无效。
- S-11 的上下文评测遇到一条不合法的 JSON 就整轮崩溃：单条样本记失败、继续跑。
- 两条 Reviewer 样本（`rs_debate_no_convergence`、`rs_judge_bias`）四个模型全错，请人复核标注。

## 9. 评测与线上的指标前端（结合 OpenTelemetry）

已经有的：Collector 三路发（Tempo、Langfuse、Redpanda）；Grafana 里有 Tempo 数据源与 TraceQL 指标面板（`deploy/data/dashboards/agent-runtime.yaml`）；
本机有 otelcol-contrib；span 按附录 J.1：`chat <模型>`（网关）、`execute_tool <工具>`、`invoke_workflow`、`runtime.round`、`runtime.sandbox`。
指标与日志的后端设计书还没选型（Collector 的 metrics、logs 只到 debug）。

分三期：

1. **静态页面（这一轮做）**：`scripts/eval_dashboard.py` 收集评测报告（`services/py-learning/.eval-reports/**`）、网关的计量记录与 Collector 的 span 文件，
   生成一个自包含的 HTML 页面：各次运行的门槛指标与波动、模型对比（准确率对每例成本）、失败原因分布、真 Task 的结局与花费、按模型的调用延迟（取自 `chat` span 的时长）。
2. **Grafana 面板（不加组件）**：在现有的 Tempo 数据源上加一个“模型与 Task 质量”面板：按模型的 `chat` 延迟 p50/p95 与错误率、按任务族的 Task 结局与成本、
   每个 Task 的 Round 数与 Verdict 分布（TraceQL 指标）。评测运行也发 span（`eval.run`、`eval.case`，属性带 subject、split、模型、指标值），同一套面板能看评测。
3. **指标后端（要人定）**：评测的数值指标与网关计量用 OTel metrics 发；Collector 加 `prometheusremotewrite`（或 OTLP）到一个指标库
   （Prometheus、VictoriaMetrics 单机版或 Grafana Mimir 三选一，设计书 4.13 要补选型），Grafana 里画趋势与告警（按租户的花费、供应商的不可用率）。
   这是设计书的改动，先在回复里提建议、由人更新原文档。

## 10. 跑主流 Benchmark（要人定预算）

用 Runtime 跑 SWE-bench Verified 的子集（例如 50 题）与 Terminal-Bench 的子集：适配器把题目变成 Task（工作区 = 题目仓库的快照，验收 = 题目的测试），
沙箱镜像按题目语言选；每题预计 $0.3–1.0（按 E4 的 $0.37/Task 估）。产出：与公开榜单同口径的解决率，以及本项目自己的指标（Round 数、Verdict 分布、成本）。

## 11. 细化与人定（2026-10-28）

### 11.1 人定的

| 项 | 决定 |
| --- | --- |
| 第 3 节：时间、工具调用预算用完 | **先问人**，不直接 abort（建议是 abort，人选了问人）。细则见 11.4 |
| 第 3～7 节的契约改动 | 合成**一份 S-01 草案**（11.3），人审过再按顺序实现 |
| 第 2 节的线上诊断 | 本机没有 kubeconfig：**先做代码侧的防护**；诊断由人执行，把输出贴回来再定位 |
| 9.3 指标后端 | **VictoriaMetrics 单机版**（设计书 4.13、4.15 的改动稿见 11.6） |
| 第 10 节 Benchmark | 预算未定，不做 |

### 11.2 各节挂在哪个工作项下（CLAUDE.md“怎么接一个任务”第 1 步）

| 节 | 工作项 |
| --- | --- |
| 2 | 复制后核对：D-06（Workspace）的后续；environment 硬信号：S-10 的后续（附录 B.6 的扩展） |
| 3 | S-03、S-04、S-10 的后续：核心链路 Task Loop → Stage Runner，同一时间只由一个 agent 改 |
| 4 | S-03（决策 activity 的超时与重试）、T-11b（进度事件）、T-05（网关）的后续 |
| 5 | S-01；S-10（规则的理由）、T-10（工具错误）的后续；coding-agent 仓库按代码翻译 |
| 6 | S-01；T-08（Registry）、S-09（分诊）、T-16（Learning）的后续 |
| 7 | S-01；S-10 的后续 |
| 8 | S-08 的后续 |
| 9.2、9.3 | T-15 的后续 |

### 11.3 S-01 草案（待人审）

只加不改：`buf lint`、`buf format`、对兼容性基线的 `buf breaking` 都通过，契约测试 472 个全过（本机没有 Postgres，那部分 Go 测试按
`AR_ALLOW_MISSING_TOOLS=1` 跳过）。兼容性基线没动，人审通过后再 `buf build -o proto/breaking-baseline.binpb`。
task-service 的 `GetTaskFamilies` 审过之前回 Unimplemented。

| 文件 | 改动 | 给哪一节 |
| --- | --- | --- |
| `proto/.../taskapi/v1/task_api.proto` | `TaskEventType` 加 `TASK_EVENT_TYPE_DECISION_RETRY = 13` | 4 |
| `proto/.../registry/v1/registry.proto` | `RegistryKind` 加 `REGISTRY_KIND_TASK_FAMILIES = 6`；消息 `TaskFamilies`（version、document、platform_default）；方法 `GetTaskFamilies` | 6 |
| `schemas/defs.schema.json` | `progress_event` 加 `decision_retry`；新 `decision_step`（plan_round、review_round、tool_loop_step）、`budget_kind`（usd、wall_clock、tool_calls）、`message_code`（snake_case，≤64）、`message_params`（键 snake_case，值是标量或短字符串列表，≤12 项）；`hard_signal` 加 `environment` | 2～5 |
| `schemas/task_event_payload.schema.json` | 新事件 `decision_retry`（round_seq、step、attempt≥2、max_attempts、tool_loop_step 时必带 node_id、error_code、error）；`verdict` 加 `reason_code`、`params`；`human_input_required` 加 `question_code`、`params`，question_id 可以是 `_budget`；`node_finished` 加 `error_code`、`params`；`tool_status` 加 `error_code`；`stage_started` 加 `goal_code` | 3～5 |
| `schemas/verdict.schema.json` | 加 `reason_code`、`reason_params`（代码给出或改写理由时） | 5 |
| `schemas/task_state.schema.json` | `constraints` 加 `wall_clock_min`、`tool_calls`；新 `budget_used`（wall_clock_min、tool_calls、extensions）；`rounds[]` 加 `tool_calls` | 3 |
| `schemas/node_output.schema.json` | 加 `regressions`（judge 发现的被改坏的既有产物：artifact、evidence），只交给 Reviewer | 7 |
| `schemas/task_families.schema.json`（新） | 任务族词表：families[]（id、description、examples）；`general.unclassified` 保留，不能列 | 6 |

审的时候请看这几处取舍：

1. **代码用 pattern，不用 enum**：新增代码不必再走 S-01；初始取值在 11.5，客户端翻不了就用同一事件里的文字。
2. **问人的预算问题沿用 `escalation` 与三种回复**：coding-web 不用改就能回复，只多一个 question_code 可翻译。
3. **`regressions` 放在 node_output 顶层**，不放进 result：不传给下游，也不依赖各个 judge 模板的 output_schema。
4. **任务族词表是 Registry 的新种类**，不并进租户策略：租户有自己的策略时不用把整份词表抄一遍；与租户策略一样有平台默认的一份。
5. **`hard_signal` 加 `environment`**、`budget_used` 的口径（不算等人的时间）是设计书的改动，见 11.6。

### 11.4 第 2～4 节的细化（与上面原文不同的地方加粗）

**第 3 节：时间与工具调用预算**

- 关口：每个 Round 开头（规划或重跑之前）查两项预算；Stage 里 tool_loop 每一步之前查时间、每次工具调用之前查次数，用完就停这个
  节点（failed、denied，error_code `budget_exhausted`，params 写明哪项、上限、用量），交给 Reviewer。**评审前不拦**：评审可能判 done，
  拦了反而交付不了。**决策 activity 不再与剩余时间赛跑**：问人之后时间不是硬上限，单次决策由第 4 节的总时限兜底；单个长 activity
  （例如 10 分钟的 Node）不中途打断。
- 问人：question_id `_budget`，kind `escalation`，回复限定三种，question_code `budget_exhausted`。`continue_as_is`：用完的那项**加一份
  （与原预算相同）**，接着规划；`replan_with_guidance`：同样加一份，Planner 的 last_round 带上人的指导；`stop_and_wrap_up` 与超时
  没回复：abort，用现有结果收尾，verdict 事件的 reason_code 是 `budget_stopped`、`budget_no_reply`。每次用完都问，次数不设上限。
- **时间只算 Agent 在跑的时间**：所有等人回复的时间（human 节点、审批、Reviewer 升级、预算问题，从发问到收到回复）不算。
- **工具调用只数 tool_loop 里的调用**（含 sub_agent、子图里的）：program 节点在 activity 里调的工具工作流看不见，这一期不数。
- 美元预算不变：仍由 Reviewer 的 floor 管（用到 100% 只能问人或收尾）。
- 预算为 0 或没给：不限。新的运行走版本分支 `BUDGETS_PATCH`，追加在 `patches.AT_START` 末尾；golden 补 `budget_ask_human`、
  `budget_tool_calls_stop`。

**第 4 节：决策调用卡住时看得见**

- **Python 的 activity 推不了进度事件**（推送是 Go 的 Session Worker 上的 publish_task_event，只能由工作流排），所以原文“activity 侧拿
  attempt、不改命令序列”的做法行不通：**重试挪进工作流**，放在版本分支 `DECISION_RETRY_PATCH` 后面（追加在 `BUDGETS_PATCH` 之后）。
  plan_round、review_round、tool_loop_step 在服务端只试一次，工作流按原来的退避重试，最多 `DECISION_MAX_ATTEMPTS`（5）次；第 2 次起
  每次尝试之前推 `decision_retry`。不可重试的错误不重试、不推。
- 总时限 `DECISION_TOTAL`（10 分钟，从第一次尝试算，够两次 5 分钟的完整尝试；剩下不到 30 秒就不再试）：尝试用完或到了总时限，规划用不调模型的保守规划再调一次
  （`PlanRoundInput.no_model`），评审只按规则判定（`ReviewRoundInput.rules_only`，理由带 reason_code `rule_fallback`）；tool_loop 的一步
  照旧让节点失败。
- 错误代码取 activity 失败的类型（ApplicationError.type），超时记 `activity_timeout`；网关的 models_exhausted 在 details 里带
  `last_error_code`（最后一次失败的代码），Worker 把它当错误代码。

**第 2 节：代码侧的防护**

- 复制后核对：新工作区（不算 `.git`）一个文件也没有、而源有文件时报 `workspace_empty_after_copy`，行与环境都撤掉；源本来就空照常复制。
  这只抓得到假设 2。**假设 1（Kata 没挂上卷，基线本身就是空的）复制核对抓不到**（0 对 0）：诊断确认是假设 1 时，再加“建完之后用
  一个新的只读环境再数一遍文件”的核对。
- Agent 侧早停：tool_loop 里 `repo_list` 列工作区根（path 为空、`.` 或 `/`）得到空目录，这个 Node 的结果带硬信号 `environment`。
  Reviewer 的 floor 见到它**只允许 done、问人或收尾**（不是一律问人：交付“工作区是空的”这种答复是对的），理由与问题的代码是
  `workspace_empty`。
- **kind 里装 Kata 的验收要嵌套虚拟化**，本机（Hyper-V 里的 Windows + WSL 2）大概率跑不了，放到能跑 Kata 的机器或线上复测。

### 11.5 代码表（message_code 的初始取值）

| 用在 | 代码 |
| --- | --- |
| verdict 的 reason_code | 规则：`rule_fallback`、`rule_node_failed`、`rule_failed_twice`、`rule_failed_thrice`、`rule_denied`、`rule_same_error_after_retry`、`rule_merge_conflict`、`rule_replan_no_progress`、`rule_unverified_quality`、`rule_budget_spent`、`rule_budget_alarm`、`rule_cap_reached`、`rule_criteria_not_met`、`rule_no_deliver`、`rule_no_checkpoint`、`rule_unknown_recipe`、`rule_recipe_over_ceiling`、`rule_regression`；工作流：`budget_stopped`、`budget_no_reply`、`human_stopped`、`human_no_reply`、`finish_rejected`、`workspace_empty` |
| human_input_required 的 question_code | `budget_exhausted`、`rule_stuck`、`recipe_over_ceiling`、`finish_rejected`、`workspace_empty` |
| node_finished、tool_status 的 error_code | `budget_exhausted`、`max_iterations`、`repeated_error`、`tool_not_allowed`、`tool_rejected`、`activity_timeout`，以及工具自己的代码（`file_not_found` 等） |
| decision_retry 的 error_code | activity 失败的类型（`models_exhausted`、`grpc_deadline_exceeded` 等）、`activity_timeout` |
| stage_started 的 goal_code | `reply_to_message`（fast 规划） |

### 11.6 设计书的改动建议（由人更新原文档后重新导出）

1. 附录 B.6：硬信号加 environment（工作区或执行环境本身不可用，换做法没用）；有它时只能交付、问人或收尾。
2. 附录 B.1、B.4、C.3：Task State 加 budget_used；时间预算只算 Agent 在跑的时间；时间与工具调用预算用完先问人，回复三种，
   继续时加一份；B.4 abort 一行的“预算用尽”限定为美元预算或人选择停止。
3. 附录 D.2：节点输出加 regressions（judge 发现的被改坏的既有产物），只交给 Reviewer；B.8 加一条：regressions 非空时至少 rollback。
4. 3.1.2 Registry：存放对象加任务族词表（租户一份，平台默认一份）；附录 C 的分诊只能从词表里选，选不出记 general.unclassified。
5. 4.14：进度事件加 decision_retry；给人看的说明一律带代码与参数，客户端按代码翻译。
6. 4.13、4.15：指标后端选 VictoriaMetrics 单机版。Collector 的 metrics 管道经 `prometheusremotewrite` 写进去；Grafana 加
   Prometheus 类型的数据源；保留 30 天。网关计量（token、成本、延迟、错误率）与评测的数值指标用 OTel metrics 发，按租户、模型、
   任务族打标签（不带用户数据）。选它而不选 Prometheus：写入走 remote write 不用抓取，单进程、单卷；不选 Mimir：单节点用不上它的
   分布式组件。

### 11.7 顺序与验收测试

1. ~~人审 11.3 的 S-01 草案，通过后更新兼容性基线~~（2026-10-28 通过，基线已更新）。
2. ~~第 3、4 节（核心链路，一个 agent 改）~~（已实现，见下面“实现记录”）。测试：`services/py-worker/tests/test_task_budgets.py`、`test_decision_retry.py`、
   `test_stage_replay.py` 的 `TMP_CASES`（golden 由 `AR_UPDATE_GOLDEN_HISTORIES=1` 补录）、
   `services/go-model-gateway/internal/gateway/tmp_fixes_test.go`。
3. ~~第 2 节的代码侧~~（已实现）：`services/py-worker/tests/test_empty_workspace.py`。
4. 第 5 节（Worker 各处出代码；coding-agent 按代码翻译）、第 6 节、第 7 节：草案审过后先写测试、停下确认，再实现。
5. 第 8 节；9.2（Grafana 面板与 eval span）；9.3（VictoriaMetrics 的部署、Collector 与面板）。
6. 第 10 节等预算。

**实现记录（2026-10-28）**

- 第 3 节：`workflows/task.py`（Round 开头的 `_budget_gate`、不算等人时间的 `_active_s`、`budget_used`）、`workflows/stage_runner.py`
  （`_budget_stop`、tool_loop 与子工作流的 `tool_calls`）。版本分支 `BUDGETS_PATCH`。
- 第 4 节：`workflows/decisions.py`（工作流里的重试与错误代码）、`PlanRoundInput.no_model`、`ReviewRoundInput.rules_only`
  （`Planner.plan_without_model`、Reviewer 跳过模型、规则判定的理由带 `rule_fallback`）；网关 `providererr.Error.LastCode` →
  details 的 `last_error_code`，Python 客户端把它写进错误说明。版本分支 `DECISION_RETRY_PATCH`。
- 第 2 节：`workspace/operations.py` 的复制后核对（源的文件数取源的只读环境里的清单：存档记录的 files 连 .git 一起数）；
  tool_loop 的 `environment` 标记与 Reviewer floor 的 `workspace_empty`。
- 验收测试的三处改动（测试本身的错，不是行为的改动）：时间预算的测试把余量放大（3 秒预算、3.5 秒的节点、人 6 秒后回复）；
  问题选项与列表比较；撤掉的副本在业务库里留一行 `deleted`，按“没有或已删除”判。
- 没做：sub_task 的子 Task 照搬父 Task 的预算（它自己的时间与工具调用另算、也会自己问人）；program 节点在 activity 里调的工具不数；
  模板大批量的几批并发时各自按启动时的剩余次数查，合起来可能略超。
