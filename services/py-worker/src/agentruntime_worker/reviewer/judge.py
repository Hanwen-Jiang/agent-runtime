"""交付的快速检查（docs/plan/feedback-2026-11-01.md N-09）：用 TypeSafe 的 Jev（System One 模型，接口 https://api.typesafe.ai/v1/systemone）
对 deliver 节点的结果问两个是非题，得到“是”的概率：

- answers：回复是不是在回答用户问的事（或如实报告了尝试的结果）；
- disowns：回复是不是说自己没有工具、读不了文件或仓库，说输入或上游结果缺了、没解析，或者承认之前的回答是编的。

Reviewer 在要判 done 时问它（reviewer/reviewer.py）；规则据此拦下（reviewer/rules.py）：disowns 高的不管谁判的都不能交付；没有模型评审
（fast 的回复、模型评审拿不到）时 answers 低的也不能交付。一次调用约 1 秒，比 Reviewer 的模型调用便宜得多，也和生成者不是同一家。

- 密钥在 SecretStore 平台作用域的 judge/jev-api-key（不进日志、错误说明与 trace）；Worker 设 AR_JUDGE=jev 才用。
- 发给 Jev 的只有用户这条消息与交付的文字（各自截断），不带 Task State、工具输出与别的租户的东西。
- 调用失败、超时、回答不合格时返回 None：没有检查结果，Reviewer 照常判定，不因此拦下交付。
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

SECRET_NAME = "judge/jev-api-key"
SECRET_PURPOSE = "judge"
DEFAULT_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
REQUEST_CHARS = 4000
REPLY_CHARS = 8000

QUESTIONS: dict[str, dict[str, Any]] = {
    "answers": {
        "type": "noul",
        "instructions": "The assistant reply addresses what the user asked, or honestly reports the outcome of trying to do it.",
        "criteria": {
            "true": "Directly addresses the user's request (an answer, a result, or a clear report of what was found or what failed)",
            "false": "Does not address the request: empty, off-topic, a placeholder, or only talks about missing inputs",
        },
    },
    "disowns": {
        "type": "noul",
        "instructions": (
            "The assistant reply says it has no tools or cannot read files or the repository, says its inputs or upstream results were "
            "missing or unresolved, or admits that an earlier answer was made up."
        ),
        "criteria": {
            "true": "Claims to have no tools or access, complains about missing or unresolved inputs, or confesses to fabricating an earlier answer",
            "false": "Answers without disowning its tools, inputs or earlier work",
        },
    },
}


@dataclass(frozen=True)
class JudgeChecks:
    """Jev 的回答：两个是非题“是”的概率（0～1），以及实际服务的模型与计量。"""

    answers: float
    disowns: float
    model: str = ""
    input_tokens: int = 0


class DeliverableJudge(Protocol):
    async def check(self, request: str, reply: str) -> JudgeChecks | None: ...


Post = Callable[[str, Mapping[str, str], bytes, float], Awaitable[tuple[int, bytes]]]


async def _urllib_post(url: str, headers: Mapping[str, str], body: bytes, timeout_s: float) -> tuple[int, bytes]:
    def send() -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 — 固定的 Jev 地址（AR_JUDGE_URL）
                return resp.status, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()

    return await asyncio.to_thread(send)


def reply_text(value: Any) -> str:
    """交付写成要检查的文字：字符串原样；常见的正文字段取出来；其余写成 JSON。"""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("text", "answer", "reply", "content", "summary"):
            if isinstance(value.get(key), str):
                return str(value[key])
    return json.dumps(value, ensure_ascii=False)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


class JevJudge:
    """Jev 的 System One 接口。key 是取密钥的函数（每次调用时取，轮换后不用重启）。"""

    def __init__(
        self, key: Callable[[], Awaitable[str | None]], *, url: str = DEFAULT_URL, model: str = DEFAULT_MODEL, timeout_s: float = 15.0, post: Post | None = None,
    ) -> None:
        self.key = key
        self.url = url.rstrip("/") + "/v1/systemone"
        self.model = model
        self.timeout_s = timeout_s
        self.post = post or _urllib_post

    async def check(self, request: str, reply: str) -> JudgeChecks | None:
        try:
            key = await self.key()
        except Exception as exc:  # noqa: BLE001 — 取不到密钥就不检查
            logger.warning("Jev：取密钥失败（%s），这次不检查", type(exc).__name__)
            return None
        if not key:
            logger.warning("Jev：SecretStore 里没有 %s，这次不检查", SECRET_NAME)
            return None
        body = json.dumps(
            {"model": self.model, "state": {"user_request": _clip(request, REQUEST_CHARS), "assistant_reply": _clip(reply, REPLY_CHARS)}, "questions": QUESTIONS},
            ensure_ascii=False,
        ).encode()
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            status, raw = await self.post(self.url, headers, body, self.timeout_s)
        except Exception as exc:  # noqa: BLE001 — 网络问题不影响判定
            logger.warning("Jev：调用失败（%s），这次不检查", type(exc).__name__)
            return None
        if status != 200:
            logger.warning("Jev：返回 %s，这次不检查", status)
            return None
        try:
            data = json.loads(raw)
            answers = data["answers"]
            usage = data.get("usage") or {}
            return JudgeChecks(
                answers=_probability(answers["answers"]), disowns=_probability(answers["disowns"]), model=str(data.get("model") or ""),
                input_tokens=int(usage.get("input_tokens") or 0),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Jev：回答不合格（%s），这次不检查", type(exc).__name__)
            return None


def _probability(answer: Any) -> float:
    value = float(answer["noul"])
    if not 0.0 <= value <= 1.0:
        raise ValueError("概率不在 0～1 之间")
    return value


def secret_key(secrets_file: Any) -> Callable[[], Awaitable[str | None]]:
    """从开发 SecretStore（AR_SECRETS_FILE）的平台作用域读 judge/jev-api-key；没有时为 None。"""

    async def read() -> str | None:
        if secrets_file is None:
            return None
        from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
        from agentruntime_core.errors import NotFoundError

        try:
            value = (await LocalFileSecretStoreProvider(secrets_file).bind_platform(SECRET_PURPOSE).read(SECRET_NAME)).reveal()
        except NotFoundError:
            return None
        return value.decode() if isinstance(value, bytes) else str(value)

    return read


__all__ = ["DEFAULT_MODEL", "DEFAULT_URL", "QUESTIONS", "SECRET_NAME", "DeliverableJudge", "JevJudge", "JudgeChecks", "reply_text", "secret_key"]
