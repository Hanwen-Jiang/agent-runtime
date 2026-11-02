"""docs/plan/feedback-2026-11-01.md N-11：Terminal-Bench 2.0 的适配器（agentruntime_learning.benchmarks.terminalbench）。

- 读任务（Harbor 格式：task.toml、instruction.md、environment/Dockerfile、tests/）；选题可复现，可按难度、类别与可移植的范围筛。
- prepare：从官方镜像取出工作目录，只收文本文件；二进制、太大、链接记为不可移植。
- run：工作区 = 工作目录的内容 + 运行环境定义（FROM 官方镜像，工作目录链到工作区）；消息只有任务说明加固定的说明；
  测试与参考答案不发给 Runtime；调度、续跑、边跑边发同 SWE-bench。
- grade：在官方镜像的容器里换上“初始内容 + diff”的工作目录（不带运行环境定义与 .git，二进制的改动带不过去并记下），拷入测试，
  跑 test.sh，读 reward；判过的不重判；判完发分数。
- report：解决率按难度与类别，写明限制。
"""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from agentruntime_learning.benchmarks import swebench as sb
from agentruntime_learning.benchmarks import terminalbench as tb

from swebench_support import FakeRuntime, Script, rounds, settings
from test_swebench_telemetry import Recorder

SECRET = "TB-HIDDEN-TEST-SECRET"
SOLUTION = "TB-SOLUTION-SECRET"


def make_task(root: Path, name: str, *, difficulty: str = "medium", category: str = "software-engineering", workdir: str = "/app", image: bool = True) -> Path:
    d = root / name
    (d / "environment").mkdir(parents=True)
    (d / "tests").mkdir()
    (d / "solution").mkdir()
    env = f'docker_image = "alexgshaw/{name}:20251031"\n' if image else ""
    (d / "task.toml").write_text(
        f'schema_version = "1.1"\n[task]\nname = "terminal-bench/{name}"\n[metadata]\ndifficulty = "{difficulty}"\ncategory = "{category}"\n'
        f'tags = ["x"]\n[verifier]\ntimeout_sec = 300.0\n[agent]\ntimeout_sec = 600.0\n[environment]\n{env}cpus = 1\n',
        "utf-8",
    )
    (d / "instruction.md").write_text(f"Write {workdir}/out.txt containing hello ({name}).\n", "utf-8")
    (d / "environment" / "Dockerfile").write_text(f"FROM python:3.13-slim\nWORKDIR {workdir}\n", "utf-8")
    (d / "tests" / "test.sh").write_text(f"#!/bin/bash\n# {SECRET}\ngrep -q hello {workdir}/out.txt && echo 1 > /logs/verifier/reward.txt || echo 0 > /logs/verifier/reward.txt\n", "utf-8")
    (d / "solution" / "solve.sh").write_text(f"echo hello > {workdir}/out.txt # {SOLUTION}\n", "utf-8")
    return d


@pytest.fixture
def tasks_root(tmp_path: Path) -> Path:
    root = tmp_path / "tb2"
    root.mkdir()
    make_task(root, "cancel-async-tasks", difficulty="hard")
    make_task(root, "fix-git", workdir="/app/personal-site", category="software-engineering")
    make_task(root, "log-summary", difficulty="easy", category="file-operations")
    (root / "README.md").write_text("not a task", "utf-8")
    return root


# --- 读任务与选题 ---


def test_tasks_are_read_from_the_harbor_layout(tasks_root: Path) -> None:
    tasks = tb.load_tasks(tasks_root)
    assert [t.instance_id for t in tasks] == ["cancel-async-tasks", "fix-git", "log-summary"]
    t = tasks[0]
    assert (t.docker_image, t.workdir, t.difficulty, t.category) == ("alexgshaw/cancel-async-tasks:20251031", "/app", "hard", "software-engineering")
    assert (t.agent_timeout_s, t.verifier_timeout_s) == (600.0, 300.0)
    assert tasks[1].workdir == "/app/personal-site", "最后一个 WORKDIR"
    assert SECRET not in json.dumps([x.raw for x in tasks]) and SOLUTION not in json.dumps([x.raw for x in tasks])


def test_a_task_without_an_official_image_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="docker_image"):
        tb.load_task(make_task(tmp_path, "no-image", image=False))


def test_selection_is_reproducible_and_filterable(tasks_root: Path) -> None:
    tasks = tb.load_tasks(tasks_root)
    assert tb.select(tasks, limit=2, seed=3) == tb.select(tasks, limit=2, seed=3)
    assert [t.instance_id for t in tb.select(tasks, difficulties=["easy"])] == ["log-summary"]
    assert [t.instance_id for t in tb.select(tasks, categories=["file-operations"])] == ["log-summary"]
    assert [t.instance_id for t in tb.select(tasks, only={"fix-git"})] == ["fix-git"]
    assert [t.instance_id for t in tb.select(tasks, ids=["log-summary", "fix-git"])] == ["log-summary", "fix-git"]
    with pytest.raises(ValueError):
        tb.select(tasks, ids=["nope"])


# --- prepare ---


def tar_of(top: str, entries: dict[str, bytes], links: Sequence[str] = ()) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        d = tarfile.TarInfo(top)
        d.type = tarfile.DIRTYPE
        tar.addfile(d)
        for name, data in entries.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name in links:
            info = tarfile.TarInfo(f"{top}/{name}")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
    return buf.getvalue()


def test_only_text_files_are_carried(tasks_root: Path) -> None:
    files, problems = tb.text_files(tar_of("app", {"run.py": b"print(1)\n", "data/notes.md": "中文\n".encode(), "model.bin": b"\0\1\2", "big.txt": b"x" * 20}, ["link"]), "/app", max_file=10)
    assert files == [{"path": "data/notes.md", "content": "中文\n"}, {"path": "run.py", "content": "print(1)\n"}]
    assert sorted(p.split(":")[0] for p in problems) == ["big.txt", "link", "model.bin"]


class FakeDocker:
    """记下 docker 命令；cp 出来的是给定的 tar，cp 进去时记下源目录里的文件；test.sh 按工作目录里有没有 hello 给 reward。"""

    def __init__(self, tars: dict[str, bytes] | None = None, *, fail: set[str] = frozenset()) -> None:
        self.tars = tars or {}
        self.fail = fail
        self.calls: list[list[str]] = []
        self.copied: dict[str, dict[str, str]] = {}
        self.reward = "0"

    def __call__(self, args: Sequence[str], *, timeout_s: float, stdout_bytes: bool = False) -> tuple[int, Any, str]:
        args = list(args)
        self.calls.append(args)
        if args[0] in self.fail:
            return 1, b"" if stdout_bytes else "", f"{args[0]} failed"
        if args[0] == "cp" and args[2] == "-":
            image = [c for c in self.calls if c[0] == "create"][-1][-1]
            return 0, self.tars.get(image, tar_of("app", {})), ""
        if args[0] == "cp":
            src = Path(args[1].rstrip("."))
            self.copied[args[2]] = {str(p.relative_to(src)): p.read_text("utf-8") for p in src.rglob("*") if p.is_file()}
            if args[2].endswith(":/app") or ":/app/" in args[2]:
                self.reward = "1" if any("hello" in v for v in self.copied[args[2]].values()) else "0"
            return 0, "", ""
        if args[0] == "exec" and args[-1] == tb.REWARD_FILE:
            return 0, self.reward + "\n", ""
        return 0, "", ""


def test_prepare_takes_the_working_directory_out_of_the_official_image(tasks_root: Path, tmp_path: Path) -> None:
    tasks = tb.load_tasks(tasks_root)
    docker = FakeDocker({
        "alexgshaw/cancel-async-tasks:20251031": tar_of("app", {"README.md": b"# app\n"}),
        "alexgshaw/fix-git:20251031": tar_of("personal-site", {"index.html": b"<p>hi</p>\n", "logo.png": b"\x89PNG\0"}),
    })
    out = tmp_path / "run"
    summary = tb.prepare(tasks[:2], out, docker=docker)
    assert summary == {"portable": 1, "unportable": 1}
    a = tb.prepared(out, tasks[0])
    assert a is not None and a["portable"] and a["files"] == [{"path": "README.md", "content": "# app\n"}] and a["workdir"] == "/app"
    b = tb.prepared(out, tasks[1])
    assert b is not None and not b["portable"] and b["unportable"] == ["logo.png: binary"]
    assert tb.portable_ids(out) == {"cancel-async-tasks"}
    assert ["cp", f"{tb._container_name('tbp', 'fix-git')}:/app/personal-site", "-"] in docker.calls, "按任务的工作目录取"
    assert sum(1 for c in docker.calls if c[0] == "rm") == 2, "取完删容器"
    assert tb.prepare(tasks[:2], out, docker=docker) == {"cached": 2}, "准备过的跳过"


def test_a_task_that_cannot_be_prepared_is_retried_later(tasks_root: Path, tmp_path: Path) -> None:
    tasks = tb.load_tasks(tasks_root)
    out = tmp_path / "run"
    assert tb.prepare(tasks[:1], out, docker=FakeDocker(fail={"create"})) == {"failed": 1}
    assert tb.prepared(out, tasks[0]) is None and (out / "prepared" / "cancel-async-tasks.error.json").exists()


# --- run ---


class TBRuntime(FakeRuntime):
    def __init__(self, scripts: dict[str, Script], **kw: Any) -> None:
        super().__init__({}, **kw)
        self.scripts = {tb.workspace_id(sb_run_id(), iid): s for iid, s in scripts.items()}


def sb_run_id() -> str:
    from swebench_support import RUN

    return RUN


def prepared_run(tasks_root: Path, tmp_path: Path) -> tuple[list[tb.TBTask], Path]:
    tasks = tb.load_tasks(tasks_root)
    out = tmp_path / "run"
    tb.prepare(tasks, out, docker=FakeDocker({t.docker_image: tar_of(t.workdir.rsplit("/", 1)[1], {"README.md": f"# {t.instance_id}\n".encode()}) for t in tasks}))
    return tasks, out


def test_run_sends_the_working_directory_and_the_instruction_only(tasks_root: Path, tmp_path: Path) -> None:
    tasks, out = prepared_run(tasks_root, tmp_path)
    fake = TBRuntime({
        "cancel-async-tasks": Script(events=rounds("done"), cost_usd=0.2, diff="diff --git a/out.txt b/out.txt\nnew file mode 100644\n--- /dev/null\n+++ b/out.txt\n@@ -0,0 +1 @@\n+hello\n", files=["out.txt"]),
        "fix-git": Script(events=rounds("abort"), status="TASK_STATUS_ABORTED", final_verdict="VERDICT_ABORT", cost_usd=0.1),
    })
    live = Recorder()
    summary = tb.run(fake, tasks[:2], out, settings(model_name="sol"), live=live)
    assert (summary.finished, summary.errors) == (2, 0)
    ws = tb.workspace_id(sb_run_id(), "cancel-async-tasks")
    created = fake.created[ws]
    assert (created["repo_url"], created["ref"]) == ("", ""), "没有仓库：从文件建工作区"
    paths = [f["path"] for f in created["files"]]
    assert paths == ["README.md", tb.DEVCONTAINER, tb.DOCKERFILE]
    dockerfile = created["files"][-1]["content"]
    assert dockerfile.startswith("FROM alexgshaw/cancel-async-tasks:20251031\n") and "ln -s /workspace /app" in dockerfile
    [first] = [s for s in fake.submits if s["workspace_id"] == ws]
    assert first["message"].startswith("Write /app/out.txt containing hello") and "working directory is /app" in first["message"]
    assert SECRET not in fake.requests() and SOLUTION not in fake.requests(), "测试与参考答案不发给 Runtime"
    recs = sb.read_records(out)
    assert recs["cancel-async-tasks"]["patch"].endswith("+hello\n") and recs["fix-git"]["status"] == "aborted"
    assert live.calls[0] == ("planned", (2, 0, 0.0)) and sorted(o for o, _ in live.of("case")) == ["failed", "submitted"]
    manifest = json.loads((out / "run.json").read_text("utf-8"))
    assert manifest["benchmark"] == "terminal-bench" and manifest["tasks"]["fix-git"]["workdir"] == "/app/personal-site"


def test_an_unprepared_task_is_an_error_not_a_crash(tasks_root: Path, tmp_path: Path) -> None:
    tasks = tb.load_tasks(tasks_root)
    summary = tb.run(TBRuntime({}), tasks[:1], tmp_path / "run", settings())
    assert summary.errors == 1 and "prepare" in sb.read_records(tmp_path / "run")["cancel-async-tasks"]["error"]


def test_the_environment_links_the_working_directory_to_the_workspace(tasks_root: Path) -> None:
    task = tb.load_tasks(tasks_root)[1]
    dockerfile = tb.environment_files(task)[1]["content"]
    assert "RUN rm -rf /app/personal-site && mkdir -p /app && ln -s /workspace /app/personal-site" in dockerfile
    same = tb.TBTask("t", task.path, "x", "img:1", "/workspace", "easy", "c", 1, 1)
    assert "ln -s" not in tb.environment_files(same)[1]["content"], "工作目录就是挂载点时不用链接"


# --- grade ---


def test_grading_replays_the_working_directory_in_the_official_image(tasks_root: Path, tmp_path: Path) -> None:
    tasks, out = prepared_run(tasks_root, tmp_path)
    fake = TBRuntime({
        "cancel-async-tasks": Script(diff="diff --git a/out.txt b/out.txt\nnew file mode 100644\n--- /dev/null\n+++ b/out.txt\n@@ -0,0 +1 @@\n+hello\n"
                                     "diff --git a/model.bin b/model.bin\nnew file mode 100644\nBinary files /dev/null and b/model.bin differ\n"),
        "log-summary": Script(diff=""),
    })
    tb.run(fake, [tasks[0], tasks[2]], out, settings())
    docker = FakeDocker()
    live = Recorder()
    summary = tb.grade(out, tasks_root, docker=docker, max_workers=1, live=live)
    assert summary["graded"] == 2 and summary["resolved_ids"] == ["cancel-async-tasks"]
    name = tb._container_name("tbg", "cancel-async-tasks")
    carried = docker.copied[f"{name}:/app"]
    assert carried == {"README.md": "# cancel-async-tasks\n", "out.txt": "hello\n"}, "初始内容加上 Agent 的改动，不带 .devcontainer 与 .git"
    assert SECRET in docker.copied[f"{name}:/tests"]["test.sh"], "测试只在判分时拷进去"
    g = json.loads((out / "grading" / "cancel-async-tasks.json").read_text("utf-8"))
    assert g["reward"] == 1.0 and g["resolved"] is True and g["binary_files_not_carried"] == ["model.bin"]
    assert ["exec", "-w", "/app", name, "bash", "/tests/test.sh"] in docker.calls
    assert ["rm", "-f", name] in docker.calls, "判完删容器"
    assert dict(live.of("score")) == {"resolved": 1.0, "resolved_rate": 0.5}
    calls = len(docker.calls)
    tb.grade(out, tasks_root, docker=docker)
    assert len(docker.calls) == calls, "判过的不重判"


def test_a_patch_that_does_not_apply_is_a_grading_error(tasks_root: Path, tmp_path: Path) -> None:
    tasks, out = prepared_run(tasks_root, tmp_path)
    tb.run(TBRuntime({"cancel-async-tasks": Script(diff="diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-nope\n+x\n")}), tasks[:1], out, settings())
    summary = tb.grade(out, tasks_root, docker=FakeDocker(), max_workers=1)
    assert summary["errors"] == ["cancel-async-tasks"] and summary["resolved_ids"] == []


def test_a_test_that_times_out_is_unresolved(tasks_root: Path, tmp_path: Path) -> None:
    tasks, out = prepared_run(tasks_root, tmp_path)
    tb.run(TBRuntime({"cancel-async-tasks": Script(diff="")}), tasks[:1], out, settings())

    class Slow(FakeDocker):
        def __call__(self, args: Sequence[str], *, timeout_s: float, stdout_bytes: bool = False) -> tuple[int, Any, str]:
            if list(args)[-1] == "/tests/test.sh":
                raise subprocess.TimeoutExpired(list(args), timeout_s)
            return super().__call__(args, timeout_s=timeout_s, stdout_bytes=stdout_bytes)

    tb.grade(out, tasks_root, docker=Slow(), max_workers=1)
    g = json.loads((out / "grading" / "cancel-async-tasks.json").read_text("utf-8"))
    assert g["timed_out"] is True and g["resolved"] is False


# --- report ---


def test_the_report_counts_by_difficulty_and_states_its_limits(tasks_root: Path, tmp_path: Path) -> None:
    tasks, out = prepared_run(tasks_root, tmp_path)
    hello = "diff --git a/out.txt b/out.txt\nnew file mode 100644\n--- /dev/null\n+++ b/out.txt\n@@ -0,0 +1 @@\n+hello\n"
    tb.run(TBRuntime({"cancel-async-tasks": Script(diff=hello, cost_usd=0.4), "log-summary": Script(cost_usd=0.2)}), [tasks[0], tasks[2]], out, settings())
    unready = tb.report(out)
    assert unready["graded"] is False and unready["resolved"] is None
    tb.grade(out, tasks_root, docker=FakeDocker(), max_workers=1)
    rep = tb.report(out)
    assert (rep["selected"], rep["ran"], rep["resolved"], rep["resolve_rate"]) == (2, 2, 1, 0.5)
    assert rep["by_difficulty"] == {"easy": {"selected": 1, "resolved": 0}, "hard": {"selected": 1, "resolved": 1}}
    assert rep["cost_usd"] == pytest.approx(0.6) and "working directory" in rep["limitations"]
    assert "限制" in (out / "report.md").read_text("utf-8")


def test_the_cli_refuses_to_run_without_prepared_tasks(tasks_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentruntime_learning.benchmarks.__main__ import main

    monkeypatch.setenv("AR_TASK_API_TOKEN", "t")
    code = main(["terminalbench", "run", "--tasks", str(tasks_root), "--out", str(tmp_path / "r"), "--limit", "1", "--task-api", "http://127.0.0.1:9",
                 "--budget-usd", "1", "--task-usd-limit", "1"])
    assert code == 2
