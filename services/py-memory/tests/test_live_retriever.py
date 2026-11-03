"""docs/plan/memory-live.md 第 5 节（F2）：对着已部署的 Memory Service 跑检索评测的检索器（agentruntime_memory.eval_retriever.live_retriever）。

用进程内的 gRPC 服务（开发后端，后台处理写入，同线上的独立运行）测：
- 写入走 SubmitCandidates，等后台处理完再查；检索走 planning 触发，结果映射回评测集的 id；
- 每个检索器用评测租户下自己的一个工作区，别的检索器写的查不到；aclose 时把自己写的逐条删掉；
- 地址或签名密钥没配时报出变量名，不带值；密钥不进 repr 与错误信息。
实现还没有时整个文件跳过。
"""

from __future__ import annotations

import pytest

from agentruntime_memory import eval_retriever
from agentruntime_memory.identity import DevTokenAuthenticator
from agentruntime_memory.server import start_server
from agentruntime_memory.service import SearchQuery

from .memory_support import Backend

if not hasattr(eval_retriever, "live_retriever"):
    pytest.skip("live_retriever 还没写：先审验收测试（docs/plan/memory-live.md 第 5 节）", allow_module_level=True)

KEY = "memory-live-retriever-test-key"
CORPUS = [
    {"id": "port", "kind": "fact", "text": "The payments service listens on port 8443 and only accepts TLS connections."},
    {"id": "tests", "kind": "recipe", "text": "To run this repository's tests, use uv run pytest from the repository root."},
]


@pytest.fixture
async def address(dev: Backend):
    srv, port = await start_server(dev.service, DevTokenAuthenticator(KEY.encode()), "127.0.0.1:0", drain_in_background=True)
    yield f"127.0.0.1:{port}"
    await srv.stop(grace=None)


def _env(address: str) -> dict[str, str]:
    return {"AR_MEMORY_ADDRESS": address, "AR_DEV_SIGNING_KEY": KEY}


async def test_writes_wait_for_the_pipeline_and_search_maps_back_to_corpus_ids(address: str) -> None:
    retriever = eval_retriever.LiveMemoryRetriever(CORPUS, env=_env(address))
    try:
        got = await retriever.retrieve("payments service port 8443 TLS", 2)
        assert got and got[0] == {"id": "port", "text": CORPUS[0]["text"]}
        assert retriever.identity.tenant_id == "evals" and retriever.identity.workspace_id.startswith("w-eval-")
    finally:
        await retriever.aclose()


async def test_each_retriever_has_its_own_workspace_and_cleans_up(address: str, dev: Backend) -> None:
    first = eval_retriever.LiveMemoryRetriever(CORPUS, env=_env(address))
    second = eval_retriever.LiveMemoryRetriever([{"id": "other", "kind": "fact", "text": "The payments service listens on port 9000 in staging."}], env=_env(address))
    try:
        await first.retrieve("payments service port", 5)
        got = await second.retrieve("payments service port", 5)
        assert [g["id"] for g in got] == ["other"], "别的工作区写的查不到"
        assert first.identity.workspace_id != second.identity.workspace_id
    finally:
        await second.aclose()
    identity = first.identity
    await first.aclose()
    left = await dev.service.search(identity, SearchQuery(trigger="planning", query="payments service port 8443 TLS", top_k=10))
    assert left == [], "aclose 把这个检索器写入的记忆都删了"


def test_missing_configuration_names_the_variables_without_values() -> None:
    with pytest.raises(ValueError) as err:
        eval_retriever.live_retriever(CORPUS, env={"AR_DEV_SIGNING_KEY": KEY})
    assert "AR_MEMORY_ADDRESS" in str(err.value) and KEY not in str(err.value)
    with pytest.raises(ValueError) as err:
        eval_retriever.live_retriever(CORPUS, env={"AR_MEMORY_ADDRESS": "127.0.0.1:1"})
    assert "AR_DEV_SIGNING_KEY" in str(err.value)


async def test_the_signing_key_stays_out_of_repr(address: str) -> None:
    retriever = eval_retriever.live_retriever(CORPUS, env=_env(address))
    try:
        assert KEY not in repr(retriever) and KEY not in str(vars(retriever))
    finally:
        await retriever.aclose()
