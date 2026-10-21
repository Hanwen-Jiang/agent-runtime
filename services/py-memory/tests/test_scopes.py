"""S-12b：作用域 ACL 由服务端根据认证身份判定（附录 I.1；“用户偏好跟人走，Workspace 知识跟项目走，二者不互相写入”）。"""

from __future__ import annotations

import pytest

from agentruntime_memory.scopes import can_delete, can_read, can_write, level_of

from .memory_support import ADMIN, ALICE, BOB, CAROL, DAVE, MAINTAINER, scope


@pytest.mark.parametrize("level", ["platform", "tenant", "workspace", "user", "user_workspace", "session", "task"])
def test_levels_follow_the_scope_fields(level: str) -> None:
    assert level_of(scope(ALICE, level)) == level


@pytest.mark.parametrize(
    ("level", "reader", "visible"),
    [
        ("platform", BOB, True),
        ("tenant", CAROL, True),
        ("tenant", BOB, False),
        ("workspace", CAROL, True),
        ("workspace", DAVE, False),
        ("user", CAROL, False),
        ("user", ALICE, True),
        ("user_workspace", ALICE, True),
        ("user_workspace", CAROL, False),
        ("session", CAROL, False),
        ("session", ALICE, True),
        ("task", ALICE, True),
        ("task", CAROL, False),
    ],
)
def test_who_can_read_alices_items(level: str, reader, visible: bool) -> None:
    assert can_read(reader, scope(ALICE, level)) is visible


def test_direct_writes_only_go_into_scopes_the_identity_owns() -> None:
    for level in ("user", "user_workspace", "session", "task"):
        assert can_write(ALICE, scope(ALICE, level)), level
        assert not can_write(CAROL, scope(ALICE, level)), f"不能写进别人的 {level}"
    for level in ("workspace", "tenant", "platform"):
        assert not can_write(ALICE, scope(ALICE, level)), f"{level} 只能经 Promotion 写入"
    assert not can_write(BOB, scope(ALICE, "session")), "别的租户"


def test_deletion_rights() -> None:
    assert can_delete(ALICE, scope(ALICE, "user")) and can_delete(ALICE, scope(ALICE, "session"))
    assert not can_delete(CAROL, scope(ALICE, "user"))
    assert not can_delete(ALICE, scope(ALICE, "workspace")) and can_delete(MAINTAINER, scope(ALICE, "workspace"))
    assert not can_delete(MAINTAINER, scope(ALICE, "tenant")) and can_delete(ADMIN, scope(ALICE, "tenant"))
    assert not can_delete(ADMIN, scope(ALICE, "platform")), "平台条目经租户接口删不了"
    assert not can_delete(BOB, scope(ALICE, "user"))
