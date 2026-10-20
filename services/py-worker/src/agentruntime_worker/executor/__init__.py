"""Node Executor（设计书 3.2.4、附录 D）：activity 侧执行 Atomic Node，并把 effect 决定的缓存、重试、幂等、审批规则
做成不可绕过的代码路径（计划 S-05）。

- keys：幂等键与缓存键（纯函数，工作流与 activity 共用）。
- model_gateway：Model Gateway 客户端接口。
- schemas：output_schema 的解析与校验。
- node_executor：execute_model_node、execute_program_node、ask_human、cancel_human、reconcile_node 五个 activity。
"""
