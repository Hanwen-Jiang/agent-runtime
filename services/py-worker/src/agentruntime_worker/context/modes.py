"""按工作模式区分上下文策略（附录 G.3）。工作模式取自 Task Plan 的 context.work_mode，没写时按 coding。

每种模式给出：
- must_keep：工具输出里必须精确保留的字段（遮蔽时留在占位符里），例如编码的退出码与测试状态、研究的 URL 与出处；
- keep_recent：遮蔽时保留原样的最近几步（“早遮蔽”的模式少留）；
- summary_focus：增量摘要里要求保留的内容（写进摘要提示）；
- probe_priority：Compaction Probe 抽事实时优先的类别。
Task Plan 的 context.must_keep 追加到 summary_focus。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

WORK_MODES = ("coding", "research", "conversation", "writing", "data_analysis", "batch", "long_horizon")
DEFAULT_MODE = "coding"


@dataclass(frozen=True)
class ModePolicy:
    name: str
    must_keep: tuple[str, ...]
    keep_recent: int
    summary_focus: tuple[str, ...]
    probe_priority: tuple[str, ...]


_POLICIES = {
    "coding": ModePolicy(
        name="coding",
        must_keep=("path", "paths", "files_changed", "command", "exit_code", "status", "failed", "passed", "error", "errors"),
        keep_recent=2,
        summary_focus=("文件改动清单", "错误原文", "命令与退出码", "测试状态", "决策与原因"),
        probe_priority=("fact", "artifact", "open_question", "constraint", "done_when", "milestone"),
    ),
    "research": ModePolicy(
        name="research",
        must_keep=("url", "title", "source", "published", "quote", "citation"),
        keep_recent=3,
        summary_focus=("引用（URL、要点、出处）", "已排除的假设", "待查问题"),
        probe_priority=("fact", "open_question", "artifact", "done_when", "constraint", "milestone"),
    ),
    "conversation": ModePolicy(
        name="conversation",
        must_keep=("preference", "commitment", "request", "ticket", "status"),
        keep_recent=4,
        summary_focus=("用户偏好", "已作出的承诺", "未解决的诉求"),
        probe_priority=("fact", "open_question", "constraint", "done_when", "milestone", "artifact"),
    ),
    "writing": ModePolicy(
        name="writing",
        must_keep=("path", "section", "comment", "status", "style"),
        keep_recent=2,
        summary_focus=("风格约定", "修改意见及其状态", "大纲与当前段落"),
        probe_priority=("constraint", "fact", "open_question", "artifact", "done_when", "milestone"),
    ),
    "data_analysis": ModePolicy(
        name="data_analysis",
        must_keep=("path", "schema", "columns", "row_count", "value", "result", "query", "code"),
        keep_recent=2,
        summary_focus=("数据的 schema", "已算出的数字及生成代码", "分析假设"),
        probe_priority=("fact", "artifact", "constraint", "open_question", "done_when", "milestone"),
    ),
    "batch": ModePolicy(
        name="batch",
        must_keep=("item", "id", "status", "error", "result"),
        keep_recent=2,
        summary_focus=("每项结果的状态表", "失败项与原因"),
        probe_priority=("fact", "artifact", "milestone", "open_question", "done_when", "constraint"),
    ),
    "long_horizon": ModePolicy(
        name="long_horizon",
        must_keep=("feature", "status", "version", "path", "milestone"),
        keep_recent=2,
        summary_focus=("功能清单", "进度", "版本历史"),
        probe_priority=("milestone", "fact", "artifact", "open_question", "done_when", "constraint"),
    ),
}


def policy_for(work_mode: str | None, *, extra_must_keep: tuple[str, ...] = ()) -> ModePolicy:
    policy = _POLICIES.get(work_mode or DEFAULT_MODE, _POLICIES[DEFAULT_MODE])
    extra = tuple(x for x in extra_must_keep if x and x not in policy.summary_focus)
    return replace(policy, summary_focus=policy.summary_focus + extra) if extra else policy
