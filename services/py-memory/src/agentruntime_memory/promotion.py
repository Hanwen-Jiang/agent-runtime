"""Promotion 规则（附录 I.4 第 5 步；计划 S-13）。

- Session → User：用户显式表达，或同一纠正出现 2 次以上，自动晋升。
- Session → Workspace：在本项目被验证成功 N 次（默认 3），或被 2 个以上不同成员复现，自动晋升（维护者可撤）。
- Workspace → Tenant：要 admin 批准。
- Tenant → Platform：要 platform_reviewer 批准；只接受通用的工具与模型经验（success、failure），且去标识化
  （不含租户、Workspace、用户等标识）。
- failure 类验证 1 次即可晋升到 User 或 Workspace，措辞改成“注意：……”，不是强制规则。
- 外部来源的条目、待人审或被判为指令式的条目一律不可晋升。
- 偏好跟人走、Workspace 知识跟项目走：preference 不晋升到 Workspace。

晋升产生新条目：作用域换成目标层级，lineage 记上来源条目（级联删除据此找派生条目）。
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agentruntime_memory.identity import Identity
from agentruntime_memory.safety import TEXT_FIELDS, instruction_signals

TARGETS = ("user", "workspace", "tenant", "platform")
CAUTION = "注意："
PLATFORM_KINDS = ("success", "failure")
_ID_LIKE = re.compile(r"\b(?:ses|tsk|mem|tr|run|nd)_[0-9A-Za-z]{8,}|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@dataclass(frozen=True)
class Evidence:
    user_explicit: bool = False
    corrections: int = 0
    """同一纠正出现的次数。"""
    verified_successes: int = 0
    distinct_members: int = 0
    """复现过的不同成员数。"""
    approved_by: frozenset[str] = field(default_factory=frozenset)
    """批准者的角色。"""


@dataclass(frozen=True)
class PromotionDecision:
    allowed: bool
    needs_review: bool
    reason: str


def _no(reason: str) -> PromotionDecision:
    return PromotionDecision(False, False, reason)


def _yes(reason: str) -> PromotionDecision:
    return PromotionDecision(True, False, reason)


def _text(item: Mapping[str, Any]) -> str:
    content = item.get("content") if isinstance(item.get("content"), Mapping) else {}
    return "\n".join(content[k] for k in TEXT_FIELDS if isinstance(content.get(k), str))


def identifying(item: Mapping[str, Any]) -> bool:
    """正文里有租户、Workspace、用户、Session、Task 的标识，或像标识的串（各类 id、邮箱）。"""
    text = _text(item)
    if _ID_LIKE.search(text):
        return True
    scope = item.get("scope") if isinstance(item.get("scope"), Mapping) else {}
    provenance = item.get("provenance") if isinstance(item.get("provenance"), Mapping) else {}
    names = [v for v in (*scope.values(), provenance.get("user"), provenance.get("session")) if isinstance(v, str) and v]
    lowered = text.lower()
    return any(re.search(rf"(?<![a-z0-9_-]){re.escape(n.lower())}(?![a-z0-9_-])", lowered) for n in names)


class PromotionPolicy:
    def __init__(self, *, workspace_successes: int = 3, workspace_members: int = 2, user_corrections: int = 2) -> None:
        self.workspace_successes = workspace_successes
        self.workspace_members = workspace_members
        self.user_corrections = user_corrections

    def evaluate(self, item: Mapping[str, Any], target: str, evidence: Evidence) -> PromotionDecision:
        if target not in TARGETS:
            return _no("unknown_target")
        provenance = item.get("provenance") if isinstance(item.get("provenance"), Mapping) else {}
        if provenance.get("source") == "external_content":
            return _no("external_content")
        if instruction_signals(_text(item)):
            return _no("instruction_like")
        kind = item.get("kind")
        failure = kind == "failure" and evidence.verified_successes >= 1
        if target == "user":
            if evidence.user_explicit or evidence.corrections >= self.user_corrections or failure:
                return _yes("user_rule")
            return _no("not_enough_evidence")
        if target == "workspace":
            if kind == "preference":
                return _no("preferences_follow_the_person")
            if failure or evidence.verified_successes >= self.workspace_successes or evidence.distinct_members >= self.workspace_members:
                return _yes("workspace_rule")
            return _no("not_enough_evidence")
        if target == "tenant":
            if "admin" in evidence.approved_by:
                return _yes("approved_by_admin")
            return PromotionDecision(False, True, "needs_admin_approval")
        if kind not in PLATFORM_KINDS:
            return _no("platform_takes_tool_experience_only")
        if identifying(item):
            return _no("not_deidentified")
        if "platform_reviewer" in evidence.approved_by:
            return _yes("approved_by_platform_reviewer")
        return PromotionDecision(False, True, "needs_platform_review")

    def promoted(self, item: Mapping[str, Any], target: str, identity: Identity, *, new_id: str) -> dict[str, Any]:
        """晋升出的新条目：作用域换成 target，lineage 加上来源，failure 的指导改成“注意：”。"""
        out = copy.deepcopy(dict(item))
        scope = item.get("scope") if isinstance(item.get("scope"), Mapping) else {}
        tenant = scope.get("tenant") or identity.tenant_id
        new_scope: dict[str, Any] = {"tenant": tenant, "workspace": None, "user": None, "session": None, "task": None}
        if target == "user":
            new_scope["user"] = scope.get("user") or identity.user_id
        elif target == "workspace":
            new_scope["workspace"] = scope.get("workspace") or identity.workspace_id
        elif target == "platform":
            # 去标识化：来源只留类别，证据（trace）不带到平台。
            new_scope["tenant"] = None
            out["provenance"] = {"source": item["provenance"]["source"]}
            out.pop("evidence", None)
        out.update({"id": new_id, "scope": new_scope, "lineage": [item["id"]], "version": 1})
        out.pop("stats", None)
        out["validity"] = {**item.get("validity", {}), "superseded_by": None}
        if item.get("kind") == "failure" and not out["content"]["guidance"].startswith(CAUTION):
            out["content"]["guidance"] = CAUTION + out["content"]["guidance"]
        return out
