"""TMP 计划 eval-observability-and-runtime-fixes 第 10 节第一期（细化见第 11.10 节）：SWE-bench Verified 的适配器。

对着假的 Runtime（swebench_support.FakeRuntime）与假的官方判分工具测，不联网、不花钱：

1. 读题与选题：.jsonl 与 .json 都收；缺字段、重复 id 报错；按种子抽的子集可复现、按 id 排；按 id 选时照给的顺序，没有的 id 报错。
2. 不泄题：Task 的消息只有题目描述（--hints 时加上 hints_text）；给 Runtime 的全部请求里没有 patch、test_patch、FAIL_TO_PASS、PASS_TO_PASS。
3. 运行环境：镜像名同官方（__ 换成 _1776_，小写；题目带 image 时用它）；写进工作区的定义过得了 D-17 的识别与静态检查，
   /testbed 指向工作区，PATH 用 conda 环境 testbed；--environment none 不写。
4. 工作区 id 合 Workspace API 的格式，同一次运行里各不相同，换 run_id 也不同。
5. 跑：工作区建在 base_commit（地址按模板），补丁取 GetDiff(base=建好时的 head)；predictions 按选题的顺序、只有官方的三个字段、LF；
   每题的记录有结局、最终 Verdict、Round 数、花费、Verdict 分布；跑完删工作区（--keep-workspaces 时留着）。
6. 总预算：开新题之前按单题上限给正在跑的预留，超了就不再开，剩下的记为没跑；预算不够一题时一题也不跑。
7. 单题超过花费上限或时间上限就取消，记下原因，补丁照取。
8. 无人值守：选项里有 stop_and_wrap_up 回它，审批回 reject，别的问题取消 Task。
9. 推流断了凭 Last-Event-ID 续读，重推的事件不重复计。
10. 续跑：已结束的跳过，记了 task_id 的接着跟、不重新提交，出错的重来；选题或影响结果的设定不一致时拒绝，只加预算可以。
11. diff 被截断时记错、补丁为空。
12. 判分：调官方工具（假的）的参数，读它的报告；工具失败时报错并带上它的输出。
13. 报告：解决率（÷ 选中的、÷ 跑了的）、花费、Round 数、Verdict 与结局分布、按仓库、判分不完整的标记。
"""

from __future__ import annotations

import json
import re
from collections import Counter
import sys
from pathlib import Path

import pytest

from agentruntime_learning.benchmarks import swebench as sb
from agentruntime_learning.benchmarks.client import ApiError
from agentruntime_worker.reviewer.rules import HUMAN_REPLIES
from agentruntime_worker.sandbox.environments import RegistryPath, check_dockerfile, detect

from swebench_support import (
    HIDDEN,
    ROWS,
    RUN,
    Crash,
    FakeClock,
    FakeRuntime,
    Script,
    instance,
    predictions,
    records,
    rounds,
    settings,
    write_instances,
)

SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


@pytest.fixture
def insts(tmp_path: Path) -> list[sb.Instance]:
    return sb.load_instances(write_instances(tmp_path / "instances.jsonl"))


# --- 1. 读题与选题 ---


def test_instances_load_from_jsonl_or_json(tmp_path: Path) -> None:
    a = sb.load_instances(write_instances(tmp_path / "a.jsonl"))
    b = sb.load_instances(write_instances(tmp_path / "b.json"))
    assert [i.instance_id for i in a] == [i.instance_id for i in b] == [r["instance_id"] for r in ROWS]
    first = a[0]
    assert (first.repo, first.base_commit) == ("acme/calc", ROWS[0]["base_commit"])
    assert first.problem_statement.startswith("ISSUE-acme__calc-1") and first.hints_text == "HINT-acme__calc-1"
    assert first.raw["test_patch"] == ROWS[0]["test_patch"], "原样留着判分要用的字段"


@pytest.mark.parametrize("field", ["instance_id", "repo", "base_commit", "problem_statement"])
def test_a_missing_field_is_an_error(tmp_path: Path, field: str) -> None:
    row = dict(ROWS[0])
    del row[field]
    with pytest.raises(ValueError, match=field):
        sb.load_instances(write_instances(tmp_path / "bad.jsonl", [row]))


def test_duplicate_ids_are_an_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="acme__calc-1"):
        sb.load_instances(write_instances(tmp_path / "dup.jsonl", [ROWS[0], ROWS[0]]))


def test_selection_is_reproducible(insts: list[sb.Instance]) -> None:
    picked = [i.instance_id for i in sb.select(insts, limit=2, seed=7)]
    assert picked == [i.instance_id for i in sb.select(list(reversed(insts)), limit=2, seed=7)], "同一个种子、与输入顺序无关"
    assert picked == sorted(picked) and len(set(picked)) == 2
    assert [i.instance_id for i in sb.select(insts, limit=10, seed=7)] == sorted(r["instance_id"] for r in ROWS), "上限比题多时全选"
    assert [i.instance_id for i in sb.select(insts, ids=["beta__lib-4", "acme__calc-1"])] == ["beta__lib-4", "acme__calc-1"]
    with pytest.raises(ValueError, match="nope__x-9"):
        sb.select(insts, ids=["nope__x-9"])


def many(repos: dict[str, int]) -> list[sb.Instance]:
    """按仓库造题；难度依次轮流。"""
    out = []
    for repo, count in repos.items():
        for n in range(count):
            row = instance(f"{repo.replace('/', '__')}-{n}", repo)
            row["difficulty"] = sb.DIFFICULTIES[n % len(sb.DIFFICULTIES)]
            out.append(sb.Instance.from_row(row))
    return out


def test_stratified_selection_follows_the_repositories() -> None:
    pool = many({"big/one": 230, "mid/two": 75, "mid/three": 44, "small/four": 2, "small/five": 1})
    picked = sb.stratified(pool, limit=50, seed=0)
    counts = Counter(i.repo for i in picked)
    assert sum(counts.values()) == 50 and len({i.instance_id for i in picked}) == 50
    assert counts["big/one"] > counts["mid/two"] > counts["mid/three"] > counts["small/four"], "按题数成比例"
    assert counts["small/four"] >= 1 and counts["small/five"] >= 1, "题少的仓库也要有，否则按仓库看时没有数据"


def test_stratified_selection_spreads_the_difficulties() -> None:
    picked = sb.stratified(many({"big/one": 400}), limit=40, seed=0)
    assert Counter(i.difficulty for i in picked) == {d: 10 for d in sb.DIFFICULTIES}


def test_stratified_selection_is_reproducible() -> None:
    pool = many({"big/one": 100, "mid/two": 30})
    picked = [i.instance_id for i in sb.stratified(pool, limit=20, seed=3)]
    assert picked == [i.instance_id for i in sb.stratified(list(reversed(pool)), limit=20, seed=3)], "同一个种子、与输入顺序无关"
    assert picked == sorted(picked)
    assert picked != [i.instance_id for i in sb.stratified(pool, limit=20, seed=4)], "换种子换题"


def test_stratified_selection_can_leave_a_repository_out() -> None:
    """Verified 里 psf/requests 的题判分要连外网，随时间不可靠（计划第 11.10 节），基线不选它们。"""
    picked = sb.stratified(many({"big/one": 100, "psf/requests": 8}), limit=20, seed=0, exclude_repos=("psf/requests",))
    assert {i.repo for i in picked} == {"big/one"} and len(picked) == 20


def test_stratified_selection_with_few_slots_takes_the_largest_repositories() -> None:
    picked = sb.stratified(many({"big/one": 200, "mid/two": 90, "mid/three": 40, "small/four": 2, "small/five": 1}), limit=3, seed=0)
    assert len(picked) == 3 and {i.repo for i in picked} <= {"big/one", "mid/two", "mid/three"}


def test_asking_for_more_than_there_are_takes_them_all() -> None:
    pool = many({"big/one": 3, "psf/requests": 2})
    assert len(sb.stratified(pool, limit=10, seed=0, exclude_repos=("psf/requests",))) == 3


def test_stratified_selection_can_leave_out_problems_already_used() -> None:
    """验证用的题不能和调过的题重复：调优只看基线那 50 题，验证要用没见过的。"""
    pool = many({"big/one": 100, "mid/two": 40})
    used = {i.instance_id for i in sb.stratified(pool, limit=20, seed=0)}
    fresh = sb.stratified(pool, limit=20, seed=0, exclude_ids=used)
    assert len(fresh) == 20 and not used & {i.instance_id for i in fresh}


# --- 2. 不泄题 ---


def test_the_task_message_carries_only_the_issue(insts: list[sb.Instance]) -> None:
    inst = insts[0]
    message = sb.task_message(inst)
    assert inst.problem_statement in message and "acme/calc" in message
    assert inst.hints_text not in message, "默认不给 hints"
    assert inst.hints_text in sb.task_message(inst, hints=True)
    for hidden in HIDDEN:
        assert hidden not in message and hidden not in sb.task_message(inst, hints=True)


def test_no_request_to_the_runtime_carries_the_answer(tmp_path: Path, insts: list[sb.Instance]) -> None:
    fake = FakeRuntime()
    sb.run(fake, insts, tmp_path / "out", settings(hints=True))
    sent = fake.requests()
    assert "ISSUE-acme__calc-1" in sent
    for hidden in HIDDEN:
        assert hidden not in sent, f"{hidden} 出现在给 Runtime 的请求里"


# --- 3. 运行环境 ---


def test_image_names_follow_the_official_naming() -> None:
    with_image = sb.Instance.from_row(instance("acme__calc-1", "acme/calc"))
    assert sb.image_of(with_image) == "swebench/sweb.eval.x86_64.acme_1776_calc-1:latest"
    plain = sb.Instance.from_row({**instance("django__django-11099", "django/django", image=False)})
    assert sb.image_of(plain) == "swebench/sweb.eval.x86_64.django_1776_django-11099:latest"
    upper = sb.Instance.from_row({**instance("Foo__Bar-1", "foo/bar", image=False)})
    assert sb.image_of(upper) == "swebench/sweb.eval.x86_64.foo_1776_bar-1:latest"


async def test_the_environment_definition_passes_d17(insts: list[sb.Instance]) -> None:
    inst = insts[0]
    files = sb.environment_files(inst)
    assert [f["path"] for f in files] == [".devcontainer/devcontainer.json", ".devcontainer/agentruntime.Dockerfile"]
    by_path = {f["path"]: f["content"].encode() for f in files}

    async def read(path: str) -> bytes | None:
        return by_path.get(path)

    definition = await detect(read, base_image="ghcr.io/agentruntime/sandbox-universal:1")
    assert definition is not None and definition.kind == "devcontainer" and definition.problem == "", definition
    dockerfile = definition.dockerfile
    assert dockerfile.splitlines()[0] == f"FROM {sb.image_of(inst)}"
    assert "ln -s /workspace /testbed" in dockerfile, "官方镜像把仓库装在 /testbed：换成指向工作区"
    assert "/opt/miniconda3/envs/testbed/bin" in dockerfile, "PATH 用 conda 环境 testbed"
    assert check_dockerfile(dockerfile, tenant_id="bench", registry=RegistryPath(host="registry.example", prefix="ar")) == []
    other = {f["path"]: f["content"] for f in sb.environment_files(inst, workspace_path="/work")}
    assert "ln -s /work /testbed" in other[".devcontainer/agentruntime.Dockerfile"]


def test_workspace_ids(insts: list[sb.Instance]) -> None:
    ids = [sb.workspace_id(RUN, i.instance_id) for i in insts]
    assert all(SLUG.match(w) for w in ids), ids
    assert len(set(ids)) == len(ids)
    assert ids == [sb.workspace_id(RUN, i.instance_id) for i in insts], "确定的"
    assert sb.workspace_id("run-2", insts[0].instance_id) != ids[0], "换一个 run_id 不撞上留着的工作区"
    long_id = "scikit-learn__scikit-learn-" + "9" * 60
    assert SLUG.match(sb.workspace_id(RUN, long_id))


# --- 5. 跑 ---


def test_a_run_writes_predictions_and_records(tmp_path: Path, insts: list[sb.Instance]) -> None:
    chosen = sb.select(insts, ids=["beta__lib-3", "acme__calc-1", "acme__calc-2"])
    fake = FakeRuntime({
        "beta__lib-3": Script(events=rounds("continue", "done"), rounds=2, cost_usd=0.25, diff="diff --git a/lib.py b/lib.py\n+fix 3\n", files=["lib.py"]),
        "acme__calc-1": Script(events=rounds("done"), cost_usd=0.1, diff="diff --git a/calc.py b/calc.py\n+fix 1\n", files=["calc.py"]),
        "acme__calc-2": Script(events=rounds("abort"), status="TASK_STATUS_ABORTED", final_verdict="VERDICT_ABORT", cost_usd=0.2),
    })
    out = tmp_path / "out"
    summary = sb.run(fake, chosen, out, settings(model_name="acme/agent"))
    assert (summary.finished, summary.not_run, summary.errors) == (3, 0, 0)
    assert summary.spent_usd == pytest.approx(0.55)

    preds = predictions(out)
    assert [p["instance_id"] for p in preds] == ["beta__lib-3", "acme__calc-1", "acme__calc-2"], "按选题的顺序"
    assert all(set(p) == {"instance_id", "model_name_or_path", "model_patch"} for p in preds)
    assert {p["model_name_or_path"] for p in preds} == {"acme/agent"}
    assert preds[0]["model_patch"] == "diff --git a/lib.py b/lib.py\n+fix 3\n" and preds[2]["model_patch"] == "", "abort 的也交，补丁为空"

    for inst in chosen:
        ws = sb.workspace_id(RUN, inst.instance_id)
        created = fake.created[ws]
        assert created["ref"] == inst.base_commit, "工作区建在 base_commit"
        assert created["repo_url"] == f"https://github.com/{inst.repo}.git"
        assert created["files"] == sb.environment_files(inst)
        assert (ws, f"head-{ws}") in fake.diffs, "补丁相对建好时的 head：不含写进去的运行环境定义"
        assert ws in fake.deleted, "跑完删工作区"

    recs = records(out)
    beta = recs["beta__lib-3"]
    assert beta["state"] == "finished" and beta["status"] == "done" and beta["final_verdict"] == "done"
    assert (beta["rounds"], beta["cost_usd"], beta["verdicts"]) == (2, 0.25, {"continue": 1, "done": 1})
    assert beta["task_id"] and beta["workspace_id"] == sb.workspace_id(RUN, "beta__lib-3") and beta["patch_files"] == ["lib.py"]
    assert recs["acme__calc-2"]["status"] == "aborted" and recs["acme__calc-2"]["final_verdict"] == "abort"

    manifest = json.loads((out / "run.json").read_text("utf-8"))
    assert manifest["run_id"] == RUN and manifest["instance_ids"] == ["beta__lib-3", "acme__calc-1", "acme__calc-2"]
    graded = [json.loads(line) for line in (out / "instances.jsonl").read_text("utf-8").splitlines()]
    assert [g["instance_id"] for g in graded] == manifest["instance_ids"] and graded[0]["test_patch"], "判分用的题原样留着"


def test_environment_none_and_kept_workspaces(tmp_path: Path, insts: list[sb.Instance]) -> None:
    fake = FakeRuntime()
    sb.run(fake, insts[:1], tmp_path / "out", settings(environment="none", keep_workspaces=True, repo_url_template="file:///mirror/{repo}.git"))
    ws = sb.workspace_id(RUN, insts[0].instance_id)
    assert fake.created[ws]["files"] == [] and fake.created[ws]["repo_url"] == "file:///mirror/acme/calc.git"
    assert fake.deleted == [], "--keep-workspaces 时留着"


# --- 6. 总预算 ---


def test_the_total_budget_reserves_running_tasks(tmp_path: Path, insts: list[sb.Instance]) -> None:
    fake = FakeRuntime({i.instance_id: Script(cost_usd=0.4) for i in insts})
    summary = sb.run(fake, insts, tmp_path / "out", settings(budget_usd=1.0, task_usd_limit=0.5, concurrency=2))
    assert len(fake.submits) == 2, "两题在跑时已经预留满了；任一题结束后 0.4 + 0.5 + 0.5 也超了"
    assert (summary.finished, summary.not_run) == (2, 2) and summary.spent_usd == pytest.approx(0.8)
    states = sorted(r["state"] for r in records(tmp_path / "out").values())
    assert states == ["finished", "finished", "not_run", "not_run"]
    assert len(predictions(tmp_path / "out")) == 2, "没跑的不写 predictions"


def test_a_budget_below_one_task_runs_nothing(tmp_path: Path, insts: list[sb.Instance]) -> None:
    fake = FakeRuntime()
    summary = sb.run(fake, insts, tmp_path / "out", settings(budget_usd=0.3, task_usd_limit=0.5))
    assert fake.submits == [] and fake.created == {} and summary.not_run == len(insts)


# --- 7. 单题上限 ---


def test_a_task_over_its_cost_limit_is_cancelled(tmp_path: Path, insts: list[sb.Instance]) -> None:
    beats = [("heartbeat", {})] * 6
    fake = FakeRuntime({"acme__calc-1": Script(events=beats, costs=[0.1, 0.3, 0.6, 0.9], diff="diff --git a/x b/x\n+partial\n", files=["x"])})
    sb.run(fake, insts[:1], tmp_path / "out", settings(task_usd_limit=0.5))
    assert fake.cancels == [("task-1", "task_usd_limit")]
    rec = records(tmp_path / "out")["acme__calc-1"]
    assert rec["status"] == "cancelled" and rec["stop_reason"] == "task_usd_limit"
    assert rec["cost_usd"] == pytest.approx(0.6)
    assert predictions(tmp_path / "out")[0]["model_patch"] == "diff --git a/x b/x\n+partial\n", "取消了也交工作区里已有的改动"


def test_a_task_over_its_time_limit_is_cancelled(tmp_path: Path, insts: list[sb.Instance]) -> None:
    clock = FakeClock()
    fake = FakeRuntime({"acme__calc-1": Script(events=[("heartbeat", {})] * 6, clock_step=600)}, clock=clock)
    sb.run(fake, insts[:1], tmp_path / "out", settings(task_timeout_s=1000, clock=clock))
    assert fake.cancels == [("task-1", "task_timeout")]
    assert records(tmp_path / "out")["acme__calc-1"]["stop_reason"] == "task_timeout"


# --- 8. 无人值守 ---


def test_reply_rules() -> None:
    assert sb.reply_for({"kind": "human", "options": list(HUMAN_REPLIES), "question_code": "budget_exhausted"}) == "stop_and_wrap_up"
    assert sb.reply_for({"kind": "approval", "options": ["approve", "reject"]}) == "reject"
    assert sb.reply_for({"kind": "tool_approval", "options": ["approve", "reject"]}) == "reject"
    assert sb.reply_for({"kind": "human", "options": ["staging", "production"]}) is None


def test_questions_are_answered_without_a_human(tmp_path: Path, insts: list[sb.Instance]) -> None:
    budget_q = ("human_input_required", {"question_id": "q-budget", "kind": "human", "options": list(HUMAN_REPLIES), "question_code": "budget_exhausted"})
    approval_q = ("human_input_required", {"question_id": "g-1", "kind": "tool_approval", "options": ["approve", "reject"]})
    open_q = ("human_input_required", {"question_id": "q-env", "kind": "human", "options": ["staging", "production"]})
    fake = FakeRuntime({
        "acme__calc-1": Script(events=[*rounds("continue"), budget_q, approval_q, *rounds("done")]),
        "acme__calc-2": Script(events=[*rounds("continue"), open_q, ("heartbeat", {})]),
    })
    sb.run(fake, insts[:2], tmp_path / "out", settings())
    assert ("task-1", "q-budget", "stop_and_wrap_up") in fake.replies and ("task-1", "g-1", "reject") in fake.replies
    assert fake.cancels == [("task-2", "unattended_question")]
    recs = records(tmp_path / "out")
    assert recs["acme__calc-1"]["auto_replies"] == [
        {"question_id": "q-budget", "code": "budget_exhausted", "option": "stop_and_wrap_up"},
        {"question_id": "g-1", "code": "", "option": "reject"},
    ]
    assert recs["acme__calc-2"]["stop_reason"] == "unattended_question"


# --- 9. 断线续读 ---


@pytest.mark.parametrize("overlap", [False, True])
def test_a_dropped_stream_is_resumed_without_double_counting(tmp_path: Path, insts: list[sb.Instance], overlap: bool) -> None:
    fake = FakeRuntime({"acme__calc-1": Script(events=rounds("continue", "done"), rounds=2, drop_after=4, overlap=overlap)})
    sb.run(fake, insts[:1], tmp_path / "out", settings())
    assert fake.event_calls == [("task-1", 0), ("task-1", 4)], "第二次带 Last-Event-ID"
    rec = records(tmp_path / "out")["acme__calc-1"]
    assert rec["verdicts"] == {"continue": 1, "done": 1} and rec["state"] == "finished"


# --- 10. 续跑 ---


def test_resuming_does_not_resubmit_or_respend(tmp_path: Path, insts: list[sb.Instance]) -> None:
    out = tmp_path / "out"
    chosen = insts[:3]
    fake = FakeRuntime({
        "acme__calc-1": Script(cost_usd=0.3),
        "acme__calc-2": Script(events=rounds("done"), cost_usd=0.3, crash_on_events=True),
        "beta__lib-3": Script(cost_usd=0.3),
    })
    with pytest.raises(Crash):
        sb.run(fake, chosen, out, settings(budget_usd=1.0, task_usd_limit=0.35))
    recs = records(out)
    assert recs["acme__calc-1"]["state"] == "finished"
    assert recs["acme__calc-2"]["state"] == "submitted" and recs["acme__calc-2"]["task_id"] == "task-2", "提交了就先记下 task_id"

    summary = sb.run(fake, chosen, out, settings(budget_usd=1.0, task_usd_limit=0.35))
    assert [s["workspace_id"] for s in fake.submits].count(sb.workspace_id(RUN, "acme__calc-2")) == 1, "接着跟，不重新提交"
    assert len(fake.submits) == 3 and fake.create_calls.count(sb.workspace_id(RUN, "acme__calc-1")) == 1, "已结束的跳过"
    assert summary.spent_usd == pytest.approx(0.9) and summary.finished == 3, "已花的从记录里算"
    assert [p["instance_id"] for p in predictions(out)] == [i.instance_id for i in chosen]


def test_resuming_a_finished_task_does_not_reply_again(tmp_path: Path, insts: list[sb.Instance]) -> None:
    out = tmp_path / "out"
    q = ("human_input_required", {"question_id": "q-budget", "kind": "human", "options": list(HUMAN_REPLIES)})
    fake = FakeRuntime({"acme__calc-1": Script(events=[q, *rounds("done")], crash_on_events=True)})
    with pytest.raises(Crash):
        sb.run(fake, insts[:1], out, settings())
    # 进程不在的时候 Task 自己跑完了（问题由别人回了）。
    for _ in fake.events("task-1", ""):
        pass
    fake.event_calls.clear()
    sb.run(fake, insts[:1], out, settings())
    assert fake.replies == [] and fake.cancels == [], "Task 已结束：只读事件算指标，不回复也不取消"
    assert records(out)["acme__calc-1"]["state"] == "finished"


def test_an_instance_that_failed_to_start_is_retried(tmp_path: Path, insts: list[sb.Instance]) -> None:
    out = tmp_path / "out"
    fake = FakeRuntime({"acme__calc-1": Script(create_error=ApiError(503, "unavailable", "克隆超时"))})
    summary = sb.run(fake, insts[:1], out, settings())
    assert summary.errors == 1 and records(out)["acme__calc-1"]["state"] == "error"
    assert "克隆超时" in records(out)["acme__calc-1"]["error"]
    sb.run(fake, insts[:1], out, settings())
    assert records(out)["acme__calc-1"]["state"] == "finished" and len(fake.submits) == 1


def test_resuming_with_different_settings_is_refused(tmp_path: Path, insts: list[sb.Instance]) -> None:
    out = tmp_path / "out"
    fake = FakeRuntime({i.instance_id: Script(cost_usd=0.4) for i in insts})
    sb.run(fake, insts, out, settings(budget_usd=1.0, task_usd_limit=0.5))
    with pytest.raises(sb.ResumeMismatch, match="hints"):
        sb.run(fake, insts, out, settings(budget_usd=1.0, task_usd_limit=0.5, hints=True))
    with pytest.raises(sb.ResumeMismatch, match="instance_ids"):
        sb.run(fake, insts[:2], out, settings(budget_usd=1.0, task_usd_limit=0.5))
    summary = sb.run(fake, insts, out, settings(budget_usd=2.0, task_usd_limit=0.5))
    assert summary.finished == 4 and summary.not_run == 0, "只加预算可以接着跑没跑的题"


# --- 11. 补丁取不全 ---


QUOTA = ApiError(403, "permission_denied", "同时最多 4 个沙箱环境，先等前面的结束")


class Flaky(FakeRuntime):
    """前几次建工作区或取 diff 按给的错误失败。"""

    def __init__(self, *args: Any, create_errors: list[Exception] | None = None, diff_errors: list[Exception] | None = None,
                 submit_errors: list[Exception] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.create_errors = list(create_errors or [])
        self.diff_errors = list(diff_errors or [])
        self.submit_errors = list(submit_errors or [])

    def submit(self, **kwargs: Any) -> dict[str, Any]:
        if self.submit_errors:
            raise self.submit_errors.pop(0)
        return super().submit(**kwargs)

    def create_workspace(self, workspace_id: str, **kwargs: Any) -> dict[str, Any]:
        if self.create_errors:
            raise self.create_errors.pop(0)
        return super().create_workspace(workspace_id, **kwargs)

    def diff(self, workspace_id: str, **kwargs: Any) -> dict[str, Any]:
        if self.diff_errors:
            raise self.diff_errors.pop(0)
        return super().diff(workspace_id, **kwargs)


def test_a_full_sandbox_quota_is_waited_out(tmp_path: Path, insts: list[sb.Instance]) -> None:
    """配额满只说明前面的环境还没回收（做完的 Task 的环境留着等复用），等一等就有名额，不算这一题出错。两层配额的说法不同，都要认。"""
    waits: list[float] = []
    fake = Flaky(create_errors=[QUOTA, ApiError(403, "permission_denied", "同时最多 4 个沙箱，先等前面的结束")])
    sb.run(fake, insts[:1], tmp_path / "out", settings(budget_usd=10, task_usd_limit=2, sleep=waits.append))
    [rec] = records(tmp_path / "out").values()
    assert rec["state"] == "finished" and "error" not in rec
    assert len(waits) == 2 and all(w > 0 for w in waits)


def test_a_finished_task_keeps_its_patch_when_the_quota_is_full(tmp_path: Path, insts: list[sb.Instance]) -> None:
    fake = Flaky(diff_errors=[QUOTA], scripts={insts[0].instance_id: Script(events=rounds("done"), rounds=1, cost_usd=0.3, diff="diff --git a/a b/a\n+1\n", files=["a"])})
    sb.run(fake, insts[:1], tmp_path / "out", settings(budget_usd=10, task_usd_limit=2, sleep=lambda _: None))
    [rec] = records(tmp_path / "out").values()
    assert rec["state"] == "finished" and rec["patch"].startswith("diff --git") and len(fake.submits) == 1


def test_waiting_for_the_quota_gives_up_eventually(tmp_path: Path, insts: list[sb.Instance]) -> None:
    fake = Flaky(create_errors=[QUOTA] * 1000)
    sb.run(fake, insts[:1], tmp_path / "out", settings(budget_usd=10, task_usd_limit=2, sleep=lambda _: None, quota_wait_tries=3))
    [rec] = records(tmp_path / "out").values()
    assert rec["state"] == "error" and "沙箱环境" in rec["error"]


def test_a_busy_tenant_is_waited_out(tmp_path: Path, insts: list[sb.Instance]) -> None:
    """租户的未完成 Task 到了上限（task-service 的准入），等前面的做完再交，不算这一题出错，也不另建工作区。"""
    waits: list[float] = []
    busy = ApiError(403, "permission_denied", "本租户已有 2 个未完成的 Task")
    fake = Flaky(submit_errors=[busy, busy])
    sb.run(fake, insts[:1], tmp_path / "out", settings(budget_usd=10, task_usd_limit=2, sleep=waits.append))
    [rec] = records(tmp_path / "out").values()
    assert rec["state"] == "finished" and "error" not in rec
    assert len(waits) == 2 and len(fake.submits) == 1 and len(fake.create_calls) == 1


def test_a_full_workspace_quota_is_waited_out(tmp_path: Path, insts: list[sb.Instance]) -> None:
    waits: list[float] = []
    fake = Flaky(create_errors=[ApiError(403, "permission_denied", "同时最多 20 个工作区，先删掉不用的")])
    sb.run(fake, insts[:1], tmp_path / "out", settings(budget_usd=10, task_usd_limit=2, sleep=waits.append))
    [rec] = records(tmp_path / "out").values()
    assert rec["state"] == "finished" and len(waits) == 1


def test_a_failed_submit_does_not_leave_its_workspace_behind(tmp_path: Path, insts: list[sb.Instance]) -> None:
    """建了工作区却没交上 Task：删掉它。不删的话出错的题越多占的工作区越多，直到租户的工作区配额满（2026-11-03 t18-rev19）。"""
    ws = sb.workspace_id("run-1", insts[0].instance_id)
    fake = Flaky(submit_errors=[ApiError(400, "invalid_argument", "消息太长")])
    sb.run(fake, insts[:1], tmp_path / "out", settings(budget_usd=10, task_usd_limit=2, sleep=lambda _: None, run_id="run-1"))
    [rec] = records(tmp_path / "out").values()
    assert rec["state"] == "error" and fake.deleted.count(ws) >= 1 and not fake.submits


def test_other_errors_are_not_waited_on(tmp_path: Path, insts: list[sb.Instance]) -> None:
    waits: list[float] = []
    fake = Flaky(create_errors=[ApiError(403, "permission_denied", "没有这个权限")])
    sb.run(fake, insts[:1], tmp_path / "out", settings(budget_usd=10, task_usd_limit=2, sleep=waits.append))
    [rec] = records(tmp_path / "out").values()
    assert rec["state"] == "error" and waits == []


def test_resuming_after_a_lost_diff_does_not_pay_for_the_task_again(tmp_path: Path, insts: list[sb.Instance]) -> None:
    """Task 已经做完、花过钱，只是没取到补丁：续跑只取补丁，不删工作区，不重新提交。"""
    script = {insts[0].instance_id: Script(events=rounds("done"), rounds=1, cost_usd=0.3, diff="diff --git a/a b/a\n+1\n", files=["a"])}
    out = tmp_path / "out"
    first = Flaky(diff_errors=[ApiError(0, "unavailable", "连不上")], scripts=script)
    sb.run(first, insts[:1], out, settings(budget_usd=10, task_usd_limit=2))
    [rec] = records(out).values()
    assert rec["state"] == "error" and rec["cost_usd"] == pytest.approx(0.3)
    second = Flaky(scripts=script)
    second.tasks = first.tasks
    second.created = first.created
    sb.run(second, insts[:1], out, settings(budget_usd=10, task_usd_limit=2))
    [rec] = records(out).values()
    assert rec["state"] == "finished" and rec["patch"].startswith("diff --git") and rec["cost_usd"] == pytest.approx(0.3)
    assert second.submits == [] and second.create_calls == [] and second.deleted == [sb.workspace_id(RUN, insts[0].instance_id)], "只在取到补丁之后才删工作区"


def test_a_truncated_diff_is_recorded_and_not_submitted(tmp_path: Path, insts: list[sb.Instance]) -> None:
    fake = FakeRuntime({"acme__calc-1": Script(diff="diff --git a/x b/x\n+half", files=["x"], truncated=True)})
    sb.run(fake, insts[:1], tmp_path / "out", settings())
    assert records(tmp_path / "out")["acme__calc-1"]["patch_error"] == "diff_truncated"
    assert predictions(tmp_path / "out")[0]["model_patch"] == ""


# --- 12. 判分 ---

FAKE_HARNESS = '''
import argparse, json, os, sys
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--dataset_name")
p.add_argument("--split", default="test")
p.add_argument("--instance_ids", nargs="+")
p.add_argument("--predictions_path")
p.add_argument("--max_workers", type=int)
p.add_argument("--run_id")
p.add_argument("--report_dir", default=".")
p.add_argument("--modal", default="false")
a = p.parse_args()
Path(os.environ["FAKE_ARGV"]).write_text(json.dumps(sys.argv[1:]))
if os.environ.get("FAKE_FAIL"):
    print("docker daemon is not running", file=sys.stderr)
    sys.exit(1)
preds = [json.loads(l) for l in Path(a.predictions_path).read_text().splitlines() if l.strip()]
dataset = [json.loads(l)["instance_id"] for l in Path(a.dataset_name).read_text().splitlines() if l.strip()]
resolved = set(filter(None, os.environ.get("FAKE_RESOLVED", "").split(",")))
errors = set(filter(None, os.environ.get("FAKE_ERRORS", "").split(",")))
submitted = {x["instance_id"] for x in preds}
empty = {x["instance_id"] for x in preds if not x["model_patch"]}
completed = submitted - empty - errors
report = {
    "total_instances": len(dataset),
    "submitted_ids": sorted(submitted),
    "completed_ids": sorted(completed),
    "incomplete_ids": sorted(set(dataset) - submitted),
    "empty_patch_ids": sorted(empty),
    "resolved_ids": sorted(resolved & completed),
    "unresolved_ids": sorted(completed - resolved),
    "error_ids": sorted(errors),
    "infra_failure_ids": [],
    "ambiguous_failure_ids": [],
    "schema_version": 2,
}
Path(a.report_dir).mkdir(parents=True, exist_ok=True)
name = preds[0]["model_name_or_path"].replace("/", "__")
(Path(a.report_dir) / f"{name}.{a.run_id}.json").write_text(json.dumps(report))
'''


def fake_harness(tmp_path: Path) -> Path:
    root = tmp_path / "harness"
    pkg = root / "swebench" / "harness"
    pkg.mkdir(parents=True)
    (root / "swebench" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "run_evaluation.py").write_text(FAKE_HARNESS)
    return root


def graded_run(tmp_path: Path, insts: list[sb.Instance]) -> Path:
    """4 题选中，预算只够 3 题；3 题的结局 done、done、aborted，Round 数 1、2、3。"""
    out = tmp_path / "out"
    fake = FakeRuntime({
        "acme__calc-1": Script(events=rounds("done"), rounds=1, cost_usd=0.3, diff="diff --git a/a b/a\n+1\n", files=["a"]),
        "acme__calc-2": Script(events=rounds("continue", "done"), rounds=2, cost_usd=0.3, diff="diff --git a/b b/b\n+2\n", files=["b"]),
        "beta__lib-3": Script(events=rounds("continue", "continue", "abort"), rounds=3, cost_usd=0.3, status="TASK_STATUS_ABORTED",
                              final_verdict="VERDICT_ABORT", diff="diff --git a/c b/c\n+3\n", files=["c"]),
    })
    sb.run(fake, insts, out, settings(model_name="acme/agent", budget_usd=1.3, task_usd_limit=0.5))
    return out


def test_grading_calls_the_official_harness(tmp_path: Path, insts: list[sb.Instance]) -> None:
    out = graded_run(tmp_path, insts)
    argv_file = tmp_path / "argv.json"
    env = {"PYTHONPATH": str(fake_harness(tmp_path)), "FAKE_ARGV": str(argv_file), "FAKE_RESOLVED": "acme__calc-2", "FAKE_ERRORS": "beta__lib-3"}
    graded = sb.grade(out, python=sys.executable, max_workers=3, env=env)
    argv = json.loads(argv_file.read_text())
    flags = {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i].startswith("--") and not argv[i + 1].startswith("--")}
    assert flags["--dataset_name"] == str(out / "instances.jsonl"), "判分用的就是这次选中的题"
    assert flags["--predictions_path"] == str(out / "predictions.jsonl")
    assert (flags["--run_id"], flags["--max_workers"], flags["--report_dir"]) == (RUN, "3", str(out / "grading"))
    ids = argv[argv.index("--instance_ids") + 1:]
    ids = ids[: next((k for k, a in enumerate(ids) if a.startswith("--")), len(ids))]
    assert sorted(ids) == ["acme__calc-1", "acme__calc-2", "beta__lib-3"], "只判交了 predictions 的题"
    assert graded["resolved_ids"] == ["acme__calc-2"] and graded["error_ids"] == ["beta__lib-3"]
    assert (out / "grading" / "acme__agent.run-1.json").exists()


def test_the_report_counts_by_difficulty(tmp_path: Path, insts: list[sb.Instance]) -> None:
    out = graded_run(tmp_path, insts)
    instances = out / "instances.jsonl"
    rows = [json.loads(line) for line in instances.read_text("utf-8").splitlines() if line.strip()]
    for n, row in enumerate(rows):
        row["difficulty"] = "<15 min fix" if n == 0 else "1-4 hours"
    instances.write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")
    env = {"PYTHONPATH": str(fake_harness(tmp_path)), "FAKE_ARGV": str(tmp_path / "argv.json"), "FAKE_RESOLVED": "acme__calc-2", "FAKE_ERRORS": "beta__lib-3"}
    sb.grade(out, python=sys.executable, env=env)
    rep = sb.report(out)
    assert set(rep["by_difficulty"]) == {"<15 min fix", "1-4 hours"}
    assert sum(e["selected"] for e in rep["by_difficulty"].values()) == rep["selected"]
    assert rep["resolved"] and sum(e["resolved"] for e in rep["by_difficulty"].values()) == rep["resolved"]
    assert all(i["difficulty"] in rep["by_difficulty"] for i in rep["instances"])
    assert "## 按难度" in (out / "report.md").read_text("utf-8")


def test_a_failing_harness_is_reported(tmp_path: Path, insts: list[sb.Instance]) -> None:
    out = graded_run(tmp_path, insts)
    env = {"PYTHONPATH": str(fake_harness(tmp_path)), "FAKE_ARGV": str(tmp_path / "argv.json"), "FAKE_FAIL": "1"}
    with pytest.raises(RuntimeError, match="docker daemon is not running"):
        sb.grade(out, python=sys.executable, env=env)


# --- 13. 报告 ---


def test_the_report(tmp_path: Path, insts: list[sb.Instance]) -> None:
    out = graded_run(tmp_path, insts)
    ungraded = sb.report(out)
    assert ungraded["graded"] is False and ungraded["resolved"] is None
    assert (ungraded["selected"], ungraded["ran"], ungraded["not_run"]) == (4, 3, 1)

    env = {"PYTHONPATH": str(fake_harness(tmp_path)), "FAKE_ARGV": str(tmp_path / "argv.json"), "FAKE_RESOLVED": "acme__calc-2", "FAKE_ERRORS": "beta__lib-3"}
    sb.grade(out, python=sys.executable, env=env)
    rep = sb.report(out)
    assert json.loads((out / "report.json").read_text("utf-8")) == rep
    assert (rep["selected"], rep["ran"], rep["not_run"], rep["errors"]) == (4, 3, 1, 0)
    assert rep["resolved"] == 1 and rep["resolve_rate"] == pytest.approx(0.25), "÷ 选中的，同榜单的口径"
    assert rep["resolve_rate_ran"] == pytest.approx(1 / 3)
    assert rep["cost_usd"] == pytest.approx(0.9) and rep["cost_per_instance_usd"] == pytest.approx(0.3)
    assert rep["cost_per_resolved_usd"] == pytest.approx(0.9)
    assert (rep["rounds"]["median"], rep["rounds"]["max"]) == (2, 3)
    assert rep["verdicts"] == {"continue": 3, "done": 2, "abort": 1}
    assert rep["statuses"] == {"done": 2, "aborted": 1}
    assert rep["by_repo"]["acme/calc"] == {"selected": 2, "ran": 2, "resolved": 1}
    assert rep["by_repo"]["beta/lib"] == {"selected": 2, "ran": 1, "resolved": 0}
    assert rep["graded"] is True and rep["graded_complete"] is False, "有判分出错的题"
    grading = {i["instance_id"]: i["grading"] for i in rep["instances"]}
    assert grading == {"acme__calc-1": "unresolved", "acme__calc-2": "resolved", "beta__lib-3": "error", "beta__lib-4": "not_run"}
    md = (out / "report.md").read_text("utf-8")
    assert "解决率" in md and "25.0%" in md and "判分不完整" in md
