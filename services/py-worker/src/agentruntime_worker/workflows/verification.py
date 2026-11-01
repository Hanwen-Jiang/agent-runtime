"""这个 Task 改过文件、验证过没有（TMP 计划第 11.15 节）：Stage Runner 按真正执行的工具调用记，Reviewer 拿它判 done。

Node 的 result 是模型写的，不算证据，这份才算。只改活动的输入，不改命令序列。
只记 Task 工作流自己的 Stage Runner：sub_agent 与 Atomic Node 里的调用不记。

纯函数与纯数据，工作流里可用；tool_loop 的只读提醒用同一个 only_reads。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# 这些工具的 effect 是写，但调用它们不等于改了文件：shell_run 可能只是在看；request_network 只是要联网。
NOT_FILE_CHANGES = frozenset({"shell_run", "request_network"})
# 判 done 被这两条规则拦下时的 reason_code（reviewer/rules.py）。
DONE_BLOCKED_CODES = frozenset({"rule_done_unverified", "rule_done_without_changes"})
WRITE_EFFECTS = frozenset({"write_reversible", "write_irreversible"})

_READ_TOOLS = frozenset({"repo_read", "repo_list", "repo_grep", "context_read"})
_READ_COMMANDS = frozenset({"cat", "find", "grep", "head", "ls", "nl", "pwd", "rg", "tail", "wc"})
_READ_GIT = frozenset({"blame", "diff", "grep", "log", "ls-files", "rev-parse", "show", "status"})


def only_reads(tool: Any, args: Any) -> bool:
    """这次调用是不是只读：读文件的工具，或者只读的命令（sed -n、git grep 这类）。认不出来的命令算动手。"""
    if tool in _READ_TOOLS:
        return True
    if tool != "shell_run" or not isinstance(args, Mapping):
        return False
    argv = args.get("argv")
    if not isinstance(argv, list) or not argv:
        return False
    words = [str(a) for a in argv]
    command = words[0].rsplit("/", 1)[-1]
    if command == "sed":
        return not any(a.startswith("-i") or a == "--in-place" for a in words[1:])
    if command == "git":
        return len(words) > 1 and words[1] in _READ_GIT
    if command == "find":
        return not any(a in ("-delete", "-exec", "-execdir") for a in words[1:])
    return command in _READ_COMMANDS


@dataclass
class VerificationRecord:
    write_capable: bool = False
    wrote: bool = False
    ran_after_write: bool = False
    last_exit_code: int | None = None
    done_blocked: int = 0

    def offered(self, tool_effects: Mapping[str, str]) -> None:
        """一个 Goal Node 拿到了这些工具。只给 shell_run 不算能改文件：很多只读的任务靠它查东西。"""
        if any(effect in WRITE_EFFECTS and tool not in NOT_FILE_CHANGES for tool, effect in tool_effects.items()):
            self.write_capable = True

    def called(self, tool: str, effect: str, args: Any, observation: Any) -> None:
        """一次调用成功返回。工具返回的 error 不算改过，只读的命令不算验证（否则测试失败之后 git diff 一下，退出码就成了 0）。"""
        if isinstance(observation, Mapping) and "error" in observation:
            return
        if effect in WRITE_EFFECTS and tool not in NOT_FILE_CHANGES:
            self.wrote = True
            self.ran_after_write = False
            self.last_exit_code = None
        elif tool == "shell_run" and self.wrote and not only_reads(tool, args):
            self.ran_after_write = True
            code = observation.get("exit_code") if isinstance(observation, Mapping) else None
            self.last_exit_code = code if isinstance(code, int) else None

    def reviewed(self, verdict: Mapping[str, Any]) -> None:
        if verdict.get("reason_code") in DONE_BLOCKED_CODES:
            self.done_blocked += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "write_capable": self.write_capable,
            "wrote": self.wrote,
            "ran_after_write": self.ran_after_write,
            "last_exit_code": self.last_exit_code,
            "done_blocked": self.done_blocked,
        }
