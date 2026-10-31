"""SWE-bench Verified 的适配器（TMP 计划 eval-observability-and-runtime-fixes 第 10 节第一期，细化见第 11.10 节）。

用 Runtime 跑 SWE-bench 的题：每题建一个停在 base_commit 的工作区（Workspace API），提交一个 Task（Task API），跟着 SSE 走到
结束，把工作区相对建好时的 head 的 diff 当补丁，写成官方判分工具认的 predictions；判分调官方工具（swebench v5，不进本仓库的
依赖，用 --python 指一个另装了它的 Python），报告里并进本项目自己的指标。

- 不泄题：Task 只拿到题目描述（可选 hints_text）与固定的说明；patch、test_patch、FAIL_TO_PASS、PASS_TO_PASS 只留在输出目录
  的 instances.jsonl 里给判分用。工作区只取到 base_commit 为止的历史，看不到上游之后的修复。
- 运行环境（environment = official）：官方镜像把仓库以开发模式装在 /testbed（conda 环境 testbed）。工作区里写一个 devcontainer
  定义，由 D-17 构建：FROM 题目的镜像，PATH 用 testbed 环境，/testbed 换成指向工作区挂载点的符号链接。不换的话测试用的是镜像里
  没改过的代码，而且不报错；带编译扩展的仓库（astropy、matplotlib、scikit-learn）因此要在工作区里重新构建。
- 花钱：开新题之前，已花的加上正在跑的每题按单题上限预留，再加这一题的上限，超过总预算就不再开新题，剩下的记为没跑。
  单题超过花费上限（QueryTask 的用量，收到事件时至多每 poll_s 秒查一次）或时间上限就取消；补丁照取。
- 无人值守：问人时按 reply_for 回，回不了的取消 Task。
- 续跑：输出目录里每题一份记录，提交了就先记下 task_id；再跑时已结束的跳过，记了 task_id 的接着跟（Task 已结束时只读事件、
  不回复），出错的删掉工作区重来。影响结果的设定（PINNED）与选中的题要与 run.json 一致；预算、并发、上限可以改。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import re
import statistics
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from agentruntime_learning.benchmarks.client import ApiError, Event

REQUIRED = ("instance_id", "repo", "base_commit", "problem_statement")
DEFAULT_DATASET = "SWE-bench/SWE-bench_Verified"
ROWS_URL = "https://datasets-server.huggingface.co/rows"
DEVCONTAINER = ".devcontainer/devcontainer.json"
DOCKERFILE = ".devcontainer/agentruntime.Dockerfile"
STOP_OPTION = "stop_and_wrap_up"
APPROVAL_KINDS = ("approval", "tool_approval")
TERMINAL = {"TASK_STATUS_DONE", "TASK_STATUS_ABORTED", "TASK_STATUS_CANCELLED", "TASK_STATUS_FAILED"}
# 续跑时要与 run.json 一致的设定：改了它们，同一个输出目录里的题就不是同一种跑法了。
PINNED = ("model_name", "hints", "environment", "repo_url_template", "workspace_path")
# 推流连续断开、又没有新事件的次数上限。
MAX_RECONNECTS = 5
EPS = 1e-9
_COMMIT = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

MESSAGE = """Resolve the following issue in the repository {repo}. The workspace is a checkout of the repository at the commit where the issue was reported.

<issue>
{problem}
</issue>
{hints}
Change the source code so that the issue is resolved. Hidden tests will be run against your changes, so do not modify the existing tests; you may add and run your own checks while you work.
"""


# Verified 的 difficulty 字段，从易到难。
DIFFICULTIES = ("<15 min fix", "15 min - 1 hour", "1-4 hours", ">4 hours")


class ResumeMismatch(ValueError):
    """输出目录里已有一次运行，这次的选题或影响结果的设定与它不一致。"""


# --- 题目 ---


@dataclass(frozen=True)
class Instance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    hints_text: str = ""
    # 原样的一行（判分要用的字段都在里面）。
    raw: dict[str, Any] = field(default_factory=dict, compare=False, hash=False, repr=False)

    @property
    def difficulty(self) -> str:
        return str(self.raw.get("difficulty") or "")

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Instance:
        for key in REQUIRED:
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError(f"题目缺字段 {key}：{str(row.get('instance_id') or '')[:80]}")
        if not _COMMIT.match(row["base_commit"]):
            raise ValueError(f"{row['instance_id']} 的 base_commit 要是完整的提交（40 位十六进制）：{row['base_commit'][:80]}")
        return cls(
            instance_id=row["instance_id"], repo=row["repo"], base_commit=row["base_commit"], problem_statement=row["problem_statement"],
            hints_text=str(row.get("hints_text") or ""), raw=dict(row),
        )


def load_instances(path: Path) -> list[Instance]:
    """读题：.json 是一个数组，其余按 JSONL。缺字段、重复的 instance_id 报错。"""
    text = Path(path).read_text("utf-8")
    rows = json.loads(text) if Path(path).suffix == ".json" else [json.loads(line) for line in text.splitlines() if line.strip()]
    out: list[Instance] = []
    seen: set[str] = set()
    for row in rows:
        inst = Instance.from_row(row)
        if inst.instance_id in seen:
            raise ValueError(f"重复的 instance_id：{inst.instance_id}")
        seen.add(inst.instance_id)
        out.append(inst)
    return out


def select(instances: Sequence[Instance], *, limit: int | None = None, seed: int = 0, ids: Sequence[str] | None = None) -> list[Instance]:
    """选题：给了 ids 时照给的顺序（没有的 id 报错）；否则按 instance_id 排好，用种子抽 limit 题，再按 instance_id 排。"""
    if ids is not None:
        by_id = {i.instance_id: i for i in instances}
        wanted = list(dict.fromkeys(ids))
        missing = [i for i in wanted if i not in by_id]
        if missing:
            raise ValueError(f"题目里没有：{', '.join(missing)}")
        return [by_id[i] for i in wanted]
    ordered = sorted(instances, key=lambda i: i.instance_id)
    if limit is None or limit >= len(ordered):
        return ordered
    return sorted(random.Random(seed).sample(ordered, limit), key=lambda i: i.instance_id)


def stratified(instances: Sequence[Instance], *, limit: int, seed: int = 0, exclude_repos: Sequence[str] = ()) -> list[Instance]:
    """分层选题：各仓库按题数成比例（最大余额法；名额够时每个仓库至少一题），仓库内按难度排好、等距取，所以难度也成比例。
    同一个种子结果相同，与输入顺序无关。"""
    pool = sorted((i for i in instances if i.repo not in set(exclude_repos)), key=lambda i: i.instance_id)
    if limit >= len(pool):
        return pool
    by_repo: dict[str, list[Instance]] = {}
    for inst in pool:
        by_repo.setdefault(inst.repo, []).append(inst)
    exact = {repo: limit * len(items) / len(pool) for repo, items in by_repo.items()}
    floor = 1 if limit >= len(by_repo) else 0
    quota = {repo: max(floor, int(share)) for repo, share in exact.items()}
    while sum(quota.values()) < limit:
        quota[max(sorted(quota), key=lambda r: exact[r] - quota[r])] += 1
    while sum(quota.values()) > limit:
        quota[max(sorted(r for r in quota if quota[r] > max(floor, 0)), key=lambda r: quota[r] - exact[r])] -= 1
    rng = random.Random(seed)
    rank = {name: n for n, name in enumerate(DIFFICULTIES)}
    picked: list[Instance] = []
    for repo in sorted(by_repo):
        items = by_repo[repo]
        rng.shuffle(items)
        items.sort(key=lambda i: rank.get(i.difficulty, len(rank)))
        count = quota[repo]
        picked += [items[int((k + 0.5) * len(items) / count)] for k in range(count)]
    return sorted(picked, key=lambda i: i.instance_id)


def task_message(inst: Instance, *, hints: bool = False) -> str:
    """给 Task 的消息：只有题目描述（hints 时加上 hints_text）与固定的说明，不带任何判分用的内容。"""
    extra = f"\n<hints>\n{inst.hints_text.strip()}\n</hints>\n" if hints and inst.hints_text.strip() else ""
    return MESSAGE.format(repo=inst.repo, problem=inst.problem_statement.strip(), hints=extra)


def image_of(inst: Instance) -> str:
    """题目的官方镜像：题目带 image 时用它；否则按官方的命名（__ 换成 _1776_，全小写）。"""
    image = inst.raw.get("image")
    if isinstance(image, str) and image.strip():
        return image.strip()
    return f"swebench/sweb.eval.x86_64.{inst.instance_id}:latest".replace("__", "_1776_").lower()


def environment_files(inst: Instance, *, workspace_path: str = "/workspace") -> list[dict[str, str]]:
    """写进工作区的运行环境定义（D-17 识别 .devcontainer/devcontainer.json）。"""
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", workspace_path):
        raise ValueError(f"工作区挂载点要是绝对路径：{workspace_path}")
    devcontainer = {"name": f"SWE-bench {inst.instance_id}", "build": {"dockerfile": DOCKERFILE.rsplit("/", 1)[1], "context": "."}}
    dockerfile = (
        f"FROM {image_of(inst)}\n"
        "ENV PATH=/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:$PATH CONDA_DEFAULT_ENV=testbed\n"
        f"RUN rm -rf /testbed && ln -s {workspace_path} /testbed\n"
    )
    return [{"path": DEVCONTAINER, "content": json.dumps(devcontainer, indent=2) + "\n"}, {"path": DOCKERFILE, "content": dockerfile}]


def workspace_id(run_id: str, instance_id: str) -> str:
    """工作区 id（格式同租户 ID）：题号的小写化加上 run_id 与题号的哈希，同一次运行里各不相同，换 run_id 也不同。"""
    slug = re.sub(r"[^a-z0-9]+", "-", instance_id.lower()).strip("-")[:40].strip("-")
    digest = hashlib.sha256(f"{run_id}\x00{instance_id}".encode()).hexdigest()[:10]
    return f"swb-{slug}-{digest}"


def reply_for(question: Mapping[str, Any]) -> str | None:
    """无人值守时怎么回：能收尾就收尾（stop_and_wrap_up），审批一律 reject；别的问题回不了（None，由调用方取消 Task）。"""
    options = [str(o) for o in question.get("options") or ()]
    if STOP_OPTION in options:
        return STOP_OPTION
    if (question.get("kind") in APPROVAL_KINDS or set(options) == {"approve", "reject"}) and "reject" in options:
        return "reject"
    return None


def fetch_instances(
    out: Path, *, dataset: str = DEFAULT_DATASET, split: str = "test", config: str = "default", rows_url: str = ROWS_URL,
    page_size: int = 100, timeout: float = 60,
) -> int:
    """从 Hugging Face datasets-server 的 rows 接口分页取全部题目，写成 JSONL（LF）。条数对不上、有被截断的格子时报错。"""
    rows: list[dict[str, Any]] = []
    total: int | None = None
    while total is None or len(rows) < total:
        query = urllib.parse.urlencode({"dataset": dataset, "config": config, "split": split, "offset": len(rows), "length": page_size})
        with urllib.request.urlopen(f"{rows_url}?{query}", timeout=timeout) as resp:
            data = json.loads(resp.read())
        total = int(data["num_rows_total"])
        page = data.get("rows") or []
        for item in page:
            if item.get("truncated_cells"):
                raise ValueError(f"第 {item.get('row_idx')} 行有被截断的格子：{', '.join(map(str, item['truncated_cells']))}")
            rows.append(item["row"])
        if not page:
            break
    if len(rows) != total:
        raise ValueError(f"只取到 {len(rows)} 行，接口说一共 {total} 行")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    _write_text(Path(out), "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return len(rows)


# --- 跑 ---


class Runtime(Protocol):
    """适配器用到的 Runtime 调用（client.RuntimeClient；测试里是假的）。"""

    def create_workspace(self, workspace_id: str, *, repo_url: str, ref: str, files: list[dict[str, str]]) -> dict[str, Any]: ...
    def delete_workspace(self, workspace_id: str) -> None: ...
    def diff(self, workspace_id: str, *, base: str) -> dict[str, Any]: ...
    def submit(self, *, workspace_id: str, message: str, client_request_id: str) -> dict[str, Any]: ...
    def events(self, task_id: str, events_url: str, *, last_event_id: int = 0) -> Iterator[Event]: ...
    def query(self, task_id: str) -> dict[str, Any]: ...
    def reply(self, task_id: str, question_id: str, option: str) -> None: ...
    def cancel(self, task_id: str, reason: str) -> None: ...


@dataclass(frozen=True)
class Settings:
    run_id: str
    budget_usd: float
    task_usd_limit: float
    model_name: str = "agent-runtime"
    concurrency: int = 1
    task_timeout_s: float = 3600.0
    hints: bool = False
    # official：写题目的运行环境定义；none：不写，用默认的运行环境（只给冒烟）。
    environment: str = "official"
    repo_url_template: str = "https://github.com/{repo}.git"
    workspace_path: str = "/workspace"
    keep_workspaces: bool = False
    poll_s: float = 30.0
    clock: Callable[[], float] = field(default=time.monotonic, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not _RUN_ID.match(self.run_id):
            raise ValueError(f"run_id 只能用字母、数字与 . _ -（最长 64）：{self.run_id}")
        if self.environment not in ("official", "none"):
            raise ValueError(f"environment 取 official 或 none：{self.environment}")
        if "{repo}" not in self.repo_url_template:
            raise ValueError("repo_url_template 里要有 {repo}")
        if self.budget_usd < 0 or self.task_usd_limit <= 0 or self.concurrency < 1 or self.task_timeout_s <= 0:
            raise ValueError("预算不能为负，单题上限、并发与时间上限要大于 0")

    def pinned(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in PINNED}


@dataclass(frozen=True)
class RunSummary:
    """跑完以后输出目录里的状况（含之前几次运行的）。"""

    finished: int
    not_run: int
    errors: int
    spent_usd: float
    # 这一次提交的 Task 数。
    submitted: int


def run(runtime: Runtime, instances: Sequence[Instance], out_dir: Path, settings: Settings, *, selection: Mapping[str, Any] | None = None) -> RunSummary:
    """跑选中的题，写 run.json、instances.jsonl、records/ 与 predictions.jsonl；同一个输出目录再跑是续跑。"""
    out_dir = Path(out_dir)
    (out_dir / "records").mkdir(parents=True, exist_ok=True)
    _manifest(out_dir, instances, settings, selection)
    _write_text(out_dir / "instances.jsonl", "".join(json.dumps(i.raw, ensure_ascii=False) + "\n" for i in instances))
    runner = _Runner(runtime, list(instances), out_dir, settings)
    try:
        runner.go()
    finally:
        write_predictions(out_dir, instances, settings.model_name)
    return runner.summary()


def _manifest(out_dir: Path, instances: Sequence[Instance], settings: Settings, selection: Mapping[str, Any] | None) -> None:
    path = out_dir / "run.json"
    ids = [i.instance_id for i in instances]
    current = {"run_id": settings.run_id, "instance_ids": ids, **settings.pinned()}
    if path.exists():
        before = json.loads(path.read_text("utf-8"))
        old = {"run_id": before.get("run_id"), "instance_ids": before.get("instance_ids"), **(before.get("settings") or {})}
        for key, value in current.items():
            if old.get(key) != value:
                raise ResumeMismatch(f"{out_dir} 里已有一次运行，{key} 不一致（原来 {str(old.get(key))[:200]}，这次 {str(value)[:200]}）：换一个输出目录")
        doc = before
    else:
        doc = {"benchmark": "swe-bench", "schema_version": 1, "run_id": settings.run_id, "created_at": datetime.now(UTC).isoformat(),
               "instance_ids": ids, "settings": settings.pinned(), "selection": dict(selection or {})}
    doc["limits"] = {"budget_usd": settings.budget_usd, "task_usd_limit": settings.task_usd_limit, "concurrency": settings.concurrency,
                     "task_timeout_s": settings.task_timeout_s}
    _write_text(path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")


def read_records(out_dir: Path) -> dict[str, dict[str, Any]]:
    return {p.stem: json.loads(p.read_text("utf-8")) for p in sorted((Path(out_dir) / "records").glob("*.json"))}


def write_predictions(out_dir: Path, instances: Sequence[Instance], model_name: str) -> None:
    """官方格式：每行 instance_id、model_name_or_path、model_patch；按选题的顺序，只写跑完的题（含补丁为空的）。"""
    recs = read_records(out_dir)
    lines = []
    for inst in instances:
        rec = recs.get(inst.instance_id) or {}
        if rec.get("state") == "finished":
            lines.append(json.dumps({"instance_id": inst.instance_id, "model_name_or_path": model_name, "model_patch": rec.get("patch") or ""}, ensure_ascii=False))
    _write_text(Path(out_dir) / "predictions.jsonl", "".join(line + "\n" for line in lines))


class _Runner:
    def __init__(self, runtime: Runtime, instances: list[Instance], out_dir: Path, settings: Settings) -> None:
        self.rt = runtime
        self.instances = instances
        self.out = out_dir
        self.s = settings
        self.cond = threading.Condition()
        self.records = read_records(out_dir)
        self.reserved: dict[str, float] = {}
        self.stopping = False
        self.submitted = 0

    # 记录

    def _save(self, rec: dict[str, Any]) -> None:
        with self.cond:
            self.records[rec["instance_id"]] = rec
            _write_text(self.out / "records" / f"{rec['instance_id']}.json", json.dumps(rec, ensure_ascii=False, indent=2) + "\n")

    def _spent(self) -> float:
        return sum(float(r.get("cost_usd") or 0) for r in self.records.values() if r.get("state") in ("finished", "error"))

    def summary(self) -> RunSummary:
        states = Counter(str((self.records.get(i.instance_id) or {}).get("state")) for i in self.instances)
        return RunSummary(states["finished"], states["not_run"], states["error"], round(self._spent(), 6), self.submitted)

    # 调度

    def go(self) -> None:
        pending = [i for i in self.instances if (self.records.get(i.instance_id) or {}).get("state") != "finished"]
        # 上次提交了的先接着跟：钱已经在花了，不受预算门槛的约束（照样占一份预留）。
        following = [i for i in pending if (self.records.get(i.instance_id) or {}).get("state") == "submitted"]
        fresh = [i for i in pending if i not in following]
        futures: list[Future[None]] = []
        with ThreadPoolExecutor(max_workers=self.s.concurrency) as pool:
            for n, inst in enumerate([*following, *fresh]):
                gated = inst not in following
                with self.cond:
                    while True:
                        if self.stopping:
                            break
                        room = len(self.reserved) < self.s.concurrency
                        fits = self._spent() + sum(self.reserved.values()) + self.s.task_usd_limit <= self.s.budget_usd + EPS
                        if room and (fits or not gated):
                            self.reserved[inst.instance_id] = self.s.task_usd_limit
                            break
                        if not self.reserved:
                            break
                        self.cond.wait()
                    if self.stopping:
                        break
                    if inst.instance_id not in self.reserved:
                        # 预算不够再开一题：这一题与之后的都记为没跑（续跑时加了预算还会跑）。
                        for rest in [*following, *fresh][n:]:
                            self._save({"instance_id": rest.instance_id, "repo": rest.repo, "state": "not_run"})
                        break
                futures.append(pool.submit(self._one, inst))
        for f in futures:
            f.result()

    def _release(self, iid: str, *, crashed: bool = False) -> None:
        with self.cond:
            self.reserved.pop(iid, None)
            if crashed:
                self.stopping = True
            self.cond.notify_all()

    # 一题

    def _one(self, inst: Instance) -> None:
        iid = inst.instance_id
        rec = dict(self.records.get(iid) or {})
        rec.update({"instance_id": iid, "repo": inst.repo})
        try:
            self._attempt(inst, rec)
        except Exception as err:  # noqa: BLE001 — 这一题出错不拖垮整轮，记下来，续跑时重来
            if rec.get("task_id") and rec.get("state") == "submitted":
                with contextlib.suppress(Exception):
                    self.rt.cancel(rec["task_id"], "adapter_error")
            rec.update({"state": "error", "error": f"{type(err).__name__}: {err}"[:1000]})
            self._save(rec)
            self._release(iid)
        except BaseException:
            self._release(iid, crashed=True)
            raise
        else:
            self._release(iid)

    def _attempt(self, inst: Instance, rec: dict[str, Any]) -> None:
        ws = workspace_id(self.s.run_id, inst.instance_id)
        interactive = True
        if rec.get("state") == "submitted" and rec.get("task_id"):
            interactive = self.rt.query(rec["task_id"])["status"] not in TERMINAL
        else:
            if rec.get("state") in ("created", "error", "submitted"):
                # 上次建了工作区却没跑完：删掉重来（不在了也行）。
                with contextlib.suppress(ApiError):
                    self.rt.delete_workspace(ws)
            for key in ("task_id", "events_url", "error", "status", "final_verdict", "stop_reason", "auto_replies", "patch", "patch_files", "patch_error"):
                rec.pop(key, None)
            files = environment_files(inst, workspace_path=self.s.workspace_path) if self.s.environment == "official" else []
            repo_url = self.s.repo_url_template.format(repo=inst.repo)
            created = self.rt.create_workspace(ws, repo_url=repo_url, ref=inst.base_commit, files=files)
            rec.update({"state": "created", "workspace_id": ws, "head": created["head"]})
            self._save(rec)
            request_id = hashlib.sha256(f"{self.s.run_id}\x00{inst.instance_id}\x00{created['head']}".encode()).hexdigest()[:32]
            sub = self.rt.submit(workspace_id=ws, message=task_message(inst, hints=self.s.hints), client_request_id=request_id)
            rec.update({"state": "submitted", "task_id": sub["task_id"], "events_url": sub["events_url"]})
            self._save(rec)
            with self.cond:
                self.submitted += 1
        finished = self._follow(rec, interactive=interactive)
        usage = finished.get("usage") or {}
        rec.update({
            "status": _status(finished.get("status")),
            "final_verdict": _verdict(finished.get("final_verdict")),
            "rounds": int(finished.get("rounds") or 0),
            "cost_usd": int(usage.get("cost_usd_micros") or 0) / 1_000_000,
        })
        diff = self.rt.diff(ws, base=rec["head"])
        if diff["truncated"]:
            rec.update({"patch": "", "patch_files": list(diff["files"]), "patch_error": "diff_truncated"})
        else:
            rec.update({"patch": diff["diff"], "patch_files": list(diff["files"]), "patch_error": None})
        rec["state"] = "finished"
        self._save(rec)
        if not self.s.keep_workspaces:
            with contextlib.suppress(ApiError):
                self.rt.delete_workspace(ws)

    def _follow(self, rec: dict[str, Any], *, interactive: bool) -> dict[str, Any]:
        """跟着 SSE 走到 task_finished，返回它的载荷；Verdict、自动回复与取消的原因记进 rec。"""
        task_id, url = rec["task_id"], rec["events_url"]
        verdicts: Counter[str] = Counter()
        replies: list[dict[str, Any]] = list(rec.get("auto_replies") or [])
        replied = {r["question_id"] for r in replies}
        stop_reason = rec.get("stop_reason")
        start = self.s.clock()
        last_poll: float | None = None
        last, quiet = 0, 0

        def cancel(reason: str) -> None:
            nonlocal stop_reason
            stop_reason = reason
            self.rt.cancel(task_id, reason)

        while True:
            fresh = False
            for ev in self.rt.events(task_id, url, last_event_id=last):
                if ev.id <= last:
                    continue  # 续读时重推的
                last, fresh = ev.id, True
                if ev.name == "task_finished":
                    rec.update({"verdicts": dict(verdicts), "auto_replies": replies, "stop_reason": stop_reason})
                    return ev.value
                if ev.name == "verdict":
                    verdicts[str(ev.value.get("verdict") or "")] += 1
                if not interactive or stop_reason:
                    continue
                if ev.name == "human_input_required":
                    qid = str(ev.value.get("question_id") or "")
                    if qid and qid not in replied:
                        option = reply_for(ev.value)
                        if option is None:
                            cancel("unattended_question")
                            continue
                        with contextlib.suppress(ApiError):  # 已经不在等这个回复（例如续跑前别人回了）
                            self.rt.reply(task_id, qid, option)
                        replied.add(qid)
                        replies.append({"question_id": qid, "code": str(ev.value.get("question_code") or ""), "option": option})
                now = self.s.clock()
                if now - start > self.s.task_timeout_s:
                    cancel("task_timeout")
                elif last_poll is None or now - last_poll >= self.s.poll_s:
                    last_poll = now
                    if self.rt.query(task_id)["cost_usd"] > self.s.task_usd_limit + EPS:
                        cancel("task_usd_limit")
            quiet = 0 if fresh else quiet + 1
            if quiet > MAX_RECONNECTS:
                raise RuntimeError(f"{task_id} 的推流连续 {quiet} 次断开都没有新事件，读不到 task_finished")


def _status(value: Any) -> str:
    return str(value or "").removeprefix("TASK_STATUS_").lower() or "unknown"


def _verdict(value: Any) -> str | None:
    v = str(value or "").removeprefix("VERDICT_").lower()
    return None if v in ("", "unspecified") else v


# --- 判分 ---


def _harness_report(out_dir: Path) -> Path:
    manifest = json.loads((Path(out_dir) / "run.json").read_text("utf-8"))
    name = str(manifest["settings"]["model_name"]).replace("/", "__")
    return Path(out_dir) / "grading" / f"{name}.{manifest['run_id']}.json"


def grade(out_dir: Path, *, python: str, max_workers: int = 4, modal: bool = False, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """调官方判分工具（python -m swebench.harness.run_evaluation），只判交了 predictions 的题，返回它的报告。"""
    out_dir = Path(out_dir).absolute()
    manifest = json.loads((out_dir / "run.json").read_text("utf-8"))
    preds = [json.loads(line) for line in (out_dir / "predictions.jsonl").read_text("utf-8").splitlines() if line.strip()]
    if not preds:
        raise ValueError(f"{out_dir} 里还没有 predictions：先跑 run")
    grading = out_dir / "grading"
    grading.mkdir(parents=True, exist_ok=True)
    argv = [
        python, "-m", "swebench.harness.run_evaluation",
        "--dataset_name", str(out_dir / "instances.jsonl"), "--split", "test",
        "--predictions_path", str(out_dir / "predictions.jsonl"),
        "--run_id", str(manifest["run_id"]), "--max_workers", str(max_workers), "--report_dir", str(grading),
        *(["--modal", "true"] if modal else []),
        "--instance_ids", *[p["instance_id"] for p in preds],
    ]
    proc = subprocess.run(argv, cwd=grading, env={**os.environ, **(env or {})}, capture_output=True, text=True, encoding="utf-8", errors="replace")
    _write_text(grading / "harness.log", proc.stdout + proc.stderr)
    if proc.returncode != 0:
        tail = (proc.stderr.strip() or proc.stdout.strip())[-2000:]
        raise RuntimeError(f"官方判分工具失败（退出码 {proc.returncode}，日志 {grading / 'harness.log'}）：{tail}")
    path = _harness_report(out_dir)
    if not path.exists():
        raise RuntimeError(f"官方判分工具跑完了，但没有报告 {path}（日志 {grading / 'harness.log'}）")
    return json.loads(path.read_text("utf-8"))


# --- 报告 ---

_LABELS = (
    ("resolved_ids", "resolved"), ("error_ids", "error"), ("infra_failure_ids", "infra_failure"),
    ("ambiguous_failure_ids", "ambiguous_failure"), ("empty_patch_ids", "empty_patch"), ("unresolved_ids", "unresolved"),
)


def report(out_dir: Path) -> dict[str, Any]:
    """汇总这次运行（判过分时并进判分结果），写 report.json 与 report.md。"""
    out_dir = Path(out_dir)
    manifest = json.loads((out_dir / "run.json").read_text("utf-8"))
    rows = {json.loads(line)["instance_id"]: json.loads(line) for line in (out_dir / "instances.jsonl").read_text("utf-8").splitlines() if line.strip()}
    ids: list[str] = list(manifest["instance_ids"])
    recs = read_records(out_dir)
    path = _harness_report(out_dir)
    graded_doc: dict[str, Any] | None = json.loads(path.read_text("utf-8")) if path.exists() else None

    done = [recs[i] for i in ids if (recs.get(i) or {}).get("state") == "finished"]
    errors = sum(1 for i in ids if (recs.get(i) or {}).get("state") == "error")
    cost = round(sum(float((recs.get(i) or {}).get("cost_usd") or 0) for i in ids if (recs.get(i) or {}).get("state") in ("finished", "error")), 6)
    verdicts: Counter[str] = Counter()
    for r in done:
        verdicts.update(r.get("verdicts") or {})
    rounds = [int(r.get("rounds") or 0) for r in done]

    labels: dict[str, str] = {}
    resolved_ids: set[str] = set()
    complete = False
    if graded_doc is not None:
        resolved_ids = set(graded_doc.get("resolved_ids") or []) & set(ids)
        for key, label in _LABELS:
            for iid in graded_doc.get(key) or []:
                labels.setdefault(iid, label)
        submitted = {r["instance_id"] for r in done}
        trouble = set().union(*(set(graded_doc.get(k) or []) for k in ("error_ids", "infra_failure_ids", "ambiguous_failure_ids")))
        complete = not (trouble & submitted) and submitted <= set(labels)
    resolved = len(resolved_ids) if graded_doc is not None else None

    instances = []
    by_repo: dict[str, dict[str, int]] = {}
    by_difficulty: dict[str, dict[str, int]] = {}
    for iid in ids:
        rec = recs.get(iid) or {}
        state = str(rec.get("state") or "not_run")
        repo = str(rows.get(iid, {}).get("repo") or rec.get("repo") or "")
        if state != "finished":
            label = "run_error" if state == "error" else "not_run"
        else:
            label = labels.get(iid, "not_graded")
        difficulty = str(rows.get(iid, {}).get("difficulty") or "unknown")
        for entry in (by_repo.setdefault(repo, {"selected": 0, "ran": 0, "resolved": 0}), by_difficulty.setdefault(difficulty, {"selected": 0, "ran": 0, "resolved": 0})):
            entry["selected"] += 1
            entry["ran"] += state == "finished"
            entry["resolved"] += iid in resolved_ids
        instances.append({
            "instance_id": iid, "repo": repo, "difficulty": difficulty, "state": state, "status": rec.get("status"), "final_verdict": rec.get("final_verdict"),
            "rounds": rec.get("rounds"), "cost_usd": rec.get("cost_usd"), "stop_reason": rec.get("stop_reason"),
            "auto_replies": len(rec.get("auto_replies") or []), "patch_files": rec.get("patch_files") or [], "patch_error": rec.get("patch_error"),
            "error": rec.get("error"), "grading": label,
        })

    selected, ran = len(ids), len(done)
    rep: dict[str, Any] = {
        "benchmark": "swe-bench",
        "run_id": manifest["run_id"],
        "model_name": manifest["settings"]["model_name"],
        "settings": manifest["settings"],
        "selection": manifest.get("selection") or {},
        "limits": manifest.get("limits") or {},
        "selected": selected,
        "ran": ran,
        "not_run": selected - ran - errors,
        "errors": errors,
        "graded": graded_doc is not None,
        "graded_complete": complete,
        "resolved": resolved,
        "resolve_rate": (resolved / selected if selected else None) if resolved is not None else None,
        "resolve_rate_ran": (resolved / ran if ran else None) if resolved is not None else None,
        "cost_usd": cost,
        "cost_per_instance_usd": round(cost / ran, 6) if ran else None,
        "cost_per_resolved_usd": round(cost / resolved, 6) if resolved else None,
        "rounds": {"median": statistics.median(rounds), "mean": round(statistics.fmean(rounds), 3), "max": max(rounds)} if rounds else None,
        "verdicts": dict(verdicts),
        "statuses": dict(Counter(str(r.get("status")) for r in done)),
        "stop_reasons": dict(Counter(str(r["stop_reason"]) for r in done if r.get("stop_reason"))),
        "auto_replies": sum(len(r.get("auto_replies") or []) for r in done),
        "patch_errors": sum(1 for r in done if r.get("patch_error")),
        "by_repo": by_repo,
        "by_difficulty": by_difficulty,
        "instances": instances,
    }
    _write_text(out_dir / "report.json", json.dumps(rep, ensure_ascii=False, indent=2) + "\n")
    _write_text(out_dir / "report.md", _markdown(rep))
    return rep


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def _usd(x: float | None) -> str:
    return "—" if x is None else f"${x:.4f}"


def _markdown(rep: dict[str, Any]) -> str:
    sel = rep["selection"]
    picked = f"种子 {sel['seed']}" if "seed" in sel else ("按 id 选" if sel.get("ids") else "")
    if sel.get("stratified"):
        picked += "，按仓库与难度分层"
    if sel.get("exclude_repos"):
        picked += f"，不含 {'、'.join(sel['exclude_repos'])}"
    lines = [
        f"# SWE-bench 运行报告：{rep['run_id']}",
        "",
        f"- 被测：`{rep['model_name']}`；选中 {rep['selected']} 题{f'（{picked}）' if picked else ''}，跑了 {rep['ran']} 题，没跑 {rep['not_run']} 题，"
        f"适配器出错 {rep['errors']} 题",
    ]
    if rep["graded"]:
        lines.append(f"- 解决率：{_pct(rep['resolve_rate'])}（{rep['resolved']} / {rep['selected']}，÷ 选中的，同榜单口径）；÷ 跑了的 {_pct(rep['resolve_rate_ran'])}")
        if not rep["graded_complete"]:
            lines.append("- ⚠ 判分不完整：有题判分出错、基础设施失败或没判到，见下表的“判分”一列")
    else:
        lines.append("- 解决率：还没判分（先跑 grade）")
    lines += [
        f"- 花费：{_usd(rep['cost_usd'])}；每题 {_usd(rep['cost_per_instance_usd'])}；每解决一题 {_usd(rep['cost_per_resolved_usd'])}",
        f"- Round 数：中位数 {rep['rounds']['median']}，最大 {rep['rounds']['max']}" if rep["rounds"] else "- Round 数：—",
        "- Verdict：" + ("，".join(f"{k} {v}" for k, v in sorted(rep["verdicts"].items())) or "—"),
        "- 结局：" + ("，".join(f"{k} {v}" for k, v in sorted(rep["statuses"].items())) or "—"),
        f"- 自动回复 {rep['auto_replies']} 次；取消：" + ("，".join(f"{k} {v}" for k, v in sorted(rep["stop_reasons"].items())) or "无"),
        "",
        "## 按仓库",
        "",
        "| 仓库 | 选中 | 跑了 | 解决 |",
        "| --- | --- | --- | --- |",
    ]
    for repo, e in sorted(rep["by_repo"].items()):
        lines.append(f"| {repo} | {e['selected']} | {e['ran']} | {e['resolved'] if rep['graded'] else '—'} |")
    lines += ["", "## 按难度", "", "| 难度 | 选中 | 跑了 | 解决 |", "| --- | --- | --- | --- |"]
    order = {name: n for n, name in enumerate(DIFFICULTIES)}
    for difficulty, e in sorted(rep["by_difficulty"].items(), key=lambda kv: order.get(kv[0], len(order))):
        lines.append(f"| {difficulty} | {e['selected']} | {e['ran']} | {e['resolved'] if rep['graded'] else '—'} |")
    lines += ["", "## 每题", "", "| 题 | 结局 | 最终 Verdict | Round | 花费 | 取消原因 | 判分 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for i in rep["instances"]:
        lines.append(
            f"| {i['instance_id']} | {i['status'] or i['state']} | {i['final_verdict'] or '—'} | {i['rounds'] if i['rounds'] is not None else '—'} | "
            f"{_usd(i['cost_usd'])} | {i['stop_reason'] or ''} | {i['grading']} |"
        )
    return "\n".join(lines) + "\n"


def _write_text(path: Path, text: str) -> None:
    """原子地写（先写临时文件再换名），LF 换行。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
