# TMP 计划：线上日志的问题、真模型实验的遗留与评测可观测（设计稿）

> 状态：2026-10-28 人确认开工，要人定的几项已定（第 11 节）。第 3～7 节的 S-01 草案人审通过（兼容性基线已更新）；
> 第 2～9 节已实现（11.7～11.9；9.1 由同事先做）。第 10 节的第一期（SWE-bench 的适配器）已实现（11.10，S-01 人审通过、
> 兼容性基线已更新）；真跑要预算与有 Docker 的环境，Terminal-Bench 是第二期。来源两处：
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
| 第 10 节 Benchmark | 预算未定，不做。2026-10-29 改为：先写 SWE-bench 的适配器与离线验收测试（不花钱），Terminal-Bench 放第二期；真跑之前再定预算与在哪儿跑（11.10） |

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
| 10 | S-08（评测）与 T-13（真模型端到端）的后续；它要的 Workspace API 改动是 D-06 的后续，走 S-01（11.10） |

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
| verdict 的 reason_code | 规则：`rule_fallback`、`rule_node_failed`、`rule_failed_twice`、`rule_failed_thrice`、`rule_denied`、`rule_same_error_after_retry`、`rule_merge_conflict`、`rule_replan_no_progress`、`rule_unverified_quality`、`rule_budget_spent`、`rule_budget_alarm`、`rule_cap_reached`、`rule_criteria_not_met`、`rule_no_deliver`、`rule_no_checkpoint`、`rule_unknown_recipe`、`rule_recipe_over_ceiling`、`rule_regression`；工作流：`budget_stopped`、`budget_no_reply`、`human_stopped`、`human_no_reply`、`finish_rejected`、`workspace_empty`、`policy_refused`（租户策略拒绝了 Task，params `{detail}`：原来的理由，截到 300 个字符） |
| human_input_required 的 question_code | `budget_exhausted`、`finish_rejected`；规则生成的问题与 Verdict 的 reason_code 相同（11.8） |
| node_finished、tool_status 的 error_code | `budget_exhausted`、`max_iterations`、`repeated_error`、`tool_not_allowed`、`tool_rejected`、`activity_timeout`、`upstream_failed`，以及工具自己的代码（`file_not_found` 等） |
| decision_retry 的 error_code | activity 失败的类型（`models_exhausted`、`grpc_deadline_exceeded` 等）、`activity_timeout` |
| stage_started 的 goal_code | `reply_to_message`（fast 规划）、`run_recipe`（Recipe 规划，params `{recipe}`）、`explore_task`（保守规划） |
| stage_started 的 exit_when_code（2026-10-30 S-01 人审通过，`stage_started` 加 `exit_when_code`、`params`） | `reply_given`（fast 规划）、`recipe_delivered`（Recipe 规划）、`node_result`（保守规划，params `{node}`） |
| Task State 的 done_when_code（2026-10-30 S-01 人审通过，`task_state` 加这一项） | `answer_question`（调用方没给 done_when 时的默认说明） |

abort 的交付没有 schema（`TaskWorkflowResult.deliverable` 是 JSON 载荷），客户端靠下面的约定翻译（2026-10-30 与上面一起人审）：理由是代码给的时候（Verdict 带 `reason_code`）多一个 `codes`，
`{reason_code, params?, nodes}`，`nodes` 是这一轮各 Node 的状态，字段同 `node_finished`（`node_id`、`role`、`status`，有的话 `failure_class`、`error`、`error_code`、`params`），按 id 排序；
`reason`、`completed`、`not_completed`、`partial` 照旧，作后备。模型自己写的 abort 没有 `codes`，文字是模型按用户的语言写的。

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

### 11.8 第 5～7 节的细化与验收测试（2026-10-28 人确认，已实现）

**第 5 节：代码**（测试 `services/py-worker/tests/test_message_codes.py`）

- 规则给出或改写的 Verdict 带 `reason_code` 与 `reason_params`，取**决定最终去向的那一条规则**的代码；模型自己的理由、规则没改的不带。
  参数：`rule_failed_twice`、`rule_failed_thrice` 是 `{node, times}`；`rule_denied` 是 `{nodes}`；`rule_cap_reached` 是
  `{step, used, cap, next}`；`rule_no_checkpoint` 是 `{checkpoint}`；`rule_unknown_recipe` 是 `{recipe}`；`rule_recipe_over_ceiling`
  是 `{recipe, risk, ceiling}`；`rule_budget_spent` 带 `spent_ratio`；`finish_rejected` 是 `{count}`。
- **question_code 不另起一套**：问题由规则生成时，`human_input_required` 的 `question_code` 就是 Verdict 的 `reason_code`；模型写的问题不带
  （11.5 里原来的 `rule_stuck` 不用了）。
- 问人后停止或没回复，工作流**另推一个 abort 的 verdict 事件**（`human_stopped`、`human_no_reply`），客户端看得到为什么收尾。
- `node_finished` 的 `error_code`：StageResult 有就用；否则取说明开头“代码：”（中文或英文冒号）里 snake_case 的代码；`budget_check`
  记为 `budget_exhausted`（params 的 budget 是 usd）；上游没成功而跳过的是 `upstream_failed`（params `{node}`）。`tool_status` 同样从工具
  错误的开头取。
- 代码写的固定规划（fast、Recipe、保守规划）：`PlanRoundOutput` 的 `goal_code`、`exit_when_code`、`params`（activity 的输出，不是契约），
  `stage_started` 带出；文字留着作后备。原地重跑沿用这个 Stage 的代码；只改 activity 的入参，不改命令序列，不加版本分支。`run_recipe` 的 params 只有
  Recipe，不放用户消息（`message_params` 的字符串上限 300，消息就显示在计划上方）。
- 默认的 done_when：`done_when_code` 只在 Task State 的查询里带（`done_when` 等于 `DEFAULT_DONE_WHEN` 时），不进工作流里的 Task State：Planner 的提示整份带 Task State，
  这样提示一个字也不变，评测不受影响。没有让客户端自己传 `done_when`：那会改变 Reviewer 的验收条件。
- abort 的交付：`codes` 见 11.5 的约定；下一条消息的对话（`session/transcript.py`）不带它。sub_task 的结果交给父 Stage 时还带着（少见，内容不多）。都只改工作流的结果与查询，不改命令序列，不加版本分支。
- 仍是中文、没有代码的（2026-10-30）：Node 的错误没有代码时用它自己的文字（进度事件也一样）；`policy_refused` 的 `detail` 是原文（租户 Hook 的话，或少见的配置错误）；
  桩 Planner 的文字（只用于开发与端到端测试）。之前结束的 Task 没有代码，仍显示原来的文字。
- 不在本仓库：coding-web 按代码翻译（coding-agent 仓库），本节做完后另起。

**第 6 节：任务族词表**（测试 `test_task_families.py`；Go：`internal/registry/tmp_task_families_test.go`、
`cmd/task-service/tmp_task_families_seed_test.go`）

- Registry：`TASK_FAMILIES` 的规则同租户策略（发布即生效、不走 Promote；平台默认不带租户、名为 default；租户自己的不带名字、优先）；
  id 重复报 `duplicate_task_family`；开发种子的 kind 是 `task_families`，`deploy/dev/registry-seed.json` 加一份平台默认的词表。
- Worker：`GrpcRegistry.get_task_families`；规划上下文的 `task_families`（Registry 里没有词表时为空，照旧规划，不算失败）。
- 有词表时任务族由服务端定：分诊给的不在词表里记 `general.unclassified`；Task Plan 里规划模型写的不算数；分诊的提示列出词表的 id 与说明。
- `PlanRoundOutput.task_family` 带出这一轮定的任务族（fast 也有）；收尾写回优先用它，没有时用 Task Plan 里的。
- 没覆盖：未分类的计数（Learning 的报告里统计）、E4 那批 CI 修复请求“全部落在同一族”（要真模型跑）、Learning 提出词表增补。

**第 7 节：regressions**（测试 `test_regressions.py`）

- role 为 judge 的 model 节点：Node Executor 按 `{result, regressions}` 的外壳向模型要输出，拆开后 `result` 照旧、`regressions` 另放（不合格时
  带着校验错误重问一次）；program 执行的 judge 可以直接在输出顶层给；其他 role 不加外壳，给了也丢掉。
- Stage Runner 把它放进 StageResult，只交给 Reviewer，下游只看得到 result。
- Reviewer 的 floor：有 regressions 时至少 rollback，退到最近的检查点；没有检查点或 rollback 已用满上限时问人。代码 `rule_regression`，
  参数 `{artifacts}`。
- 评测样本 `rs_rollback_corrupted_accounts` 要在 judge 的结果里补上 regressions 才能由代码判对：那是评测集的标注改动，要人确认，没改。

**实现记录**

- 第 5 节：`reviewer/rules.py`（floor 的每条规则带代码与参数；`make` 带代码；`Floor.code`、`reason_code`）；`workflows/events.py`
  （`code_of` 从“代码：说明”取代码，`stage_started` 的 `goal_code`）；`workflows/task.py` 的 `_stopped`，版本分支 `CODES_PATCH`
  （`tmp-message-codes`，追加在 `AT_START` 末尾；第 4 节测试里“最后两项是第 3、4 节的分支”改成“紧接着 PROGRESS_DETAIL_PATCH”）；
  golden 补 `codes_human_stopped`。
- 第 6 节：Go `internal/registry`（种类规格、`perTenant`、`duplicate_task_family`、`GetTaskFamilies`、种子的 `task_families`）；
  `deploy/dev/registry-seed.json` 加平台默认的词表（9 族）；Worker `GrpcRegistry.get_task_families`、`planner/context.TaskFamily`、
  分诊提示、`planner._settle_family`、`PlanRoundOutput.task_family`、收尾写回优先用它。
- 第 7 节：`executor/node_executor.judge_envelope`；Stage Runner 只收 judge 的 regressions；floor 的 `rule_regression`（至少 rollback，
  只允许 rollback、问人、收尾）；`make("rollback")` 退到最近的检查点，没有检查点时问人。
- 没做：质量包装（revise 等）里的 judge 发现的 regressions 还没有带到被包装的 Node 上；sub_task 等子 Task 不推进度事件，不受影响。
测试辅助的两处改动：`registry_fakes.FakeRegistry` 加 `GetTaskFamilies`；`stage_harness.Script` 加 `goal_code`、`task_family`（设了才传给
PlanRoundOutput，不影响其他测试）。

### 11.9 第 8、9.2、9.3 节的细化与验收测试（2026-10-29 人确认，已实现）

**第 8 节：评测本身**（测试 `services/py-learning/tests/test_eval_validity.py`）

- 报告加 `models`：每个 role（CallContext.role 的小写名，route、plan、judge……）实际用到的 `model_profile_id` 与次数，由
  `CountingGateway` 记成功的调用。
- 报告加 `metrics.fallbacks`：`{planner, reviewer, decisions, rate}`。回退取 Planner 的 `PlanResult.fallback` 与 Reviewer 的
  `ReviewResult.fallback`；分母只算经模型的那一侧（`planner_subject` 的 Reviewer 是基线，不算）。回退率超过阈值（默认 5%，
  `run_eval(max_fallback_rate=…)`、命令行 `--max-fallback-rate`）的运行 `valid = false`、写 `invalid_reason`，摘要第一行写“无效”。
  不经模型的被测对象（baseline、oracle）一律有效。
- `--repeat` 的汇总：中位数、最小、最大只取有效的运行，`values` 仍列出每一次；`invalid_runs` 记无效的次数，全部无效时没有中位数。
- S-11 的压缩评测：一条样本出错（例如模型的 JSON 解析不了）只记这一条——行里带 `error`，指标多一项 `errors`——整轮继续。
- 要人做的：`rs_debate_no_convergence`、`rs_judge_bias` 两条 Reviewer 样本四个模型全错，请复核标注。

**第 9.2 节：面板与评测的 span**（测试 `tests/deploy/test_observability_quality.py`、`services/py-learning/tests/test_eval_telemetry.py`）

- 新面板 `deploy/data/dashboards/quality.yaml`（uid `agent-runtime-quality`，“模型与 Task 质量”）。查 Tempo 的：按模型的 chat
  延迟 p50/p95（`quantile_over_time(duration, …)`）与错误率、按任务族的 Task 结局与花费（`invoke_workflow` 的
  `runtime.task_family`、`runtime.task_status`、`runtime.cost_usd`）、每个 Task 的 Round 数（`runtime.rounds`）、Verdict 分布
  （`runtime.round` 的 `verdict`）、评测运行（`eval.run`）。查 VictoriaMetrics 的趋势见 9.3。
- **发现的问题（实现时更正）**：原面板“模型花费（美元，按租户）”写的 `name = "chat"` 不是一条也选不到——Worker 的 model 节点的 span
  就叫 chat——但只选到 Node 的调用，漏了 Planner、Reviewer 与 Context Builder 直接经网关的调用（网关的 span 叫“chat {模型}”）。
  两边的 span 都带 `gen_ai.operation.name = chat`，只按它选会重复计，所以**模型调用一律按网关的服务名选**
  （`resource.service.name = "model-gateway"`，新面板再加 `span.gen_ai.operation.name = "chat"`）；原面板的花费改成网关的全部调用
  （含 embeddings）。`tests/deploy/test_cloud.py` 核对面板名字时因此也读网关的代码。
- 评测运行发 span：一次运行一个 `eval.run`（集、被测对象、划分、是否有效、数值指标、用到的模型），下面每条用例一个 `eval.case`
  （id、类别、是否通过、花费）；留出集不发 `eval.case`；不带用例原文。另发指标 `runtime.eval.runs`（计数）与 `runtime.eval.score`
  （每个数值指标一次观测，属性 `eval.metric`）。命令行设了 `OTEL_EXPORTER_OTLP_ENDPOINT` 时经 OpenTelemetry，按平台作用域 `evals` 绑定。

**第 9.3 节：VictoriaMetrics**（测试 `tests/deploy/test_observability_quality.py`、`test_collector_config.py` 改了两处、
`services/go-model-gateway/internal/gateway/tmp_metrics_test.go`）

- 部署：`vm/victoria-metrics-single`，装在 observability，`deploy/data/values/victoria-metrics.yaml` 保留 30 天、开持久卷；版本写进
  `deploy/cluster/versions.conf` 的 `VM_CHART_VERSION`（实现时按发布页核对，取发布满两周的）。
- Grafana：加 Prometheus 类型的数据源（uid `victoriametrics`），Tempo 仍是默认；告警两条——模型供应商不可用率、租户每小时花费。
- Collector：metrics 管道 `memory_limiter → attributes/redact → deltatocumulative → batch → prometheusremotewrite/victoriametrics`
  （应用按 delta 时间性发，remote write 要累计值）；地址由 Chart 的 `external.victoriametrics.remoteWrite` 注入
  `VM_REMOTE_WRITE_ENDPOINT`。日志仍只到 debug。
- **指标由应用经已有的 Telemetry 接口发**（`Count`、`Observe`，S-02 早就有，OpenTelemetry 实现也已经导出 OTLP 指标），不在 Collector
  里从 span 推算：Model Gateway 每次调用 `runtime.model.calls`（outcome、error.type、gen_ai.operation.name、gen_ai.provider.name、
  gen_ai.response.model、role、tier，租户由绑定加上）与 `gen_ai.client.operation.duration`（**实现时改成每次都记**，失败的也记，
  同 GenAI 语义约定），上游计了费时（含计了费但网关判为失败的）再记 `runtime.model.cost_usd`、`gen_ai.client.token.usage`
  （input、output）。Prometheus 里的名字是点换下划线，计数器加 `_total`，直方图有 `_sum`、`_count`、`_bucket`。
- 风险：`tenant_id` 作标签，开放注册后租户数涨上去，单机 VictoriaMetrics 的序列数跟着涨；眼下规模没问题，到时候按租户聚合或降采样。
- 本机覆盖不到：`helm` 与 `otelcol-contrib` 不在 PATH，Chart 渲染与 Collector 自检的几条测试跳过（CI 里有）；没有 kubeconfig，
  集群上的安装没有试。

**实现记录**

- 第 8 节：`evals/model.py`（PlannerOutput、ReviewerOutput 加 `fallback`，不经模型的为 None）；`evals/worker_subject.py`
  （`CountingGateway.models` 按 role 记成功调用的 model_profile_id，`role_name`）；`evals/runner.py`（`models`、`metrics.fallbacks`、
  `valid`、`invalid_reason`、`max_fallback_rate`，摘要第一行写“这次运行无效”）；`evals/repeat.py`（中位数只取有效的，`invalid_runs`）；
  `evals/context.py`（一条样本出错只记这一条：题全算错、不算重复操作，行里带 `error`，指标 `errors`）；命令行 `--max-fallback-rate`。
- 第 9.2 节：`evals/runner.py` 收 `telemetry`：`eval.run` 与每条用例的 `eval.case` span（留出集不发），`runtime.eval.runs`、
  `runtime.eval.score`；命令行设了 `OTEL_EXPORTER_OTLP_ENDPOINT` 才发（服务名 learning，平台作用域 evals），导出失败只提示、不让评测失败。
  面板 `deploy/data/dashboards/quality.yaml`；原面板的模型花费见上面的更正。
- 第 9.3 节：Model Gateway 的 `traced.metrics`（`gateway.go`）；`gen_ai.token.type` 的值是 input、output 时不脱敏（键名含 token，
  Python 与 Go 的规则一起改）；`deploy/data/values/victoria-metrics.yaml`、`install.sh`、`versions.conf` 的 `VM_CHART_VERSION=0.49.0`
  （2026-10-12 发布；Service 名按 Chart 的旧命名核对过）；Grafana 的数据源与两条告警（阈值是起步值：供应商 15 分钟失败率超过两成且
  至少 10 次调用、持续 10 分钟；租户一小时花费超过 5 美元）；Collector 的 metrics 管道；Chart 的 `external.victoriametrics.remoteWrite`；
  `scripts/check_chart_values.py` 加上 VictoriaMetrics 的 Chart（2026-10-29 跑过，七个 Chart 的键名都对得上）。
- 没覆盖：告警没有配 contact point（只在 Grafana 里看）；“供应商不可用率”按调用算，网关切换成功的那次不算失败，要看每次尝试得另加
  指标；`helm`、`otelcol-contrib` 不在本机，Chart 渲染与 Collector 自检的测试在本机跳过，集群上没有试装；TraceQL 与 PromQL 的查询
  只核对了用到的名字，没在真的 Tempo、VictoriaMetrics 上跑过。
- 本机的共享虚拟环境按 `uv sync --all-packages --locked` 补装了锁定的依赖（之前缺 PyYAML、OpenTelemetry 等）。

### 11.10 第 10 节第一期：SWE-bench Verified 的适配器（2026-10-29 人确认，已实现）

范围：用 Runtime 跑 SWE-bench Verified 的子集，产出官方判分工具能直接用的 predictions，判分用官方工具，报告同时给出与榜单同口径的
解决率和本项目自己的指标。这一期只写代码与离线验收测试，不花钱；真跑要先定预算，并且要在有 Docker 的地方（云上集群或装了 Docker
的机器；本 VM 没有 Docker，也没有 WSL）。Terminal-Bench 放第二期：它的 agent 要在题目自己的容器里操作，Runtime 现在的沙箱都是
自己起环境，要另加一种后端。

**核对过的官方格式**（SWE-bench v5.0.1，2026-08-18 发布，满两周）：

- 题目：Hugging Face 的 `SWE-bench/SWE-bench_Verified`，500 题；字段有 `instance_id`、`repo`、`base_commit`、`problem_statement`、
  `hints_text`、`image`（例如 `swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest`）、`patch`、`test_patch`、
  `FAIL_TO_PASS`、`PASS_TO_PASS` 等。datasets-server 的 rows 接口（`https://datasets-server.huggingface.co/rows`）只用标准库就能取。
- 镜像名：`swebench/sweb.eval.x86_64.<instance_id>:latest`，`__` 换成 `_1776_`，全小写；题目里有 `image` 时直接用它。
- predictions：JSONL，每行 `instance_id`、`model_name_or_path`、`model_patch`。
- 判分：`python -m swebench.harness.run_evaluation --dataset_name <本地 .jsonl> --predictions_path … --run_id … --report_dir …
  --instance_ids … --max_workers …`（`--modal true` 在 Modal 上跑，不要本机 Docker，但花钱）；报告是
  `<report_dir>/<model_name_or_path，/ 换成 __>.<run_id>.json`，有 `resolved_ids`、`unresolved_ids`、`empty_patch_ids`、
  `error_ids`、`infra_failure_ids`、`incomplete_ids` 等。
- 官方工具不进本仓库的依赖（设计书 4.15 之外）：判分时用 `--python` 指一个另装了 `swebench` 的 Python。

**S-01：Workspace API（D-06 的后续；2026-10-29 人审通过，兼容性基线已更新）**。现在的 CreateWorkspace 只能克隆到分支或标签，也不能在克隆时再写文件；SWE-bench
要停在 `base_commit`，还要放进题目的运行环境定义。两处都是只加不改，字段编号不变，`buf breaking` 只看结构，不受影响：

- `CreateWorkspaceRequest.ref`：注释改成“分支、标签，或完整的提交（40 位或 64 位十六进制）。给提交时只取到这个提交为止的历史，
  不取远端的分支与标签（克隆下来看不到这个提交之后的提交），HEAD 分离在这个提交上（branch 为空）”。Worker 的做法是
  `git init` → `git fetch --no-tags <远端> <提交>` → `git checkout --detach FETCH_HEAD`。
- `CreateWorkspaceRequest.files`：注释改成“建空工作区时写入的文件，写完做一次初始提交；与 repo_url 一起给时，克隆之后写入、
  另做一次提交（提交说明 Add workspace files），workspace.head 是这次提交。与 from_workspace_id 互斥”。task-service 的
  `conflicting_sources` 改成只拦 from_workspace_id 与另两种同给。

不改的话只能把每道题的仓库截到 base_commit、推到一个自己的 https 仓库再克隆，要在 GitHub 上建镜像仓库，比改两条注释重。

**适配器**：`services/py-learning/src/agentruntime_learning/benchmarks`（`swebench.py`、`client.py`、`__main__.py`）。命令：

```text
python -m agentruntime_learning.benchmarks swebench fetch --out <instances.jsonl> [--dataset SWE-bench/SWE-bench_Verified] [--split test]
python -m agentruntime_learning.benchmarks swebench run --instances <文件> --out <目录> (--limit N [--seed S] | --ids <文件>)
    --task-api <地址> --budget-usd X --task-usd-limit Y [--concurrency 2] [--task-timeout-min 60] [--model-name agent-runtime]
    [--hints] [--environment official|none] [--repo-url-template https://github.com/{repo}.git] [--keep-workspaces]
python -m agentruntime_learning.benchmarks swebench grade --out <目录> --python <装了 swebench 的 Python> [--max-workers 4] [--modal]
python -m agentruntime_learning.benchmarks swebench report --out <目录>
```

Task API 的令牌取环境变量 `AR_TASK_API_TOKEN`，不写进输出目录。输出目录：`run.json`（这次跑的设定与选中的题）、`instances.jsonl`
（选中的题，判分时当 `--dataset_name`）、`records/<instance_id>.json`（每题一份）、`predictions.jsonl`、`grading/`、
`report.json` 与 `report.md`。

每题的流程：

1. 建工作区：`CreateWorkspace(repo_url=模板代入 repo, ref=base_commit, files=运行环境定义)`；记下返回的 head。工作区 id 是
   `swb-<instance_id 的小写化>-<run_id 与 instance_id 的哈希>`，同一次运行里不重名，换一个 run_id 不撞上留着的工作区。
2. 运行环境（`--environment official`，默认）：写 `.devcontainer/devcontainer.json`（`build.dockerfile` 指向
   `.devcontainer/agentruntime.Dockerfile`）与这个 Dockerfile：`FROM <题目的镜像>`，PATH 换成 conda 环境 testbed，`/testbed`
   换成指向工作区挂载点（默认 `/workspace`）的符号链接。官方镜像把仓库以开发模式装在 `/testbed`，不换的话测试跑的是镜像里没改过的
   代码，而且不报错。由 D-17 构建（集群里有 `environments` 一节时）。`none` 不写，用默认的运行环境，只给冒烟。
3. 提交 Task：消息只有题目描述（`--hints` 时加上 hints_text）与固定的说明（改源码、不改已有测试、隐藏测试会判），
   **不带 patch、test_patch、FAIL_TO_PASS、PASS_TO_PASS**。
4. 跟着 SSE 走到 task_finished：断线凭 Last-Event-ID 续上，事件按 id 去重；Verdict 按事件计数。无人值守：问人时选项里有
   stop_and_wrap_up 就回它，审批（approve/reject）回 reject，别的问题取消 Task；回过的问题记进这一题的记录。每收到一个事件（含心跳）
   至多每 `poll_s` 秒查一次 QueryTask 的花费：超过 `--task-usd-limit` 或超过 `--task-timeout-min` 就取消，记下原因。
5. 结束后不管结局（done、abort、取消、失败）都取 `GetDiff(base=建好时的 head)` 当补丁：只含 agent 的改动，不含运行环境定义；
   diff 被截断时记错、补丁为空。之后删工作区（`--keep-workspaces` 时留着）。

总预算：开始一题之前，已花的加上正在跑的每题按 `--task-usd-limit` 预留，再加这一题的上限，超过 `--budget-usd` 就不再开新题，
剩下的记为没跑。单题会多出取消前的最后一步调用，所以租户策略的 `budget.usd` 应设得不高于 `--task-usd-limit`。

续跑：同一个输出目录再跑一次 `run` 时，`run.json` 的设定与选中的题要一致（不一致就拒绝）；已结束的跳过；记了 task_id 的接着跟，
不重新提交；建了工作区还没提交的、出错的，删掉工作区重来；已花的钱从记录里算。

报告（`report`，判过分时并进判分结果）：选中、跑了、没跑、出错的题数；解决数，解决率分两种（÷ 选中的，与榜单同口径；÷ 跑了的）；
总花费、每题平均花费、每解决一题的花费；Round 数的中位数与最大；Verdict 分布；结局分布；自动回复与超限取消的次数；按仓库的解决率；
判分没跑成的（`error_ids`、`infra_failure_ids`）单列，有这类题时报告标明“判分不完整”。

**验收测试**：

| 测试 | 覆盖 |
| --- | --- |
| `services/go-task-service/internal/workspaceapi/server_test.go` 的 `TestCreateAtACommitWithFiles` | repo_url + 提交 + files 能建、参数带给 Worker；files 与 from_workspace_id 同给仍是 InvalidArgument |
| `services/py-worker/tests/test_workspace_at_commit.py` | 克隆到提交：head 是写入文件的那次提交，它的父提交是 base；看不到之后的提交（`git log --all`、对象都没有）；branch 为空；diff 相对 head 只含之后的改动；不在远端的提交报错且撤掉这一行；存档与挂卷两种模式 |
| `services/py-learning/tests/test_swebench.py` | 读题与选题（种子可复现、按 id 选、缺字段与重复 id 报错）；不泄题；镜像名；运行环境定义过得了 D-17 的识别与静态检查；工作区 id；predictions 的格式与顺序；总预算（并发时预留）；单题超限与超时取消；无人值守的回复；断线续读不重复计；续跑不重复提交、设定不一致时拒绝；diff 的基准是建好时的 head；判分的参数与读报告（假的官方工具）；报告的各项指标；令牌不进输出目录 |
| `services/py-learning/tests/test_benchmark_client.py` | 对着假的 HTTP 服务：connect 的路径、认证头、驼峰与下划线字段的换算、int64 的字符串、SSE 带 Last-Event-ID 续读、错误信息里没有令牌；`fetch` 分页取全、条数对不上时报错 |
| `tests/e2e/test_swebench_e2e.py` | 整套（mock 供应商、桩决策、本地沙箱、存档模式、`--allow-local-remotes`）：命令行 `run` 对着本机裸仓库的一道构造题，工作区停在 base、看不到之后的修复提交、环境定义在 head 的提交里，Task 结束，predictions 有这一题，记录里有结局、Round 数与花费；`report` 写出报告 |

**真跑前要人定的**：总预算与单题上限；在哪儿跑（集群要配 D-17 的 `environments`，构建要能从 Docker Hub 拉 `swebench/*`；
租户策略的 `sandbox.network.hosts` 要含 github.com，`effect_ceiling` 至少 write_reversible）；判分在哪儿跑（有 Docker 的机器，
或 Modal）。建议先 5 题冒烟，再 50 题。

**已知的偏差与风险**：官方镜像里带编译扩展的仓库（astropy、matplotlib、scikit-learn，约占 Verified 的 18%）换成工作区以后
扩展要重新编译，agent 跑测试前得先构建，会多花时间与钱，报告按仓库分开看；每题一个环境镜像，集群的磁盘要够（D-17 闲置清理会回收）；
子集的解决率与榜单的 500 题全量不完全可比，报告写明子集大小与种子。

**实现记录**

- 契约：`workspace.proto` 改了 CreateWorkspace、`ref`、`files` 三处注释，重新生成（生成代码只有注释变了）；`buf breaking` 通过，
  兼容性基线已更新。
- task-service：`conflicting_sources` 只拦 from_workspace_id 与另两种来源同给。Worker（`workspace/operations.py`）：ref 是完整的
  提交时 `git init` → `git fetch --no-tags <远端> <提交>` → `git checkout --detach FETCH_HEAD`；远端没有这个提交时 git 报
  “not our ref”，归为 NotFound；相对工作区里没有的提交取 diff 时 git 报 “bad object”，也归为 NotFound（原来是 git_failed）。files 在克隆之后写入，`git add --force` 只暂存写入的这几个文件（仓库的 .gitignore 忽略了也进提交），
  `--allow-empty` 提交 “Add workspace files”。原有测试里“repo_url 与 files 同给报错”的一条按新契约改成 from_workspace_id 与 files。
- 适配器：`agentruntime_learning.benchmarks`（`client.py`、`swebench.py`、`__main__.py`）；e2e 包加了对 agentruntime-learning 的依赖
  （uv.lock 只多这一项）。
- 对着真数据核对过（2026-10-29）：`fetch` 取到 Verified 的 500 题，全部过得了读题的检查；种子 0 抽 50 题是 django 22、sympy 11、
  sphinx 6、matplotlib 4……有两题（django__django-15695、sympy__sympy-15599）的题目描述本身带着测试文件的 diff：这是公开的题面，
  榜单上的 agent 都看得到，不算泄题。
- 没覆盖：没有真跑（没花钱）；官方判分工具只对着假的测，参数与报告格式对照的是 v5.0.1 的源码；D-17 构建官方镜像与 `/testbed`
  的符号链接没在集群上试过，带编译扩展的仓库要先重新构建这一点没验证；单题的花费上限靠适配器取消，取消前会多出一步调用；
  后端登记里没有 `environments` 时 `--environment official` 写进去的定义不会被构建，Task 用默认的运行环境，适配器不报错——真跑前要
  确认集群配好了。

**服务器准备（2026-10-30，没花模型的钱）**

- 跑与判都放在线上那台集群（`ssh cloudnium_cloud`，cgent.link，D-02 的 k3s 单节点）：它有 Docker 29，后端登记里配了 D-17 的
  `environments`（构建可以访问 Docker Hub）。集群从 5f2658a 升级到 13924d7（第 2～10 节一起上线；步骤与核对见
  [cloud-deploy.md](cloud-deploy.md) 第 10 节“升级到 13924d7”），之前线上的 Worker 不会克隆到提交。
- 判分：服务器上 `/opt/swebench/venv`（uv 建的 Python 3.13，swebench 5.0.1，不进本仓库的依赖），适配器用
  `PYTHONPATH=services/py-learning/src` 直接跑（只用标准库）。题在 `/opt/swebench/data/verified.jsonl`（`fetch` 取的 500 题）。
- **真判分工具核对过**（不经 Runtime，用适配器自己的写法造输出目录）：pytest-dev__pytest-5262 交官方修复判为 resolved，
  psf__requests-1724 交空补丁判为 empty_patch，psf__requests-1766 交无关的新文件判为 unresolved；`report` 的标签、解决率、
  判分完整与官方报告对得上。**psf__requests-2317 交官方修复也判为 unresolved**：补丁打上了，失败的 7 条测试都要连外网的
  httpbin.org，这个老版本 requests 带的根证书验不过现在的证书（`CERTIFICATE_VERIFY_FAILED`）。Verified 里 requests 的 8 题
  判分要联网、随时间不可靠，报告里按仓库看时要把它们单列；沙箱默认断网，agent 跑这些测试也会失败。
- 磁盘：三道题的官方镜像加新的 Runtime 镜像让 Docker 的占用从 17 GB 到 30 GB（一题约 3～4 GB，同一仓库同一版本的共用一部分）。
  真跑时同一个镜像还会在环境镜像仓库与 k3s 的镜像存储里各有一份；系统盘剩 370 GB，5 题冒烟够，50 题要先把空着的第二块盘
  （931 GB，没分区）用上，或者每判完一批就删掉题目镜像。
- 还差：给跑题的租户 `swebench` 发租户策略（预算、配额要放宽：平台默认是单题 1 美元、50 次工具调用、20 分钟，每天沙箱 7200 秒，
  最多 10 个工作区）与签发令牌，都等人定；D-17 构建官方镜像与 `/testbed` 要在第一次冒烟时看。

**5 题冒烟（2026-10-30，人定单题 2 美元、总预算 10 美元；停在半途）**

- 准备：租户 `swebench` 的策略经 `registry-seed` 发布（write_reversible；单题 2 美元、150 次工具调用、60 分钟；gVisor；联网只放
  github.com；沙箱同时 4 个、每天 10 万秒；工作区 20 个、每个 4 GB）；令牌 72 小时，只在服务器的 `/root/ar-release` 里。线上的
  Workspace API 克隆到提交过了（pytest 的一题，22 秒，head 是写入文件的提交，HEAD 分离，未知提交 404）。题：种子 0 的 5 题
  （django 2、sphinx 1、sympy 2）。
- 第一次：5 题都在订阅事件时连接被拒、被取消（没花钱）。线上没配 `AR_PUBLIC_BASE_URL`，SubmitTask 给的订阅地址是 task-service 的
  监听地址 `http://0.0.0.0:8080/…`；适配器改为只取路径、按 Task API 的地址补全（0fdd7b4）。**Chart 里没有这个变量**，别的 API 客户端
  拿到的订阅地址也是 0.0.0.0，要在 Chart 里按 `ingress.host` 配上。
- 第二次跑了 4 题后人工停下、取消（都是 aborted 或 cancelled，没有补丁）：
  - **Model Gateway 计的花费是 12.16 美元**（4 个 Task，每个 2.81～3.31），超过了人给的总预算 10 美元。适配器的总预算没起作用：
    **Task API 的用量从来没填**——task-service 的 QueryTask 与 Worker 的 TaskWorkflowResult 都不设 `usage`，花费恒为 0；花费只在
    Task State 的 `rounds[].cost_usd`，而且比网关计的少（同一个 Task，Worker 的 budget_check 说 2.20，网关是 2.85）。适配器第一期
    没核实这一点就当真了（离线测试的假 Runtime 报花费，端到端用的 mock 模型花费本来是 0）。
  - **单题 2 美元的上限超了四到六成**：budget_check 只在 Node 开始前看，一次 Claude 调用要 0.3～0.5 美元，规划与评审的调用不受它拦。
  - **每次 claude-sonnet-5-5 调用的输入是 11 万～24 万 token、输出约 5 千**；10 月 28 日实验里同一个模型的执行、规划、评审输入中位数是
    3.7k、15k、11.6k。同一 Task 里路由（gemini）只有 745，评审换成 gpt-5.5 只有 4243；Task State 只有 1.9 KB，Reviewer 的输入按规定
    只有 Stage 结果、Task State 与验收条件。所以多出来的十几万 token 很可能不是我们发的内容，而是经 a6api 中转调 Claude 时被抬高或
    被中转方加进去的。后来核实了：见下面的“中转核实”。
  - django-15561 的探索 Node 只用 `context_read` 去读仓库文件（`obj://django/…`），三次 not_found 后失败，之后预算用完、问人、
    自动回 stop_and_wrap_up、abort。没调到 shell_run，所以 D-17 的构建一次也没起，`/testbed` 也没验证到。另一题（Task 尾号
    ZJ82K7）的 investigate 用了 repo_grep 与 repo_read（Tempo 里有 `execute_tool` span）。为什么 django 那题没用读仓库的工具，还没查。
- 留下的：6 个 `swebench` 的工作区（续跑时适配器删了重建）；沙箱都已销毁；没有环境镜像。

**中转核实（2026-10-30，人定花 0.5 美元以内；网关计约 0.08 美元）**

- 做法：在 worker-model 的 Pod 里经线上的 Model Gateway 调（租户 `swebench`，task_id `relay-probe-1`，与冒烟的计量分开），每种请求先
  gpt-5.5 再 claude-sonnet-5-5，tier strong@medium，用 `pinned_model_profile` 指定模型（都没换）。脚本在服务器的
  `/opt/swebench/scripts/relay_probe*.py`，不进仓库。

  | 请求 | gpt-5.5 报回的用量 | claude-sonnet-5-5 报回的用量 |
  | --- | --- | --- |
  | 两条短消息（约 15 token），思考 low | 输入 21、输出 1，答 OK | 输入 631、输出 3，答 OK |
  | 同上，思考 medium（只调 Claude） | — | 631、3，没有思考 token |
  | 500 行已知文本（o200k 约 1 万 token） | 14401（缓存命中 4224），答 500 | 15876，答 500 |
  | Reviewer 真实的系统区与 Verdict schema，要结构化输出 | 7431、272，回了 JSON | 0、0，finish 是 tool_calls，没有正文 |
  | tool_loop 的形状，只给一个工具 `repo_read` | 4459、39，调 `repo_read` | 103、0，**调了我们没给的工具 `Bash`** |
  | 不给工具，问它能调哪些工具 | 4435（缓存命中 4224），答 NONE | 73、0，**又调了 `Bash`** |

- 中转不按比例抬高普通提示的 token：Claude 每次固定多约 610；gpt 多数请求多一段约 4.2k 的前缀（缓存命中 4224；问它时答没有工具）。
- **a6api 的 Claude 通道不是原样转发**：请求里被加进了我们没给的工具（至少有 `Bash`），不给工具时模型也去调它；要结构化输出时回的
  是工具调用、没有正文。这几种回答报回的用量是 0 或几十 token，网关计不上。像是包了一层 Claude Code 一类的代理。冒烟里 Claude 每次
  11 万～24 万输入、约 5 千输出，与我们发的内容对不上：同一 Task 里连续几轮 tool_loop 的输入忽高忽低（129828、141933、119799、
  153246、127735），真实的对话只会变长。最可能的来源是这层代理自己的系统提示、工具与它内部的多轮，但从我们这边证实不了；中转方
  按哪个数字扣费也看不到。10 月 28 日经同一个中转的 Claude 用量是正常量级，中转方可能之后换了 Claude 的通道。
- 影响：经这个中转跑 Claude 的结果不能算作 Runtime 的结果。模型看到的工具不是我们给的，结构化输出走不通，用量也不可信。django 那题
  不用读仓库的工具，可能与此有关（没证实）。冒烟超支的 12.16 美元是按中转报回的数字算的。另外两条与中转无关，仍要修：Task API 的
  用量没填，budget_check 拦不住规划与评审。
- Runtime 这边的防线：Model Gateway 把请求里没有的工具调用、要结构化输出却回来的工具调用当作失败（带上已计费的用量），有回答而
  用量为 0 的调用记异常。细化与实现见 11.11。
- SWE-bench 要换成官方 API 或原样转发的中转再跑；换之前不再跑。

**gpt-5.5 的 5 题冒烟（2026-10-31，人定用 gpt-5.5 经中转，单题 3 美元、总预算 170 美元）**

- 准备：线上第 11 版（`a63e92b` 的 Chart，镜像仍是 52174d9）。Model Gateway 读 `agent-runtime-model-allowlist`，`swebench` 只能用不是 Claude 的模型，于是 strong 档是 gpt-5.5；route、deliver 走最便宜的 gemini-3.5-flash-lite。Reviewer 的 `avoid_vendor` 因此避不开（`avoid_vendor_unmet`）。租户策略 v2：单题 3 美元。选题：`--stratify --exclude-repo psf/requests --seed 0`。另有独立的看守脚本盯网关计量，出现 Claude 或花费超限就停。
- 结果：**解决 0 / 5**，花费 4.73 美元，每题 0.95 美元。适配器与网关的花费对得上（第 11.11 节的修复生效）。每次调用的输入约 8 千 token，上次经 Claude 通道是 11 万～24 万。结局 aborted 4、done 1；Verdict：replan 11、continue 9、escalate 6、abort 4、repair 2、done 1。补丁都打得上，FAIL_TO_PASS 通过 0/1、6/9、2/4、0/2，sympy 那题没有补丁；PASS_TO_PASS 没有变坏。
- **官方环境一次也没建成**：Docker Hub 把镜像层重定向到 `production.cloudfront.docker.com`，Chart 只放行了 `production.cloudflare.docker.com`，4 次构建都被出网网关拒绝（`host_not_granted`），Task 退回 universal。所以这次的 0 / 5 不代表有项目环境时的水平。修复在 `e90bf98`（Chart），还没部署。
- 中转的 GPT 通道也不完全干净：不带工具的 explore 节点上，模型调用了没给的 `open_page`、`output`，网关拒绝（`unexpected_tool_call`，25 次，白花 0.18 美元）。这些节点是 Planner 排的不带工具的 Atomic Node，第 11.11 节之前说的规划问题还在。另有 `bad_structured_output` 12 次，0.17 美元。
- 一题克隆时出网网关连不上 github.com（`connect_failed`，502），没花钱，续跑时重试成功。基线跑完要检查有没有这种题，再续跑一次。

**修好 CDN 之后重跑（2026-10-31，第 12 版；花 2.06 美元，没有成绩）**

- 放行了 `production.cloudfront.docker.com` 之后镜像下得下来，但官方环境仍然用不上，原因都在 D-17 的构建这条路上：
  - **Task 不等构建**：`shell_run` 第一次用到才起构建，好之前一直用 universal。构建在 Kata 里约 25 分钟（解压约 20 分钟，`rm -rf /testbed` 近 4 分钟），一题约 15 分钟就做完了，Task 永远赶不上自己的镜像。
  - **构建两次都失败**：到导出阶段失败（第 25 分钟），原因看不到：`mark_failed` 只留前 500 个字符，而日志的尾部在后面。
  - **失败永久缓存**：按定义的哈希记着，一次网络问题就让这个定义一直失败，只能改定义。
  - **镜像个数**：`building` 与 `ready` 合计最多 5 个，50 题放不下。
- **配额**：做完的 Task 的环境留着等复用（空闲 15 分钟才回收），加上构建，占满 `max_concurrent: 4` 之后，建工作区与取 diff 都被拒。两题做完了、钱花了，补丁没取回来。适配器已修（等配额、Task 做完了续跑只取补丁）。
- **绕开构建器是可行的**：服务器的 Docker 里按同样的定义构建 3 秒，1.2 GB，uid 10001、只读根文件系统下跑通了项目测试（django-16877，Python 3.11，Django 从 `/workspace` 加载）。题目的官方镜像是可信来源，不需要 Kata 隔离。

**用上官方环境的 5 题（2026-10-31，人定绕开构建器）**

- 做法：`/opt/swebench/scripts/prepare_envs.py` 在服务器的 Docker 里构建，`docker save | k3s ctr images import` 导入，再打上 `127.0.0.1:30500/envs/swebench/env@sha256:…` 的引用（K8s 后端只收这种写法，线上没有配从环境镜像仓库拉取的认证，kubelet 按 IfNotPresent 用本地的），在业务库的 `sandbox_images` 里只给 `swebench` 写 ready 的行。键用 Worker 镜像里的 `detect` 算，与库里已有的失败记录核对过。每题约 3 分钟。租户策略 v3：`max_concurrent` 12，`idle_timeout_s` 60。
- 结果：**解决 0 / 5**，4.57 美元。`shell_run` 的 `environment` 是 ready，项目测试真的跑得起来。但 4 题的补丁是空的：模型几乎没有走到改代码。在通用环境那次 4 题都有补丁，这次不是环境变差，而是下面几个问题占了上风：
  - **Planner 回退后的保守规划只给 `context_read`**：Planner 的结构化输出失败就回退到“探索并推进这个任务”，探索节点没有读仓库的工具，又空转一两轮。一题 12 轮里 5 轮是它。
  - **`tool_loop` 一步失败就让 Node 失败**：`bad_structured_output` 39 次、`unexpected_tool_call` 35 次（`web_search`、`open_page`、`output`）。改代码的节点（`apply_fix`、`implement_escapeseq`）连续失败 5 次就没了。输出只有几百 token，不是截断。
  - **Reviewer 判“完成”却没有改动**：django-11433 全程没有成功的 `repo_edit`，最后判 done（第 10 节说的假完成）。
  - 中转报 `system_cpu_overloaded`（`models_exhausted`）的调用也让步骤失败。
- 中转核实（约 0.02 美元，短请求直连）：结构化输出都是合法 JSON，`output` 工具没有复现，所以还不能说它是我们的 `response_format` 被转成了工具调用。同样的请求有时输入算 4400 多 token、有时 130，说明背后有两种通路，一种带隐藏提示。

**50 题基线（进行中）**：`/opt/swebench/runs/gpt55-base50`，种子 0，按仓库与难度分层，不含 psf/requests，并发 2，单题 3 美元，这一轮上限 145 美元。按人的要求先测基线：上面这些问题这次不修，修复在基线之后。


### 11.11 冒烟查出的 Runtime 问题：用量、预算与网关的防线（2026-10-30 人确认，已实现）

11.10 的冒烟与中转核实里，下面四件与中转无关、是 Runtime 自己的问题（中转让它们更显眼）：

1. **Task API 的用量从来不填**：`QueryTaskResponse.usage` 与 `TaskWorkflowResult.usage` 都没人设，客户端（SWE-bench 的适配器）的
   总预算看到的花费恒为 0。
2. **已计费却失败的调用，Worker 不知道花了钱**：网关对这种调用写一行 status failed 的计量，但错误里不带花费，Planner、Reviewer 的
   成本里少了它（冒烟里两次评审各 0.33、0.52 美元）。同一次调用里先失败、后换下一个 Profile 成功的，回答的 Usage 也只算成功的那次。
3. **美元预算用完了，Planner、Reviewer 仍会再问一次**：两者在回答不合格时都带上错误重问一次。冒烟里第二轮规划第一次回答后已经
   超过 2 美元，仍重问了一次（每次约 0.4 美元）；评审同样。
4. **中转塞进来的工具与零用量**：模型调了请求里没给的工具（`Bash`），网关当正常回答交了出去；要结构化输出却回来的工具调用也一样。
   回答的用量为 0 时网关当作免费，计量与成本都记不上。

**挂在哪个工作项下**

| 问题 | 工作项 |
| --- | --- |
| 1 | S-03（Task API 的查询、Task 工作流的结局）的后续；Task 工作流是核心链路，同一时间只由一个 agent 改 |
| 2、4 | T-05（Model Gateway，`docs/plan/model-gateway-direct.md`）的后续，含 Python 客户端 |
| 3 | S-09（Planner）、S-10（Reviewer）的后续 |

**做法（与 11.10 草稿不同的地方加粗）**

- 用量（问题 1）：美元的用量按 `task_state.schema.json` 的口径，是各 Round 的 `cost_usd` 之和。
  - Worker 在 `TaskWorkflowResult.usage.cost_usd_micros` 填它（done、abort 都填）。
  - task-service 的 QueryTask 从工作流的 Task State 算出同一个数；查不到 Task State 时取业务库里记下的结局（`Outcome`）的 usage；
    两样都没有时不设 usage。
  - **正在跑的 Round 在查询结果里带上已经花的钱**（规划加上已完成的节点）。只改查询给出的副本，不写进交给 activity 的 Task State：
    Stage Runner 另外把规划的花费算进已花的钱，写进去会重复计。
  - token 数这一期不填：Worker 只记美元。
  - 不改命令序列，不用版本分支；不改 proto 与 schema。
- 已计费的失败（问题 2）：
  - 网关的错误在 details 里带 `billed_cost_usd_micros`：这次 RPC 里所有已计费的尝试之和，大于 0 才带。
  - **成功的回答，Usage 也是这次 RPC 里所有已计费的尝试之和**（token 与成本都加）。计量仍按尝试一行一行写。
  - Python 客户端把它还原到错误上（`executor.model_gateway.billed_cost_usd(exc)`；构造用 `with_billed_cost`）。
  - Planner、Reviewer 调用失败时把它记进这次的 `calls`。
- 不合格时重问（问题 3）：**11.4 定的“评审前不拦”不变**，第一次规划与第一次评审照常调用；只是美元预算已经用完时不再重问：
  - Planner：Task 预算减去之前各 Round 的成本，再减去这次已调的（分诊与第一次规划），不大于 0 就不重问，直接用保守规划。
  - Reviewer：台账的已花（含本轮的规划与 Stage）加上这次第一次评审的花费，不小于预算就不重问，按规则判定（`fallback`）。
  - 没有美元预算时照旧重问。
- 网关的防线（问题 4）：
  - **回答里有请求没给的工具调用（没给工具时任何工具调用都算）**：这次尝试记一行 status failed、error_code `unexpected_tool_call`
    的计量（带上已计费的用量），不在同一个 Profile 上重试，换同档的下一个 Profile。
  - **同档都这样**：报 Invalid `unexpected_tool_call`，不报 transient：Worker 的决策重试不会再连打几次。
  - 有回答但 input 与 output token 都是 0：照常交给调用方。计量记 `usage_missing: true`，写一条警告日志，另记计数
    `runtime.model.usage_missing`（属性同 `runtime.model.calls`），好在 Grafana 上告警。
  - 流式这一期不查：工具调用的分片在查出问题之前已经发出去了。Worker 的决策与 tool_loop 都用非流式。

**人确认的（2026-10-30）**

1. 上面的四处加粗。“不合格时重问”只在美元预算用完时省掉，第一次规划与评审不拦，保留 11.4 的人定（点按钮时的说法是“规划与
   评审也受预算拦”，与 11.4 冲突，人确认按 11.4）。
2. 同档都给出没给的工具调用时报 Invalid，而不是 transient。

**没覆盖的**

- tool_loop 一步的模型调用失败时（activity 失败），这一步已计费的花费仍记不进 Round 的成本；要改 Stage Runner 的失败路径，另做。
- 单次调用的成本上限（`CallContext.max_cost_usd_micros`）网关仍不看：要先估算输入的 token，估不准；中转虚报的用量本来也拦不住。
- token 用量。

**验收测试**（覆盖上面每一条）

| 文件 | 内容 |
| --- | --- |
| `services/go-model-gateway/internal/gateway/usage_guard_test.go` | 没给的工具调用（有工具、没给工具两种）：记 failed、不重试、换下一个 Profile、回答的 Usage 是两次之和；同档都这样报 Invalid 并带上花费；已计费的 Invalid 带 `billed_cost_usd_micros`；零用量照常返回，计量、日志与计数都记上 |
| `services/py-worker/tests/test_billed_cost.py` | 客户端还原 `billed_cost_usd`；Planner、Reviewer 记上已计费的失败；美元预算用完时不重问（还有剩余时照旧重问） |
| `services/py-worker/tests/test_task_usage.py` | done 与 abort 的 `TaskWorkflowResult.usage`；跑到一半查询时本轮带上已花的钱，交给 Stage 的 Task State 里没有 |
| `services/go-task-service/internal/taskapi/usage_test.go` | QueryTask 的 usage：从 Task State 算；查不到时取记下的结局；都没有时不设 |

**实现记录（2026-10-30）**

- 网关：`internal/gateway/gateway.go`（`checkToolCalls`、`skipsProfile`：没给的工具调用与 model_not_found 一样换下一个 Profile；
  `call.billed` 与 `meterFailed` 累计已计费的失败，`charged` 带进错误，成功时加进回答的 Usage；`served` 标 `usage_missing`，
  `metrics` 记 `runtime.model.usage_missing`）。`libs/go-core`：`providererr.Error.BilledCostUSDMicros`，connecterr 写成 details 的
  `billed_cost_usd_micros`（与 LastCode 同一做法）。
- task-service：`internal/taskapi/server.go` 的 `taskUsage`。
- Worker：`executor/model_gateway.py` 的 `billed_cost_usd`、`with_billed_cost`，`grpc_gateway.py` 从 details 还原；Planner 与
  Reviewer 的 `_call` 记上已计费的失败，重问之前看美元预算；`workflows/task.py` 的 `_running_spent`、`_state_with_running_cost`
  与结局的 usage（查询与结局用副本，交给 activity 的 Task State 不变）。
- 没改 proto、schema，没加版本分支；golden 历史照旧回放。

### 11.12 评测看板对外开放（2026-10-30 人确认，已部署 7210e3e，页面已发布，等 Cloudflare 的 DNS 记录与 Full (strict) 规则）

人的要求：评测图表页面对外开放，放在合适的子域名下，定英文与中文的术语名。

**术语与子域名（建议）**

| 项 | 建议 | 理由 |
| --- | --- | --- |
| 英文名 | **Eval Dashboard** | “eval”是这个领域通行的叫法（设计书用“内部评测”，英文一般说 evals），dashboard 说的是把图表放在一页 |
| 中文名 | **评测看板** | 对外展示的数据页通常叫看板；与内部 Grafana 的“面板”、每次运行的“评测报告”（JSON）都分得开 |
| 子域名 | **`evals.cgent.link`** | 与 `api`、`code` 一样是一个短词，也是这个领域最常见的说法 |

不用 Leaderboard、排行榜（不是外部提交的排名）；不用 Benchmark（目前主要是内部评测集，SWE-bench 以后作为其中一节）；不用 status（那是可用性页面）。
两个名字设计书附录 A 里还没有，建议由人加一行（见下）。

**挂在哪个工作项下**：第 9.1 节静态页面（T-15 的后续）与 D-04（Runtime 的 Chart 与入口）的后续。

**公开什么**

- 只公开**评测集上的汇总**：门槛指标的趋势、模型对比、各次运行、失败原因（代码与次数）、S-11 的压缩评测。这些数据不来自用户。留出集仍只有汇总。
- **不公开**生产数据：网关计量与 span 带租户（名字里可能有 GitHub 用户名）、Task id 与每个 Task 的花费；Grafana 也不开放（匿名访问能经数据源查询 trace）。
- `scripts/eval_dashboard.py --public` 生成公开版：
  - 给了 `--metering` 或 `--spans` 就报错退出，不生成；
  - 数据里只有 `generated_at` 与 `reports`，运行行里去掉报告的文件名；页面没有运行指标的几节与卡片；
  - 标题写两个名字，每节标题中英文都有，开头一段说明这是什么、数字怎么来的（含模型调用经第三方中转、成本按它报的用量与标价算）；
  - 页面不连外网（没有外部脚本、样式、图片）；超过 1 MB（ConfigMap 的上限）就报错。
- 不加 `--public` 时内部版不变。

**怎么托管**

- Chart 加组件 `eval-dashboard`（默认关；`evalDashboard.enabled`、`evalDashboard.host`；要 `ingress.enabled` 且 `ingress.type=traefik`，否则渲染失败）：
  Deployment、ClusterIP 的 Service、Traefik 的 IngressRoute（复用 `ingress.traefik.tlsSecretName`，源站证书已含 `*.cgent.link`）、NetworkPolicy
  （进只来自 `kube-system` 的入口控制器，出一概不放）。
- 不加新依赖：用已有的 learning 镜像跑只用标准库的 `agentruntime_learning.evals.dashboard_server`：
  只答 `/`、`/index.html` 与 `/healthz`，只收 GET、HEAD，带 CSP 等安全响应头，每次请求现读文件，不回显请求内容。
- Pod 没有 ServiceAccount 令牌、没有 Secret、没有业务环境变量与其他卷，根文件系统只读，以非 root 运行。
- 页面放在 ConfigMap `<fullname>-eval-dashboard` 的 `index.html`，**由发布步骤写，Chart 不渲染**（否则每次 `helm upgrade` 都会清掉），卷标为 optional（没发布时 `/` 回 404、`/healthz` 仍 200）。发布：
  生成公开版，`kubectl create configmap … --from-file=index.html=… --dry-run=client -o yaml | kubectl apply -f -`；ConfigMap 更新后约一分钟生效，不用重启。

**要人做的（Cloudflare，我没有这个账号的权限）**

1. 加 DNS 记录 `evals`，开代理，指向与 `api` 相同的源站；
2. 把 `evals.cgent.link` 加进设为 Full (strict) 的配置规则（否则可能走 Flexible，源站的 80 跳 443 会造成重定向循环）。

**人确认的（2026-10-30）**：英文名 Eval Dashboard、中文名评测看板、子域名 `evals.cgent.link`；只放评测集的汇总，生产数据与 Grafana 不开放；现有的 26 份报告全部放上去，页面开头写明经第三方中转。

**设计书的改动建议（由人更新原文档后重新导出）**：附录 A 加一行：评测看板（Eval Dashboard）：对外展示评测结果的页面，只含评测集上的汇总，不含生产数据。

**没覆盖的**：页面要手动发布（不随评测自动更新）；公开版的运行名（如 `exp-2026-10-28/e2-opus`）是内部分组名，这一期不改；SWE-bench 的结果等真跑之后再加一节。

**验收测试**

| 文件 | 内容 |
| --- | --- |
| `services/py-learning/tests/test_eval_dashboard.py` | 公开版：两个名字、中英文标题、说明段；只有评测的几节，数据不带生产字段与文件名；给了生产数据的输入就拒绝；不连外网；超过上限报错；内部版不变 |
| `services/py-learning/tests/test_eval_dashboard_server.py` | 只答固定路径；安全响应头；只收 GET、HEAD；没发布时的行为；现读文件；不回显、不露版本；只用标准库、`python -m` 能起 |
| `tests/deploy/test_eval_dashboard.py` | 默认不渲染；打开后的 Deployment、Service、IngressRoute、NetworkPolicy；没有令牌、Secret 与业务变量；Chart 不渲染 ConfigMap；缺 host 或入口不是 traefik 时渲染失败 |

**实现记录（2026-10-30）**

- `scripts/eval_dashboard.py`：`--public`（`render(public=True)`、`public_data`）；运行指标的几节挪到 `RUNTIME_*`，公开版连同脚本一起不出现；内部版不变。
- `agentruntime_learning.evals.dashboard_server`（只用标准库）；Chart 的 `templates/eval-dashboard.yaml` 与 `evalDashboard`；发布步骤见 `cloud-deploy.md`。
- 在浏览器里经这个服务（带 CSP）看过公开版：26 份报告都显示，控制台没有错误。Chart 的测试要 helm，本机没有，在服务器上跑（`AR_REPO_ROOT` 指向解开的目录）。

### 11.13 Session 里的后续消息没有上下文（2026-10-30，P0，人确认，已实现并上线 7210e3e；真模型的效果等人复测）

**现象**（人测试的一个 Session，内容已脱敏）：第一条消息让 Agent 读仓库并说明结构，回答里提到了一个模块名。接着：

- 问“解释一下 <模块名>”：回答说没有这方面的可靠信息，开始猜它可能属于哪个行业；
- 贴回上一条回答并问“这不是你上面说的吗”：回答说执行环境里没有更早的对话，还说“只收到了 request 和 answer@v1 模板”；
- 重新问一遍读仓库（又花 0.32 美元），再问“深入介绍一下 <模块名> integration”：又说没有信息，并猜了几个拼写相近的产品；
- 问“你上面说的下面就没有印象了吗”：回答说看不到之前的对话，请用户再贴一次。

**根因**（三处，都不是中转的问题）：

1. **Task 拿不到之前的对话。** Session 里每条消息是一个新 Task；`TaskWorkflowInput` 只有这一条消息；`PlanningContext` 没有对话；`tasks` 表连命令都不记。
   设计书 3.1.1 的“追加记录（每个 Task 的命令与交付摘要）”、3.2.1 的“Session 上下文”、附录 B.9 的“更新 Session 状态”都没有实现。
2. **分诊只看这一条消息。** 追问看起来像简单问答，判 fast：单次调用、没有工具，读不了仓库。
3. **answer 节点的提示泄露内部名字。** 用户消息是 `{"template":"answer@v1","inputs":{"request":…}}`，系统提示说“你在 Agent Runtime 里执行一个 Atomic Node”（S-05 留下的最小上下文），模型照着说了出来。

应用（coding-agent）是对的：同一会话一直传同一个 Runtime 的 `sessionId`。

**挂在哪个工作项下**：S-03（Session 与 Task）、S-05（Node Executor，核心链路）、S-09（分诊）、S-11（Context Builder）的后续。设计书已经这样要求，不是设计改动。

**做法**（不改 /proto、/schemas，不改工作流的命令序列，不用版本分支）：

- **记**：task-service 的 tasks 表加 `message`（迁移 4），提交时记命令原文，重复提交不改。交付已经在 `outcome` 里。迁移之前的 Task 没有原话，交付照样用。
- **读**：`agentruntime_worker.session.transcript.SessionTranscript`：按认证绑定的租户读同一 Session 里更早的、已结束的 Task（从早到晚）。
  默认最多 6 轮；最近一轮的回答最多留 16000 字、更早的 4000 字（截断处留头尾并写明省略）；总量 32000 字，超了丢最早的。
  表归 task-service 所有，这边只读；表不存在或读失败时当作没有对话，Task 照常跑（记警告）。
- **用**：
  - 分诊与规划：`PlanningContext` 加 `conversation`、`has_workspace`（`TranscriptContextSource` 填）。分诊的提示带上最近的对话（4000 字以内）与“这个 Session 有工作区”，
    规则加一条：要读工作区才答得好的（含追问之前读过的内容），effect 填 read，于是不走 fast。规划的提示带 16000 字以内。
  - fast 的 answer、rewrite 节点（输入有 `request` 时）：按真实的对话问模型（system，之前各轮的 user、assistant 交替，最后是用户这条消息的原文）；系统提示改成对用户的口吻，
    说明前面的对话是真实发生过的，不提内部的名字。其余 model 节点不变。
  - tool_loop：Context Builder 的 brief 区放 8000 字以内（每一步相同，不破坏提示缓存）。
- **对话不进 Node 的 inputs**：activity 自己读。不进工作流历史与进度事件，也到不了 Reviewer（`review_view` 会带 Node 的 inputs，而 Reviewer 的输入按规定不含对话）。

**没覆盖的**（这一期）：

- 设计书说的“Session 摘要”（brief 区的滚动摘要）与 profile 区：这一期只取最近几轮原文，更早的看不到。
- fast 的回答发现需要读仓库时不会自己升级；靠分诊判对，错了用户仍会得到“读不了”。评测集要补这类样本。
- 正在跑的 Task（并行的只读 Task）的中间结果看不到。

**评测集**：加了 4 条编写的分诊样本（只凭之前的对话能答的追问、与仓库无关的问题、要读工作区的追问两条），放在单独的 `services/py-learning/evalsets/task-loop-session-drafts`：`task-loop` 要求标注全部确认（`test_every_label_is_confirmed`），未确认的样本放进去 CI 就红。人确认后移进 `task-loop`；跑法是 `… evals run --eval-set evalsets/task-loop-session-drafts --subject <…>`，还没有用真模型跑过。

**验收测试**

| 文件 | 内容 |
| --- | --- |
| `services/go-task-service/internal/taskapi/session_transcript_test.go` | 提交时记下命令；重复提交不改 |
| `services/py-worker/tests/test_session_transcript.py` | 读：从早到晚、不串 Session 与租户、取交付、没交付的结局、迁移前的 Task、轮数与字数的上限、读失败不影响 Task、列名与 Go 的迁移对得上。用：分诊与规划的提示、fast 的 Stage 不带对话、answer 节点按真实对话问且不露内部名字、其余节点不变、tool_loop 的 brief |
| `tests/e2e/test_dev_stack.py::test_a_later_task_can_read_the_earlier_turn` | 真的 task-service 写（Go 的迁移、真的结局格式），SessionTranscript 读同一个 SQLite：两边对得上（离线，不调模型） |
| `services/py-learning/tests/test_session_regression_cases.py` | 回归样本：都是未确认、不混进主集、参照输出得满分、评测把对话传给 Planner |

**实现记录（2026-10-30）**

- task-service：`taskstore` 的迁移 4 与 `Task.Message`，`SubmitTask` 记命令（重复提交靠 `ON CONFLICT DO NOTHING` 不改）。
- Worker：`agentruntime_worker.session.transcript`；`planner/context.py`（`conversation`、`has_workspace`、`TranscriptContextSource`）、`planner/prompts.py`、`executor/node_executor.py`（`_conversational`、`CONVERSATION_SYSTEM_PROMPT`）、`executor/tool_loop.py`、`context/builder.py`；组装在 `executor/dev.py`、`executor/tool_loop.py`、`planner/activities.py`，同一个库共用一个 `SessionTranscript`（只建一个连接池）。Reviewer 没有改。
- 与上面写法不同的：绑定时只传 `tenant_id`（行按租户隔离，Session 是查询条件；`TenantContext` 还会校验 `session_id` 的格式）；交付只有一个文字字段时直接取它（JSON 的转义读着费劲，也更占字数）。
- 能离线验证的都验了；**模型实际答得对不对、分诊会不会把要读工作区的追问判对，只有真模型才知道**，没有跑（这一轮不花钱）。

### 11.14 把租户出过的问题整理成可修的条目（2026-10-30，设计稿，要人定）

人的要求：评测系统把租户历史上出现过的问题整理出来并推动修复；识别到用户的语气因平台出现剧烈不满时，相关 trace 要尽快优化；必须脱敏。

**现状**：Learning Pipeline（T-16）读 trace，做失败归因、归纳 Recipe、评测后发布；但 span 按规定不带消息与输出的原文，所以现在**看不到用户说了什么**，也没有“用户不满”这个信号。11.13 这一例就是人自己发现的，系统没有报警。

**建议的做法**（分三层，只有第三层碰原文）：

1. **信号（不存原文）**：分诊本来就对每条消息调一次小模型，在同一次调用里多要两个字段：`frustration`（none、mild、strong）与 `about_platform`（不满是不是冲着平台，而不是用户自己的业务）。
   不多花一次调用。只把这两个枚举写进 `invoke_workflow` span 与指标 `runtime.user.frustration`（不带文字），Grafana 上 strong 且 about_platform 就告警。
   另加不靠模型的信号：同一 Session 里重复提交几乎相同的消息、回答里说“看不到之前的对话”、Task 刚开始就被取消。
2. **问题条目（不存原文）**：Learning Pipeline 按结构化特征归类（例如 `context_lost`、`fast_without_tools`、`internal_name_leak`、`budget_overrun`），每类记次数、影响的租户数（只有数）、最近一次的 trace id、严重度。
   strong 的优先级最高，排到最前面。条目是内部的，不进公开的评测看板。人或 Agent 据此修；系统不自动改代码、不自动发布提示。
3. **脱敏样本（只有被选中的 Session，要人审）**：把问题固化成评测集的回归样本，修完以后不再回退：
   - 先用确定性的规则替换（密钥、邮箱、URL、路径、仓库名、代码标识符、数字），同一个词替换成同一个占位符（`<TERM_1>`），保留对话结构与指代关系，这样“上面说的 <TERM_1>”仍能复现问题；
   - 原文不离开租户的作用域、不进日志与 trace、不发给外部；
   - 样本标未确认，人审过才进评测集（CLAUDE.md 的规定不变），不进公开页面。

**要人定的**：

1. **用户内容的使用边界**：第 1、2 层只存枚举与计数；第 3 层要读用户的原文。开放注册之后，这要写进隐私条款，并给租户一个开关（租户策略加一项，走 S-01）。建议第 3 层默认只对内部租户打开，其他租户明确同意后才开。
2. **分诊的模型经第三方中转**：分诊本来就把消息发给中转；多要两个字段不多发内容，但第 3 层如果用模型脱敏就会多发一次。建议第 3 层只用规则，不用模型。
3. **优先级**：建议 11.13 先修上线，再做第 1、2 层（可以一起做），第 3 层等第 1 点定了再做。

**第一例**：11.13 就按这个流程走（人已授权看这个 Session）：归类 `context_lost`、`fast_without_tools`、`internal_name_leak`，严重度最高，脱敏样本由我手写（不含原文里的任何专有名词），标未确认。
