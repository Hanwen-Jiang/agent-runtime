# 运行中的交互：并行工具调用、纠偏与打断、上下文占比、输出汇总

> 状态：2026-10-30 第 6 节的决定都按建议定了，第 5 节的 S-01（含标“草案之外”的几处）人审通过，兼容性基线已更新。来源：人测试 coding-web 时提的四件事。第 1～4 节都已实现（第 9 节列出实现时的改动与还没验证的），还没有部署。

| 节 | 问题 | 工作项 | 改契约 |
| --- | --- | --- | --- |
| 1 | 每个工具调用都多一次模型请求 | S-04（tool_loop，附录 E.1）、S-11（Context Builder）的后续 | 否 |
| 2 | 运行中的输入只能排队，不能纠偏、打断、撤回 | S-03、S-09（Triage = replan）、T-11b（进度事件）的后续；coding-agent 的输入框 | 是 |
| 3 | 侧栏“上下文”显示的是内容，不是占比 | S-11、T-11b 的后续；coding-agent 的右栏 | 是 |
| 4 | 侧栏“输出”是上一条的全文，不是各轮的汇总 | S-10、T-11b 的后续；coding-agent 的右栏 | 是 |

## 1. 一次模型回答可以调用多个工具

**现象与根因**：请求确实多了。`ModelToolLoop.step` 只取 `response.tool_calls[0]`，其余的直接丢掉；系统提示还写着“一步只调用一个工具”。读 5 个文件现在要 6 次模型调用，每次都带上全部上下文；并行后约 2 次（估算）。

**做法**

- `ToolLoopStep` 加 `calls`（`[{tool, args}]`，按模型给的顺序）；`tool`、`args` 仍是第一个，旧的工作流照旧只用它。系统提示改成：互不依赖的读取放在同一步一起调，后面要用前面的结果时才分步。
- 工作流按版本分支 `PARALLEL_TOOL_CALLS_PATCH`（追加在 `AT_START` 末尾）执行：
  - 连续的只读调用（effect 为 none、read）并发，结果按模型给的顺序写进 history；
  - **每批的第一个先单独跑**：`find_reusable` 找不到环境时，同时开始的调用会各建一个沙箱，默认配额（同时 2 个）下第三个会被拒。第一个跑完环境就有了，其余的共用；
  - 写操作按顺序一个一个跑。读在写之后的，等写完。读的失败不影响别的读；**写的失败或被拒，这次回答里后面的调用都不执行**，记一条说明（它们是按前面成功来写的）；
  - 不可逆工具逐个审批，审批 id 仍是 `<调用 id>.1`；
  - 一次最多 `MAX_TOOL_CALLS_PER_STEP`（8）个，多的记一条说明，模型可以下一步再调；工具调用预算用完就停，已经发出去的等它结束。
- 调用 id：第一个是 `fix.2`（不变），之后是 `fix.2.2`、`fix.2.3`。幂等键含 Node id，所以各不相同。
- history 里同一次回答的多个调用带 `step`；只有一个调用的回答，记录与现在完全一样。Context Builder 把同一 `step` 的放进一条 assistant 消息，后面跟对应的 tool 消息。`max_iterations` 与重复错误的检测都按模型的步数算，不按调用数。
- 进度事件的载荷不用改：`tool_status` 每个调用各一组，`step` 相同，靠 `target` 区分。**去重键要改**（实现时发现）：原来是 `<Goal>/<步>/<状态>`，同一步的第 2 个调用会被当成重复的事件丢掉，所以第 2 个起加上序号。
- 实现时另外修了一个相关的缺陷：被拒的调用，Context Builder 给模型的是“输出：null”，模型既不知道被拒，也看不到审批人的指导（附录 E.1 要求送回循环）。现在写明被拒或超时，并带上指导。

**设计书的改动建议**：附录 E.1“每一步是一次 model 或 program 调用”改成“一步思考可以发起多个工具调用：只读的并发，写的按顺序”。

**验收**（`services/py-worker/tests/test_parallel_tool_calls.py`）：模型请求数；全部执行、结果按序；并发的真实顺序（看事件历史）；写的顺序与失败后不再执行；逐个审批；预算；上限；不认识的工具；进度事件；重复错误按步算；Context Builder 的消息结构；并发读共用一个环境。另外补一份 golden（`tool_loop_parallel`），证明旧历史仍能回放。

**上线后看**：`chat` 次数与工具调用次数之比，这个数字说明实际省了多少。

**已实现**（2026-10-30）：py-worker 全套 1121 通过、14 跳过（缺 Postgres、Docker 等工具的用例），另 1 条写死了“最后一个版本分支”的测试已改，改后单独重跑通过；py-learning 368 通过、12 跳过；golden `tool_loop_parallel` 已录。端到端测试与真模型没有跑，还没有部署。

## 2. 纠偏、打断、撤回与修改

**现状**：设计书早就要求（3.2.2、附录 C.2：给运行中的 Task 加约束不开新 Task，以 replan 重做剩余部分），没实现。`SignalTaskRequest` 只有 `human_reply`，运行中发的消息一律排队成下一个 Task。排队中的 Task 能取消，但不能改。

**用户能选的三种**（由用户选，不让模型猜；猜错了代价太大）：

| 选择 | 含义 | 操作（建议） |
| --- | --- | --- |
| 纠偏 | 这个 Task，下一个检查点生效 | Enter |
| 立刻打断 | 放弃正在进行的调用，马上生效 | Ctrl+Enter |
| 下一个任务 | 排队，现在的行为 | 输入框的菜单 |

**纠偏与打断在各阶段的效果**

| Task 正在 | 纠偏（下一个检查点） | 立刻打断 |
| --- | --- | --- |
| tool_loop 等模型 | 这一步回来后，只读的工具照跑；**写的不执行**（用户可能正是要拦它），下一次模型调用带上纠偏 | 取消这次模型调用，丢弃未完成的输出，下一步带上纠偏，同一个循环继续、上下文不丢 |
| tool_loop 跑工具 | 已经开始的跑完，还没开始的写不执行（同上一行的理由），下一次模型调用带上 | 按下表撤销，再继续 |
| 等审批或问人 | 继续等，回复后带上 | 撤回问题，按拒绝处理，带上纠偏 |
| Atomic Node、模板、sub_agent、子 Task | 这个 Stage 跑完回 Planner，以 replan 重做剩余部分（不调 Reviewer 的模型，判定由代码给出，`reason_code` 是 `user_guidance`；这个 Stage 里有 tool_loop 已经带上了纠偏时，照常走 Reviewer） | 取消这个 Stage，已成功的 Node 保留，以 replan 继续（verdict 的 `reason_code` 是 `user_interrupt`） |
| Planner 规划中 | 规划完放弃这个 Stage，带上纠偏重新规划 | 取消规划，带上纠偏重新规划 |
| Reviewer 评审中 | 不管判了什么（含 done）都不收尾，回 Planner | 取消评审，回 Planner |
| 已结束 | 拒绝（`task_finished`），界面提示可作为新任务发送 | 同左 |

多条纠偏按顺序一起带上。纠偏写进 Task State（Planner 与 Reviewer 都看得到，Reviewer 据此验收）。

**撤销这次调用**

| 被打断的调用 | 怎么处理 | 告诉用户与模型 |
| --- | --- | --- |
| 模型调用 | 取消 gRPC，网关随之断开上游；输出丢弃 | 上游可能已对部分输出计费，网关拿不到用量，这部分不进成本，计量记一行 `cancelled` |
| effect 为 none、read 的工具 | 取消 | “被用户打断，没有结果” |
| write_reversible（`shell_run`、`repo_edit`） | 调用前在工作副本建一个检查点（`GIT_INDEX_FILE` 指向临时索引，`git add -A` 加 `write-tree`，不动分支与用户的索引）。打断时杀掉整个进程组，把文件还原到检查点 | “已撤销这次调用的改动”。网络请求与已经跑完的后台效果撤不回，要照实说 |
| write_irreversible | **不打断**（做到一半撤不回）。等它结束；结果未知时按幂等键对账；之后立刻生效 | 界面说明“这一步不可撤销，完成后立刻生效” |

**撤回与修改**

| 消息的状态 | 撤回 | 修改 |
| --- | --- | --- |
| 排队中的下一个任务 | `CancelTask`（已有） | 新信号，在 Session 工作流的队列里原位替换，保持顺序；业务库的 `message` 同步 |
| 还没生效的纠偏 | 可以 | 可以 |
| 已经生效的纠偏 | 不行，提示“已生效”，可以再发一条 | 不行 |
| 立刻打断 | 不行：发出去调用就取消了 | 不行 |

工作流是单线程的，撤回与生效不会交错：谁的信号先到谁算。DurableEngine 只有信号没有 Update，所以结果靠事件 `guidance_updated` 告诉客户端。

**权限**：同取消：提交者本人，或有 `task.approve` 的人。租户取自认证身份。纠偏的文字不进 span 与日志，长度上限同消息（每个 Task 待生效的最多 20 条）。

**难点与防错**

- 取消要真的送到：`tool_loop_step`、`plan_round`、`review_round` 现在没有心跳，Temporal 送不到取消，调用会一直跑、一直花钱。这三个要加心跳。
- 打断与取消整个 Task 都表现为 `CancelledError`，要用标记区分。整个 Task 的取消优先。
- 打断时已发出的只读调用也要等取消完成，才写 history；每个 `tool_call` 都要有结果，否则下一次模型调用会被拒。
- `shell_run` 的取消要杀进程组，三种后端（local、K8s、Cloudflare）都要测。
- 改变命令序列：版本分支 `GUIDANCE_PATCH`；golden 补 `guidance_next_step`、`guidance_interrupt_tool_loop`、`guidance_interrupt_stage`。
- Temporal 的心跳有节流（heartbeat 超时的 0.8 倍）。现在 Node 的 heartbeat 超时最长 30 秒，取消要 24 秒才送到，不算“立刻”。能被打断的 activity 的 heartbeat 超时要短到几秒，实际发心跳也要跟上。
- 被打断的模型调用不占 `max_iterations`：模型没有回答，这一步还是同一步。
- 同时跑着几个 tool_loop 时，纠偏与打断对它们全都生效。
- 没有 `GUIDANCE_PATCH` 的旧运行收到信号只记日志，界面等 `task_finished` 再提示没有生效。
- 写测试时发现的现有缺陷，不只影响打断，**现在取消整个 Task 也一样**：
  - `AgentClient.exec` 是 `asyncio.to_thread` 里的阻塞请求：协程取消了，连接不断，K8s 与 Cloudflare 沙箱里的命令一直跑到超时。
  - 本地后端与 host 的 `shell_run` 取消时不杀进程，超时也只杀直接的子进程。
  - 真的 Cloudflare 桥（`containerFetch`）不转发客户端的断开，假桥转发。要改桥，并用 `AR_LIVE_CLOUDFLARE=1` 验证（花钱）。
- activity 分不清打断与取消整个 Task（都是取消）。所以取消整个 Task 时，正在跑的这次写调用也会被杀掉并还原；之前跑完的不动。

**验收**：上表每一格一条；撤回与生效的先后；每种后端的进程真的停了；检查点还原后工作副本与调用前一致（含未跟踪文件），用户的索引与分支不变；被打断的写调用不重试；write_irreversible 不被打断；取消整个 Task 优先于打断；重放。文件见第 8 节。

**实现时的改动**（2026-10-30，测试是按这些改的）

- 撤销的结果不走 Temporal 的取消失败：Python SDK 对“请求取消后以 CancelledError 结束”的 activity 一律上报不带 details 的取消，结果带不回来。activity 改为抛 `ApplicationError`（type `tool_interrupted`，details 第一项是结果），工作流当取消处理（`workflows/interrupts.py`）。
- activity 正好正常结束时，它吞掉同时到来的取消（等它的任务不再收到取消）。发问的 activity 正是这样：所以发问之后查一次任务是否被要求取消过（`Task.cancelling()`），是就撤回问题。别的调用吞了也无妨：结果照用，还没看过立刻打断的循环不再开始新的工具调用。
- Cloudflare 桥不用改：`AgentClient.exec` 给每条命令一个 `exec_id`，取消时既断开连接，也发 `POST /v1/exec/cancel`（agent 的新接口；取消先到时记一分钟，命令不再开始）。经 K8s API Server 或 Cloudflare 的桥时断开不一定传得到，这条路径不依赖它。
- 模型调用（规划、评审、tool_loop 的一步）被打断时不等取消送到就重来（TRY_CANCEL）：更快，代价是旧的调用最多再跑约 6 秒。工具调用与 Node 仍等取消完成，撤销的结果才带得回来。
- 能被打断的 activity 的 heartbeat 超时是 8 秒（`INTERRUPTIBLE_HEARTBEAT`），取消约 6 秒内送到。只对 `GUIDANCE_PATCH` 之后的运行。
- 用户要的 replan 不进 Reviewer 的升级台账（`self._history`）：否则用户纠偏几次会被算成“一直在 replan”，升级去问人。
- 修改排队中的消息：Session 工作流按信号的先后原位替换。coding-web 自己的任务记录在界面收到 `task_message_edited` 之后才改（`POST .../message/result`），Task API 的 QueryTask 不带消息，这样不用再改契约。

**设计书的改动建议**：附录 A 加“纠偏（Guidance）”；3.2.2 与附录 C.2 写明用户可以选生效时机，以及打断的撤销规则。

## 3. 侧栏“上下文”：显示占比（参考 Claude Code 的 /context）

**现在**：`contextBody` 显示 Task State 的内容（目标、事实、约束）。**要的是**这次模型调用的上下文里各类内容占多少，以及还剩多少。

| 界面的类别 | 来源 |
| --- | --- |
| System prompt | system 分区 |
| System tools | 平台工具的定义（`tool_tokens`） |
| MCP tools | MCP 工具的定义 |
| Memory | profile 分区（用户偏好与 Workspace 约定） |
| Task brief | brief 分区里之前对话以外的部分 |
| Messages | 之前的对话、工具调用与结果（history）、精确状态、status |
| Free space | 窗口减去以上之和 |

- `ContextBuilder.build` 在组装时算出各类的 token 估算（已有 `estimate`、`message_tokens`、`tool_tokens`），不新增模型调用。fast 的 answer 节点也算一份。
- 每次模型调用之后推一条 `context_usage` 事件：只有数字与类别名，没有内容。还带用过的压缩步骤（`applied`）。
- 界面：一条分段的进度条，加上各类的数字与百分比，标出压缩线。工具默认只看合计，展开能看到每个工具占多少。

## 4. 侧栏“输出”：各轮重要输出的汇总

**现在**：`outputBody` 是上一个 Task 的完整回答。**要的是**按轮汇总的重要文件与重要答案；全文仍在对话里。

- 每轮一条 `highlights`：`files`（重要文件，含为什么重要的一句话）、`answers`（重要答案，每条不超过 300 字），各最多 5 条。写进 Task State 的 `rounds[]`。
- 来源：
  - 走 Reviewer 模型的轮：Progress Check 的回答多一个 `highlights`，**同一次调用**，不多花一次模型请求。
  - fast 的轮（不调 Reviewer 模型）：代码取回答开头两句，不调模型。
  - 文件路径由代码核对：模型写的路径要是这一轮工具调用的目标，否则去掉（防编造）。工作流不能做 I/O，Reviewer 碰不到工作区，所以不查工作区；这一轮没碰过的文件也算不上这一轮的重要输出。改动过的文件（`repo_edit` 的目标）由代码补上；`shell_run` 改了什么代码不知道，不补。
  - Reviewer 模型出错、回答里 `highlights` 不合格，或者这一轮被纠偏、打断（没有 Reviewer 的回答）：同 fast 的轮，代码取开头两句。不合格的 `highlights` 不为它重问。
- 界面：按 Task、Round 分组；文件点开进文件抽屉；答案旁有“在对话里看”。不显示全文。

## 5. S-01（都是只加不改）

草案 2026-10-30 人审通过，已提交。**兼容性基线还没更新**：下面标“草案之外”的几处是实现时才发现要加的，等人看过再 `buf build -o proto/breaking-baseline.binpb`。

| 文件 | 改动 | 给哪一节 |
| --- | --- | --- |
| `proto/.../common/v1/enums.proto` | 枚举 `GuidanceMode`（`NEXT_STEP`、`INTERRUPT`）。草案写在 task_api.proto，但工作流的信号也要用，两边都引用 common，所以放这里（**草案之外**） | 2 |
| `proto/.../taskapi/v1/task_api.proto` | 消息 `SubmitGuidance`（guidance_id、text、mode）、`EditGuidance`（guidance_id、text）、`RetractGuidance`（guidance_id）、`EditQueuedMessage`（message）；`SignalTaskRequest.signal` 加这四个（4～7）；`TaskEventType` 加 `GUIDANCE_UPDATED` = 14、`TASK_MESSAGE_EDITED` = 15、`CONTEXT_USAGE` = 16 | 2、3 |
| `proto/.../workflow/v1/task.proto` | 枚举 `TaskGuidanceKind`（`SUBMIT`、`EDIT`、`RETRACT`）；`TaskGuidanceSignal`（guidance_id、kind、text、mode、by_user_id、at） | 2 |
| `proto/.../workflow/v1/session.proto` | `EditQueuedTaskSignal`（task_id、message、by_user_id、at）。草案写在 task.proto，但发给 Session 工作流的信号都在这个文件（**草案之外**） | 2 |
| `proto/.../workflow/v1/names.proto` | `SIGNAL_NAME_TASK_GUIDANCE`（`task_guidance`）、`SIGNAL_NAME_EDIT_QUEUED_TASK`（`edit_queued_task`）（**草案之外**） | 2 |
| `proto/.../modelgateway/v1/model_gateway.proto` | `ModelChoice.context_window`：网关把 Model Profile 的窗口带回来，第 3 节的分母才是真实窗口（**草案之外**） | 3 |
| `schemas/defs.schema.json` | `progress_event` 加三种；`guidance_id`、`guidance_mode`、`guidance_state`、`guidance_event_state`、`queued_edit_state`、`context_category`、`context_step`（枚举只能放在 defs，契约测试要求） | 2、3 |
| `schemas/task_state.schema.json` | 顶层 `guidance[]`（guidance_id、text、mode、state 为 pending 或 applied、applied_round）；`rounds[].highlights`（`files[]` 为 path、why，`answers[]` 不超过 300 字，各最多 5 条）。撤回的不留在 Task State，模型看不到 | 2、4 |
| `schemas/task_event_payload.schema.json` | `guidance_updated`（guidance_id、state、mode、round_seq、reason_code）；`task_message_edited`（state、reason_code，不带文字）；`context_usage`（round_seq、node_id、step、window_tokens、total_tokens、categories、applied，**另加** `effective_tokens` 与 `tools`：界面要标压缩线、能展开每个工具；`step` 可选，answer 节点没有） | 2、3 |
| `reviewer/prompts.py` 的输出 schema（代码里，不走 S-01） | Progress Check 加 `highlights` | 4 |

`reason_code` 的 `user_interrupt`、`user_guidance` 不用改 schema（代码用 pattern）。

## 6. 要人定的

| # | 问题 | 建议 |
| --- | --- | --- |
| 1 | 运行中按 Enter 是什么 | 纠偏（下一个检查点生效）；Ctrl+Enter 立刻打断；排队放进菜单 |
| 2 | 打断时是否自动还原这次调用改的文件 | 自动还原（你说的“撤销这次调用”）；代价是每次写调用前多一条 git 命令。网络请求撤不回，照实说 |
| 3 | 上下文占比的分母 | 模型的真实窗口，另标 Runtime 的压缩线（`effective_tokens`，现在默认 32000，比窗口小得多，只显示它会误以为快满了） |
| 4 | 输出汇总的来源 | Reviewer 同一次调用加 `highlights`；fast 的轮由代码截取，不调模型 |
| 5 | 原来“上下文”里的目标、事实、Round 列表放哪儿 | 不丢，收进右栏底部折叠的“任务详情” |

## 7. 顺序

1. 第 1 节（不改契约，省钱最直接）。
2. S-01（第 2～4 节一份）。
3. 第 3、4 节（风险低）。
4. 第 2 节：先纠偏与撤回、修改，再立刻打断（要加心跳、进程组与检查点，风险最高）。
5. coding-web：每一步的界面跟着上线，改动放在 coding-agent 仓库。

## 8. 验收测试

| 文件 | 看什么 |
| --- | --- |
| `services/py-worker/tests/test_task_guidance.py` | 第 2 节两张表的每一格（工作流）：等模型、跑工具、等审批、Atomic Node、模板、sub_agent、子 Task、规划中、评审中、收尾；撤回与修改的先后；取消整个 Task 优先；Planner 与 Reviewer 看得到；文字不进事件与日志 |
| `services/py-worker/tests/test_interrupt_undo.py` | 真的 activity：等模型时发心跳、取消送到；进程组被杀、文件还原、撤不回的照实说 |
| `services/py-worker/tests/test_workspace_checkpoint.py` | 检查点：还原后一致（含未跟踪文件）、用户的索引与分支不变、被忽略的文件与调用自己动了分支照实说、还原失败不报成功 |
| `services/py-worker/tests/test_context_usage.py` | 第 3 节：各类占比加起来等于总数、对话算 Messages、工具分系统与 MCP、真实窗口、没有内容、不多调模型、事件 |
| `services/py-worker/tests/test_round_highlights.py` | 第 4 节：同一次调用、不合格不重问、路径核对、改动文件补上、截断、脱敏、fast 的轮取开头两句 |
| `services/py-worker/tests/test_stage_replay.py` | 三份新 golden：`guidance_next_step`、`guidance_interrupt_tool_loop`、`guidance_interrupt_stage` |
| `libs/py-core/.../conformance/sandbox.py` | 一致性测试“取消 exec 后命令与子进程都停了”，本地、假桥上真的 agent、kind、真桥都跑这一条 |
| `libs/py-core/tests/adapters/test_sandbox_exec_cancel.py` | `AgentClient` 取消时断开连接 |
| `services/go-sandbox-agent/.../disconnect_test.go` | agent 看到断开、收到取消都杀进程组；取消先到时命令不开始 |
| `services/go-model-gateway/.../interaction_test.go` | 回答带窗口；取消不重试、不切换；计量记 `cancelled` |
| `services/go-task-service/.../taskapi/guidance_test.go` | 权限同取消、租户取自认证身份、参数校验、已结束与还没开始的原因码、修改排队的消息发给 Session |
| `services/go-task-service/.../session/guidance_test.go` | 原位替换保持顺序、最后一次算数、开始之后不改、续接带上、每次修改一个事件、活动重试不重复、只有 Session 能推 |
| `coding-agent` 的 `internal/server/interaction_test.go` | 转发给 Task API、原因翻成人话 |
| `coding-agent` 的 `internal/web/interaction_test.go` | 真的页面：Enter、Ctrl+Enter、菜单里排队、撤回、修改、已生效、不可逆的说明、排队的修改、结束时作为新任务；占比的数字与百分比、压缩线、工具折叠、任务详情；各轮汇总、不显示全文 |

## 9. 还没验证的

- **真的 Cloudflare 桥**：取消经 `POST /v1/exec/cancel` 走桥的通用转发，代码上不用改桥，但没有对真桥跑过（`AR_LIVE_CLOUDFLARE=1`，花钱，要人定）。同一个容器里，桥是不是能在 `/v1/exec` 没结束时处理第二个请求，只有真桥能证明。
- **真的 K8s（kind）**：这台机器没有，夜间任务或人工跑。sandbox-agent 的镜像要重新构建才有新接口；旧的 agent 收到取消是 404，只剩断开连接这一条路。
- **Docker 沙箱（`--sandbox docker`）的取消**：写了 `docker rm -f`，这台机器没有 Docker，没跑过。
- **真模型上 Reviewer 给的 `highlights` 好不好**：要评测，花钱，这里只测形状。
- **`repo_edit` 在被打断时**：host 上一次写文件没有取消点，做不到“写到一半”；存档模式的环境里没有检查点，照实说没能撤销。
- **sub_agent 读过的文件**：子工作流的工具调用目标没有带回父 Stage，模型提到它们时被当成“这一轮没碰过”去掉。
- **没有 `GUIDANCE_PATCH` 的旧运行收到信号**：只能看代码，重放测试只证明旧历史仍能回放。
