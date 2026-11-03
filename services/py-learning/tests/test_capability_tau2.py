"""τ²-bench（capability.evals.tau2）：客服与用户模拟器的对话、工具调用、结束条件与判分的边界（用假的 Episode，不装 tau2 包）。

官方 tau2 包的接法（OfficialBackend）在装了 tau2 2.0.0 的 Python 里核对过：三个领域各一道题按标准动作走一遍都得 1 分，
什么都不做的 retail 题得 0 分（见 docs/plan/capability-matrix-evals.md 的实现记录）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_learning.capability.base import PinningGateway, role_enum
from agentruntime_learning.capability.evals import tau2 as t2

from capability_support import ScriptGateway

CANDIDATE = "a6api/claude-sonnet-5-5@2026-10"
TOOL = {"name": "cancel_reservation", "description": "Cancel a reservation.",
        "parameters": {"type": "object", "properties": {"reservation_id": {"type": "string"}, "nights": {"type": "integer"}}, "required": ["reservation_id"]}}
PHONE = {"name": "toggle_airplane_mode", "description": "Toggle airplane mode.", "parameters": {"type": "object", "properties": {}}}


class FakeEpisode:
    agent_system_prompt = "<instructions>agent</instructions><policy>policy</policy>"
    user_system_prompt = "# User Simulation Guidelines\n<scenario>cancel ABC123</scenario>"

    def __init__(self, *, user_tools: bool = False, tool_error: bool = False) -> None:
        self.agent_tools = [TOOL]
        self.user_tools = [PHONE] if user_tools else []
        self.tool_error = tool_error
        self.calls: list[tuple[str, str, dict]] = []
        self.evaluated: list[tuple[list, str]] = []

    def call_tool(self, requestor: str, call_id: str, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        self.calls.append((requestor, name, arguments))
        return ("Error: not found", True) if self.tool_error else (f"{name} ok", False)

    def is_user_stop(self, text: str) -> bool:
        return "###STOP###" in text

    def evaluate(self, transcript: list[dict[str, Any]], termination: str) -> dict[str, Any]:
        self.evaluated.append((list(transcript), termination))
        done = any(c[1] == "cancel_reservation" for c in self.calls) and termination == "user_stop"
        return {"reward": 1.0 if done else 0.0, "basis": ["DB"], "components": {"ENV": 1.0 if done else 0.0}, "skipped": []}


class FakeBackend:
    version = "tau2-bench@fake"

    def __init__(self, episode: FakeEpisode) -> None:
        self.ep = episode

    def task_ids(self, domain: str) -> list[str]:
        return [f"{domain[:1]}{i}" for i in range(3)]

    def episode(self, domain: str, task_id: str) -> FakeEpisode:
        return self.ep


def script(user: list[Any], agent: list[Any]) -> ScriptGateway:
    """按角色依次取答：user 给用户模拟器，agent 给客服；取完了用户说 ###STOP###、客服说一句话。"""

    def reply(req: mg.GenerateRequest) -> Any:
        queue = agent if req.call.role == role_enum("act") else user
        return queue.pop(0) if queue else ("Thanks ###STOP###" if queue is user else "Anything else?")

    return ScriptGateway(reply)


async def run(ev: t2.Tau2Eval, gw: Any) -> Any:
    return await ev.run_item(ev.items(1, 0)[0], gw, candidate=CANDIDATE)


async def test_a_conversation_with_tool_calls_is_scored_by_the_end_state() -> None:
    ep = FakeEpisode()
    ev = t2.make(backend=FakeBackend(ep))
    inner = script(user=["Please cancel ABC123."], agent=[{"tool_calls": [("cancel_reservation", {"reservation_id": "ABC123", "nights": 2})]},
                                                         "Your reservation ABC123 is cancelled."])
    res = await run(ev, PinningGateway(inner, role="act", candidate=CANDIDATE))
    assert res.score == 1.0 and res.error is None and not res.contaminated and res.record["termination"] == "user_stop"
    assert ep.calls == [("assistant", "cancel_reservation", {"reservation_id": "ABC123", "nights": 2})], "整数参数还原成 int"
    roles = [r.call.role for r in inner.requests]
    assert roles == [role_enum("deliver"), role_enum("act"), role_enum("act"), role_enum("deliver")]
    user_req, agent_req, agent_req2, _ = inner.requests
    assert user_req.call.pinned_model_profile == t2.DEFAULT_USER_MODEL and agent_req.call.pinned_model_profile == CANDIDATE
    assert [m.text for m in user_req.generation.messages] == [FakeEpisode.user_system_prompt, t2.FIRST_AGENT_MESSAGE]
    assert [m.role for m in user_req.generation.messages] == [mg.CHAT_ROLE_SYSTEM, mg.CHAT_ROLE_USER], "用户模拟器那边角色对调"
    assert [t.name for t in agent_req.generation.tools] == ["cancel_reservation"] and not user_req.generation.tools
    tail = agent_req2.generation.messages[-2:]
    assert tail[0].role == mg.CHAT_ROLE_ASSISTANT and tail[0].tool_calls[0].name == "cancel_reservation"
    assert tail[1].role == mg.CHAT_ROLE_TOOL and tail[1].text == "cancel_reservation ok" and tail[1].tool_call_id == tail[0].tool_calls[0].id
    transcript, termination = ep.evaluated[0]
    assert [r["role"] for r in transcript] == ["assistant", "user", "assistant", "tool", "assistant", "user"]
    assert transcript[3] == {"role": "tool", "id": tail[0].tool_calls[0].id, "requestor": "assistant", "content": "cancel_reservation ok", "error": False}
    assert res.record["transcript"] == transcript and res.cost_usd == pytest.approx(0.004)


async def test_the_user_can_use_its_own_tools() -> None:
    ep = FakeEpisode(user_tools=True)
    ev = t2.make(backend=FakeBackend(ep), domains=("telecom",))
    gw = script(user=[{"tool_calls": [("toggle_airplane_mode", {})]}, "It works now. ###STOP###"], agent=[])
    res = await run(ev, gw)
    assert ep.calls == [("user", "toggle_airplane_mode", {})] and res.record["termination"] == "user_stop"
    assert [t.name for t in gw.requests[0].generation.tools] == ["toggle_airplane_mode"]
    assert gw.requests[1].generation.messages[-1].role == mg.CHAT_ROLE_TOOL, "用户工具的结果回到用户模拟器"
    assert res.score == 0.0, "没取消就不算完成"


async def test_step_and_error_limits_end_the_conversation() -> None:
    chatty = t2.make(backend=FakeBackend(FakeEpisode()), max_steps=6)
    res = await run(chatty, ScriptGateway(lambda r: "Hello?"))
    assert res.record["termination"] == "max_steps" and res.score == 0.0 and len(res.record["transcript"]) == 6

    ep = FakeEpisode(tool_error=True)
    failing = t2.make(backend=FakeBackend(ep), max_errors=3)
    res = await run(failing, script(user=["cancel"], agent=[{"tool_calls": [("cancel_reservation", {"reservation_id": "X"})]}] * 5))
    assert res.record["termination"] == "too_many_errors" and len(ep.calls) == 3


async def test_errors_and_options() -> None:
    ev = t2.make(backend=FakeBackend(FakeEpisode()))
    res = await run(ev, ScriptGateway(lambda r: RuntimeError("provider down")))
    assert res.score == 0.0 and res.error == "RuntimeError: provider down" and res.record["transcript"][0]["content"] == t2.FIRST_AGENT_MESSAGE
    assert [i.payload["domain"] for i in ev.items(3, 0)] == ["airline", "retail", "telecom"], "各领域轮流取"
    assert ev.version == f"tau2-bench@fake:user={t2.DEFAULT_USER_MODEL}" and ev.role == "act"
    with pytest.raises(ValueError):
        t2.make(user_role="act")
    with pytest.raises(ValueError):
        t2.make(domains=("banking",))


def test_the_official_backend_needs_the_tau2_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        import tau2  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("装了 tau2 包")
    (tmp_path / "tau2" / "domains" / "airline").mkdir(parents=True)
    monkeypatch.setenv("TAU2_DATA_DIR", "")  # 测完还原
    with pytest.raises(RuntimeError, match="tau2 包"):
        t2.OfficialBackend(data_dir=tmp_path)
