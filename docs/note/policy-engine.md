# Policy Engine：挂载点、Hook 与 fail-closed

> 学习笔记，2026-10-24。对应代码：`services/py-worker/src/agentruntime_worker/policy/`（`__init__.py` 引擎、`hooks.py` Hook 目录）；
> 调用点在 `workflows/stage_runner.py`（`_hooks`、`_node_hooks`、`_guarded`、`_registered`）与 `workflows/task.py`（Task 级挂载点、before_finish）；
> Hook 的来源 `registry/catalog.py` 的 `RegistryStagePolicy`。设计依据：设计书 3.2.6，附录 D.4、F；细化 `docs/plan/policy-engine.md`；
> 评审 `docs/plan/review-R-01-policy-engine.md` 与另一家复审 `docs/plan/reviews/R-01-gpt-6-sol.md`。

## 1. 它是什么

Worker 进程内的一个库：在固定的挂载点执行**便宜、确定**的检查。不单独部署，不调模型，不做网络 I/O。每个 Hook 只返回四种结果之一：

| 结果 | 意思 |
| --- | --- |
| allow | 放行 |
| deny | 拒绝本次动作：Node 记为 denied，交给 Reviewer，不自动重试 |
| modify | 改写输入或输出（JSON Patch），例如脱敏 |
| inject | 往上下文追加信息，例如项目规则 |

## 2. 挂载点

| 挂载点 | 时机 | 例子 |
| --- | --- | --- |
| on_task_received、on_task_start | 规划之前 | 敏感内容过滤；deny 时 Task 直接 abort，不规划 |
| before_stage、after_stage | 每个 Stage 前后 | 预算检查；after_stage deny 让整个 Stage 的结果作废 |
| before_node、after_node | 每个 Node 前后 | 预算、幂等键、写者；trace、检查点、审计、脱敏 |
| before_compact | Context Builder 压缩前 | 第一期没接：策略在这里声明了 Hook，Task 一开始就拒绝（不悄悄跳过） |
| before_finish | Reviewer 判 done 之后、收尾之前 | 对照验收条件复核；驳回最多 2 次，第 3 次问人 |
| on_task_end | 收尾 | 只记录，不改变结局 |

## 3. 默认 Hook（按 effect 自动插入，策略去不掉）

| effect | before_node | after_node |
| --- | --- | --- |
| 所有 | budget_check | audit_log（永远最后） |
| read | — | trace_result（结果写进 trace，按 UTF-8 截到 8 KiB） |
| write_reversible | idempotency_check、single_writer_lock | checkpoint |
| write_irreversible | idempotency_check | — （人工审批是 Stage Runner 的代码路径，不是 Hook） |

策略声明的 Hook（租户策略、Task 固定的 Recipe，经 Registry 合并）只能加：`pii_redaction`（after_node，modify）、`acceptance_recheck`（before_finish）。
after_node 上策略声明的排在默认的前面，**前一个 Hook 的 modify 先作用到 subject 上再交给下一个**：脱敏之后 trace_result 记的才是脱敏过的结果
（另一家复审发现之前 trace 先记了原文）。

## 4. fail-closed

拿不准就拒绝：

- Hook 名字不认识、挂错挂载点、Hook 抛异常、返回的结果不认识或不完整（含用不上的 patch）→ deny；异常只记类型，不记原文（可能带数据）。
- `run_hooks` 这个 local activity 本身失败（重试 3 次之后）→ “Hook 执行失败”，按 deny。之前没给重试上限，SDK 默认无限重试，
  Collector 不可用时所有 Task 卡在 after_node 上——现在 Telemetry 写不出去只记日志，Hook 结果照常返回。
- 租户策略、固定的 Recipe 取不到 → Task 一开始就 abort；Node Catalog 取不到 → 整个 Stage 拒绝。
- 接了 Registry 时，Atomic Node 的 executor 与登记的不同、effect 写低了、模板没登记，都在 Hook 与审批之前拒绝。

## 5. 审计

- 每次 deny 记一条 `policy.decision`；`audit_log` 记 Node、模板、effect、role、结局（status、failure_class）、输入与结果的哈希，不记原文。
  执行失败、结果未知、审批被拒的 Node 也审计（R-01 评审之后）。
- 记录写进 Telemetry（开发是标准错误）。持久的审计存储还没有，已登记。

## 6. 还没做的（登记在 `docs/reference/risks-and-open-questions.md` 3.3 节）

tool_loop 的单次工具调用不经过 before_node、after_node（只有整个 Goal Node 前后各一次）；并行 Node 的预算预留；before_compact；提示注入检测；
持久审计；`single_writer_lock` 只复述 Stage Runner 的判断；GateRecord 来源的验证。

## 7. 面试速查

**Q：Hook 里为什么不能调模型？**
A：Hook 在每个 Node 前后都跑，必须便宜、确定、可审计。调模型既慢又贵，结果还不确定，同样的输入可能放行也可能拒绝，策略就没法测试和解释。需要推理的判断交给 Reviewer。

**Q：fail-closed 是什么意思？为什么这样设计？**
A：检查本身出错时按拒绝处理，而不是放行。安全检查最怕“出错了就跳过”：Hook 没加载、策略取不到、返回了奇怪的结果，都不能变成默认放行。代价是可用性，
所以要配合重试上限与清晰的拒绝理由，让 Reviewer 能处理。

**Q：deny 之后为什么不重试？**
A：被策略拒绝的动作原样重试结果还是拒绝，只会浪费 Round。Reviewer 对 denied 判 replan（换做法或问人），并受各级次数上限约束。

**Q：人工审批为什么不做成 Hook？**
A：Hook 的来源是策略，策略可以配错或被改写；审批是 write_irreversible 的硬性要求，做成 Stage Runner 的固定代码路径，Node Executor 再兜底检查审批记录。

**Q：脱敏 Hook 和 trace Hook 的顺序有什么讲究？**
A：改写类的 Hook 必须先作用、后面的 Hook 看到改写后的结果，否则记录类的 Hook 会把原文写进 trace。实现上引擎按顺序执行，把前一个的 modify 应用到副本上再交给下一个，
after_node 上策略声明的（脱敏）排在默认的（trace）前面。

**Q：驳回类的 Hook 怎么防止死循环？**
A：有次数上限：before_finish 驳回最多 2 次，之后问人；Node 级的 deny 由 Reviewer 的升级上限兜住；tool_loop 里不可逆工具被拒 2 次就把整个 Goal Node 记为 denied。
