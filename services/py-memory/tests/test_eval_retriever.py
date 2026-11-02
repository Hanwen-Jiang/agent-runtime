"""docs/plan/feedback-2026-11-01.md N-13：记忆检索评测（Ragas）用的检索器 agentruntime_memory.eval_retriever。

- 评测集的条目写成合乎 schema、评测身份能直接写的条目，走 submit → drain 的写入流水线；
- 检索走 planning 触发，结果映射回评测集的 id；
- 后端是临时目录里的开发后端，aclose 后删掉。
"""

from __future__ import annotations

from pathlib import Path

from agentruntime_memory.eval_retriever import IDENTITY, MemoryEvalRetriever, memory_item
from agentruntime_memory.scopes import can_write
from agentruntime_memory.service import schema_valid

CORPUS = [
    {"id": "tests", "kind": "recipe", "text": "To run this repository's tests, use uv run pytest from the repository root."},
    {"id": "port", "kind": "fact", "text": "The payments service listens on port 8443 and only accepts TLS connections."},
    {"id": "lang", "kind": "preference", "text": "The user prefers answers in Chinese with code identifiers left in English."},
]


def test_items_are_valid_and_writable() -> None:
    for n, entry in enumerate(CORPUS, 1):
        item = memory_item(n, entry)
        assert schema_valid(item) and can_write(IDENTITY, item["scope"])


async def test_retrieval_goes_through_the_real_service() -> None:
    retriever = MemoryEvalRetriever(CORPUS, env={})
    got = await retriever.retrieve("payments service port 8443 TLS", 2)
    assert got and got[0]["id"] == "port" and got[0]["text"] == CORPUS[1]["text"]
    assert retriever.report is not None and len(retriever.report.accepted) == 3, "三条都过了写入流水线"
    workdir = Path(retriever._dir or "")
    await retriever.aclose()
    assert not workdir.exists(), "临时的后端删掉了"
