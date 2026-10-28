#!/usr/bin/env python3
"""本地与 CI 共用的检查脚本（TMP 计划 T-14）。只用标准库，Python 3.11+。

    uv run --no-sync python scripts/check.py                 # pr 档：除故障注入套件外的全部检查
    uv run --no-sync python scripts/check.py --tier full     # 再加故障注入套件（夜间任务那组）
    uv run --no-sync python scripts/check.py --only go,py-worker
    uv run --no-sync python scripts/check.py --list          # 只打印计划；加 --json 给机器读
    uv run --no-sync python scripts/check.py --clone D:\\tmp\\ar   # 在当前提交的本地克隆里跑

（这台机器 PATH 上没有裸的 python，用 uv 带的；CI 里直接 python3 scripts/check.py。）

步骤按固定顺序，先快后慢：proto、contract、go、py-core、py-memory、py-learning、py-worker、e2e、faults。
- Python 步骤之前统一 uv sync 一次（--no-sync 跳过），之后都用 uv run --no-sync。
- contract 不跑 Go 模块的 build、vet、test：那是 go 这一步的事，不跑两遍。有 tests/deploy 时同时跑它（Helm Chart 与 Collector 配置，要 helm）。
- 每一步先查它要的工具；缺了就失败并写明缺什么，设了 AR_ALLOW_MISSING_TOOLS=1 时跳过这一步。
- 失败了也接着跑，最后一起报告（--fail-fast 在第一个失败处停下）。退出码：0 全部通过，1 有失败，2 用法错误。
- Windows 上仓库在网络盘、又没设 UV_PROJECT_ENVIRONMENT 时拒绝跑 Python 步骤：Temporal 的原生模块不能从共享盘加载。
- --clone DIR：把当前提交克隆到 DIR（新目录或空目录）再跑，用 DIR 自己的 .venv，不动别处的虚拟环境；克隆留着。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TextIO

ROOT = Path(__file__).resolve().parents[1]
STEP_IDS = ("proto", "contract", "go", "py-core", "py-memory", "py-learning", "py-worker", "e2e", "faults")
TIERS = {"pr": tuple(s for s in STEP_IDS if s != "faults"), "full": STEP_IDS}
SYNC = ["uv", "sync", "--all-packages", "--all-extras", "--locked"]
ALLOW_MISSING = "AR_ALLOW_MISSING_TOOLS"
POSTGRES_TOOLS = ("initdb", "pg_ctl", "psql")
STATUS_TEXT = {"passed": "通过", "failed": "失败", "skipped": "跳过"}


@dataclass
class Command:
    argv: list[str]
    cwd: Path


@dataclass
class Step:
    id: str
    title: str
    commands: list[Command]
    tools: list[str]
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Result:
    id: str
    status: str
    """passed、failed 或 skipped。"""
    seconds: float = 0.0
    message: str = ""


class UsageError(Exception):
    pass


# --- 计划 ---


def go_modules(root: Path, text: str | None = None) -> list[Path]:
    """go.work 里 use 的模块目录，按出现的顺序。text 给了时用它（例如当前提交里的 go.work），否则读工作区的。"""
    modules: list[Path] = []
    block = False
    for raw in (text if text is not None else (root / "go.work").read_text("utf-8")).splitlines():
        line = raw.split("//", 1)[0].strip()
        if block:
            if line == ")":
                block = False
            elif line:
                modules.append(root / line)
        elif line == "use (":
            block = True
        elif line.startswith("use "):
            modules.append(root / line.removeprefix("use ").strip())
    return modules


def _pytest(package: str, *args: str) -> list[str]:
    return ["uv", "run", "--no-sync", "--package", package, "pytest", "-ra", *args]


def _python(step_id: str, title: str, cwd: Path, package: str, args: Sequence[str], tools: list[str]) -> Step:
    return Step(id=step_id, title=title, commands=[Command(argv=_pytest(package, *args), cwd=cwd)], tools=tools)


def all_steps(root: Path, *, committed: bool = False) -> dict[str, Step]:
    """全部步骤。committed 为真时按当前提交算（--clone：克隆里只有提交了的内容），否则按工作区。"""
    if committed:
        go_work = subprocess.run(["git", "show", "HEAD:go.work"], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8").stdout
        deploy = subprocess.run(["git", "cat-file", "-e", "HEAD:tests/deploy"], cwd=root, capture_output=True).returncode == 0
    else:
        go_work = None
        deploy = (root / "tests" / "deploy").is_dir()
    # tests/deploy（T-15）还没进仓库时不跑它，也不要 helm。
    go = [Command(argv=["go", verb, "./..."], cwd=module) for module in go_modules(root, go_work) for verb in ("build", "vet", "test")]
    steps = [
        Step(
            id="proto",
            title="proto：buf lint、格式、兼容性",
            commands=[
                Command(argv=["buf", "lint"], cwd=root),
                Command(argv=["buf", "format", "--diff", "--exit-code"], cwd=root),
                Command(argv=["buf", "breaking", "--against", "proto/breaking-baseline.binpb"], cwd=root),
            ],
            tools=["buf"],
        ),
        _python(
            "contract",
            "契约测试与部署检查（Go 模块的测试交给 go 这一步）",
            root,
            "agentruntime-contract-tests",
            # tests/deploy（T-15）：Helm Chart 与 Collector 配置对照代码的检查，同一次 pytest 里跑。
            ["tests/contract", *(["tests/deploy"] if deploy else []), "-k", "not test_go_modules_build_vet_and_test"],
            ["uv", "buf", "go", *(["helm"] if deploy else [])],
        ),
        Step(id="go", title="Go：go.work 的每个模块 build、vet、test", commands=go, tools=["go", "temporal", "postgres"]),
        _python(
            "py-core", "libs/py-core（含 Postgres、Qdrant、Milvus 适配器与 K8s 沙箱）", root / "libs" / "py-core", "agentruntime-core", ["tests"],
            ["uv", "temporal", "postgres", "qdrant", "docker", "go", "kind", "kubectl"],
        ),
        _python("py-memory", "services/py-memory（开发后端与 Postgres + Qdrant）", root / "services" / "py-memory", "agentruntime-memory", ["tests"], ["uv", "postgres", "qdrant"]),
        _python("py-learning", "services/py-learning（含消费 trace 主题的 Redpanda 容器）", root / "services" / "py-learning", "agentruntime-learning", ["tests"],
                ["uv", "docker"]),
        _python("py-worker", "services/py-worker（沙箱环境表在 SQLite 与 Postgres 上各跑一遍）", root / "services" / "py-worker", "agentruntime-worker", ["tests"],
                ["uv", "temporal", "postgres", "go"]),
        _python("e2e", "端到端测试（不含故障注入）", root / "tests" / "e2e", "agentruntime-e2e-tests", ["-m", "not faults"], ["uv", "go", "temporal"]),
        _python("faults", "故障注入与恢复套件", root / "tests" / "e2e", "agentruntime-e2e-tests", ["faults"], ["uv", "go", "temporal"]),
    ]
    return {s.id: s for s in steps}


def _ids(raw: str | None) -> list[str]:
    return [s.strip() for s in (raw or "").split(",") if s.strip()]


def plan(
    root: Path,
    tier: str = "pr",
    only: Sequence[str] = (),
    skip: Sequence[str] = (),
    *,
    sync: bool = True,
    clone: Path | None = None,
    junit_dir: Path | None = None,
) -> list[Step]:
    """要跑的步骤，按固定顺序；有 Python 步骤且 sync 为真时前面加一次 uv sync。clone 给了时路径都换到克隆里。"""
    if tier not in TIERS:
        raise UsageError(f"没有档位 {tier}：只有 {'、'.join(TIERS)}")
    unknown = [s for s in (*only, *skip) if s not in STEP_IDS]
    if unknown:
        raise UsageError(f"没有这些步骤：{'、'.join(unknown)}；步骤有 {'、'.join(STEP_IDS)}")
    outside = [s for s in only if s not in TIERS[tier]]
    if outside:
        raise UsageError(f"{'、'.join(outside)} 不在 {tier} 档里（要先 --tier full）")
    base = clone if clone is not None else root
    # 克隆还没做出来：计划按当前提交算，再把目录换到克隆里（克隆的就是这个提交）。
    steps = all_steps(root, committed=clone is not None)
    chosen = [steps[s] for s in TIERS[tier] if (not only or s in only) and s not in skip]
    if junit_dir is not None:
        for step in chosen:
            for command in step.commands:
                if "pytest" in command.argv:
                    command.argv.append(f"--junitxml={junit_dir / (step.id + '.xml')}")
    if clone is not None:
        for step in chosen:
            for command in step.commands:
                command.cwd = clone / command.cwd.relative_to(root)
            if "uv" in step.tools:
                step.env["UV_PROJECT_ENVIRONMENT"] = str(clone / ".venv")
    if sync and any("uv" in s.tools for s in chosen):
        env = {"UV_PROJECT_ENVIRONMENT": str(clone / ".venv")} if clone is not None else {}
        chosen.insert(0, Step(id="sync", title="安装 Python 依赖（uv sync，一次）", commands=[Command(argv=list(SYNC), cwd=base)], tools=["uv"], env=env))
    return chosen


# --- 工具与环境 ---


def _which(name: str, env: Mapping[str, str]) -> str | None:
    return shutil.which(name, path=env.get("PATH") or env.get("Path") or None)


def tool_available(tool: str, env: Mapping[str, str]) -> bool:
    if tool == "postgres":
        directory = env.get("AR_POSTGRES_BIN", "")
        if directory:
            return all(shutil.which(t, path=directory) for t in POSTGRES_TOOLS)
        return all(_which(t, env) for t in POSTGRES_TOOLS)
    if tool == "qdrant":
        configured = env.get("AR_QDRANT_BIN", "")
        if configured:
            path = Path(configured)
            return path.is_file() or shutil.which("qdrant", path=str(path)) is not None
        return _which("qdrant", env) is not None
    if tool in ("docker", "kubectl"):
        # docker：Milvus 适配器的测试拉起单机版容器（milvustest），K8s 沙箱的测试用 kind（kindtest），
        # Learning Pipeline 消费 trace 主题的测试拉起 Redpanda（services/py-learning/tests/redpanda_support.py）。
        # kubectl：kindtest 用它应用 deploy/cluster 的清单；Docker Desktop 把它与 docker 放在同一目录。
        configured = env.get("AR_DOCKER_BIN", "")
        if configured:
            path = Path(configured)
            directory = path if path.is_dir() else path.parent
            if shutil.which(tool, path=str(directory)) is not None:
                return True
        return _which(tool, env) is not None
    if tool == "kind" and env.get("AR_KIND_BIN"):
        return Path(env["AR_KIND_BIN"]).exists()
    return _which(tool, env) is not None


def missing_tools(step: Step, env: Mapping[str, str]) -> list[str]:
    return [t for t in step.tools if not tool_available(t, env)]


def is_remote_drive(path: Path) -> bool:
    """Windows：UNC 路径，或映射的网络盘。"""
    text = str(path)
    if text.startswith("\\\\"):
        return True
    if sys.platform != "win32" or len(text) < 2 or text[1] != ":":
        return False
    import ctypes

    drive_remote = 4
    return ctypes.windll.kernel32.GetDriveTypeW(text[:2] + "\\") == drive_remote


def venv_problem(root: Path, env: Mapping[str, str], *, platform: str = sys.platform, is_remote: Callable[[Path], bool] = is_remote_drive) -> str | None:
    """Windows 上仓库在网络盘、又没设本地虚拟环境时，说明为什么不能跑、怎么设；否则 None。"""
    if platform != "win32" or env.get("UV_PROJECT_ENVIRONMENT") or not is_remote(root):
        return None
    return (
        f"仓库在网络盘上（{root}）：Temporal Python SDK 的原生模块不能从共享盘加载，虚拟环境要放在本地盘。\n"
        '先设 $env:UV_PROJECT_ENVIRONMENT = "$env:USERPROFILE\\.local\\venvs\\agent-runtime"（CLAUDE.md），或用 --clone 在本地克隆里跑。'
    )


# --- 执行 ---


def _say(out: TextIO, text: str) -> None:
    out.write(text + "\n")
    out.flush()


def run_steps(steps: Sequence[Step], *, env: Mapping[str, str], fail_fast: bool = False, out: TextIO = sys.stdout) -> list[Result]:
    """逐步执行。缺工具：失败（AR_ALLOW_MISSING_TOOLS=1 时跳过）。命令失败：这一步失败，默认接着跑后面的步骤。"""
    allow_missing = env.get(ALLOW_MISSING) == "1"
    results: list[Result] = []
    for step in steps:
        missing = missing_tools(step, env)
        if missing:
            message = f"缺少工具：{'、'.join(missing)}"
            status = "skipped" if allow_missing else "failed"
            _say(out, f"== {step.id}：{message}，{STATUS_TEXT[status]}")
            results.append(Result(step.id, status, 0.0, message))
            if status == "failed" and fail_fast:
                break
            continue
        _say(out, f"== {step.id}：{step.title}")
        started = time.monotonic()
        status, message = "passed", ""
        step_env = {**env, **step.env}
        for command in step.commands:
            _say(out, f"   $ {' '.join(command.argv)}    （{command.cwd}）")
            executable = shutil.which(command.argv[0], path=step_env.get("PATH") or step_env.get("Path") or None) or command.argv[0]
            code = subprocess.run([executable, *command.argv[1:]], cwd=command.cwd, env=step_env, check=False).returncode
            if code != 0:
                status, message = "failed", f"{' '.join(command.argv)} 退出码 {code}"
                break
        results.append(Result(step.id, status, time.monotonic() - started, message))
        if status == "failed" and fail_fast:
            break
    return results


def exit_code(results: Sequence[Result]) -> int:
    return 1 if any(r.status == "failed" for r in results) else 0


def summary(results: Sequence[Result], out: TextIO = sys.stdout) -> None:
    _say(out, "\n结果：")
    width = max((len(r.id) for r in results), default=4)
    for r in results:
        _say(out, f"  {r.id.ljust(width)}  {STATUS_TEXT[r.status]}  {r.seconds:7.1f}s  {r.message}".rstrip())
    failed = [r.id for r in results if r.status == "failed"]
    _say(out, f"失败：{'、'.join(failed)}" if failed else "全部通过（跳过的不算失败）")


# --- 克隆 ---


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()


def make_clone(root: Path, target: Path, out: TextIO) -> None:
    """把当前提交克隆到 target（新目录或空目录）。有未提交的改动时提醒：它们不在克隆里。"""
    head = _git("rev-parse", "HEAD", cwd=root)
    if _git("status", "--porcelain", cwd=root):
        _say(out, "提醒：工作区有未提交的改动，它们不在克隆里；克隆的是提交 " + head[:12])
    # 换行符按源检出的设置：Windows 的 Git 常在系统级设 core.autocrlf=true，克隆出来是 CRLF，buf format 会报差异。
    autocrlf = subprocess.run(["git", "config", "--get", "core.autocrlf"], cwd=root, capture_output=True, text=True).stdout.strip() or "false"
    subprocess.run(["git", "clone", "--quiet", "--no-local", "--config", f"core.autocrlf={autocrlf}", str(root), str(target)], check=True)
    _git("checkout", "--quiet", "--detach", head, cwd=target)
    _say(out, f"克隆在 {target}（提交 {head[:12]}）；跑完留着，不用了自己删")


# --- 入口 ---


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="本地与 CI 共用的检查（TMP 计划 T-14）。步骤：" + "、".join(STEP_IDS))
    p.add_argument("--tier", default="pr", choices=sorted(TIERS), help="pr（默认，不含故障注入套件）或 full")
    p.add_argument("--only", help="只跑这些步骤，逗号分隔")
    p.add_argument("--skip", help="不跑这些步骤，逗号分隔")
    p.add_argument("--list", action="store_true", help="只打印计划，不执行")
    p.add_argument("--json", action="store_true", help="和 --list 一起用：输出 JSON")
    p.add_argument("--no-sync", action="store_true", help="不先 uv sync（依赖已经装好时）")
    p.add_argument("--fail-fast", action="store_true", help="在第一个失败处停下")
    p.add_argument("--junit-dir", type=Path, help="pytest 的 JUnit 报告写进这个目录（<步骤>.xml）")
    p.add_argument("--clone", type=Path, help="把当前提交克隆到这个目录（新目录或空目录）再跑")
    return p


def _as_json(steps: Sequence[Step], tier: str, root: Path) -> str:
    def encode(value: Any) -> Any:
        return str(value) if isinstance(value, Path) else value

    data = {"tier": tier, "root": str(root), "steps": [json.loads(json.dumps(asdict(s), default=encode)) for s in steps]}
    return json.dumps(data, ensure_ascii=False, indent=2)


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None, out: TextIO = sys.stdout) -> int:
    # Windows 上输出被管道接走时默认是 ANSI 代码页，中文会乱：一律 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    env = dict(os.environ if env is None else env)
    clone = args.clone.resolve() if args.clone is not None else None
    try:
        steps = plan(
            ROOT,
            args.tier,
            _ids(args.only),
            _ids(args.skip),
            sync=not args.no_sync,
            clone=clone,
            junit_dir=args.junit_dir.resolve() if args.junit_dir is not None else None,
        )
    except UsageError as err:
        print(err, file=sys.stderr)
        return 2
    if args.list:
        if args.json:
            _say(out, _as_json(steps, args.tier, clone or ROOT))
        else:
            for step in steps:
                _say(out, f"{step.id}：{step.title}（工具：{'、'.join(step.tools) or '无'}）")
                for command in step.commands:
                    _say(out, f"   $ {' '.join(command.argv)}    （{command.cwd}）")
        return 0
    if clone is not None:
        if clone.exists() and any(clone.iterdir()):
            print(f"{clone} 不是空目录：--clone 要新目录或空目录", file=sys.stderr)
            return 2
    elif any("uv" in s.tools for s in steps):
        problem = venv_problem(ROOT, env)
        if problem:
            print(problem, file=sys.stderr)
            return 2
    if clone is not None:
        make_clone(ROOT, clone, out)
    results = run_steps(steps, env=env, fail_fast=args.fail_fast, out=out)
    summary(results, out)
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
