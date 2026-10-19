"""Task 工作流与 Go 的 Session 工作流对上的名字（附录 K.1、K.5）。

取值必须等于 proto/agentruntime/workflow/v1/names.proto 里的 wire_name，由 tests/test_names.py 比对。
"""

from __future__ import annotations

SESSION_WORKFLOW = "SessionWorkflow"
TASK_WORKFLOW = "TaskWorkflow"

# Task 工作流收到的信号与提供的查询。
HUMAN_REPLY_SIGNAL = "human_reply"
TASK_STATE_QUERY = "task_state"
# Task 工作流结束时发给 Session 工作流。
TASK_FINISHED_SIGNAL = "task_finished"

TASK_QUEUE = "task"
DECISION_QUEUE = "decision"
MODEL_QUEUE = "model"
PROGRAM_QUEUE = "program"

# activity 名。
PLAN_ROUND = "plan_round"
REVIEW_ROUND = "review_round"
FINALIZE_TASK = "finalize_task"
EXECUTE_MODEL_NODE = "execute_model_node"
EXECUTE_PROGRAM_NODE = "execute_program_node"

TASK_STATE_SCHEMA_ID = "https://agentruntime.example/schemas/task_state.schema.json"
