"""F3-E2E（docs/plan/n18-file-delivery-f3.md 第 5 节第 5 条）：文件交付的端到端。

整套由 scripts/dev_stack.py 拉起（mock 供应商、桩决策；--sandbox local：工作区在本机目录，task-service 与 Worker 共用业务库与
ObjectStore）。桩 Planner 的 deliver_file 计划让一个 tool_loop 调真的 deliver_file 工具（Worker 的 Node Executor），再由 answer 交付文字：
1. Task 结束后 ListTaskArtifacts 列出这个文件（元数据与内容的 sha256 对得上），下载路由给出原文与安全的响应头；
2. 这一轮的重要输出里，交付的文件排第一、标 delivered；
3. 别的租户列不到、下载不到（404），没有令牌下载是 401。
"""

from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from e2e_support.api import ApiError, TaskApi, finished_payload, stream_until_finished
from e2e_support.devstack import DevStack

pytestmark = pytest.mark.e2e

WORKSPACE = "w-f3"
CONTENT = "quarter,rate\n3,0.12\n"


@pytest.fixture(scope="module")
def f3_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[DevStack]:
    root = tmp_path_factory.mktemp("f3-stack")
    s = DevStack.up(root, "--provider", "mock", "--decisions", "stub", "--sandbox", "local", "--tenant-effect", "t1=write_reversible")
    try:
        yield s
    finally:
        s.down()


def download(stack: DevStack, path: str, token: str) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(stack.task_api + path, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def deliver(stack: DevStack, api: TaskApi, workspace: Path) -> dict[str, Any]:
    out = workspace / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.csv").write_bytes(CONTENT.encode())
    sub = api.submit("把季度报告交给我", stub_plan="deliver_file", inputs={"stub_file": "out/report.csv"}, workspace_id=WORKSPACE)
    result = finished_payload(stream_until_finished(sub["eventsUrl"], api.token or "", timeout=180))
    assert result["status"] == "TASK_STATUS_DONE", result
    return sub


def test_a_delivered_file_is_listed_downloaded_and_highlighted(f3_stack: DevStack) -> None:
    stack = f3_stack
    api = TaskApi(stack.task_api, stack.token("t1", "u1"))
    sub = deliver(stack, api, stack.workspace_root / "t1" / WORKSPACE)

    [artifact] = api.list_artifacts(sub["taskId"])["artifacts"]
    assert artifact["name"] == "report.csv" and artifact["path"] == "out/report.csv" and artifact["mediaType"] == "text/csv"
    assert artifact["state"] == "TASK_ARTIFACT_STATE_READY"
    assert artifact["sha256"] == hashlib.sha256(CONTENT.encode()).hexdigest() and int(artifact["sizeBytes"]) == len(CONTENT.encode())
    assert artifact["downloadPath"] == f"/v1/tasks/{sub['taskId']}/artifacts/{artifact['artifactId']}"

    status, headers, body = download(stack, artifact["downloadPath"], api.token or "")
    assert status == 200 and body == CONTENT.encode()
    assert headers.get("Content-Type") == "text/csv" and headers.get("Content-Disposition", "").startswith("attachment;")
    assert headers.get("X-Content-Type-Options") == "nosniff" and headers.get("Cache-Control") == "no-store"

    state = api.query(sub["taskId"])["taskState"]["value"]
    assert state["rounds"][0]["highlights"]["files"][0] == {"path": "out/report.csv", "delivered": True}


def test_other_tenants_and_anonymous_callers_get_nothing(f3_stack: DevStack) -> None:
    stack = f3_stack
    api = TaskApi(stack.task_api, stack.token("t1", "u1"))
    sub = deliver(stack, api, stack.workspace_root / "t1" / WORKSPACE)
    [artifact] = api.list_artifacts(sub["taskId"])["artifacts"]

    other = TaskApi(stack.task_api, stack.token("t2", "u2"))
    with pytest.raises(ApiError) as err:
        other.list_artifacts(sub["taskId"])
    assert err.value.status == 404
    assert download(stack, artifact["downloadPath"], other.token or "")[0] == 404
    assert download(stack, artifact["downloadPath"], "")[0] == 401
