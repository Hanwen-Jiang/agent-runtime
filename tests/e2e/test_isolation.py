"""S-12a 越权测试（服务层）：以租户 t2 的身份，对 Task API 的每个接口与 SSE 端点读写租户 t1 的数据，全部被拒。

- 访问别的租户的 Task 或 Session，与访问不存在的一样报 not_found（SSE 为 404），不泄露对象是否存在。
- 请求里冒充别的租户（tenant_id 与身份不一致）一律 permission_denied（CLAUDE.md：tenant_id 只用来校验一致）。
- 越权的写操作对 t1 的 Task 没有任何影响；同一个 client_request_id 在两个租户里是两个 Task，不会去重到别人的。
S-03 的 test_tenancy.py 是这里的子集；这里按接口逐个列出，新增接口时补进 OPERATIONS。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from e2e_support.api import ApiError, SseStream, TaskApi, finished_payload, stream_until_finished
from e2e_support.stack import Stack

pytestmark = pytest.mark.e2e

# 格式合法、但谁的都不是的 ID。
NOBODY_TASK = "tsk_01J" + "Z" * 22 + "Q"
NOBODY_SESSION = "ses_01J" + "Z" * 22 + "Q"

# 接口 → 以 api 的身份对 (task_id, session_id) 做一次操作；tenant_id 非空时请求里带上它。
OPERATIONS: dict[str, Callable[[TaskApi, str, str, str], Any]] = {
    "QueryTask": lambda api, task, session, tenant: api.call("QueryTask", _with(tenant, {"task_id": task})),
    "CancelTask": lambda api, task, session, tenant: api.call("CancelTask", _with(tenant, {"task_id": task, "reason": "越权"})),
    "SignalTask": lambda api, task, session, tenant: api.call(
        "SignalTask", _with(tenant, {"task_id": task, "human_reply": {"question_id": "confirm", "option": "approve"}})
    ),
    "SubmitTask(session_id)": lambda api, task, session, tenant: api.call(
        "SubmitTask",
        _with(tenant, {"client_request_id": uuid.uuid4().hex, "message": "越权", "session_id": session, "inputs": {"stub_plan": "answer"}}),
    ),
}


def _with(tenant: str, body: dict[str, Any]) -> dict[str, Any]:
    return {**body, "tenant_id": tenant} if tenant else body


def error_of(fn: Callable[[], Any]) -> ApiError:
    with pytest.raises(ApiError) as err:
        fn()
    return err.value


def anonymized(message: str) -> str:
    return re.sub(r"(tsk|ses)_[0-9A-Z]{26}", "<id>", message)


@pytest.fixture
def running(stack: Stack, api: TaskApi) -> Iterator[dict]:
    """t1 正在运行的 Task（model 节点等门）。"""
    sub = api.submit(stub_plan="slow_answer")
    stack.wait_call(sub["taskId"], "execute_model_node", "start")
    yield sub
    stack.open_gate(sub["taskId"])


@pytest.mark.parametrize("operation", OPERATIONS)
def test_another_tenants_task_looks_exactly_like_a_missing_one(operation: str, running: dict, other_tenant: TaskApi) -> None:
    op = OPERATIONS[operation]
    theirs = error_of(lambda: op(other_tenant, running["taskId"], running["sessionId"], ""))
    nobodys = error_of(lambda: op(other_tenant, NOBODY_TASK, NOBODY_SESSION, ""))
    assert theirs.code == "not_found", f"{operation}：越权应报 not_found，实际 {theirs}"
    assert (theirs.code, anonymized(theirs.message)) == (nobodys.code, anonymized(nobodys.message)), "与不存在的对象报同样的错，不泄露是否存在"


@pytest.mark.parametrize("operation", OPERATIONS)
def test_impersonating_another_tenant_is_denied(operation: str, running: dict, other_tenant: TaskApi) -> None:
    err = error_of(lambda: OPERATIONS[operation](other_tenant, running["taskId"], running["sessionId"], "t1"))
    assert err.code == "permission_denied", f"{operation}：请求里的 tenant_id 与身份不一致应被拒，实际 {err}"


def test_sse_of_another_tenant_is_not_found(running: dict, other_tenant: TaskApi) -> None:
    for url in (running["eventsUrl"], running["eventsUrl"].replace(running["taskId"], NOBODY_TASK)):
        for last_event_id in (None, 0):
            stream = SseStream(url, other_tenant.token, last_event_id)
            try:
                assert stream.status == 404, f"{url}（Last-Event-ID={last_event_id}）应返回 404，实际 {stream.status}"
            finally:
                stream.close()


def test_rejected_writes_leave_the_victims_task_untouched(stack: Stack, api: TaskApi, other_tenant: TaskApi, running: dict) -> None:
    for op in OPERATIONS.values():
        with pytest.raises(ApiError):
            op(other_tenant, running["taskId"], running["sessionId"], "")
    assert api.query(running["taskId"])["status"] == "TASK_STATUS_RUNNING", "t2 的取消不该影响 t1 的 Task"
    stack.open_gate(running["taskId"])
    result = finished_payload(stream_until_finished(running["eventsUrl"], api.token or ""))
    assert result["status"] == "TASK_STATUS_DONE"
    assert stack.count(running["taskId"], "plan_round", "start") == 1, "t1 的 Task 照常只规划一次，没有被越权的信号打乱"


def test_client_request_ids_are_scoped_per_tenant(api: TaskApi, other_tenant: TaskApi) -> None:
    crid = f"shared-{uuid.uuid4().hex}"
    mine = api.submit(client_request_id=crid)
    theirs = other_tenant.submit(client_request_id=crid)
    assert theirs["taskId"] != mine["taskId"] and theirs["sessionId"] != mine["sessionId"]
    assert not theirs.get("deduplicated"), "别的租户的同名请求不能去重到 t1 的 Task"
    for who, sub in ((api, mine), (other_tenant, theirs)):
        assert finished_payload(stream_until_finished(sub["eventsUrl"], who.token or ""))["status"] == "TASK_STATUS_DONE"
