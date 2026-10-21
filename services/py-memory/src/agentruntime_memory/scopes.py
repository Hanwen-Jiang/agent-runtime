"""作用域 ACL（附录 I.1；计划 S-12b“ACL 过滤条件由服务端根据认证身份拼接”）。

作用域的层级由 memory_item.scope 推出：platform（tenant 为 null）、tenant、workspace、user、user_workspace、session、task。
读写哪些作用域只由身份决定，请求里不接受任何过滤条件：

- 读：平台（只读）、本租户、本人所在的 Workspace、本人、本人在本 Workspace、本 Session、本 Task。
- 直接写：只能写进本人、本人在本 Workspace、本 Session、本 Task；Workspace、Tenant、Platform 只能经 Promotion 写入。
- 删除：本人、本人在本 Workspace、本 Session 的条目本人可删；Workspace 的条目要 maintainer；Tenant 的条目要 admin；
  平台条目经租户接口删不了。

检索时的 ACL 用访问标记表达：每个条目有一个标记（access_token），身份有一组可读的标记（read_tokens），
向量库按“条目标记属于身份的标记集合”过滤。两者都由服务端算出。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agentruntime_memory.identity import Identity

LEVELS = ("platform", "tenant", "workspace", "user", "user_workspace", "session", "task")
SCOPE_WEIGHTS = {"user": 1.0, "user_workspace": 1.0, "session": 1.0, "task": 1.0, "workspace": 0.9, "tenant": 0.8, "platform": 0.6}
"""检索打分里的作用域权重（附录 I.6 第 5 步）。"""
OWNED = ("user", "user_workspace", "session", "task")
"""属于某个用户的层级：本人可直接写、可删。"""


def _get(scope: Mapping[str, Any], key: str) -> str:
    value = scope.get(key)
    return value if isinstance(value, str) else ""


def level_of(scope: Mapping[str, Any]) -> str:
    if not _get(scope, "tenant"):
        return "platform"
    if _get(scope, "task"):
        return "task"
    if _get(scope, "session"):
        return "session"
    if _get(scope, "user"):
        return "user_workspace" if _get(scope, "workspace") else "user"
    if _get(scope, "workspace"):
        return "workspace"
    return "tenant"


def access_token(scope: Mapping[str, Any]) -> str:
    """条目的访问标记。Task 的条目与所在 Session 的条目同一组读者。"""
    level = level_of(scope)
    user, workspace, session = _get(scope, "user"), _get(scope, "workspace"), _get(scope, "session")
    return {
        "platform": "platform",
        "tenant": "tenant",
        "workspace": f"ws:{workspace}",
        "user": f"user:{user}",
        "user_workspace": f"uw:{user}:{workspace}",
        "session": f"ses:{user}:{session}",
        "task": f"ses:{user}:{session}",
    }[level]


def read_tokens(identity: Identity) -> tuple[str, ...]:
    """身份在本租户内可读的访问标记（平台条目另在平台作用域里查）。"""
    tokens = ["tenant"]
    if identity.workspace_id:
        tokens.append(f"ws:{identity.workspace_id}")
    if identity.user_id:
        tokens.append(f"user:{identity.user_id}")
        if identity.workspace_id:
            tokens.append(f"uw:{identity.user_id}:{identity.workspace_id}")
        if identity.session_id:
            tokens.append(f"ses:{identity.user_id}:{identity.session_id}")
    return tuple(tokens)


def can_read(identity: Identity, scope: Mapping[str, Any]) -> bool:
    if level_of(scope) == "platform":
        return True
    return _get(scope, "tenant") == identity.tenant_id and access_token(scope) in read_tokens(identity)


def _owner(identity: Identity, scope: Mapping[str, Any]) -> bool:
    return bool(identity.user_id) and _get(scope, "user") == identity.user_id


def can_write(identity: Identity, scope: Mapping[str, Any]) -> bool:
    """直接写入（不经 Promotion）。"""
    return level_of(scope) in OWNED and can_read(identity, scope) and _owner(identity, scope)


def can_delete(identity: Identity, scope: Mapping[str, Any]) -> bool:
    """删除；也是审核（人审队列）的权限。"""
    level = level_of(scope)
    if level == "platform" or _get(scope, "tenant") != identity.tenant_id:
        return False
    if level in OWNED:
        return _owner(identity, scope)
    if level == "workspace":
        return "maintainer" in identity.roles and _get(scope, "workspace") == identity.workspace_id
    return "admin" in identity.roles
