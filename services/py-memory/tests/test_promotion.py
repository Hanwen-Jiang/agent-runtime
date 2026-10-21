"""S-13：Promotion 规则（附录 I.4 第 5 步）。外部来源的条目不可晋升；作用域越大越需要人工确认。"""

from __future__ import annotations

import pytest

from agentruntime_memory.promotion import Evidence, PromotionPolicy

from .memory_support import ADMIN, ALICE, item, new_id, verified

POLICY = PromotionPolicy()


def test_session_to_user_needs_an_explicit_statement_or_a_repeated_correction() -> None:
    pref = item(kind="preference", title="汇报格式", guidance="汇报用表格，不超过一页", source="user_explicit")
    assert POLICY.evaluate(pref, "user", Evidence(user_explicit=True)).allowed
    corr = item(kind="preference", source="user_correction")
    assert not POLICY.evaluate(corr, "user", Evidence(corrections=1)).allowed
    assert POLICY.evaluate(corr, "user", Evidence(corrections=2)).allowed


def test_session_to_workspace_needs_repeated_verification_or_several_members() -> None:
    success = item(kind="success", evidence=verified(2))
    assert not POLICY.evaluate(success, "workspace", Evidence(verified_successes=2)).allowed
    assert POLICY.evaluate(success, "workspace", Evidence(verified_successes=3)).allowed
    assert POLICY.evaluate(success, "workspace", Evidence(distinct_members=2)).allowed


def test_failures_promote_fast_but_as_a_caution() -> None:
    failure = item(kind="failure", guidance="该 API 分页参数是 cursor 不是 page", evidence=verified(1))
    assert POLICY.evaluate(failure, "workspace", Evidence(verified_successes=1)).allowed
    promoted = POLICY.promoted(failure, "workspace", ALICE, new_id=new_id())
    assert promoted["content"]["guidance"].startswith("注意：")
    assert promoted["lineage"] == [failure["id"]]
    assert promoted["scope"] == {"tenant": ALICE.tenant_id, "workspace": ALICE.workspace_id, "user": None, "session": None, "task": None}


def test_tenant_and_platform_need_approval() -> None:
    ws = item(level="workspace", kind="success", evidence=verified(5))
    pending = POLICY.evaluate(ws, "tenant", Evidence(verified_successes=5))
    assert not pending.allowed and pending.needs_review
    assert POLICY.evaluate(ws, "tenant", Evidence(verified_successes=5, approved_by=frozenset({"admin"}))).allowed
    tenant_item = item(level="tenant", kind="failure", title="docker compose v1 已废弃", guidance="用 docker compose 而不是 docker-compose")
    assert not POLICY.evaluate(tenant_item, "platform", Evidence(approved_by=frozenset({"admin"}))).allowed, "平台要 platform_reviewer 批准"
    assert POLICY.evaluate(tenant_item, "platform", Evidence(approved_by=frozenset({"platform_reviewer"}))).allowed


def test_platform_only_takes_generic_deidentified_tool_experience() -> None:
    reviewer = Evidence(approved_by=frozenset({"platform_reviewer"}))
    pref = item(level="tenant", kind="preference")
    assert not POLICY.evaluate(pref, "platform", reviewer).allowed
    identifying = item(level="tenant", kind="failure", guidance="t-alpha 的 w-1 项目里 u-alice 的仓库要先装 Redis")
    assert not POLICY.evaluate(identifying, "platform", reviewer).allowed
    promoted = POLICY.promoted(item(level="tenant", kind="failure"), "platform", ADMIN, new_id=new_id())
    assert promoted["scope"]["tenant"] is None


@pytest.mark.parametrize("target", ["user", "workspace", "tenant", "platform"])
def test_external_content_is_never_promoted(target: str) -> None:
    external = item(kind="success", source="external_content", trust=0.2, evidence=verified(10))
    everything = Evidence(user_explicit=True, corrections=9, verified_successes=10, distinct_members=9, approved_by=frozenset({"admin", "platform_reviewer"}))
    decision = POLICY.evaluate(external, target, everything)
    assert not decision.allowed and not decision.needs_review
