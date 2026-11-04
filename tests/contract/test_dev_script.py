"""T-14 验收：本地与 CI 共用的检查脚本 scripts/check.py（TMP 计划 T-14“把本地 clone 跑测试写成脚本”）。

- 只用标准库，任何 Python 3.11+ 都能跑；CI 的每个 job 也调用它，本地跑的与 CI 跑的是同一组命令。
- 步骤：proto、contract、go、py-core、py-memory、py-learning、py-worker、e2e、faults。档位 pr（默认）不含 faults，full 全有。
  --only、--skip 挑步骤；--list（加 --json）只打印计划。
- Python 步骤之前统一 uv sync 一次，之后都用 uv run --no-sync；--no-sync 跳过这一次同步。
- go 覆盖 go.work 的每个模块：build、vet、test；contract 不再跑 Go 模块的测试（交给 go）。
- 缺工具时这一步失败并写明缺什么；AR_ALLOW_MISSING_TOOLS=1 时跳过它。
- Windows 上仓库在网络盘、又没设 UV_PROJECT_ENVIRONMENT 时直接拒绝（Temporal 的原生模块不能从共享盘加载，CLAUDE.md）。
- --clone DIR：把当前提交克隆到 DIR 再跑，用 DIR 自己的 .venv，不动别处的虚拟环境；有未提交的改动时提醒。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from contract_tests.paths import REPO_ROOT
from contract_tests.tools import require_tool, run

SCRIPT = REPO_ROOT / "scripts" / "check.py"
STEP_IDS = ("proto", "contract", "go", "rust", "py-core", "py-memory", "py-learning", "py-worker", "e2e", "faults")


def load() -> ModuleType:
    if not SCRIPT.is_file():
        pytest.fail(f"缺少 {SCRIPT.relative_to(REPO_ROOT)}", pytrace=False)
    spec = importlib.util.spec_from_file_location("agentruntime_check", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass 要能找到所在的模块
    spec.loader.exec_module(module)
    return module


def cli(*args: str, env: dict[str, str] | None = None, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    if not SCRIPT.is_file():
        pytest.fail(f"缺少 {SCRIPT.relative_to(REPO_ROOT)}", pytrace=False)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )


def plan(*args: str) -> dict[str, Any]:
    proc = cli("--list", "--json", *args)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def steps_by_id(p: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["id"]: s for s in p["steps"]}


def same_path(a: str | Path, b: str | Path) -> bool:
    return Path(a).resolve() == Path(b).resolve()


# --- 计划 ---


def test_tiers_and_step_order() -> None:
    pr, full = plan(), plan("--tier", "full")
    assert pr["tier"] == "pr", "默认档位是 pr"
    work = [s["id"] for s in full["steps"] if s["id"] != "sync"]
    assert work == list(STEP_IDS), "步骤按固定顺序：先快后慢"
    assert [s["id"] for s in pr["steps"] if s["id"] != "sync"] == [i for i in STEP_IDS if i != "faults"], "pr 档只少了故障注入套件"


def test_go_step_covers_every_module_in_the_workspace() -> None:
    work = json.loads(run([require_tool("go"), "work", "edit", "-json"]).stdout)
    modules = [REPO_ROOT / use["DiskPath"] for use in work.get("Use") or []]
    commands = steps_by_id(plan("--only", "go"))["go"]["commands"]
    for module in modules:
        mine = [c["argv"][1:] for c in commands if same_path(c["cwd"], module)]
        assert ["build", "./..."] in mine and ["vet", "./..."] in mine and ["test", "./..."] in mine, f"{module.relative_to(REPO_ROOT)}：要有 build、vet、test，实际 {mine}"


def test_python_steps_cover_every_workspace_member_with_tests() -> None:
    import tomllib

    members = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))["tool"]["uv"]["workspace"]["members"]
    pytest_commands = [c for s in plan("--tier", "full")["steps"] for c in s["commands"] if "pytest" in c["argv"]]
    for member in members:
        if member == "proto/gen/python":
            continue  # 生成代码，没有测试
        covered = any(same_path(c["cwd"], REPO_ROOT / member) or member in c["argv"] for c in pytest_commands)
        assert covered, f"{member} 的测试不在任何一步里"


def test_python_steps_share_one_sync() -> None:
    steps = plan("--only", "py-learning,py-worker")["steps"]
    assert [s["id"] for s in steps] == ["sync", "py-learning", "py-worker"], "Python 步骤之前统一同步一次"
    assert steps[0]["commands"][0]["argv"] == ["uv", "sync", "--all-packages", "--all-extras", "--locked"]
    for step in steps[1:]:
        for command in step["commands"]:
            assert command["argv"][:3] == ["uv", "run", "--no-sync"], f"{step['id']}：同步过了，之后都用 uv run --no-sync：{command['argv']}"
    assert [s["id"] for s in plan("--only", "py-learning", "--no-sync")["steps"]] == ["py-learning"]
    assert [s["id"] for s in plan("--only", "go")["steps"]] == ["go"], "只跑 Go 时不同步 Python 依赖"


def test_rust_step_runs_fmt_clippy_test_deny_and_the_black_box_suite() -> None:
    commands = [c["argv"] for c in steps_by_id(plan("--only", "rust"))["rust"]["commands"]]
    assert ["cargo", "fmt", "--all", "--check"] in commands
    assert ["cargo", "clippy", "--locked", "--workspace", "--all-targets", "--", "-D", "warnings"] in commands
    assert ["cargo", "test", "--locked", "--workspace"] in commands
    assert ["cargo", "deny", "check"] in commands
    assert any("tests/sandbox-agent" in argv and "pytest" in argv for argv in commands), "sandbox-agent 的黑盒测试（D-28）"


def test_contract_step_leaves_go_module_tests_to_the_go_step() -> None:
    [command] = steps_by_id(plan("--only", "contract"))["contract"]["commands"]
    argv = command["argv"]
    assert "-k" in argv and "not test_go_modules_build_vet_and_test" in argv[argv.index("-k") + 1]


def test_e2e_and_faults_steps() -> None:
    steps = steps_by_id(plan("--tier", "full", "--only", "e2e,faults"))
    [e2e], [faults] = steps["e2e"]["commands"], steps["faults"]["commands"]
    assert same_path(e2e["cwd"], REPO_ROOT / "tests" / "e2e") and "-m" in e2e["argv"] and "not faults" in e2e["argv"]
    assert same_path(faults["cwd"], REPO_ROOT / "tests" / "e2e") and "faults" in faults["argv"]


def test_steps_declare_the_tools_they_need() -> None:
    tools = {s["id"]: set(s["tools"]) for s in plan("--tier", "full")["steps"]}
    assert {"buf"} <= tools["proto"]
    assert {"uv", "buf", "go"} <= tools["contract"]
    assert {"go", "temporal", "postgres"} <= tools["go"]
    assert {"cargo", "cargo-deny", "go", "uv"} <= tools["rust"], "D-28：Rust 的检查与两版 agent 的黑盒测试"
    assert {"uv", "temporal", "postgres", "qdrant", "docker", "go", "cargo", "kind", "kubectl"} <= tools["py-core"]
    assert {"uv", "postgres", "qdrant"} <= tools["py-memory"]
    assert {"uv", "docker"} <= tools["py-learning"]
    assert {"uv", "temporal", "cargo"} <= tools["py-worker"], "沙箱的测试编译 Rust 版 sandbox-agent"
    assert {"uv", "go", "temporal"} <= tools["e2e"] and {"uv", "go", "temporal"} <= tools["faults"]


def test_only_and_skip_select_steps() -> None:
    ids = [s["id"] for s in plan("--skip", "e2e,py-worker,proto")["steps"]]
    assert "e2e" not in ids and "py-worker" not in ids and "proto" not in ids and "go" in ids
    proc = cli("--list", "--only", "pytho")
    assert proc.returncode == 2 and "pytho" in proc.stdout + proc.stderr, "不认识的步骤名是用法错误"
    proc = cli("--list", "--only", "faults")
    assert proc.returncode == 2, "faults 不在 pr 档里：要先 --tier full"


def test_junit_reports_go_to_the_given_directory(tmp_path: Path) -> None:
    for step in plan("--only", "py-learning,e2e", "--junit-dir", str(tmp_path))["steps"]:
        for command in step["commands"]:
            if "pytest" in command["argv"]:
                assert any(a.startswith("--junitxml=") and same_path(a.removeprefix("--junitxml="), tmp_path / f"{step['id']}.xml") for a in command["argv"])


# --- 执行 ---


def test_runner_keeps_going_and_reports_failures() -> None:
    check = load()
    ok = check.Step(id="ok", title="能过", commands=[check.Command(argv=[sys.executable, "-c", "pass"], cwd=REPO_ROOT)], tools=[])
    bad = check.Step(id="bad", title="会失败", commands=[check.Command(argv=[sys.executable, "-c", "raise SystemExit(3)"], cwd=REPO_ROOT)], tools=[])
    after = check.Step(id="after", title="后面的", commands=[check.Command(argv=[sys.executable, "-c", "pass"], cwd=REPO_ROOT)], tools=[])
    results = check.run_steps([ok, bad, after], env=dict(os.environ))
    assert [(r.id, r.status) for r in results] == [("ok", "passed"), ("bad", "failed"), ("after", "passed")], "默认失败了也接着跑，最后一起报告"
    assert check.exit_code(results) == 1
    stopped = check.run_steps([ok, bad, after], env=dict(os.environ), fail_fast=True)
    assert [r.id for r in stopped] == ["ok", "bad"], "--fail-fast 在第一个失败处停下"
    assert check.exit_code([r for r in results if r.status == "passed"]) == 0


def test_missing_tools_fail_the_step_unless_allowed(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("PATH", "Path", "AR_ALLOW_MISSING_TOOLS")}
    env["PATH"] = str(tmp_path)  # 什么工具也没有
    proc = cli("--only", "proto", env=env)
    assert proc.returncode == 1 and "buf" in proc.stdout + proc.stderr, f"缺工具时失败并写明缺什么：{proc.stdout}{proc.stderr}"
    proc = cli("--only", "proto", env={**env, "AR_ALLOW_MISSING_TOOLS": "1"})
    assert proc.returncode == 0 and "跳过" in proc.stdout, f"AR_ALLOW_MISSING_TOOLS=1 时跳过：{proc.stdout}{proc.stderr}"


def test_a_real_run_of_the_proto_step() -> None:
    require_tool("buf")
    proc = cli("--only", "proto")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "proto" in proc.stdout and "通过" in proc.stdout, "最后打印每一步的结果"


def test_python_on_a_network_share_needs_a_local_venv() -> None:
    check = load()
    share = Path(r"Z:\agentruntime\agent-runtime")
    remote = lambda _p: True  # noqa: E731
    local = lambda _p: False  # noqa: E731
    problem = check.venv_problem(share, {}, platform="win32", is_remote=remote)
    assert problem and "UV_PROJECT_ENVIRONMENT" in problem, "Windows 上仓库在网络盘、没设本地虚拟环境：拒绝并说明怎么设"
    assert check.venv_problem(share, {"UV_PROJECT_ENVIRONMENT": r"C:\venvs\ar"}, platform="win32", is_remote=remote) is None
    assert check.venv_problem(share, {}, platform="win32", is_remote=local) is None
    assert check.venv_problem(Path("/srv/agent-runtime"), {}, platform="linux", is_remote=remote) is None


# --- 在本地克隆里跑 ---


def test_clone_plan_uses_the_clones_own_venv(tmp_path: Path) -> None:
    target = tmp_path / "clone"
    p = plan("--clone", str(target), "--only", "py-learning")
    step = steps_by_id(p)["py-learning"]
    assert same_path(step["commands"][0]["cwd"], target / "services" / "py-learning")
    assert same_path(step["env"]["UV_PROJECT_ENVIRONMENT"], target / ".venv"), "克隆里的步骤用克隆自己的 .venv，不改别处的虚拟环境"
    assert not target.exists(), "--list 只打印计划，不克隆"


def test_clone_runs_the_committed_tree(tmp_path: Path) -> None:
    require_tool("buf")
    git = require_tool("git")
    target = tmp_path / "clone"
    proc = cli("--clone", str(target), "--only", "proto")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    head = run([git, "rev-parse", "HEAD"]).stdout.strip()
    assert run([git, "-C", str(target), "rev-parse", "HEAD"]).stdout.strip() == head, "克隆的是当前提交"
    assert (target / "proto").is_dir()
    dirty = run([git, "status", "--porcelain"]).stdout.strip()
    if dirty:
        assert "未提交" in proc.stdout + proc.stderr, "有未提交的改动时提醒：它们不在克隆里"
    occupied = tmp_path / "occupied"
    (occupied / "x").mkdir(parents=True)
    assert cli("--clone", str(occupied), "--only", "proto").returncode == 2, "目标目录不空时拒绝"
