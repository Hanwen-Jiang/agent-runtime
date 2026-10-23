"""T-14 验收：PR 与 main 上自动跑测试（TMP 计划 T-14“CI 增加 py-learning、py-worker、py-memory 的日常测试，不只是夜间任务”）。

.github/workflows/ci.yml：
- pull_request 与推到 main 时触发；只读权限；同一个分支上新的运行取消旧的。
- 每个 job 调用 scripts/check.py --only …，所有 job 合起来恰好是脚本 pr 档的全部步骤，各一次：本地跑的与 CI 跑的是同一组命令。
- 每个 job 装好它的步骤声明要的工具（脚本 --list --json 里的 tools），版本与夜间任务、CLAUDE.md 一致；缺工具不跳过。
- 测试报告（JUnit）总是上传，失败时也能看。
夜间任务（nightly.yml）不变：故障注入套件与 Postgres 业务库上的端到端测试仍在那里。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

import pytest

from contract_tests.paths import REPO_ROOT

CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
NIGHTLY = REPO_ROOT / ".github" / "workflows" / "nightly.yml"
SCRIPT = REPO_ROOT / "scripts" / "check.py"
CHART_VALUES = REPO_ROOT / "deploy" / "helm" / "agent-runtime" / "values.yaml"

# 步骤声明的工具 → job 里装它的标志。
INSTALLS = {
    "go": "actions/setup-go",
    "uv": "astral-sh/setup-uv",
    "temporal": "TEMPORAL_CLI_VERSION",
    "postgres": "AR_POSTGRES_BIN",
    "qdrant": "AR_QDRANT_BIN",
    "buf": "BUF_VERSION",
    "helm": "HELM_VERSION",
}


def text() -> str:
    if not CI.is_file():
        pytest.fail(f"缺少 {CI.relative_to(REPO_ROOT)}", pytrace=False)
    return CI.read_text("utf-8")


def jobs() -> dict[str, str]:
    """jobs: 下面每个 job 的原文（按两格缩进的键切开）。"""
    body = text().split("\njobs:\n", 1)
    assert len(body) == 2, "ci.yml 要有 jobs"
    out: dict[str, str] = {}
    current = ""
    for line in body[1].splitlines():
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            current = match.group(1)
            out[current] = ""
        elif current:
            out[current] += line + "\n"
    return out


def only(job: str) -> list[str]:
    calls = re.findall(r"scripts/check\.py[^\n]*?--only[ =]([A-Za-z0-9_,-]+)", job)
    return [step for call in calls for step in call.split(",")]


def script_plan(*args: str) -> dict:
    if not SCRIPT.is_file():
        pytest.fail(f"缺少 {SCRIPT.relative_to(REPO_ROOT)}", pytrace=False)
    proc = subprocess.run([sys.executable, str(SCRIPT), "--list", "--json", *args], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def env_value(source: str, name: str) -> str:
    """第一个以 name: 开头的行的取值（行首只有空白，version 不会匹配到 go-version）。"""
    match = re.search(rf"(?m)^\s*{re.escape(name)}:\s*\"?([^\"\n]+)\"?", source)
    return match.group(1).strip() if match else ""


def test_runs_on_pull_requests_and_main() -> None:
    t = text()
    assert re.search(r"(?m)^\s*pull_request:", t), "PR 上要自动跑"
    assert re.search(r"(?ms)^\s*push:\s*\n\s*branches:\s*\[?\s*\"?main", t), "推到 main 时也跑"
    assert re.search(r"(?m)^permissions:\s*\n\s*contents:\s*read", t), "只读权限"
    assert "concurrency:" in t and "cancel-in-progress: true" in t, "同一个分支上新的运行取消旧的"


def test_jobs_together_run_exactly_the_pr_tier() -> None:
    pr = [s["id"] for s in script_plan()["steps"] if s["id"] != "sync"]
    ran = [step for job in jobs().values() for step in only(job)]
    assert sorted(ran) == sorted(pr), f"CI 的各 job 合起来应恰好是 pr 档的步骤、各一次：pr 档 {pr}，CI {ran}"
    assert "--tier full" not in text(), "故障注入套件留在夜间任务"


def test_every_job_installs_the_tools_its_steps_need() -> None:
    tools = {s["id"]: set(s["tools"]) for s in script_plan("--tier", "full")["steps"]}
    problems = []
    for name, job in jobs().items():
        assert "runs-on: ubuntu-latest" in job and "timeout-minutes:" in job, f"{name} 要在 ubuntu-latest 上跑并设超时"
        steps = only(job)
        assert steps, f"{name} 没有调用 scripts/check.py --only"
        for tool in sorted(set().union(*(tools[s] for s in steps))):
            if INSTALLS[tool] not in job:
                problems.append(f"{name}（{','.join(steps)}）要装 {tool}，job 里没有 {INSTALLS[tool]}")
    assert not problems, "\n".join(problems)


def test_tool_versions_match_the_nightly_job_and_claude_md() -> None:
    t, nightly = text(), NIGHTLY.read_text("utf-8")
    assert env_value(t, "go-version") == env_value(nightly, "go-version") == "1.27"
    assert env_value(t, "version") == "0.12.x", "uv 0.12"
    for name in ("TEMPORAL_CLI_VERSION", "QDRANT_VERSION"):
        assert env_value(t, name) == env_value(nightly, name), f"{name} 与夜间任务一致"
    assert env_value(t, "BUF_VERSION").startswith("1.73"), "buf 1.73（CLAUDE.md）"


def test_contract_job_validates_the_collector_config() -> None:
    # T-15 的 Collector 配置在 CI 里也用 otelcol-contrib 自己的 validate 校验（tests/deploy 随 contract 步骤跑），
    # 版本与 Chart 里的 Collector 镜像一致（docs/plan/deploy-telemetry.md 的工具表）。
    if not CHART_VALUES.is_file():
        pytest.skip("还没有 Chart（T-15）")
    image = re.search(r"opentelemetry-collector-contrib:([0-9][0-9.]*)", CHART_VALUES.read_text("utf-8"))
    assert image, f"{CHART_VALUES.relative_to(REPO_ROOT)} 里找不到 Collector 镜像的版本"
    assert "OTELCOL_VERSION" in jobs()["contract"], "契约 job 要装 otelcol-contrib"
    assert env_value(text(), "OTELCOL_VERSION") == image.group(1), "otelcol-contrib 与 Chart 里的 Collector 镜像同一版本"


def test_missing_tools_are_never_skipped_in_ci() -> None:
    t = text()
    assert "AR_ALLOW_MISSING_TOOLS" not in t, "CI 里缺工具就失败，不跳过"


def test_test_reports_are_always_uploaded() -> None:
    for name, job in jobs().items():
        if set(only(job)) <= {"proto", "go"}:
            continue  # 只有 buf、go 的 job 没有 pytest 报告
        assert "--junit-dir" in job, f"{name}：pytest 的报告写进 --junit-dir"
        assert re.search(r"if:\s*always\(\)\s*\n\s*uses:\s*actions/upload-artifact@", job), f"{name}：报告总是上传，失败时也能看"
