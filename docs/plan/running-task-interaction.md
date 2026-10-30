# 运行中的交互：并行工具调用、纠偏与打断、上下文占比、输出汇总

> 状态：2026-10-30 设计稿，待人确认。来源：人测试 coding-web 时提的四件事。第 1 节不改契约，验收测试已写；第 2～4 节要改 proto 与 schema，草案在第 5 节，人审过 S-01 再写测试。
> 另一个会话（Runtime 进度事件的代码）正在改 `workflows/task.py`、`planner`、`task_event_payload.schema.json` 与 coding-web 的 `app.js`。核心链路同一时间只由一个 agent 改，这里的实现排在它提交之后。

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
- 进度事件不用改：`tool_status` 每个调用各一组，`step` 相同，靠 `target` 区分。

**设计书的改动建议**：附录 E.1“每一步是一次 model 或 program 调用”改成“一步思考可以发起多个工具调用：只读的并发，写的按顺序”。

**验收**（`services/py-worker/tests/test_parallel_tool_calls.py`）：模型请求数；全部执行、结果按序；并发的真实顺序（看事件历史）；写的顺序与失败后不再执行；逐个审批；预算；上限；不认识的工具；进度事件；重复错误按步算；Context Builder 的消息结构；并发读共用一个环境。另外补一份 golden（`tool_loop_parallel`），证明旧历史仍能回放。

**上线后看**：`chat` 次数与工具调用次数之比，这个数字说明实际省了多少。

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
| tool_loop 跑工具 | 跑完这批，下一次模型调用带上 | 按下表撤销，再继续 |
| 等审批或问人 | 继续等，回复后带上 | 撤回问题，按拒绝处理，带上纠偏 |
| Atomic Node、模板、sub_agent、子 Task | 这个 Stage 跑完回 Planner，以 replan 重做剩余部分 | 取消这个 Stage，已成功的 Node 保留，以 replan 继续（verdict 的 `reason_code` 是 `user_interrupt`） |
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

**验收**（S-01 审过后写）：上表每一格一条；撤回与生效的先后；每种后端的进程真的停了；检查点还原后工作副本与调用前一致（含未跟踪文件），用户的索引与分支不变；被打断的写调用不重试；write_irreversible 不被打断；取消整个 Task 优先于打断；重放。

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
  - 文件路径由代码核对：模型写的路径要在这一轮的工具调用目标或工作区里存在，否则去掉（防编造）。改动过的文件由代码补上。
- 界面：按 Task、Round 分组；文件点开进文件抽屉；答案旁有“在对话里看”。不显示全文。

## 5. S-01 草案（待人审，都是只加不改）

| 文件 | 改动 | 给哪一节 |
| --- | --- | --- |
| `proto/.../taskapi/v1/task_api.proto` | 枚举 `GuidanceMode`（`NEXT_STEP`、`INTERRUPT`）；消息 `SubmitGuidance`（guidance_id、text、mode）、`EditGuidance`（guidance_id、text）、`RetractGuidance`（guidance_id）、`EditQueuedMessage`（message）；`SignalTaskRequest.signal` 加这四个；`TaskEventType` 加 `GUIDANCE_UPDATED`、`TASK_MESSAGE_EDITED`、`CONTEXT_USAGE`（编号等另一个会话提交后再定） | 2、3 |
| `proto/.../workflow/v1/task.proto` | `TaskGuidanceSignal`（guidance_id、kind、text、mode、by_user_id、at）；`EditQueuedTaskSignal`（task_id、message） | 2 |
| `schemas/task_state.schema.json` | 顶层 `guidance[]`（guidance_id、text、mode、state、applied_round）；`rounds[].highlights`（files、answers） | 2、4 |
| `schemas/task_event_payload.schema.json` | 事件 `guidance_updated`（guidance_id、state 为 pending、applied、retracted、rejected，mode，reason_code）、`task_message_edited`、`context_usage`（round_seq、node_id、step、window_tokens、total_tokens、categories、applied） | 2、3 |
| `reviewer/prompts.py` 的输出 schema（代码里，不走 S-01） | Progress Check 加 `highlights` | 4 |

`reason_code` 的 `user_interrupt` 不用改 schema（代码用 pattern）。

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
