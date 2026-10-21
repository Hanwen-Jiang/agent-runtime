"""Planner：Round 的开头，决定这一轮做什么（设计书 3.2.2，附录 C、D.7、E.1；计划 S-09）。

- 分诊与 Triage（triage）：规则由代码判定，小模型分类器只给信号（消息类别、任务族、置信度、预计的 effect、
  不可逆动作的参数是否明确）。风险高于本 Session 已授权的等级一律 full；分类器出错按 full。
- fast（不调用强模型）：一个 model 节点，续改用改写模板，其余用作答模板。
- light（不调用强模型）：命中的 Recipe 展开为 template 模式的 Goal Node，后接一个 deliver 节点。
- full 与 replan：一次强模型调用，结构化输出 Task Plan 与 Stage；代码校验（validate），不合格带上错误重问一次，
  仍不合格回退到保守规划（一个 tool_loop 的 Goal Node）。Triage 级别、Stage 的 seq 与幂等键由代码定，不由模型写。
- replan 只重做剩余部分：上一轮 Reviewer 保留的 Node（replan.keep_nodes）不得出现在新 Stage 里。
  Reviewer 判 repair 换方法时，修复范围之外、上一轮成功的 Node 同样不得重做（S-10）。
- 提示按 Context Zones 分区（prompts）：固定说明与 Node Catalog 在 system 区并设缓存断点，Task Plan 与 Session
  上下文在 brief 区，本轮的消息在最后，保住提示缓存。

Session 上下文（已授权的风险等级、Recipe 缓存、剩余预算、Node Catalog）由 context.PlanningContextSource 提供；
Registry 与 Memory Service 还没有实现，先用内存实现。
"""
