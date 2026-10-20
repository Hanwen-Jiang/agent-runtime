"""S-06 验收（端到端）：大载荷经 task-service（Go 客户端与 Session Worker）与 Python Worker 往返，事件历史里只有引用。

- 提交 100 KB 的命令：Go 一侧编码提交信号与子工作流的输入，Python 一侧解码，再把含这段文本的交付编码回去，
  Go 一侧解码后经 SSE 推给客户端。两种语言的 Claim-Check 互相兼容（计划 S-06 难点“跨语言一致”）。
- 对象在租户自己的前缀下（obj://t1/claim-check/…），其他租户的作用域里没有（计划 S-06 难点“租户隔离”）。
- 带引用的事件历史用当前代码能回放（回放工具从同一个 ObjectStore 取回对象）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.tenancy import TenantContext

from e2e_support import history as h
from e2e_support.api import TaskApi, finished_payload, stream_until_finished
from e2e_support.stack import Stack

pytestmark = pytest.mark.e2e

MESSAGE_KB = 100
THRESHOLD = 64 * 1024


def stored_result(stack: Stack, deliverable: dict) -> dict:
    """外置的 Node 结果（ref 指向共用的 ObjectStore）。"""
    assert deliverable["ref"].startswith("obj://t1/node-results/"), deliverable
    store = LocalDiskObjectStoreProvider(stack.objects_dir, signing_key=b"e2e").bind(TenantContext(tenant_id="t1"))
    data = asyncio.run(store.get(store.key_of(deliverable["ref"]))).data
    return json.loads(data)


def test_large_message_round_trips_through_go_and_python_as_references(stack: Stack, api: TaskApi) -> None:
    message = "量" * (MESSAGE_KB * 1024 // 3)
    sub = api.submit(message)
    result = finished_payload(stream_until_finished(sub["eventsUrl"], api.token or "", timeout=120))
    assert result["status"] == "TASK_STATUS_DONE"
    # 回答超过 Node Executor 的内联上限，交付是外置结果的引用（S-05）；取回来核对内容：命令经两种语言往返后不变。
    assert stored_result(stack, result["deliverable"]["value"]) == {"text": f"桩回答：{message}", "final": True}
    assert api.query(sub["taskId"])["status"] == "TASK_STATUS_DONE", "QueryTask 的结果同样经过 Claim-Check"

    task = h.fetch_one(stack.temporal_address, sub["taskId"])
    sessions = h.fetch(stack.temporal_address, sub["sessionId"])
    for report in [h.analyze(task), *map(h.analyze, sessions)]:
        assert report.max_payload_bytes < THRESHOLD, f"{report.workflow_type} 的历史里最大的载荷有 {report.max_payload_bytes} 字节"
    h.assert_clean(h.analyze(task))
    h.assert_session_clean(sessions)

    # 开发用 ObjectStore 的布局：<根>/<作用域>/data/<键的 sha256>，元数据在同名 .json 里（键原样记在里面）。
    keys = [json.loads(p.read_text("utf-8"))["key"] for p in (stack.objects_dir / "t1" / "data").glob("*.json")]
    assert any(k.startswith("claim-check/") for k in keys), f"Claim-Check 的对象应在租户 t1 的作用域里，实际 {keys}"

    h.assert_replays_python([task], env=stack.shared_store_env())
    h.assert_replays_go(stack.task_service_bin, sessions, env=stack.shared_store_env())
