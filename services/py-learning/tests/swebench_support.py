"""SWE-bench 适配器测试用的假 Runtime 与题目（TMP 计划 eval-observability-and-runtime-fixes 第 11.10 节）。

FakeRuntime 实现适配器用到的那几个调用（建、删工作区，diff，提交、跟事件、查询、回复、取消 Task），每个 Task 按脚本（Script）走：
脚本里的事件依次推出来，最后推 task_finished；被取消时下一个事件就是取消的 task_finished。可以让推流中途断一次、续读时重推一个、
第一次读事件时崩溃，QueryTask 依次给出累计花费，每推一个事件把假时钟往前拨。
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentruntime_learning.benchmarks import swebench as sb
from agentruntime_learning.benchmarks.client import ApiError, Event

RUN = "run-1"
# 只在判分时用、绝不能出现在给 Runtime 的请求里的内容（每题各有一份，带上题号）。
HIDDEN = ("PATCH-SECRET", "TESTPATCH-SECRET", "F2P_SECRET", "P2P_SECRET")


def instance(iid: str, repo: str, *, image: bool = True) -> dict[str, Any]:
    n = iid.rsplit("-", 1)[-1]
    row: dict[str, Any] = {
        "instance_id": iid,
        "repo": repo,
        "base_commit": hashlib.sha1(iid.encode()).hexdigest(),
        "problem_statement": f"ISSUE-{iid}: add() subtracts instead of adding",
        "hints_text": f"HINT-{iid}",
        "patch": f"diff --git a/x b/x\nPATCH-SECRET-{iid}\n",
        "test_patch": f"diff --git a/t b/t\nTESTPATCH-SECRET-{iid}\n",
        "FAIL_TO_PASS": [f"tests/test_calc.py::test_F2P_SECRET_{n}"],
        "PASS_TO_PASS": [f"tests/test_calc.py::test_P2P_SECRET_{n}"],
        "version": "1.0",
        "environment_setup_commit": "f" * 40,
    }
    if image:
        row["image"] = f"swebench/sweb.eval.x86_64.{iid.replace('__', '_1776_')}:latest"
    return row


ROWS = [
    instance("acme__calc-1", "acme/calc"),
    instance("acme__calc-2", "acme/calc"),
    instance("beta__lib-3", "beta/lib"),
    instance("beta__lib-4", "beta/lib"),
]


def write_instances(path: Path, rows: list[dict[str, Any]] | None = None) -> Path:
    rows = ROWS if rows is None else rows
    if path.suffix == ".json":
        path.write_text(json.dumps(rows), encoding="utf-8", newline="\n")
    else:
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n")
    return path


class Crash(BaseException):
    """模拟适配器进程中途被杀（Ctrl-C 之类，不是 Exception）。"""


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Script:
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    status: str = "TASK_STATUS_DONE"
    final_verdict: str = "VERDICT_DONE"
    rounds: int = 1
    cost_usd: float = 0.1
    # QueryTask 依次给出的累计花费（用完后一直是最后一个）；None 时一直是 cost_usd。
    costs: list[float] | None = None
    diff: str = ""
    files: list[str] = field(default_factory=list)
    truncated: bool = False
    # 第一次推流推到第几个事件就断开。
    drop_after: int | None = None
    # 续读时从 Last-Event-ID 那一个开始重推（推流可能重复）。
    overlap: bool = False
    # 每推一个事件，假时钟往前拨几秒。
    clock_step: float = 0.0
    # 第一次建工作区时报这个错。
    create_error: Exception | None = None
    # 第一次读事件时崩溃（Crash）。
    crash_on_events: bool = False
    # task_finished 带的交付（DeepEval 的评分用，N-12）。
    deliverable: Any = None
    # EV-TB-2：EnvironmentService 给这个工作区列出的沙箱镜像；None 时像没有 EnvironmentService 的老 Runtime（Unimplemented）。
    sandbox_images: list[str] | None = None


def verdict(v: str) -> tuple[str, dict[str, Any]]:
    return "verdict", {"verdict": v, "reason": f"判 {v}"}


def rounds(*verdicts: str) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = [("task_accepted", {})]
    for i, v in enumerate(verdicts, 1):
        out += [("round_started", {"round_seq": i}), ("heartbeat", {}), verdict(v)]
    return out


@dataclass
class _Task:
    task_id: str
    workspace_id: str
    script: Script
    log: list[Event] = field(default_factory=list)
    cursor: int = 0
    cancelled: str = ""
    done: bool = False
    crashed: bool = False
    dropped: bool = False
    queried: int = 0
    last_cost: float = 0.0


class FakeRuntime:
    def __init__(self, scripts: dict[str, Script] | None = None, *, run_id: str = RUN, clock: FakeClock | None = None) -> None:
        # 脚本按题号给，换成工作区 id 查（Task 只知道工作区）。
        self.scripts = {sb.workspace_id(run_id, iid): s for iid, s in (scripts or {}).items()}
        self.default = Script()
        self.clock = clock
        self.lock = threading.Lock()
        self.created: dict[str, dict[str, Any]] = {}
        self.create_calls: list[str] = []
        self.deleted: list[str] = []
        self.diffs: list[tuple[str, str]] = []
        self.submits: list[dict[str, Any]] = []
        self.replies: list[tuple[str, str, str]] = []
        self.cancels: list[tuple[str, str]] = []
        self.event_calls: list[tuple[str, int]] = []
        self.tasks: dict[str, _Task] = {}
        self._create_failed: set[str] = set()
        # ListEnvironments 的调用：（工作区，Session，当时工作区删了没有）。
        self.environment_calls: list[tuple[str, str, bool]] = []

    def script(self, workspace_id: str) -> Script:
        return self.scripts.get(workspace_id, self.default)

    def requests(self) -> str:
        """给 Runtime 的全部请求（建工作区与提交 Task），用来查有没有泄题。"""
        return json.dumps({"created": self.created, "submits": self.submits}, ensure_ascii=False)

    # --- Workspace API ---

    def create_workspace(self, workspace_id: str, *, repo_url: str, ref: str, files: list[dict[str, str]]) -> dict[str, Any]:
        with self.lock:
            self.create_calls.append(workspace_id)
            script = self.script(workspace_id)
            if script.create_error is not None and workspace_id not in self._create_failed:
                self._create_failed.add(workspace_id)
                raise script.create_error
            if workspace_id in self.created and workspace_id not in self.deleted:
                raise ApiError(409, "already_exists", f"工作区 {workspace_id} 已经有了")
            if workspace_id in self.deleted:
                self.deleted.remove(workspace_id)
            self.created[workspace_id] = {"repo_url": repo_url, "ref": ref, "files": files}
            return {"workspace_id": workspace_id, "head": f"head-{workspace_id}", "branch": ""}

    def delete_workspace(self, workspace_id: str) -> None:
        with self.lock:
            self.deleted.append(workspace_id)

    def diff(self, workspace_id: str, *, base: str) -> dict[str, Any]:
        with self.lock:
            self.diffs.append((workspace_id, base))
            s = self.script(workspace_id)
            return {"diff": s.diff, "files": list(s.files), "truncated": s.truncated}

    # --- Task API ---

    def submit(self, *, workspace_id: str, message: str, client_request_id: str) -> dict[str, Any]:
        with self.lock:
            task_id = f"task-{len(self.submits) + 1}"
            self.submits.append({"workspace_id": workspace_id, "message": message, "client_request_id": client_request_id, "task_id": task_id})
            self.tasks[task_id] = _Task(task_id, workspace_id, self.script(workspace_id))
            return {"task_id": task_id, "events_url": f"http://fake/v1/tasks/{task_id}/events", "session_id": f"rs-{task_id}"}

    def environments(self, workspace_id: str, session_id: str) -> list[dict[str, Any]]:
        with self.lock:
            self.environment_calls.append((workspace_id, session_id, workspace_id in self.deleted))
            images = self.script(workspace_id).sandbox_images
            if images is None:
                raise ApiError(404, "unimplemented", "EnvironmentService/ListEnvironments")
            return [{"image": image, "state": "ENVIRONMENT_STATE_DESTROYED"} for image in images]

    def _finish(self, t: _Task) -> Event:
        s = t.script
        if t.cancelled:
            status, final, cost = "TASK_STATUS_CANCELLED", "VERDICT_UNSPECIFIED", max(t.last_cost, 0.0)
        else:
            status, final, cost = s.status, s.final_verdict, s.cost_usd
        t.done = True
        value = {"task_id": t.task_id, "status": status, "final_verdict": final, "rounds": s.rounds,
                 "usage": {"cost_usd_micros": str(round(cost * 1_000_000))}}
        if s.deliverable is not None:
            value["deliverable"] = s.deliverable
        return Event(len(t.log) + 1, "task_finished", value)

    def _next(self, t: _Task) -> Event:
        if t.cancelled or t.cursor >= len(t.script.events):
            ev = self._finish(t)
        else:
            name, value = t.script.events[t.cursor]
            t.cursor += 1
            ev = Event(len(t.log) + 1, name, dict(value))
        t.log.append(ev)
        return ev

    def events(self, task_id: str, events_url: str, *, last_event_id: int = 0) -> Iterator[Event]:
        with self.lock:
            t = self.tasks[task_id]
            self.event_calls.append((task_id, last_event_id))
            if t.script.crash_on_events and not t.crashed:
                t.crashed = True
                raise Crash()
            first = not t.dropped and t.script.drop_after is not None
            start = last_event_id - 1 if (t.script.overlap and last_event_id) else last_event_id
            replay = list(t.log[start:])
        sent = 0
        for ev in replay:
            yield self._tick(t, ev)
            sent += 1
        while True:
            with self.lock:
                if t.done:
                    return
                if first and sent >= (t.script.drop_after or 0):
                    t.dropped = True
                    return
                ev = self._next(t)
            yield self._tick(t, ev)
            sent += 1

    def _tick(self, t: _Task, ev: Event) -> Event:
        if self.clock is not None:
            with self.lock:
                self.clock.now += t.script.clock_step
        return ev

    def query(self, task_id: str) -> dict[str, Any]:
        with self.lock:
            t = self.tasks[task_id]
            s = t.script
            costs = s.costs if s.costs is not None else [s.cost_usd]
            t.last_cost = costs[min(t.queried, len(costs) - 1)]
            t.queried += 1
            status = "TASK_STATUS_RUNNING" if not t.done else (t.log[-1].value["status"])
            return {"status": status, "cost_usd": t.last_cost}

    def reply(self, task_id: str, question_id: str, option: str) -> None:
        with self.lock:
            self.replies.append((task_id, question_id, option))

    def cancel(self, task_id: str, reason: str) -> None:
        with self.lock:
            self.cancels.append((task_id, reason))
            self.tasks[task_id].cancelled = reason


def settings(**kw: Any) -> sb.Settings:
    base: dict[str, Any] = {"run_id": RUN, "budget_usd": 10.0, "task_usd_limit": 1.0, "poll_s": 0.0}
    base.update(kw)
    return sb.Settings(**base)


def records(out: Path) -> dict[str, dict[str, Any]]:
    return {p.stem: json.loads(p.read_text("utf-8")) for p in sorted((out / "records").glob("*.json"))}


def predictions(out: Path) -> list[dict[str, Any]]:
    text = (out / "predictions.jsonl").read_bytes().decode("utf-8")
    assert "\r" not in text, "predictions 用 LF 换行"
    return [json.loads(line) for line in text.splitlines() if line.strip()]
