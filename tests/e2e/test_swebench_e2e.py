"""TMP 计划 eval-observability-and-runtime-fixes 第 10 节第一期（细化见第 11.10 节）端到端：SWE-bench 适配器经真的 Task API 与
Workspace API 跑一道构造的题。

整套由 scripts/dev_stack.py 拉起（mock 供应商、桩决策，不花钱；本地沙箱、存档模式、--allow-local-remotes 收 file://）。题目的
仓库是本机的裸仓库 acme/calc：base 提交里 add() 写成了减法，之后还有一个修复提交。命令行 `run` 跑这一题：

1. 工作区停在 base：提交记录只有“写入运行环境定义”的提交与 base；之后的修复提交在工作区里不存在（相对它取 diff 报 NotFound）；
   运行环境定义在 head 的提交里。
2. Task 结束；predictions 有这一题，记录里有结局、Round 数与 task_id。
3. Task API 的令牌只经环境变量传，输出目录里没有它。
4. `report` 写出报告：选中 1 题、跑了 1 题、还没判分。

e2e 包要依赖 agentruntime-learning（命令行在那个包里）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from e2e_support.devstack import DevStack, run_script

pytestmark = pytest.mark.e2e

WORKSPACE = "/agentruntime.workspace.v1.WorkspaceService/"
INSTANCE = "acme__calc-1"


def git(*args: str, cwd: Path | None = None) -> str:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()


@pytest.fixture(scope="module")
def swb_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[DevStack]:
    root = tmp_path_factory.mktemp("swebench-stack")
    try:
        s = DevStack.up(root, "--provider", "mock", "--decisions", "stub", "--sandbox", "local", "--workspace", "archive", "--allow-local-remotes")
    except RuntimeError as err:
        run_script("down", "--root", str(root))
        pytest.fail(str(err), pytrace=False)
    try:
        yield s
    finally:
        s.down()


def make_repo(root: Path) -> tuple[Path, str, str]:
    """<root>/mirror/acme/calc.git：base 之后还有修复提交。返回 (mirror 目录, base, 修复)。"""
    mirror = root / "mirror"
    bare = mirror / "acme" / "calc.git"
    bare.parent.mkdir(parents=True)
    git("init", "--quiet", "--bare", "-b", "main", str(bare))
    seed = root / "seed"
    git("init", "--quiet", "-b", "main", str(seed))
    (seed / "calc.py").write_bytes(b"def add(a, b):\n    return a - b\n")
    git("add", "-A", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--quiet", "-m", "base", cwd=seed)
    base = git("rev-parse", "HEAD", cwd=seed)
    (seed / "calc.py").write_bytes(b"def add(a, b):\n    return a + b\n")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--quiet", "-am", "fix add", cwd=seed)
    fix = git("rev-parse", "HEAD", cwd=seed)
    git("push", "--quiet", str(bare), "main", cwd=seed)
    return mirror, base, fix


def call(stack: DevStack, token: str, method: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        stack.task_api + WORKSPACE + method, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def cli(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agentruntime_learning.benchmarks", "swebench", *args],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
    )


def test_one_instance_through_the_runtime(swb_stack: DevStack, tmp_path: Path) -> None:
    mirror, base, fix = make_repo(tmp_path)
    instances = tmp_path / "instances.jsonl"
    row = {
        "instance_id": INSTANCE, "repo": "acme/calc", "base_commit": base,
        "problem_statement": "add() subtracts instead of adding", "hints_text": "",
        "image": "swebench/sweb.eval.x86_64.acme_1776_calc-1:latest",
        "patch": "PATCH-SECRET", "test_patch": "TESTPATCH-SECRET", "FAIL_TO_PASS": ["test_F2P_SECRET"], "PASS_TO_PASS": [],
    }
    instances.write_text(json.dumps(row) + "\n", encoding="utf-8", newline="\n")
    out = tmp_path / "out"
    token = swb_stack.token("t1", "u-bench")
    env = {**os.environ, "AR_TASK_API_TOKEN": token, "PYTHONUTF8": "1"}

    run = cli(
        "run", "--instances", str(instances), "--out", str(out), "--limit", "1", "--run-id", "e2e-1",
        "--task-api", swb_stack.task_api, "--budget-usd", "1", "--task-usd-limit", "0.5",
        "--repo-url-template", mirror.as_uri() + "/{repo}.git", "--keep-workspaces", env=env,
    )
    assert run.returncode == 0, f"run 失败：\n{run.stdout[-2000:]}\n{run.stderr[-3000:]}"

    [pred] = [json.loads(line) for line in (out / "predictions.jsonl").read_text("utf-8").splitlines()]
    assert pred["instance_id"] == INSTANCE and isinstance(pred["model_patch"], str)
    record = json.loads((out / "records" / f"{INSTANCE}.json").read_text("utf-8"))
    assert record["state"] == "finished" and record["task_id"] and record["rounds"] >= 1, record
    assert record["status"] in ("done", "aborted"), record

    ws = record["workspace_id"]
    status, log = call(swb_stack, token, "GetLog", {"workspaceId": ws})
    assert status == 200, log
    assert [c["subject"] for c in log["commits"]] == ["Add workspace files", "base"], "停在 base，上面只有写入运行环境定义的提交"
    status, dockerfile = call(swb_stack, token, "ReadFile", {"workspaceId": ws, "path": ".devcontainer/agentruntime.Dockerfile"})
    assert status == 200 and dockerfile["content"].startswith("FROM swebench/sweb.eval.x86_64.acme_1776_calc-1:latest"), dockerfile
    status, missing = call(swb_stack, token, "GetDiff", {"workspaceId": ws, "base": fix})
    assert status == 404, f"修复提交不该在工作区里：{status} {missing}"

    for path in out.rglob("*"):
        if path.is_file():
            assert token not in path.read_text("utf-8", errors="replace"), f"{path} 里有 Task API 的令牌"

    rep = cli("report", "--out", str(out), env=env)
    assert rep.returncode == 0, rep.stderr[-3000:]
    report = json.loads((out / "report.json").read_text("utf-8"))
    assert (report["selected"], report["ran"], report["graded"]) == (1, 1, False)
