"""docs/plan/feedback-2026-11-01.md N-09：Jev（TypeSafe 的 System One 模型）做交付的快速检查。

- 只在要判 done 时问（模型提议 done，或没有模型的提议、规则会判 done）；问的是用户这条消息与交付的文字，不带别的。
- 交付“不认自己的工具、输入或之前的工作”的概率高（≥ 0.8）：不管谁判的 done 都拦，replan 回 Planner 带工具重做（线上那条“我没有工具”是 0.99）；
- 没有模型评审时（fast、模型评审拿不到）“回答了用户问的事”的概率低（≤ 0.2）也拦；模型评审过的以模型为准。
- Jev 调用失败、返回不合格、没有密钥：没有检查结果，照常判定。
- 密钥只在请求头里，不进日志与结果；配置 AR_JUDGE=jev 才组装。设 AR_LIVE_JEV=1（另要 JEV_API_KEY）时对真接口跑一条。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from typing import Any

import pytest

from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.reviewer.judge import QUESTIONS, JevJudge, JudgeChecks, reply_text
from agentruntime_worker.reviewer.reviewer import Reviewer

from .reviewer_support import FakeReviewGateway, atomic, done, goal, ok, request, rounds, stage, task_state
from .support import assert_valid

KEY = "jev-test-key-0123456789"
STAGE = stage(goal("explore"), atomic("answer", role="deliver", after=("explore",)), goal_text="读仓库并说明结构", exit_when="交付了")
FAST = stage(atomic("answer", role="deliver"), goal_text="回复用户这条消息", exit_when="answer 给出回复")


class FakeJudge:
    def __init__(self, checks: JudgeChecks | None) -> None:
        self.result = checks
        self.asked: list[tuple[str, str]] = []

    async def check(self, request: str, reply: str) -> JudgeChecks | None:
        self.asked.append((request, reply))
        return self.result


def results(reply: str) -> dict[str, Any]:
    return {"explore": ok(), "answer": ok("deliver", {"text": reply})}


def state(goal_text: str = "读一下这个仓库讲讲结构", **kw: Any) -> dict[str, Any]:
    s = task_state(rounds=rounds(**kw))
    s["goal"] = goal_text
    return s


async def review(judge: FakeJudge, res: dict[str, Any], *, model: list[Any] | None = None, stage_: dict[str, Any] = STAGE, **kw: Any) -> Any:
    gateway = FakeReviewGateway(model if model is not None else [RuntimeError("模型不可用"), RuntimeError("模型不可用")])
    result = await Reviewer(gateway, judge=judge).review(request(stage_, res, state=state(**kw)))
    assert_valid("verdict", result.verdict)
    return result


async def test_a_reply_that_disowns_its_tools_is_not_delivered_even_when_the_model_says_done() -> None:
    judge = FakeJudge(JudgeChecks(answers=0.92, disowns=0.99, model="jev-1.14.0"))
    result = await review(judge, results("坦白说：现在这一轮我没有可用的工具。前面那份仓库结构表是我编出来的。"), model=[done("answer")])
    assert result.verdict["verdict"] == "replan" and result.verdict["reason_code"] == "rule_judge_disowns"
    assert result.verdict["reason_params"]["probability"] == 0.99
    assert "answer" not in (result.verdict["replan"].get("keep_nodes") or []), "要重做的交付不保留"
    assert judge.asked == [("读一下这个仓库讲讲结构", "坦白说：现在这一轮我没有可用的工具。前面那份仓库结构表是我编出来的。")]
    assert result.checks is not None and result.checks.model == "jev-1.14.0"


async def test_a_fast_reply_that_disowns_its_tools_goes_back_to_planning() -> None:
    judge = FakeJudge(JudgeChecks(answers=0.9, disowns=0.97))
    res = {"answer": ok("deliver", {"text": "我没有读取文件的工具，没法看你的仓库。"})}
    result = await review(judge, res, model=[], stage_=FAST, triage="fast", goal_text="你是怎么读取的目录结构呢")
    assert result.verdict["verdict"] == "replan", "回 Planner：下一轮的分诊按 replan 走，有工具"


async def test_without_a_model_review_an_off_topic_reply_is_not_delivered() -> None:
    judge = FakeJudge(JudgeChecks(answers=0.05, disowns=0.1))
    result = await review(judge, results("今天天气不错。"))
    assert result.verdict["verdict"] == "replan" and result.verdict["reason_code"] == "rule_judge_off_topic"


async def test_with_a_model_review_a_low_answer_probability_alone_does_not_block() -> None:
    judge = FakeJudge(JudgeChecks(answers=0.05, disowns=0.1))
    result = await review(judge, results("这是仓库结构：docs/、src/……"), model=[done("answer")])
    assert result.verdict["verdict"] == "done", "模型评审过的以模型为准"


async def test_a_good_reply_is_delivered() -> None:
    judge = FakeJudge(JudgeChecks(answers=0.95, disowns=0.02))
    result = await review(judge, results("根目录有 docs/、api-docs/、archive/ 三个目录和 README.md……"))
    assert result.verdict["verdict"] == "done"


async def test_no_check_result_means_the_usual_verdict() -> None:
    judge = FakeJudge(None)
    result = await review(judge, results("根目录有 docs/……"))
    assert result.verdict["verdict"] == "done" and result.checks is None


async def test_jev_is_only_asked_when_the_verdict_would_be_done() -> None:
    judge = FakeJudge(JudgeChecks(answers=0.9, disowns=0.99))
    failing = {"explore": {"role": "transform", "status": "failed", "failure_class": "invalid_output", "error": "坏了"}, "answer": {"role": "deliver", "status": "skipped", "error": "上游没有成功"}}
    await review(judge, failing)
    await review(judge, results("x"), model=[{"verdict": {"verdict": "continue", "reason": "还没完", "progress_check": {"criteria_met": False, "progress": True, "looping": False, "assumptions_hold": True, "next_step": "继续"}}, "completion": 0.4, "state_update": {}}])
    assert judge.asked == [], "有失败的 Node、模型判 continue 时都不问"


# --- 接口与配置 ---


class Recorder:
    def __init__(self, status: int, body: Any) -> None:
        self.status, self.body = status, body
        self.calls: list[tuple[str, Mapping[str, str], dict[str, Any]]] = []

    async def __call__(self, url: str, headers: Mapping[str, str], body: bytes, timeout_s: float) -> tuple[int, bytes]:
        self.calls.append((url, headers, json.loads(body)))
        if isinstance(self.body, Exception):
            raise self.body
        return self.status, self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()


async def key() -> str:
    return KEY


ANSWER = {"model": "jev-1.14.0", "answers": {"answers": {"type": "noul", "noul": 0.92}, "disowns": {"type": "noul", "noul": 0.99}}, "usage": {"input_tokens": 529, "output_tokens": 56}}


async def test_the_request_carries_only_the_message_and_the_reply() -> None:
    post = Recorder(200, ANSWER)
    checks = await JevJudge(key, post=post).check("你都有什么工具呢", "我没有工具。" + "x" * 20000)
    assert checks == JudgeChecks(answers=0.92, disowns=0.99, model="jev-1.14.0", input_tokens=529)
    ((url, headers, body),) = post.calls
    assert url == "https://api.typesafe.ai/v1/systemone" and headers["Authorization"] == f"Bearer {KEY}"
    assert body["model"] == "jev-latest" and set(body["state"]) == {"user_request", "assistant_reply"}
    assert len(body["state"]["assistant_reply"]) <= 8001 and body["questions"] == QUESTIONS


@pytest.mark.parametrize(
    ("status", "body"),
    [(401, {"detail": "bad key"}), (200, b"not json"), (200, {"answers": {}}), (200, {**ANSWER, "answers": {"answers": {"noul": 1.5}, "disowns": {"noul": 0.1}}}), (200, OSError("断网"))],
)
async def test_failures_give_no_check_and_never_leak_the_key(status: int, body: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    assert await JevJudge(key, post=Recorder(status, body)).check("问题", "回答") is None
    assert KEY not in caplog.text


async def test_without_a_key_jev_is_not_called() -> None:
    async def none() -> None:
        return None

    post = Recorder(200, ANSWER)
    assert await JevJudge(none, post=post).check("问题", "回答") is None and post.calls == []


def test_reply_text_takes_the_body_of_a_deliverable() -> None:
    assert reply_text({"text": "正文"}) == "正文" and reply_text("原样") == "原样" and json.loads(reply_text({"files": ["a"]})) == {"files": ["a"]}


def test_the_judge_is_configured_explicitly() -> None:
    assert WorkerConfig.from_env({}).judge == ""
    cfg = WorkerConfig.from_env({"AR_JUDGE": "jev", "AR_JUDGE_MODEL": "jev-preview"})
    assert cfg.judge == "jev" and cfg.judge_model == "jev-preview" and cfg.judge_url == "https://api.typesafe.ai"
    with pytest.raises(ValueError):
        WorkerConfig.from_env({"AR_JUDGE": "gpt"})


def test_the_assembled_reviewer_gets_jev_only_when_asked(tmp_path: Any) -> None:
    from agentruntime_worker.reviewer.activities import dev_reviewer

    off = dev_reviewer(WorkerConfig.from_env({"AR_DATA_DIR": str(tmp_path)}))
    on = dev_reviewer(WorkerConfig.from_env({"AR_DATA_DIR": str(tmp_path), "AR_JUDGE": "jev"}))
    assert off.reviewer.judge is None and isinstance(on.reviewer.judge, JevJudge)


@pytest.mark.skipif(os.environ.get("AR_LIVE_JEV") != "1" or not os.environ.get("JEV_API_KEY"), reason="设 AR_LIVE_JEV=1 与 JEV_API_KEY 才对真接口跑")
async def test_live_jev_flags_the_reply_from_the_log() -> None:
    async def live_key() -> str:
        return os.environ["JEV_API_KEY"]

    checks = await JevJudge(live_key).check("你都有什么工具呢 现在", "坦白说：现在这一轮我没有可用的工具。我不能读取文件、遍历目录、执行命令。前面那份仓库结构表，就是我编出来的。")
    assert checks is not None and checks.disowns >= 0.8, checks
    good = await JevJudge(live_key).check("读一下这个仓库讲讲结构", "根目录有 docs/（主体文档）、api-docs/、archive/（归档）、resources/，以及 README.md、AGENTS.md。")
    assert good is not None and good.disowns < 0.5 and good.answers > 0.5, good
