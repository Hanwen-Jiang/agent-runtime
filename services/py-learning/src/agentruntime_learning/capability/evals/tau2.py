"""τ²-bench（docs/plan/capability-matrix-evals.md 第 3 节，role act）：带用户模拟器的多轮工具交互（airline、retail、telecom），按环境终态判。

分两层：

- **对话**在这里：被测的客服（role act，矩阵的 PinningGateway 钉在候选模型上）与用户模拟器（role `user_role`，默认 deliver，
  `pinned_model_profile` 明确填 `user_model`，所以不被钉、所有候选面对同一个“用户”）轮流说话，都经传进来的 Model Gateway；
  开场白、回合与出错上限同官方的 Orchestrator（"Hi! How can I help you today?"，max_steps 100，max_errors 10）。
  用户说出 ###STOP###、###TRANSFER###、###OUT-OF-SCOPE### 时结束。
- **环境**在 `Backend`/`Episode` 后面：工具定义、执行工具调用、两边的系统提示、结束判断与判分。`OfficialBackend` 用官方的
  tau2 包（`eval-requirements.txt` 钉在 v2.0.0 的提交上，只装在评测的 Python 里；本模块不装它也能 import，离线测试用假的 Episode）：
  `registry` 的环境与任务（split base）、`tau2.agent.llm_agent` 的客服系统提示、`UserSimulator.system_prompt`、
  `Environment.get_response` 执行工具，判分用 `evaluate_simulation` 按任务的 reward_basis 逐项算再相乘：
  DB 与 ENV_ASSERTION（EvaluationType.ENV，环境终态）、ACTION、COMMUNICATE（用官方原来的子串匹配
  `CommunicateScorer.SUBSTRING`，不用要 LLM 的语义判定）。

和官方的差别（实现记录里有）：

- retail 的 112 题的 reward_basis 有 NL_ASSERTION，官方由 tau2 自己经 litellm 调裁判模型；这里的模型调用只能经 Model Gateway，
  所以先不算这一项（record 的 `skipped` 写明），只按数据库终态判——人定的“按它自带的数据库终态判”。
- 官方的 LLM 调用经 litellm、温度等参数由它给；这里经 Gateway，参数按 Profile。
- 数据：tau2 读 `TAU2_DATA_DIR`；没设时从钉死的提交稀疏检出 `data/tau2/domains/{airline,retail,telecom}` 与英文的语言包到
  `<AR_EVAL_DATA_DIR>/tau2-bench/<提交>/data`（要 PATH 上的 git，约 140 MB）。
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_learning.capability.base import Item, ItemResult
from agentruntime_learning.capability.evals import _datasets as ds
from agentruntime_worker.executor.model_gateway import ModelGateway

NAME = "tau2-bench"
ROLE = "act"
REPO_URL = "https://github.com/sierra-research/tau2-bench"
COMMIT = "989d3285ae669e49503324bdb9981bf14fccedeb"  # v2.0.0，2026-10-19
DOMAINS = ("airline", "retail", "telecom")
SPARSE = ("/data/tau2/domains/airline/", "/data/tau2/domains/retail/", "/data/tau2/domains/telecom/", "/data/tau2/multilingual/en/",
          "/data/tau2/multilingual/*.json")
FIRST_AGENT_MESSAGE = "Hi! How can I help you today?"
DEFAULT_USER_MODEL = "a6api/gpt-5.5@2026-10"
DEFAULT_USER_TIER = "strong@medium"  # 网关只在请求的档里找钉的模型：换 user_model 时一起换成它的 class
MAX_STEPS = 100
MAX_ERRORS = 10


class Episode(Protocol):
    """一道题的环境（见模块说明）。工具是 {"name", "description", "parameters"}；transcript 的每条是
    {"role": assistant|user|tool, "content", "tool_calls": [{"id", "name", "arguments"}], 工具结果另有 "id"、"requestor"、"error"}。"""

    agent_system_prompt: str
    agent_tools: list[dict[str, Any]]
    user_system_prompt: str
    user_tools: list[dict[str, Any]]

    def call_tool(self, requestor: str, call_id: str, name: str, arguments: dict[str, Any]) -> tuple[str, bool]: ...

    def is_user_stop(self, text: str) -> bool: ...

    def evaluate(self, transcript: list[dict[str, Any]], termination: str) -> dict[str, Any]: ...


class Backend(Protocol):
    version: str

    def task_ids(self, domain: str) -> list[str]: ...

    def episode(self, domain: str, task_id: str) -> Episode: ...


# --- 官方 tau2 包 ---


def fetch_data(dest: Path, *, commit: str = COMMIT) -> Path:
    """从钉死的提交稀疏检出 tau2 要读的数据（见模块说明）；dest 已有时不动。返回 data 目录。"""
    dest = Path(dest)
    if (dest / "tau2" / "domains" / "airline").is_dir():
        return dest
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("取 τ²-bench 的数据要 PATH 上的 git，或者设 TAU2_DATA_DIR 指向官方仓库的 data 目录")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        def run(*args: str) -> None:
            subprocess.run([git, "-C", tmp, *args], check=True, capture_output=True, timeout=1800)

        run("init", "-q")
        run("remote", "add", "origin", REPO_URL)
        run("sparse-checkout", "set", "--no-cone", *SPARSE)
        run("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", commit)
        run("checkout", "-q", "FETCH_HEAD")
        os.replace(Path(tmp) / "data", dest)
    return dest


def _tau2(module: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise RuntimeError("τ²-bench 要官方的 tau2 包：装在评测的 Python 里（services/py-learning/eval-requirements.txt）") from exc


def _tool_dict(tool: Any) -> dict[str, Any]:
    fn = tool.openai_schema["function"]
    return {"name": fn["name"], "description": fn.get("description", ""), "parameters": fn.get("parameters") or {"type": "object", "properties": {}}}


class OfficialEpisode:
    def __init__(self, domain: str, task: Any) -> None:
        registry = _tau2("tau2.registry").registry
        agent = _tau2("tau2.agent.llm_agent")
        user_sim = _tau2("tau2.user.user_simulator")
        self.domain, self.task = domain, task
        self.env = registry.get_env_constructor(domain)()
        init = task.initial_state
        if init is not None and init.message_history:
            raise ValueError(f"{task.id} 带对话历史，暂不支持")
        if init is not None:
            self.env.set_state(initialization_data=init.initialization_data, initialization_actions=init.initialization_actions, message_history=[])
        try:
            user_tools = self.env.get_user_tools()
        except ValueError:
            user_tools = []
        self.agent_tools = [_tool_dict(t) for t in self.env.get_tools()]
        self.user_tools = [_tool_dict(t) for t in user_tools]
        self.agent_system_prompt = agent.SYSTEM_PROMPT.format(agent_instruction=agent.AGENT_INSTRUCTION, domain_policy=self.env.get_policy())
        self._user_cls = user_sim.UserSimulator
        self.user_system_prompt = user_sim.UserSimulator(llm="gateway", instructions=str(task.user_scenario), tools=user_tools or None).system_prompt

    def call_tool(self, requestor: str, call_id: str, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        message = _tau2("tau2.data_model.message")
        result = self.env.get_response(message.ToolCall(id=call_id, name=name, arguments=arguments, requestor=requestor))
        self.env.sync_tools()
        return result.content or "", bool(result.error)

    def is_user_stop(self, text: str) -> bool:
        message = _tau2("tau2.data_model.message")
        return bool(self._user_cls.is_stop(message.UserMessage(role="user", content=text)))

    def _messages(self, transcript: list[dict[str, Any]]) -> list[Any]:
        m = _tau2("tau2.data_model.message")
        out = []
        for row in transcript:
            if row["role"] == "tool":
                out.append(m.ToolMessage(id=row["id"], role="tool", content=row["content"], requestor=row["requestor"], error=row.get("error", False)))
                continue
            calls = [m.ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"], requestor=row["role"]) for c in row.get("tool_calls") or ()]
            cls = m.AssistantMessage if row["role"] == "assistant" else m.UserMessage
            out.append(cls(role=row["role"], content=row.get("content") or None, tool_calls=calls or None))
        return out

    def evaluate(self, transcript: list[dict[str, Any]], termination: str) -> dict[str, Any]:
        sim_mod = _tau2("tau2.data_model.simulation")
        ev = _tau2("tau2.evaluator.evaluator")
        sc = _tau2("tau2.data_model.semantic_communication")
        now = datetime.now(UTC).isoformat()
        sim = sim_mod.SimulationRun(id=f"{self.domain}:{self.task.id}", task_id=self.task.id, start_time=now, end_time=now, duration=0.0,
                                    termination_reason=sim_mod.TerminationReason(termination), messages=self._messages(transcript))
        basis = {b.value for b in (self.task.evaluation_criteria.reward_basis if self.task.evaluation_criteria else [])}
        plan = [("ENV", ev.EvaluationType.ENV)] if basis & {"DB", "ENV_ASSERTION"} else []
        plan += [(name, getattr(ev.EvaluationType, name)) for name in ("ACTION", "COMMUNICATE") if name in basis]
        components: dict[str, float] = {}
        infos: dict[str, Any] = {}
        for name, kind in plan:
            info = ev.evaluate_simulation(sim, self.task, kind, False, self.domain,
                                          communicate_scoring=sc.CommunicateScoring(scorer=sc.CommunicateScorer.SUBSTRING))
            components[name] = float(info.reward)
            infos[name] = info.info
        reward = 1.0
        for v in components.values():
            reward *= v
        return {"reward": reward if plan else 0.0, "basis": sorted(basis), "components": components, "skipped": sorted(basis & {"NL_ASSERTION"}),
                "info": infos}


class OfficialBackend:
    """官方 tau2 包（见模块说明）。data_dir 为空时用 TAU2_DATA_DIR，再没有就按钉死的提交取。"""

    def __init__(self, *, data_dir: Path | None = None, split: str = "base") -> None:
        self.split = split
        if data_dir is not None or not os.environ.get("TAU2_DATA_DIR"):
            os.environ["TAU2_DATA_DIR"] = str(fetch_data(Path(data_dir or ds.data_dir() / NAME / COMMIT / "data")))
        utils = _tau2("tau2.utils")
        if Path(utils.DATA_DIR).resolve() != Path(os.environ["TAU2_DATA_DIR"]).resolve():
            raise RuntimeError(f"tau2 已经按 {utils.DATA_DIR} 加载过数据，和 TAU2_DATA_DIR 不同：在 import tau2 之前设好")
        from importlib.metadata import version

        self.version = f"tau2-bench@{COMMIT[:7]}"
        self.package_version = version("tau2")
        self._tasks: dict[str, dict[str, Any]] = {}

    def _load(self, domain: str) -> dict[str, Any]:
        if domain not in self._tasks:
            registry = _tau2("tau2.registry").registry
            self._tasks[domain] = {t.id: t for t in registry.get_tasks_loader(domain)(self.split)}
        return self._tasks[domain]

    def task_ids(self, domain: str) -> list[str]:
        return sorted(self._load(domain))

    def episode(self, domain: str, task_id: str) -> OfficialEpisode:
        return OfficialEpisode(domain, self._load(domain)[task_id])


# --- 对话 ---


def _restore_ints(value: Any) -> Any:
    """Gateway 的工具参数是 Struct，数都是浮点数；整数值还原成 int（模型写 JSON 时就是整数，tau2 的工具按 Python 的类型用）。"""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_restore_ints(v) for v in value]
    if isinstance(value, dict):
        return {k: _restore_ints(v) for k, v in value.items()}
    return value


def _tools(specs: Sequence[dict[str, Any]]) -> list[mg.ToolSpec]:
    return [ds.tool_spec(t["name"], t.get("description", ""), t["parameters"]) for t in specs]


class Tau2Eval:
    name = NAME
    role = ROLE
    est_tokens = (200_000, 3000)  # 客服一侧：政策与工具每轮重发（提示缓存命中后便宜很多）
    est_other_usd = 0.35  # 用户模拟器（默认 gpt-5.5）约 60k 输入

    def __init__(self, *, domains: Sequence[str] = DOMAINS, backend: Backend | None = None, user_model: str = DEFAULT_USER_MODEL,
                 user_role: str = "deliver", user_tier: str = DEFAULT_USER_TIER, max_steps: int = MAX_STEPS, max_errors: int = MAX_ERRORS,
                 max_output_tokens: int = 4096, data_dir: Path | None = None) -> None:
        unknown = set(domains) - set(DOMAINS)
        if unknown:
            raise ValueError(f"τ²-bench 的领域只有 {'、'.join(DOMAINS)}")
        if user_role == ROLE:
            raise ValueError("用户模拟器不能用被测的角色，否则会被钉在候选模型上")
        self.domains, self.user_model, self.user_role, self.user_tier = tuple(domains), user_model, user_role, user_tier
        self.max_steps, self.max_errors, self.max_output_tokens = max_steps, max_errors, max_output_tokens
        self._backend, self._data_dir = backend, data_dir
        tag = "" if self.domains == DOMAINS else ":" + "+".join(self.domains)
        self.version = f"{backend.version if backend else f'tau2-bench@{COMMIT[:7]}'}{tag}:user={user_model}"

    def backend(self) -> Backend:
        if self._backend is None:
            self._backend = OfficialBackend(data_dir=self._data_dir)
        return self._backend

    def items(self, n: int, seed: int) -> list[Item]:
        b = self.backend()
        ids = {f"{d}:{t}": (d, t) for d in self.domains for t in b.task_ids(d)}
        return [Item(i, {"domain": ids[i][0], "task_id": ids[i][1]}) for i in ds.select(sorted(ids), n, seed, group=lambda i: ids[i][0])]

    async def run_item(self, item: Item, gateway: ModelGateway, *, candidate: str) -> ItemResult:
        meter = ds.Meter(gateway, item_id=item.id, eval_name=self.name)
        record: dict[str, Any] = {"domain": item.payload["domain"], "task_id": item.payload["task_id"], "candidate": candidate, "user_model": self.user_model}
        transcript: list[dict[str, Any]] = [{"role": "assistant", "content": FIRST_AGENT_MESSAGE}]
        record["transcript"] = transcript
        try:
            episode = self.backend().episode(item.payload["domain"], item.payload["task_id"])
            termination = await self._converse(meter, episode, transcript)
            outcome = episode.evaluate(transcript, termination)
            record.update({"termination": termination, "evaluation": outcome})
            return meter.result(min(max(float(outcome["reward"]), 0.0), 1.0), record)
        except Exception as exc:
            return meter.error(exc, record)

    async def _converse(self, meter: ds.Meter, episode: Episode, transcript: list[dict[str, Any]]) -> str:
        agent_msgs = [ds.chat("system", episode.agent_system_prompt), ds.chat("assistant", FIRST_AGENT_MESSAGE)]
        user_msgs = [ds.chat("system", episode.user_system_prompt), ds.chat("user", FIRST_AGENT_MESSAGE)]
        agent_tools, user_tools = _tools(episode.agent_tools), _tools(episode.user_tools)
        errors = 0
        while len(transcript) < self.max_steps:
            # 用户的回合：可以先调自己的工具（telecom 的手机），最后说一句话
            response = await meter.generate(role=self.user_role, node="tau2.user", messages=user_msgs, tools=user_tools, pinned=self.user_model,
                                            tier=self.user_tier, max_output_tokens=self.max_output_tokens)
            if response.tool_calls:
                errors += self._run_tools(episode, "user", response, transcript, user_msgs)
                if errors >= self.max_errors:
                    return "too_many_errors"
                continue
            text = response.text
            transcript.append({"role": "user", "content": text})
            if episode.is_user_stop(text):
                return "user_stop"
            agent_msgs.append(ds.chat("user", text))
            user_msgs.append(ds.chat("assistant", text))
            # 客服的回合：工具调用若干次，最后回用户一句话
            while len(transcript) < self.max_steps:
                response = await meter.generate(role=ROLE, node="tau2.agent", messages=agent_msgs, tools=agent_tools,
                                                max_output_tokens=self.max_output_tokens)
                if response.tool_calls:
                    errors += self._run_tools(episode, "assistant", response, transcript, agent_msgs)
                    if errors >= self.max_errors:
                        return "too_many_errors"
                    continue
                transcript.append({"role": "assistant", "content": response.text})
                agent_msgs.append(ds.chat("assistant", response.text))
                user_msgs.append(ds.chat("user", response.text))
                break
        return "max_steps"

    @staticmethod
    def _run_tools(episode: Episode, requestor: str, response: mg.GenerateResponse, transcript: list[dict[str, Any]], history: list[mg.ChatMessage]) -> int:
        calls = []
        for n, c in enumerate(response.tool_calls):
            calls.append({"id": c.id or f"call_{len(transcript)}_{n}", "name": c.name, "arguments": _restore_ints(ds.arguments(c))})
        transcript.append({"role": requestor, "content": response.text, "tool_calls": calls})
        history.append(ds.chat("assistant", response.text, tool_calls=[ds.tool_call(c["id"], c["name"], c["arguments"]) for c in calls]))
        errors = 0
        for c in calls:
            content, error = episode.call_tool(requestor, c["id"], c["name"], c["arguments"])
            errors += int(error)
            transcript.append({"role": "tool", "id": c["id"], "requestor": requestor, "content": content, "error": error})
            history.append(ds.chat("tool", content, tool_call_id=c["id"]))
        return errors


def make(**options: Any) -> Tau2Eval:
    return Tau2Eval(**options)


__all__ = ["Backend", "Episode", "OfficialBackend", "Tau2Eval", "fetch_data", "make"]
