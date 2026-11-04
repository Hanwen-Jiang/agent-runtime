"""Terminal-Bench 2.0 的适配器（docs/plan/feedback-2026-11-01.md N-11）：经 Task API 跑真正的系统，判分在本机的 Docker 里。

Terminal-Bench 2.0（https://github.com/laude-institute/terminal-bench-2，Harbor 格式）每个任务一个目录：

    <任务>/task.toml          元数据：难度、类别，[environment] docker_image（官方预先构建的镜像）、[agent]/[verifier] timeout_sec
    <任务>/instruction.md     给 Agent 的任务说明
    <任务>/environment/       镜像的构建上下文（Dockerfile 的 WORKDIR 几乎都是 /app）
    <任务>/tests/test.sh      判分脚本：在任务的容器里跑，把 reward（1 或 0）写进 /logs/verifier/reward.txt
    <任务>/solution/          参考答案

跑法（与 SWE-bench 的适配器同一套调度：预算、并发、续跑、无人值守的回复、超时与超支取消，见 swebench._Runner）：

1. prepare（要 Docker）：从官方镜像里取出工作目录（WORKDIR）的内容。只收文本文件（UTF-8、没有 NUL，单个与总量有上限）；
   有二进制或太大的任务记为不可移植（portable=false），run 默认不选，报告里单列。
2. run：工作区 = 工作目录的内容 + 运行环境定义（D-17：FROM 官方镜像，把工作目录换成指向工作区的链接），消息 = instruction.md
   加一段固定的说明（工作目录在哪、判分只看工作目录里的文件）。测试与参考答案一概不发给 Runtime。跑完取工作区相对初始提交的 diff。
3. grade（要 Docker，判分脚本要联网装 pytest）：起官方镜像的容器，工作目录换成“初始内容 + diff”，拷入 tests/，跑 test.sh
   （verifier 的超时），读 reward。
4. report：解决率（reward ≥ 1）、按难度与类别、花费。

限制（报告里写明）：判分只带回工作目录里的文件。要靠工作目录以外的状态（装到系统里的包、起着的服务、写到别处的文件）的任务，
在这里判不出来，分数偏保守。真正在 Agent 用过的容器里判分要 Runtime 提供不经模型的验收步骤（契约变更，S-01），见 N-11 的说明。
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from agentruntime_learning.benchmarks import swebench as sb

SUITE = "terminal-bench"
DEFAULT_REPO = "https://github.com/laude-institute/terminal-bench-2.git"
REPO_LABEL = "terminal-bench"
DEVCONTAINER = sb.DEVCONTAINER
DOCKERFILE = sb.DOCKERFILE
ENV_DEFINITION_VERSION = 1
DIFFICULTIES = ("easy", "medium", "hard")
# 工作目录里取出来的文件的上限：单个、总量（经 Workspace API 的 files 传，太大的任务记为不可移植）。
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
# 工作目录里不带回判分容器的：运行环境定义与 git 的元数据。
NOT_GRADED = (".devcontainer", ".git")
REWARD_FILE = "/logs/verifier/reward.txt"
GRADE_LOG_CHARS = 4000
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")

MESSAGE = """{instruction}

<harness>
You are working in a Linux container. The task's working directory is {workdir}. When you finish, hidden tests check the result.
Only the files under {workdir} are carried over to the tests: keep everything the task asks for there (installed tools and
running services are not carried over, so the code under {workdir} must work on its own).
</harness>
"""


class Live(Protocol):
    def planned(self, total: int, *, carried: int = 0, carried_cost_usd: float = 0.0) -> None: ...
    def case(self, outcome: str, cost_usd: float | None = None) -> None: ...
    def score(self, metric: str, value: float) -> None: ...
    def finished(self, *, model: str = "") -> None: ...


@dataclass(frozen=True)
class TBTask:
    """一个 Terminal-Bench 任务（只有给 Agent 的部分；tests 与 solution 留在 path 里，不进 raw）。"""

    instance_id: str
    path: Path
    instruction: str
    docker_image: str
    workdir: str
    difficulty: str
    category: str
    agent_timeout_s: float
    verifier_timeout_s: float
    raw: dict[str, Any] = field(default_factory=dict, compare=False)
    repo: str = REPO_LABEL

    @property
    def base_commit(self) -> str:  # swebench._Runner 用它算请求 id 之外不用；Terminal-Bench 没有仓库
        return ""


def _workdir(dockerfile: str) -> str:
    """Dockerfile 里最后一个 WORKDIR（相对路径接在前一个后面）；没有时是 /app。"""
    current = "/"
    seen = False
    for line in dockerfile.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].upper() == "WORKDIR":
            value = parts[1].strip().strip('"')
            current = value if value.startswith("/") else current.rstrip("/") + "/" + value
            seen = True
    return current if seen else "/app"


def load_task(path: Path) -> TBTask:
    path = Path(path)
    meta = tomllib.loads((path / "task.toml").read_text("utf-8"))
    env = meta.get("environment") or {}
    image = str(env.get("docker_image") or "").strip()
    if not image:
        raise ValueError(f"{path.name}：task.toml 没有 environment.docker_image（要官方预先构建的镜像）")
    dockerfile = path / "environment" / "Dockerfile"
    workdir = _workdir(dockerfile.read_text("utf-8", errors="replace")) if dockerfile.exists() else "/app"
    if not _PATH.match(workdir):
        raise ValueError(f"{path.name}：工作目录 {workdir!r} 不是简单的绝对路径")
    metadata = meta.get("metadata") or {}
    name = path.name
    if not _NAME.match(name):
        raise ValueError(f"任务目录名不合法：{name}")
    raw = {
        "instance_id": name, "docker_image": image, "workdir": workdir, "difficulty": str(metadata.get("difficulty") or ""),
        "category": str(metadata.get("category") or ""), "tags": list(metadata.get("tags") or []),
    }
    return TBTask(
        instance_id=name, path=path, instruction=(path / "instruction.md").read_text("utf-8").strip(), docker_image=image, workdir=workdir,
        difficulty=raw["difficulty"], category=raw["category"],
        agent_timeout_s=float((meta.get("agent") or {}).get("timeout_sec") or 900), verifier_timeout_s=float((meta.get("verifier") or {}).get("timeout_sec") or 900),
        raw=raw,
    )


def load_tasks(root: Path) -> list[TBTask]:
    """root 下每个带 task.toml 的目录一个任务，按名字排序。"""
    tasks = [load_task(p) for p in sorted(Path(root).iterdir()) if (p / "task.toml").is_file()]
    if not tasks:
        raise ValueError(f"{root} 里没有任务（每个任务一个带 task.toml 的目录）")
    return tasks


def select(
    tasks: Sequence[TBTask], *, limit: int | None = None, seed: int = 0, ids: Sequence[str] | None = None,
    difficulties: Sequence[str] = (), categories: Sequence[str] = (), only: Collection[str] | None = None,
) -> list[TBTask]:
    """按 ids 选，或按难度、类别筛过之后按种子抽 limit 个；only 是可选的范围（例如可移植的任务）。顺序按名字，可复现。"""
    by_id = {t.instance_id: t for t in tasks}
    if ids:
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise ValueError(f"没有这些任务：{', '.join(missing)}")
        return [by_id[i] for i in ids]
    pool = [t for t in tasks if (not difficulties or t.difficulty in difficulties) and (not categories or t.category in categories)]
    if only is not None:
        pool = [t for t in pool if t.instance_id in only]
    if limit is not None and limit < len(pool):
        pool = sorted(random.Random(seed).sample(pool, limit), key=lambda t: t.instance_id)
    return pool


# --- prepare：从官方镜像取出工作目录 ---


class Docker(Protocol):
    """用到的 docker 命令（测试里换成假的）。run 返回（退出码，标准输出，标准错误）；stdout_bytes 时标准输出原样给字节。"""

    def __call__(self, args: Sequence[str], *, timeout_s: float, stdout_bytes: bool = False) -> tuple[int, Any, str]: ...


def docker_cli(binary: str = "docker") -> Docker:
    def run(args: Sequence[str], *, timeout_s: float, stdout_bytes: bool = False) -> tuple[int, Any, str]:
        proc = subprocess.run([binary, *args], capture_output=True, timeout=timeout_s, check=False)
        out = proc.stdout if stdout_bytes else proc.stdout.decode("utf-8", errors="replace")
        return proc.returncode, out, proc.stderr.decode("utf-8", errors="replace")

    return run


def _container_name(prefix: str, task: str) -> str:
    return f"{prefix}-{re.sub(r'[^a-z0-9]+', '-', task.lower())[:40]}-{hashlib.sha256(task.encode()).hexdigest()[:8]}"


def text_files(archive: bytes, root: str, *, max_file: int = MAX_FILE_BYTES, max_total: int = MAX_TOTAL_BYTES) -> tuple[list[dict[str, str]], list[str]]:
    """docker cp 出来的 tar（顶层目录是工作目录的名字）里的文本文件，路径相对工作目录；第二项是带不走的（二进制、太大、链接）。"""
    files: list[dict[str, str]] = []
    problems: list[str] = []
    total = 0
    top = root.rstrip("/").rsplit("/", 1)[-1] or root
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            name = member.name.split("/", 1)
            rel = name[1] if len(name) == 2 and name[0] == top else ("" if member.name == top else member.name)
            if not rel or member.isdir():
                continue
            if any(part in ("", "..") for part in rel.split("/")):
                problems.append(f"{rel}: path")
                continue
            if member.issym() or member.islnk():
                problems.append(f"{rel}: link")
                continue
            if not member.isfile():
                continue
            data = tar.extractfile(member)
            content = data.read() if data else b""
            if len(content) > max_file:
                problems.append(f"{rel}: {len(content)} bytes")
                continue
            if b"\0" in content:
                problems.append(f"{rel}: binary")
                continue
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                problems.append(f"{rel}: not utf-8")
                continue
            total += len(content)
            files.append({"path": rel, "content": text})
    if total > max_total:
        problems.append(f"total {total} bytes")
    return sorted(files, key=lambda f: f["path"]), problems


def prepare(tasks: Sequence[TBTask], out_dir: Path, *, docker: Docker | None = None, timeout_s: float = 1800, remove_images: bool = False) -> dict[str, Any]:
    """每个任务写 <out_dir>/prepared/<任务>.json：工作目录、取出来的文件、能不能移植。已经准备过的跳过。返回汇总。"""
    run = docker or docker_cli()
    root = Path(out_dir) / "prepared"
    root.mkdir(parents=True, exist_ok=True)
    summary: Counter[str] = Counter()
    for task in tasks:
        path = root / f"{task.instance_id}.json"
        if path.exists():
            summary["cached"] += 1
            continue
        name = _container_name("tbp", task.instance_id)
        doc: dict[str, Any] = {"task": task.instance_id, "docker_image": task.docker_image, "workdir": task.workdir}
        try:
            code, _, err = run(["create", "--name", name, task.docker_image], timeout_s=timeout_s)
            if code != 0:
                raise RuntimeError(f"docker create：{err.strip()[-300:]}")
            code, tar_bytes, err = run(["cp", f"{name}:{task.workdir}", "-"], timeout_s=timeout_s, stdout_bytes=True)
            if code != 0:
                raise RuntimeError(f"docker cp：{err.strip()[-300:]}")
            files, problems = text_files(tar_bytes, task.workdir)
            doc.update({"files": files, "unportable": problems, "portable": not problems})
            summary["portable" if not problems else "unportable"] += 1
        except Exception as exc:  # noqa: BLE001 — 一个任务准备不了不拖垮其余的；不写文件，下次再试
            summary["failed"] += 1
            doc = {**doc, "error": f"{type(exc).__name__}: {exc}"[:500]}
            sb._write_text(root / f"{task.instance_id}.error.json", json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
            continue
        finally:
            run(["rm", "-f", name], timeout_s=120)
            if remove_images:
                run(["rmi", task.docker_image], timeout_s=600)
        sb._write_text(path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    return dict(summary)


def prepared(out_dir: Path, task: TBTask) -> dict[str, Any] | None:
    path = Path(out_dir) / "prepared" / f"{task.instance_id}.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else None


def portable_ids(out_dir: Path) -> set[str]:
    root = Path(out_dir) / "prepared"
    out = set()
    for path in root.glob("*.json") if root.is_dir() else []:
        if path.name.endswith(".error.json"):
            continue
        doc = json.loads(path.read_text("utf-8"))
        if doc.get("portable"):
            out.add(str(doc["task"]))
    return out


# --- run：经 Task API ---


def task_message(task: TBTask) -> str:
    return MESSAGE.format(instruction=task.instruction, workdir=task.workdir)


def environment_files(task: TBTask, *, workspace_path: str = "/workspace") -> list[dict[str, str]]:
    """运行环境定义（D-17 识别 .devcontainer/devcontainer.json）：官方镜像，工作目录换成指向工作区的链接。"""
    if not _PATH.match(workspace_path):
        raise ValueError(f"工作区挂载点要是绝对路径：{workspace_path}")
    devcontainer = {"name": f"Terminal-Bench {task.instance_id}", "build": {"dockerfile": DOCKERFILE.rsplit("/", 1)[1], "context": "."}}
    link = "" if task.workdir == workspace_path else f"RUN rm -rf {task.workdir} && mkdir -p {task.workdir.rsplit('/', 1)[0] or '/'} && ln -s {workspace_path} {task.workdir}\n"
    dockerfile = f"FROM {task.docker_image}\n# agentruntime terminal-bench environment v{ENV_DEFINITION_VERSION}\n{link}WORKDIR {task.workdir}\n"
    return [{"path": DEVCONTAINER, "content": json.dumps(devcontainer, indent=2) + "\n"}, {"path": DOCKERFILE, "content": dockerfile}]


def workspace_id(run_id: str, task: str) -> str:
    """工作区 id（格式同租户 ID）：任务名的小写化加上 run_id 与任务名的哈希。"""
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:40].strip("-")
    digest = hashlib.sha256(f"{run_id}\x00{task}".encode()).hexdigest()[:10]
    return f"tb-{slug}-{digest}"


class _Runner(sb._Runner):
    def _workspace_id(self, inst: Any) -> str:
        return workspace_id(self.s.run_id, inst.instance_id)

    def _workspace_args(self, inst: Any) -> tuple[str, str, list[dict[str, str]]]:
        prep = prepared(self.out, inst)
        if prep is None or "files" not in prep:
            raise ValueError(f"{inst.instance_id} 还没准备（先跑 prepare）")
        env = environment_files(inst, workspace_path=self.s.workspace_path) if self.s.environment == "official" else []
        return "", "", [*prep["files"], *env]

    def _message(self, inst: Any) -> str:
        return task_message(inst)


def run(runtime: sb.Runtime, tasks: Sequence[TBTask], out_dir: Path, settings: sb.Settings, *, selection: Mapping[str, Any] | None = None, live: Live | None = None) -> sb.RunSummary:
    """跑选中的任务，写 run.json、records/；同一个输出目录再跑是续跑（与 SWE-bench 的适配器相同的规则）。"""
    out_dir = Path(out_dir)
    (out_dir / "records").mkdir(parents=True, exist_ok=True)
    _manifest(out_dir, tasks, settings, selection)
    runner = _Runner(runtime, list(tasks), out_dir, settings, live)  # type: ignore[arg-type]
    runner.go()
    if live is not None:
        live.finished(model=settings.model_name)
    return runner.summary()


def _manifest(out_dir: Path, tasks: Sequence[TBTask], settings: sb.Settings, selection: Mapping[str, Any] | None) -> None:
    path = out_dir / "run.json"
    ids = [t.instance_id for t in tasks]
    pinned = {k: v for k, v in settings.pinned().items() if k not in ("hints", "repo_url_template")}
    if path.exists():
        before = json.loads(path.read_text("utf-8"))
        for key, value in {"run_id": settings.run_id, "instance_ids": ids, **pinned}.items():
            old = before.get(key, (before.get("settings") or {}).get(key))
            if old != value:
                raise sb.ResumeMismatch(f"{out_dir} 里已有一次运行，{key} 不一致：换一个输出目录")
        doc = before
    else:
        doc = {"benchmark": "terminal-bench", "schema_version": 1, "run_id": settings.run_id, "created_at": datetime.now(UTC).isoformat(),
               "instance_ids": ids, "settings": pinned, "selection": dict(selection or {}),
               "tasks": {t.instance_id: t.raw for t in tasks}}
    doc["limits"] = {"budget_usd": settings.budget_usd, "task_usd_limit": settings.task_usd_limit, "concurrency": settings.concurrency}
    sb._write_text(path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")


# --- grade：在官方镜像里跑隐藏的测试 ---


def text_patch(patch: str) -> tuple[str, list[str]]:
    """去掉补丁里的二进制文件（Workspace API 的 diff 不带二进制的内容，git apply 打不上）；第二项是去掉的文件。"""
    blocks = re.split(r"(?m)^(?=diff --git )", patch)
    kept, dropped = [], []
    for block in blocks:
        if re.search(r"(?m)^(Binary files .* differ|GIT binary patch)$", block):
            head = re.match(r"diff --git a/(\S+)", block)
            dropped.append(head.group(1) if head else "?")
        else:
            kept.append(block)
    return "".join(kept), dropped


def apply_patch(files: Sequence[Mapping[str, str]], patch: str, into: Path, *, git: str = "git") -> list[str]:
    """把初始文件写进 into，提交一次，再 git apply 补丁（补丁是 Workspace API 相对初始提交的 diff）。返回带不过去的二进制文件。"""
    into.mkdir(parents=True, exist_ok=True)
    for f in files:
        target = into / f["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["content"], "utf-8", newline="")
    env = {"GIT_AUTHOR_NAME": "tb", "GIT_AUTHOR_EMAIL": "tb@example.invalid", "GIT_COMMITTER_NAME": "tb", "GIT_COMMITTER_EMAIL": "tb@example.invalid", "PATH": _path()}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "--allow-empty", "-m", "initial"]):
        subprocess.run([git, *args], cwd=into, env=env, check=True, capture_output=True)
    patch, dropped = text_patch(patch)
    if patch.strip():
        proc = subprocess.run([git, "apply", "--whitespace=nowarn", "-"], cwd=into, env=env, input=patch.encode(), capture_output=True, check=False)
        if proc.returncode != 0:
            raise ValueError(f"补丁打不上：{proc.stderr.decode('utf-8', errors='replace')[-300:]}")
    shutil.rmtree(into / ".git", ignore_errors=True)
    for name in NOT_GRADED:
        target = into / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    return dropped


def _path() -> str:
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")


def _reward(text: str) -> float | None:
    try:
        return float(text.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def grade_one(task: TBTask, rec: Mapping[str, Any], files: Sequence[Mapping[str, str]], *, docker: Docker, work: Path) -> dict[str, Any]:
    """一个任务的判分：起官方镜像的容器，换上 Agent 留下的工作目录，拷入测试，跑 test.sh，读 reward。"""
    name = _container_name("tbg", task.instance_id)
    tree = work / task.instance_id
    result: dict[str, Any] = {"task": task.instance_id, "reward": None, "resolved": False}
    try:
        dropped = apply_patch(files, str(rec.get("patch") or ""), tree)
        if dropped:
            result["binary_files_not_carried"] = dropped
        steps: list[tuple[list[str], float]] = [
            (["run", "-d", "--name", name, "--entrypoint", "sleep", task.docker_image, "infinity"], 1800),
            (["exec", name, "sh", "-c", f"rm -rf {task.workdir} && mkdir -p {task.workdir} /logs/verifier /tests"], 120),
            (["cp", f"{tree}/.", f"{name}:{task.workdir}"], 600),
            (["cp", f"{task.path / 'tests'}/.", f"{name}:/tests"], 600),
        ]
        for args, timeout in steps:
            code, _, err = docker(args, timeout_s=timeout)
            if code != 0:
                raise RuntimeError(f"docker {args[0]}：{err.strip()[-300:]}")
        try:
            code, out, err = docker(["exec", "-w", task.workdir, name, "bash", "/tests/test.sh"], timeout_s=task.verifier_timeout_s)
            result["exit_code"] = code
            result["log"] = (str(out) + str(err))[-GRADE_LOG_CHARS:]
        except subprocess.TimeoutExpired:
            result["timed_out"] = True
        code, out, _ = docker(["exec", name, "cat", REWARD_FILE], timeout_s=60)
        reward = _reward(str(out)) if code == 0 else None
        result.update({"reward": reward, "resolved": reward is not None and reward >= 1.0})
    except Exception as exc:  # noqa: BLE001 — 判分出错记下来，算没解决，报告里单列
        result["error"] = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        docker(["rm", "-f", name], timeout_s=120)
        shutil.rmtree(tree, ignore_errors=True)
    return result


def grade(out_dir: Path, tasks_root: Path, *, docker: Docker | None = None, max_workers: int = 4, live: Live | None = None) -> dict[str, Any]:
    """判跑完的任务（state 为 finished），写 grading/<任务>.json 与 grading/summary.json；已经判过的跳过。"""
    out_dir = Path(out_dir)
    run_doc = json.loads((out_dir / "run.json").read_text("utf-8"))
    by_id = {t.instance_id: t for t in load_tasks(tasks_root)}
    records = sb.read_records(out_dir)
    grading = out_dir / "grading"
    grading.mkdir(parents=True, exist_ok=True)
    todo = []
    for iid in run_doc["instance_ids"]:
        rec = records.get(iid) or {}
        if rec.get("state") != "finished" or (grading / f"{iid}.json").exists():
            continue
        prep = prepared(out_dir, by_id[iid]) or {}
        settings = run_doc.get("settings") or {}
        env = environment_files(by_id[iid], workspace_path=str(settings.get("workspace_path") or "/workspace")) if settings.get("environment", "official") == "official" else []
        todo.append((by_id[iid], rec, [*(prep.get("files") or []), *env]))
    runner = docker or docker_cli()
    with tempfile.TemporaryDirectory(prefix="tb-grade-") as tmp, ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        for result in pool.map(lambda item: grade_one(item[0], item[1], item[2], docker=runner, work=Path(tmp)), todo):
            sb._write_text(grading / f"{result['task']}.json", json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    graded = {p.stem: json.loads(p.read_text("utf-8")) for p in grading.glob("*.json") if p.name != "summary.json"}
    resolved = sorted(i for i, g in graded.items() if g.get("resolved"))
    summary = {"graded": len(graded), "resolved_ids": resolved, "errors": sorted(i for i, g in graded.items() if g.get("error"))}
    sb._write_text(grading / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    if live is not None:
        live.score("resolved", float(len(resolved)))
        live.score("resolved_rate", len(resolved) / max(1, len(run_doc["instance_ids"])))
    return summary


# --- report ---


def report(out_dir: Path) -> dict[str, Any]:
    """汇总：选了几个、跑完几个、解决几个（按难度、类别），花费；写 report.json 与 report.md。"""
    out_dir = Path(out_dir)
    run_doc = json.loads((out_dir / "run.json").read_text("utf-8"))
    records = sb.read_records(out_dir)
    grading = out_dir / "grading"
    graded = {p.stem: json.loads(p.read_text("utf-8")) for p in grading.glob("*.json") if p.name != "summary.json"} if grading.is_dir() else {}
    tasks = run_doc.get("tasks") or {}
    rows = []
    for iid in run_doc["instance_ids"]:
        rec = records.get(iid) or {}
        g = graded.get(iid) or {}
        meta = tasks.get(iid) or {}
        rows.append({
            "instance_id": iid, "difficulty": meta.get("difficulty"), "category": meta.get("category"), "state": rec.get("state") or "not_run",
            "status": rec.get("status"), "cost_usd": rec.get("cost_usd"), "rounds": rec.get("rounds"),
            "grading": "resolved" if g.get("resolved") else "error" if g.get("error") else "unresolved" if g else None, "reward": g.get("reward"),
            "environment_used": rec.get("environment_used"),
        })
    selected = len(rows)
    ran = sum(1 for r in rows if r["state"] == "finished")
    resolved = sum(1 for r in rows if r["grading"] == "resolved")
    cost = round(sum(float(r["cost_usd"] or 0) for r in rows), 6)

    def by(key: str) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            k = str(r[key] or "?")
            slot = out.setdefault(k, {"selected": 0, "resolved": 0})
            slot["selected"] += 1
            slot["resolved"] += int(r["grading"] == "resolved")
        return dict(sorted(out.items()))

    rep = {
        "benchmark": "terminal-bench", "run_id": run_doc["run_id"], "model_name": (run_doc.get("settings") or {}).get("model_name"),
        "selected": selected, "ran": ran, "graded": bool(graded), "resolved": resolved if graded else None,
        "resolve_rate": (resolved / selected) if graded and selected else None, "cost_usd": cost, "cost_per_instance_usd": (cost / ran) if ran else None,
        "by_difficulty": by("difficulty"), "by_category": by("category"),
        **sb.environment_summary(records, run_doc["instance_ids"], {r["instance_id"] for r in rows if r["grading"] == "resolved"} if graded else None,
                                 official=(run_doc.get("settings") or {}).get("environment", "official") == "official"),
        "instances": rows,
        "limitations": "Only files under each task's working directory are carried to grading; tasks that need state outside it are scored as unresolved.",
    }
    sb._write_text(out_dir / "report.json", json.dumps(rep, ensure_ascii=False, indent=2) + "\n")
    lines = [
        f"# Terminal-Bench 2.0 · {rep['run_id']}", "",
        f"- 选了 {selected} 个任务，跑完 {ran} 个；" + (f"解决 {resolved} 个（{resolved / selected:.1%}）" if graded and selected else "还没判分"),
        f"- 花费 ${cost:.2f}" + (f"，每个跑完的任务 ${cost / ran:.2f}" if ran else ""),
        *sb.environment_note(rep),
        "- 限制：判分只带回工作目录里的文件，要靠工作目录以外的状态的任务判不出来（分数偏保守）。", "",
        "| 难度 | 选了 | 解决 |", "| --- | --- | --- |",
        *[f"| {k} | {v['selected']} | {v['resolved']} |" for k, v in rep["by_difficulty"].items()],
    ]
    sb._write_text(out_dir / "report.md", "\n".join(lines) + "\n")
    return rep


def fetch(out: Path, *, repo: str = DEFAULT_REPO, ref: str = "", git: str = "git", runner: Callable[..., Any] = subprocess.run) -> str:
    """克隆题库（浅克隆；给了 ref 时检出它），返回检出的提交。已经克隆过的不再克隆。"""
    out = Path(out)
    if not (out / ".git").exists():
        runner([git, "clone", "-q", *([] if ref else ["--depth", "1"]), repo, str(out)], check=True)
    if ref:
        runner([git, "-C", str(out), "checkout", "-q", ref], check=True)
    head = runner([git, "-C", str(out), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    return str(head.stdout).strip()


__all__ = [
    "DEFAULT_REPO", "SUITE", "TBTask", "environment_files", "fetch", "grade", "grade_one", "load_task", "load_tasks", "portable_ids", "prepare",
    "report", "run", "select", "task_message", "text_files", "workspace_id",
]
