"""D-06e 端到端：应用经 task-service 的 Workspace API 管工作区（docs/plan/workspace-layer.md 第 5、6 节）。

整套由 scripts/dev_stack.py 拉起（mock 供应商、桩决策），Worker 的沙箱是登记的本地后端、存档模式（--sandbox local
--workspace archive）；远端是本机的裸仓库（--allow-local-remotes 才收 file://）。经 HTTP + JSON（connect 协议）调用：

1. 建（克隆）→ 列出 → diff（没有改动）→ 提交（没有改动）→ 推到 agent/* → 再推一次是对账 → 存档 → 删除。
2. 别的租户看不到这个工作区（NotFound，列表里没有）；推到策略外的分支 PermissionDenied。
3. 克隆与推送带的凭据不出现在 task-service 与 Worker 的日志里。
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from e2e_support.devstack import DevStack, run_script

pytestmark = pytest.mark.e2e

SERVICE = "/agentruntime.workspace.v1.WorkspaceService/"
SECRET = "pw-E2E-SECRET-0123456789"


def git(*args: str, cwd: Path | None = None) -> str:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()


@pytest.fixture(scope="module")
def remote(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("remote")
    bare, seed = root / "remote.git", root / "seed"
    git("init", "--quiet", "--bare", "-b", "main", str(bare))
    git("init", "--quiet", "-b", "main", str(seed))
    (seed / "README.md").write_bytes(b"# e2e\n")
    git("add", "-A", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--quiet", "-m", "seed", cwd=seed)
    git("push", "--quiet", str(bare), "main", cwd=seed)
    return bare


@pytest.fixture(scope="module")
def ws_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[DevStack]:
    root = tmp_path_factory.mktemp("workspace-stack")
    try:
        s = DevStack.up(root, "--provider", "mock", "--decisions", "stub", "--sandbox", "local", "--workspace", "archive", "--allow-local-remotes")
    except RuntimeError as err:
        run_script("down", "--root", str(root))
        pytest.fail(str(err), pytrace=False)
    try:
        yield s
    finally:
        s.down()


class Api:
    def __init__(self, base: str, token: str) -> None:
        self.base, self.token = base, token

    def call(self, method: str, body: dict[str, Any], *, timeout: float = 180) -> tuple[int, dict[str, Any]]:
        request = urllib.request.Request(
            self.base + SERVICE + method, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    def ok(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        status, out = self.call(method, body)
        assert status == 200, f"{method}：{status} {out}"
        return out


def test_the_workspace_lifecycle_through_the_api(ws_stack: DevStack, remote: Path) -> None:
    assert ws_stack.info["workspace_api"] is True and ws_stack.info["workspace"] == "archive"
    api = Api(ws_stack.task_api, ws_stack.token("t1", "u-alice"))
    url = remote.as_uri()
    credential = {"username": "bot", "password": SECRET}

    created = api.ok("CreateWorkspace", {"workspaceId": "w-e2e", "repoUrl": url, "credential": credential})["workspace"]
    assert created["workspaceId"] == "w-e2e" and created["head"] == git("rev-parse", "main", cwd=remote)
    listed = api.ok("ListWorkspaces", {})["workspaces"]
    assert [w["workspaceId"] for w in listed] == ["w-e2e"]
    assert listed[0]["state"] == "WORKSPACE_STATE_ARCHIVED", "建好以后环境销毁了，只剩存档"

    diff = api.ok("GetDiff", {"workspaceId": "w-e2e"})
    assert diff.get("files", []) == [] and diff["head"] == created["head"]
    commit = api.ok("CommitWorkspace", {"workspaceId": "w-e2e", "message": "没有改动"})
    assert commit.get("committed", False) is False and commit["sha"] == created["head"]

    status, denied = api.call("PushWorkspace", {"workspaceId": "w-e2e", "branch": "main", "credential": credential})
    assert status == 403 and denied["code"] == "permission_denied", denied
    pushed = api.ok("PushWorkspace", {"workspaceId": "w-e2e", "branch": "agent/e2e", "credential": credential})
    assert pushed["sha"] == created["head"] and not pushed.get("reconciled", False)
    assert git("rev-parse", "agent/e2e", cwd=remote) == created["head"]
    again = api.ok("PushWorkspace", {"workspaceId": "w-e2e", "branch": "agent/e2e"})
    assert again["reconciled"] is True

    archived = api.ok("ArchiveWorkspace", {"workspaceId": "w-e2e"})
    assert archived["snapshotId"].startswith("wss-")

    # 应用浏览与管理工作区（第 8 节）：文件树、读文件、提交记录、从别的工作区复制、丢弃改动、与远端同步。
    entries = {e["name"]: e["type"] for e in api.ok("ListFiles", {"workspaceId": "w-e2e"})["entries"]}
    assert entries == {"README.md": "FILE_TYPE_FILE"}, "不列 .git"
    assert api.ok("ReadFile", {"workspaceId": "w-e2e", "path": "README.md"})["content"] == "# e2e\n"
    status, refused = api.call("ReadFile", {"workspaceId": "w-e2e", "path": ".git/config"})
    assert status == 400, refused
    [commit_entry] = api.ok("GetLog", {"workspaceId": "w-e2e"})["commits"]
    assert commit_entry["subject"] == "seed" and commit_entry["sha"] == created["head"] and commit_entry["authoredAt"]
    copy = api.ok("CreateWorkspace", {"workspaceId": "w-e2e-copy", "fromWorkspaceId": "w-e2e"})["workspace"]
    assert copy["repoUrl"] == url and copy["branch"] == "main"
    sample = api.ok("CreateWorkspace", {"workspaceId": "w-sample", "files": [{"path": "hello.py", "content": "print('hi')\n"}],
                                        "authorName": "Alice", "authorEmail": "alice@example.com"})["workspace"]
    assert api.ok("GetLog", {"workspaceId": "w-sample"})["commits"][0]["authorName"] == "Alice" and sample["head"]
    assert api.ok("DiscardChanges", {"workspaceId": "w-e2e-copy"})["head"] == created["head"]
    synced = api.ok("SyncWorkspace", {"workspaceId": "w-e2e-copy", "branch": "agent/e2e", "credential": credential})
    assert synced == {"head": created["head"], "branch": "agent/e2e"}

    other = Api(ws_stack.task_api, ws_stack.token("t2", "u-bob"))
    status, missing = other.call("GetDiff", {"workspaceId": "w-e2e"})
    assert status == 404 and missing["code"] == "not_found", missing
    status, missing = other.call("ListFiles", {"workspaceId": "w-e2e"})
    assert status == 404, missing
    assert other.ok("ListWorkspaces", {}).get("workspaces", []) == []

    for ws in ("w-e2e", "w-e2e-copy", "w-sample"):
        assert api.ok("DeleteWorkspace", {"workspaceId": ws}) == {}
    assert api.ok("ListWorkspaces", {}).get("workspaces", []) == []

    for name in ("task-service", "worker"):
        log = Path(ws_stack.info["components"][name]["log"]).read_text("utf-8", errors="replace")
        assert SECRET not in log, f"{name} 的日志里出现了凭据"
