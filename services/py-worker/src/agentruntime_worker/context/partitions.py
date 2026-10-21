"""五个分区（附录 G.1）：越稳定越靠前，以保住提示缓存的前缀。

| 分区 | 内容 | 缓存 |
| system | 平台提示、工具集定义、租户策略 | 末尾设断点 1 |
| profile | 用户偏好、Workspace 约定；新偏好以变更形式追加，不改写 | 末尾设断点 2 |
| brief | Task Plan：目标、验收条件、约束、计划摘要、检索到的经验；replan 时整体替换 | 末尾设断点 3 |
| history | 动作与观察的轨迹，只追加；压缩的主要对象 | |
| status | 进度、待办、下一步与精确状态，放在最末尾，约 500 token 以内 | |
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg

from agentruntime_worker.context.tokens import truncate

STATUS_TOKENS = 500
PROFILE_HEADER = "【用户偏好与 Workspace 约定】"
BRIEF_HEADER = "【任务简报】"
STATUS_HEADER = "【当前状态】"


@dataclass
class Partitions:
    system: list[str] = field(default_factory=list)
    profile: list[str] = field(default_factory=list)
    brief: list[str] = field(default_factory=list)
    history: list[mg.ChatMessage] = field(default_factory=list)
    status: str = ""

    def to_messages(self) -> list[mg.ChatMessage]:
        """system、profile、brief 各成一条消息并在末尾设缓存断点（空的分区省略）；history 原样；status 是最后一条。
        status 超过 STATUS_TOKENS 时截断。"""
        out: list[mg.ChatMessage] = []
        if self.system:
            out.append(mg.ChatMessage(role=mg.CHAT_ROLE_SYSTEM, text="\n\n".join(self.system), cache_breakpoint=True))
        if self.profile:
            out.append(mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=PROFILE_HEADER + "\n" + "\n".join(f"- {p}" for p in self.profile), cache_breakpoint=True))
        if self.brief:
            out.append(mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=BRIEF_HEADER + "\n" + "\n".join(self.brief), cache_breakpoint=True))
        for message in self.history:
            copy = mg.ChatMessage()
            copy.CopyFrom(message)
            copy.cache_breakpoint = False
            out.append(copy)
        if self.status:
            out.append(mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=truncate(STATUS_HEADER + "\n" + self.status, STATUS_TOKENS)))
        return out


def profile_with_changes(previous: list[str], current: list[str]) -> list[str]:
    """profile 只追加：已有的条目原样保留，新增的条目以“偏好变更”追加在后面，删掉的条目以“撤销”追加。"""
    out = list(previous)
    out += [f"偏好变更：新增 {p}" for p in current if p not in previous]
    out += [f"偏好变更：撤销 {p}" for p in previous if p not in current and not p.startswith("偏好变更：")]
    return out
