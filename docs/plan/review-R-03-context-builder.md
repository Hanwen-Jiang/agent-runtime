# R-03 评审：Context Builder 主体

> 评审对象：`6e4c003` 时的 `services/py-worker/src/agentruntime_worker/context`（builder、partitions、modes、summary、probe、tokens）与它的使用者
> `executor/tool_loop.py`，对照 Node Executor 的 model 节点（`executor/node_executor.py`）。主体在 `582a547`（Context Builder 主体与 S-11）提交，
> 之后 `54db03a`、`6c0b2e1` 有小改。
> 依据：实施计划第 5 节 R-03 的评审重点；设计书 3.2.7、附录 G、H.1（effective_ctx）；测试结果。按 6.2 的模板，不看实现过程。
> 实现者的自述 `docs/note/context-compression.md` 只拿来对照，结论以代码为准。评审日期 2026-10-23。
>
> **评审独立性**：同 R-04：实现出自之前的 Claude 会话，评审在新会话里只看代码、设计书与测试结果。计划第 7 节把 Context Builder 主体列为
> 中档实现、强档评审；实际由强档写成，严格按写审分离还要另一家的强模型或人复审。
>
> **范围**：S-11 的摘要提示与 Compaction Probe 以评测集为评审（T-02 已过门槛）。这里只看分区组装、阈值、mask 与 offload，以及 Probe 的接线。

## 测试结果（评审时）

- py-worker：`test_context_partitions.py`、`test_context_ladder.py`、`test_context_summary.py`、`test_context_probe.py`，以及 tool_loop、
  Node Executor 相关测试，共 86 项通过。
- S-11 真模型评测（T-02，2026-10-22）：full 在开发集与留出集上都不劣于 mask_only（Probe 通过率中位数 96% 对 70%、83% 对 50%）。

## 1. 评审重点逐条

### 1.1 分区顺序与缓存断点

| 方面 | 结论 | 位置与说明 |
| --- | --- | --- |
| 分区顺序 | **已处理** | system、profile、brief、history、status 依次排列，status 在最后（`partitions.py:33-50`）。 |
| 断点 | **已处理**（按 G.1） | system、profile、brief 三条消息末尾设 `cache_breakpoint`，history 里一律清掉（`partitions.py:38-47`）；网关原样转成供应商的 `cache_control`（`openaicompat/wire.go:111`）。 |
| 前缀稳定 | **已处理** | brief 只放 Task 内不变的内容（目标、验收条件、本 Goal、输入、计划、经验，`builder.py:212-236`）；mask 是确定的，已遮蔽的不再变（`builder.py:177-182`）；持久摘要存在 ObjectStore，重复构建读回同一份（`builder.py:341-348`）；profile 只追加变更（`partitions.py:53`）。 |
| history 没有断点 | **不确定**（设计取舍） | history 最长、每步都在增长，现在每一步都按全价重读整段 history。T-05 时提出过在 history 最后一条消息上加滚动断点（Anthropic 每次最多 4 个断点，正好再加一个），因与 G.1 不一致没做（`model-gateway-direct.md:96`）。收益要用真模型对比 `cache_read_tokens`；要不要改 G.1 需要人定。 |
| model 节点 | **未处理** | 3.2.7 说 Context Builder 为“每次生成型模型调用”组装上下文，但 Node Executor 的 model 节点还是 S-05 的最小上下文：inject 的内容拼进 system 文本（`node_executor.py:491-505`），没有分区、没有断点，也不带 Task State 与 Task Plan。不同 Node 的 inject 不同，system 前缀在 Node 之间不能复用。目前只有 tool_loop 经 Context Builder（`tool_loop.py:54`）。 |

### 1.2 外置内容只以路径出现

| 方面 | 结论 | 位置与说明 |
| --- | --- | --- |
| 大工具输出 | **已处理** | 单条观察超过 offload_tokens（默认 2000）就写进 ObjectStore（按租户绑定，键 `context/<task>/r<round>/<node>/step-<i>.json`），上下文只留 400 字符的摘录与路径（`builder.py:170-175`、`builder.py:271-273`、`builder.py:333-339`）。 |
| 遮蔽 | **已处理** | 占位符带路径和工作模式要求精确保留的短字段（`builder.py:269-275`）；工具报的错误不遮蔽（`builder.py:263`）。 |
| Node 的大结果 | **已处理** | Node Executor 把超过 64 KiB 的结果写进 ObjectStore，只留摘要与引用（`node_executor.py:373-382`）。 |
| 按需取回 | **未处理** | 3.2.7 第 2 条与 G.1 要求外置的内容“按需取回”，tool_loop 的平台提示也让模型“用读取类工具按路径取回”（`tool_loop.py:32`），但没有任何工具能读 `obj://` 路径：`repo_read` 只读工作区文件，ObjectStore 不对模型开放。被外置或遮蔽的内容，模型只能重新调用原来的工具（重读文件、重跑命令、重取网页），这正是 S-11 要压低的重复操作；对 shell_run，重跑还可能有副作用。 |
| Goal Node 的输入 | **未处理** | brief 原样写入 Node 的全部 inputs（`builder.py:222-223`），不外置、不截断，而 brief 不压缩。上游结果在 64 KiB 以内都是原文，几十 KB 的输入会让固定分区本身就超过 summarize_at：阶梯每一级都压不下来，每步都摘要并 reset，最后照样发出超长的上下文。 |

### 1.3 阈值按有效长度设

| 方面 | 结论 | 位置与说明 |
| --- | --- | --- |
| 阈值来源 | **已处理（配置）/ 未处理（按模型）** | mask_at 40%、summarize_at 70% 乘以 effective_tokens，Task Plan 可以覆盖（`builder.py:160-163`）。effective_tokens 来自 `AR_CONTEXT_EFFECTIVE_TOKENS`，全局一个值（`tool_loop.py:128`）。附录 H.1 的 `capability.effective_ctx` 不在 Model Profile 的 schema 里（`schemas/model_profile.schema.json` 只有标称的 `constraints.context_window`）；Worker 也不知道这次调用会落到哪个模型（R-02 评审 1.4：没有按 Task 固定主模型）。 |
| 计算口径 | **未处理（少算）** | 只算消息（`builder.py:133-139`），不算随请求给出的工具定义（`tool_loop.py:68`）。token 按字符估算（`tokens.py:8`：ASCII 每 4 个字符一个，其余每个字符一个），没有用网关返回的实际 input_tokens 校准过，偏差的方向和大小没测过。 |
| status 的上限 | **未处理（截掉精确状态）** | status 整体截到 500 token，从尾部截（`partitions.py:49`）。status 里依次是：进度、补回的事实、精确状态（Task State 的约束、已验证事实、产物、里程碑、待查与假设）、最近的动作、最近的错误、下一步（`builder.py:238-255`）。Task State 稍大（十几条事实）时，最近的动作、错误与“下一步”先被截掉，再多就连精确状态也截。3.2.7 第 4 条要求精确状态由 Task State 注入、不交给摘要；截掉的部分既不在摘要里，也不在上下文里。 |
| reset 的触发 | **部分处理** | 同一动作连续 reset_repeats（3）次，或摘要后仍超过 summarize_at（`builder.py:191-198`）。G.2 的“压缩若干次后质量下降”没有：Probe 答错只补回原文，不触发 reset，也不回退到更保守的压缩（G.4）。 |

## 2. 其他风险

### 2.1 确定性

Context Builder 只在 tool_loop_step activity 里运行（`tool_loop.py:110`），模型调用与 ObjectStore 都在 activity 里；外置的键由步号决定、
带 `if_absent` 写入（`builder.py:336`），重复构建结果相同。没有发现确定性风险。

### 2.2 并发

持久摘要 `summary.json` 直接覆盖写（`builder.py:371`）。activity 超时被重试、上一次尝试还没结束时，两次尝试各自摘要，后写的覆盖先写的，
覆盖范围可能倒退。后果只是多花一次摘要：history 按 covered 截取，不会丢步骤。

### 2.3 租户隔离

**已处理**：ObjectStore 按 Task 的租户绑定（`builder.py:164`），摘要与 Probe 的模型调用都带租户上下文（`builder.py:362`、`builder.py:386`）。

### 2.4 持久摘要只增不减

增量合并按分区追加去重，旧条目原样保留（`summary.py:73`），符合“只做增量合并，不整体重写”。但“未决”“下一步”两个分区写的是当时的状态，
也只增不减：早已解决的未决项、已经做完的下一步一直留在摘要里，可能把模型引回去重做（附在摘要后的“已执行过的动作”能抵消一部分）。
长任务里摘要本身也没有上限，最终和 1.2 的输入一样，变成压不下去的固定部分。

### 2.5 Probe 的信号偏弱

Probe 的事实全部取自 Task State 与 Task Plan（`probe.py:100-117`），而这些行原样写在 status 与 brief 里（`probe.py:62` 的 state_lines 就是
status 的写法）。只要 status 没被截断，Probe 几乎必过：它测的是“注入还在不在”，不是“摘要保住了什么”。和 1.3 的 status 截断合起来看，
Probe 能抓到的问题恰好来自截断。

### 2.6 运行时指标

G.6 的指标（压缩后的成功率、重复操作率、缓存命中率、每个 Task 的 token）只在评测里算；运行时只把用到的阶梯步骤与 Probe 结果写一条日志
（`tool_loop.py:80-87`），没有 span 属性或指标。

## 3. 与设计书不一致的地方

1. 3.2.7：model 节点不经 Context Builder。
2. 3.2.7 第 2 条、G.1：外置的内容不能按需取回。
3. 3.2.7 第 3 条、G.5、H.1：有效长度是全局配置，不按模型；Model Profile 的 schema 缺 effective_ctx。
4. 3.2.7 第 4 条：精确状态会被 status 的上限截掉。
5. G.2 的 reset 没有“质量下降”触发；G.4 的摘要模型不按目标切换（固定 `small@low`）。
6. G.5“推理模型：工具调用期间保留思考的连续性”：思考不回传（rationale 只进 trace），prune 是空操作（`builder.py` 模块说明第 3 条）。

## 4. 处理建议

**本期修**：

1. Goal Node 的输入超过 offload_tokens 时外置：写进 ObjectStore，brief 里只留路径与摘录（写法同 offload）。
2. 精确状态不受 status 的 500 token 上限截断（具体做法见回复里的决定项）。
3. 阈值的 token 计数加上工具定义。

**需要人定**：

- 按需取回：加一个只读的平台工具，读本租户 `context/` 与 `node-results/` 下的对象，并让每个 tool_loop 的工具集都带上它。
- history 的滚动断点：要不要改 G.1。
- 持久摘要的“未决”“下一步”是否改为按最新一次摘要替换。

**登记到后续**（T-17 时写进 `docs/reference/risks-and-open-questions.md`）：model 节点接 Context Builder；effective_ctx 进 Model Profile
（改 schema，走 S-01）并按 Task 固定主模型（R-02）；token 估算按实际 input_tokens 校准；reset 的质量触发与 G.4 按目标切换摘要模型；
运行时指标；`summary.json` 改条件写入。

## 5. 处理结果（2026-10-23）

人确认了决定 D6–D8、D11–D13。验收测试先行：`services/py-worker/tests/test_context_r03.py`、`test_context_read.py`，以及
`test_context_summary.py` 里新增的三条。

1. Goal Node 的输入超过 offload_tokens 时写进 `context/<task>/r<round>/<node>/inputs.json`，brief 里只留路径与 400 字符的摘录（算作 offload）；
   小的输入照旧原样写。
2. 精确状态单独成段（【精确状态】），放在 history 之后、status 之前，不受 status 500 token 的上限截断。它自己的上限是
   `ContextConfig.state_tokens`（1500）：超出时按 Task State 的顺序放到上限为止，注明“另有 N 条”与完整 Task State 的路径
   （`…/task-state.json`）。status 只留进度、补回的事实、最近的动作与错误、下一步。两条旧测试按新布局改了取消息的位置
   （`test_exact_state_comes_from_the_task_state_not_the_summary`、`test_the_prefix_is_byte_stable_while_history_grows`）。
3. `ContextRequest.tools`：工具定义计入阈值与 `BuiltContext.tokens`；tool_loop 先取工具定义，同一份交给 Context Builder 与 Model Gateway。
4. 按需取回（D11）：平台内工具 `context_read`（read）总是登记，按 obj:// 路径读本租户 `context/` 与 `node-results/` 下的对象，长内容按
   offset 分段；别的租户的路径 not_found，别的前缀 denied。Context Builder 外置的内容与它读同一个 ObjectStore（`tools.context_objects`）。
   真正的 Planner 在 plan_round 里给每个 tool_loop 的 Goal Node 追加它（已登记、且这个 Goal 的 effect 上限允许 read 时）；只在 activity 里加，
   Stage Runner 与工作流不变。没覆盖：ToolInvocation 不带 task_id，只能限到租户，读得到同租户别的 Task 的上下文对象；桩 Planner 与
   Recipe 在 Stage Runner 里展开出的 Goal Node 不加。
5. 持久摘要（D13）：当前的“未决”“下一步”交给摘要模型，按这一段的进展更新（解决的去掉、仍成立的保留、新的加上），合并时以最新一次替换；
   意图、决策、产物仍只追加，也不交给模型。只替换不交给模型会丢掉早先仍未解决的问题，所以两件事一起做。S-11 的真模型评测等额度补上后重跑。
6. history 的滚动断点（D12）：等 T-13 用真模型看到缓存命中率再定，G.1 暂不改。
