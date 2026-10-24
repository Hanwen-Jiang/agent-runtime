"""activity 超时（计划 S-03 难点“心跳”）：长 activity 要发心跳，heartbeat 超时短于 start-to-close 超时，
Worker 崩溃后 activity 才能被及时接手。纯函数，工作流代码可以直接用。
"""

from __future__ import annotations

from datetime import timedelta

from temporalio.common import RetryPolicy

# Planner、Reviewer 等决策 activity 的重试。
DECISION_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_interval=timedelta(seconds=30), maximum_attempts=5)
# Policy Engine 的 Hook（local activity）。重试有上限：用完就按“Hook 执行不了”拒绝（R-01 评审第 4 节第 1 条），
# 不用 SDK 默认的无限重试。
HOOK_START_TO_CLOSE = timedelta(seconds=30)
HOOK_RETRY = RetryPolicy(initial_interval=timedelta(milliseconds=500), maximum_interval=timedelta(seconds=5), maximum_attempts=3)

# Node 没给 budget.timeout_s 时的 start-to-close。
DEFAULT_NODE_TIMEOUT = timedelta(minutes=10)
# heartbeat 超时的上下限。
MAX_HEARTBEAT = timedelta(seconds=30)
MIN_HEARTBEAT = timedelta(seconds=1)

# Planner、Reviewer 的决策 activity 与收尾写回。
DECISION_START_TO_CLOSE = timedelta(minutes=2)
# 要等强档模型的决策 activity：plan_round、review_round、tool_loop_step。T-13 的真模型端到端里，一次完整规划（约 1.2 万输入 token、
# 带思考）经聚合网关超过 90 秒，2 分钟只够一次尝试，Planner 悄悄回退到保守规划。放到 5 分钟，网关的两次尝试与切换来得及。
# 这些 activity 不发心跳，Worker 崩溃后要到超时才重试，所以只给这三个放宽。
MODEL_DECISION_START_TO_CLOSE = timedelta(minutes=5)
FINALIZE_START_TO_CLOSE = timedelta(minutes=1)


def model_node_timeouts(timeout_s: float | None) -> tuple[timedelta, timedelta]:
    """由 Node 的 budget.timeout_s 给出 (start_to_close, heartbeat_timeout)，保证 0 < heartbeat < start_to_close。

    heartbeat 取 start-to-close 的 1/20，限制在 1 到 30 秒之间；节点很短时不超过 start-to-close 的一半。
    """
    if timeout_s is None:
        start_to_close = DEFAULT_NODE_TIMEOUT
    elif timeout_s <= 0:
        raise ValueError("timeout_s 必须大于 0")
    else:
        start_to_close = timedelta(seconds=timeout_s)
    heartbeat = min(MAX_HEARTBEAT, max(MIN_HEARTBEAT, start_to_close / 20))
    if heartbeat >= start_to_close / 2:
        heartbeat = start_to_close / 2
    return start_to_close, heartbeat
