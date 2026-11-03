"""各轮的重要输出（docs/plan/running-task-interaction.md 第 4 节）：给人看的汇总，写进 Task State 的 rounds[]，不给 Planner 与 Reviewer。

- 文件：模型写的路径要是这一轮工具调用的目标，否则去掉（防编造）；改动过的文件由代码补上。工作流不能做 I/O，所以不查工作区。
- 交付给用户的文件（这一轮成功的 deliver_file，N-18）排第一、标 delivered: true，客户端据此给下载入口。
- 答案：模型挑的；没有时（fast 的轮、Reviewer 模型出错、这一轮被纠偏或打断）代码取回答开头两句，不调模型。
- 每轮最多 5 个文件、5 条答案，每条不超过 300 字。Task State 给客户端看，所以都脱敏。

纯函数，工作流里可用。
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Mapping
from typing import Any

from agentruntime_worker.workflows.events import redact

MAX_ITEMS = 5
MAX_CHARS = 300
# 回答在 dict 里时取这些键。
_ANSWER_KEYS = ("answer", "text", "summary", "result")
# 句子的结尾：中文的句号、问号、叹号，或者后面是空白的英文句点（1.2.3 里的点不算）。
_SENTENCE_END = re.compile(r"[。！？!?]+|\.(?=\s|$)")


def _clip(text: str) -> str:
    text = redact(text.strip())
    return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1] + "…"


def normalize_path(path: str) -> str:
    """./src/a.py 与 src/a.py 是同一个文件；工具的 path 参数也可能写反斜杠。"""
    return posixpath.normpath(path.strip().replace("\\", "/"))


def _first_sentences(text: str, count: int = 2) -> str:
    end = 0
    for i, match in enumerate(_SENTENCE_END.finditer(text), start=1):
        end = match.end()
        if i == count:
            return text[:end]
    return text


def _quote(deliverable: Any) -> str:
    if isinstance(deliverable, Mapping):
        deliverable = next((deliverable[k] for k in _ANSWER_KEYS if isinstance(deliverable.get(k), str)), None)
    return _first_sentences(deliverable.strip()) if isinstance(deliverable, str) else ""


def _files(raw: Any, touched: set[str], writes: Iterable[str], delivered: list[str]) -> list[dict[str, Any]]:
    """交付的文件（N-18）排第一、标 delivered；然后是模型挑的（要是这一轮碰过的），再是代码补上的改动文件。"""
    picked: dict[str, str] = {}
    for item in raw.get("files") or [] if isinstance(raw, Mapping) else []:
        path = normalize_path(str(item.get("path") or ""))
        if path in touched and path not in picked:
            picked[path] = str(item.get("why") or "").strip()
    files: dict[str, dict[str, Any]] = {}
    for path in delivered:
        why = picked.get(path, "")
        files[path] = {"path": redact(path), "delivered": True, **({"why": _clip(why)} if why else {})}
    for path, why in picked.items():
        files.setdefault(path, {"path": redact(path), **({"why": _clip(why)} if why else {})})
    for path in writes:
        files.setdefault(path, {"path": redact(path)})
    return list(files.values())[:MAX_ITEMS]


def round_highlights(
    raw: Any, *, reads: Iterable[str] = (), writes: Iterable[str] = (), delivered: Iterable[str] = (), deliverable: Any = None,
) -> dict[str, Any]:
    """raw 是 Reviewer 给的 highlights（已查过形状），没有时为 None；reads、writes 是这一轮工具调用的目标，delivered 是其中经
    deliver_file 交付给用户的文件（N-18）。这一轮什么也没有时是 {}。"""
    changed = list(dict.fromkeys(normalize_path(p) for p in writes))
    given = list(dict.fromkeys(normalize_path(p) for p in delivered))
    touched = {normalize_path(p) for p in reads} | set(changed) | set(given)
    out: dict[str, Any] = {}
    files = _files(raw, touched, changed, given)
    if files:
        out["files"] = files
    answers = [_clip(a) for a in (raw.get("answers") or [] if isinstance(raw, Mapping) else []) if isinstance(a, str) and a.strip()]
    if not answers:
        quoted = _quote(deliverable)
        answers = [_clip(quoted)] if quoted else []
    if answers:
        out["answers"] = answers[:MAX_ITEMS]
    return out
