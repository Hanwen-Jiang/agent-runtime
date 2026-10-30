"""Task 工作流与 Go 的 Session 工作流对上的名字（附录 K.1、K.5）。

取值必须等于 proto/agentruntime/workflow/v1/names.proto 里的 wire_name，由 tests/test_names.py 比对。
"""

from __future__ import annotations

SESSION_WORKFLOW = "SessionWorkflow"
TASK_WORKFLOW = "TaskWorkflow"

# Task 工作流收到的信号与提供的查询。
HUMAN_REPLY_SIGNAL = "human_reply"
TASK_GUIDANCE_SIGNAL = "task_guidance"
TASK_STATE_QUERY = "task_state"
# Task 工作流结束时发给 Session 工作流。
TASK_FINISHED_SIGNAL = "task_finished"

TASK_QUEUE = "task"
# Go 的 Session 工作流与它的 activity 所在的队列；Task 工作流经这里的 PUBLISH_TASK_EVENT 推送进度事件（T-11b）。
SESSION_QUEUE = "session"
DECISION_QUEUE = "decision"
MODEL_QUEUE = "model"
PROGRAM_QUEUE = "program"

# activity 名。
PLAN_ROUND = "plan_round"
REVIEW_ROUND = "review_round"
FINALIZE_TASK = "finalize_task"
EXECUTE_MODEL_NODE = "execute_model_node"
EXECUTE_PROGRAM_NODE = "execute_program_node"

# Stage Runner 用到的 activity（S-04）。
LOAD_STAGE_POLICY = "load_stage_policy"
RESOLVE_CATALOG = "resolve_catalog"
EXPAND_TEMPLATE = "expand_template"
TOOL_LOOP_STEP = "tool_loop_step"
CALL_TOOL = "call_tool"
# Policy Engine 的 Hook，作为 local activity 在运行工作流的 Worker 上执行。
RUN_HOOKS = "run_hooks"

# Node Executor 用到的 activity（S-05）。
ASK_HUMAN = "ask_human"
CANCEL_HUMAN = "cancel_human"
RECONCILE_NODE = "reconcile_node"
# 内置的 program 模板：在 Sandbox 里执行代码，inputs 为 {"runtime", "code", "stdin"?}。
SANDBOX_TEMPLATE = "sandbox_exec"

# 跨语言的 activity：Task 工作流把进度事件交给 Go 的 Session Worker，由它分配 event_id 并推进 Cache（T-11b）。
PUBLISH_TASK_EVENT = "publish_task_event"

# Python 内部的子工作流（附录 K.1：Goal Node 的 sub_agent 与大批量 template 展开）。
SUB_AGENT_WORKFLOW = "SubAgentWorkflow"
SUBGRAPH_WORKFLOW = "SubgraphWorkflow"

# 沙箱环境表的对账（D-11）：平台级的定时工作流（固定的工作流 ID，Worker 启动时确保它在跑）与它的 activity，只在 Python 里。
SANDBOX_RECONCILE_WORKFLOW = "SandboxReconcileWorkflow"
SANDBOX_RECONCILE_WORKFLOW_ID = "platform-sandbox-reconcile"
RECONCILE_SANDBOXES = "reconcile_sandboxes"
# 按项目定制的运行环境（D-17）：每个环境定义一个构建工作流（ID 由租户与键定，同一个定义只构建一次）与它的 activity，只在 Python 里。
ENV_BUILD_WORKFLOW = "EnvironmentBuildWorkflow"
BUILD_ENVIRONMENT = "build_environment"
# Workspace API 的操作（D-06）：task-service 的 WorkspaceService 起这个工作流（task 队列，ID wsop-<operation_id>），
# 工作流名在 names.proto 里；它的 activity 只在 Python 里（program 队列）。
WORKSPACE_OPERATION_WORKFLOW = "WorkspaceOperationWorkflow"
WORKSPACE_OPERATION = "workspace_operation"

TASK_STATE_SCHEMA_ID = "https://agentruntime.example/schemas/task_state.schema.json"
