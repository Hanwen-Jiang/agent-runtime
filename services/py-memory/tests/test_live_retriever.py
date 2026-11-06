"""docs/plan/memory-live.md 第 5 节（F2）：对着已部署的 Memory Service 跑检索评测的检索器（agentruntime_memory.eval_retriever.live_retriever）。

用进程内的 gRPC 服务（开发后端，后台处理写入，同线上的独立运行）测：
- 写入走 SubmitCandidates，等后台处理完再查；检索走 planning 触发，结果映射回评测集的 id；
- 每个检索器用评测租户下自己的一个工作区，别的检索器写的查不到；aclose 时把自己写的逐条删掉；
- 地址或签名密钥没配时报出变量名，不带值；密钥不进 repr 与错误信息。
"""

from __future__ import annotations

import pytest

from agentruntime_memory import eval_retriever
from agentruntime_memory.identity import DevTokenAuthenticator
from agentruntime_memory.server import start_server
from agentruntime_memory.service import SearchQuery

from .memory_support import Backend

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
    retriever = eval_retriever.LiveMemoryRetriever(CORPUS, env=_env(address), single_writer_verified=True)
    try:
        got = await retriever.retrieve("payments service port 8443 TLS", 2)
        assert got and got[0] == {"id": "port", "text": CORPUS[0]["text"]}
        assert retriever.identity.tenant_id == "evals" and retriever.identity.workspace_id.startswith("w-eval-")
    finally:
        await retriever.aclose()


async def test_each_retriever_has_its_own_workspace_and_cleans_up(address: str, dev: Backend) -> None:
    first = eval_retriever.LiveMemoryRetriever(CORPUS, env=_env(address), single_writer_verified=True)
    second = eval_retriever.LiveMemoryRetriever([{"id": "other", "kind": "fact", "text": "The payments service listens on port 9000 in staging."}], env=_env(address), single_writer_verified=True)
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
    retriever = eval_retriever.LiveMemoryRetriever(CORPUS, env=_env(address), single_writer_verified=True)
    try:
        assert KEY not in repr(retriever) and KEY not in str(vars(retriever))
    finally:
        await retriever.aclose()


class _OfflineClient:
    """Exercise adapter lifecycle/fault boundaries without a listening socket."""

    def __init__(self, address, token):
        self.address = address
        self.items = {}
        self.deleted = []
        self.searches = []
        self.closed = False
        self.reject_last_corpus = False
        self.delete_failures = 0
        self.hold_queue = False

    async def submit_candidates(self, request):
        from google.protobuf import json_format
        from agentruntime.memory.v1 import memory_pb2
        for candidate in request.candidates:
            item = json_format.MessageToDict(candidate.memory_item.value)
            if not self.hold_queue and not (self.reject_last_corpus and "port 8443" in item["content"]["guidance"]):
                self.items[item["id"]] = item
        return memory_pb2.SubmitCandidatesResponse(accepted=len(request.candidates))

    async def search(self, request):
        from agentruntime.memory.v1 import memory_pb2
        self.searches.append(request.query)
        return memory_pb2.SearchResponse(items=[memory_pb2.RetrievedItem(memory_item=eval_retriever._payload(i)) for i in self.items.values()])

    async def delete(self, request):
        import grpc
        from agentruntime.memory.v1 import memory_pb2
        if self.delete_failures:
            self.delete_failures -= 1
            raise grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, (), (), "private transport detail")
        if request.memory_id not in self.items:
            raise grpc.aio.AioRpcError(grpc.StatusCode.NOT_FOUND, (), (), "not found")
        self.items.pop(request.memory_id)
        self.deleted.append(request.memory_id)
        return memory_pb2.DeleteResponse(deleted_memory_ids=[request.memory_id])

    async def aclose(self):
        self.closed = True


@pytest.fixture
def offline_retriever(monkeypatch):
    monkeypatch.setattr(eval_retriever, "MemoryServiceClient", _OfflineClient)
    return eval_retriever.LiveMemoryRetriever(CORPUS[:1], env=_env("offline"), wait_s=0.01, rpc_timeout_s=0.01, single_writer_verified=True)


async def test_offline_rejected_last_corpus_does_not_block_fifo_sentinel(offline_retriever) -> None:
    retriever = offline_retriever
    retriever._client.reject_last_corpus = True
    assert await retriever.retrieve("question", 50) == []
    assert retriever.diagnostics["pipeline_complete"] and not retriever.timed_out
    assert len(retriever._client.deleted) == 1, "sentinel is deleted before the scored search"
    await retriever.aclose()
    assert retriever.diagnostics["cleanup_complete"]
    assert len(retriever.diagnostics["not_found"]) == 1
    assert retriever._client.closed


async def test_offline_cleanup_retries_transport_failure_and_confirms_deletion(offline_retriever) -> None:
    retriever = offline_retriever
    assert await retriever.retrieve("question", 50)
    retriever._client.delete_failures = 1
    await retriever.aclose()
    assert retriever.diagnostics["cleanup_complete"] and not retriever._client.items
    assert not retriever.diagnostics["cleanup_errors"]


async def test_offline_unavailable_delete_is_not_mistaken_for_not_found(offline_retriever) -> None:
    retriever = offline_retriever
    await retriever.retrieve("question", 50)
    retriever._client.delete_failures = 3
    with pytest.raises(RuntimeError, match="cleanup_incomplete"):
        await retriever.aclose()
    assert not retriever.diagnostics["cleanup_complete"]
    assert list(retriever.diagnostics["cleanup_errors"].values()) == ["UNAVAILABLE"]
    assert retriever.diagnostics["pending_ids"] and retriever._client.closed
    assert "private transport detail" not in str(retriever.diagnostics)


async def test_offline_unprocessed_queue_not_found_does_not_prove_cleanup(offline_retriever) -> None:
    retriever = offline_retriever
    retriever._client.hold_queue = True
    with pytest.raises(TimeoutError, match="pipeline_timeout"):
        await retriever.retrieve("question", 50)
    with pytest.raises(RuntimeError, match="cleanup_incomplete"):
        await retriever.aclose()
    assert retriever.timed_out and not retriever.diagnostics["pipeline_complete"]
    assert len(retriever.diagnostics["pending_ids"]) == 2
    assert not retriever.diagnostics["not_found"]


def test_single_writer_evidence_rejects_replica_overlap_and_terminating_pods(tmp_path) -> None:
    import copy
    import json
    deployment = {"kind": "Deployment", "metadata": {"uid": "deployment", "generation": 2},
                  "spec": {"replicas": 1, "selector": {"matchLabels": {"component": "memory"}},
                           "template": {"spec": {"containers": [{"image": "memory:frozen"}]}}},
                  "status": {"replicas": 1, "readyReplicas": 1, "updatedReplicas": 1, "availableReplicas": 1, "observedGeneration": 2}}
    pod = {"kind": "Pod", "metadata": {"uid": "pod", "labels": {"component": "memory"}},
           "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}
    path = tmp_path / "topology.json"
    path.write_text(json.dumps({"items": [deployment, pod]}), "utf-8")
    assert eval_retriever.single_writer_evidence(str(path))["images"] == ["memory:frozen"]
    for bad in ([deployment, pod, copy.deepcopy(pod)],
                [deployment, {**pod, "metadata": {**pod["metadata"], "deletionTimestamp": "2026-11-06T00:00:00Z"}}],
                [{**deployment, "status": {**deployment["status"], "replicas": 2}}, pod]):
        path.write_text(json.dumps({"items": bad}), "utf-8")
        with pytest.raises(ValueError, match="topology"):
            eval_retriever.single_writer_evidence(str(path))


def test_missing_topology_refuses_before_creating_a_client(monkeypatch) -> None:
    def unexpected(*args):
        pytest.fail("must not contact live Memory before proving single writer")
    monkeypatch.setattr(eval_retriever, "MemoryServiceClient", unexpected)
    with pytest.raises(ValueError, match="AR_MEMORY_TOPOLOGY_EVIDENCE"):
        eval_retriever.LiveMemoryRetriever(CORPUS, env=_env("live"))


@pytest.fixture
async def inprocess_retriever_factory(monkeypatch, dev):
    """Run the real servicer, authenticator and storage without a TCP socket.
    The separate gRPC tests above still validate transport when sockets exist.
    """
    import grpc
    from agentruntime_memory.server import MemoryServicer
    servicer = MemoryServicer(dev.service, DevTokenAuthenticator(KEY.encode()), drain_in_background=True)

    class Client:
        def __init__(self, address, token):
            self.address, self.token = address, token

        def context(self, request):
            token = self.token(request.tenant)

            class Context:
                def invocation_metadata(self):
                    return (("authorization", f"Bearer {token}"),)

                async def abort(self, code, detail):
                    raise grpc.aio.AioRpcError(code, (), (), detail)
            return Context()

        async def submit_candidates(self, request):
            return await servicer.SubmitCandidates(request, self.context(request))

        async def search(self, request):
            return await servicer.Search(request, self.context(request))

        async def delete(self, request):
            return await servicer.Delete(request, self.context(request))

        async def aclose(self):
            pass

    monkeypatch.setattr(eval_retriever, "MemoryServiceClient", Client)
    yield lambda corpus: eval_retriever.LiveMemoryRetriever(corpus, env=_env("in-process"), single_writer_verified=True, wait_s=5)
    import asyncio
    if servicer._drains:
        await asyncio.gather(*servicer._drains)


async def test_inprocess_real_pipeline_isolated_workspaces_and_cleanup(inprocess_retriever_factory, dev) -> None:
    first = inprocess_retriever_factory(CORPUS)
    second = inprocess_retriever_factory([{"id": "other", "kind": "fact", "text": "The payments service listens on port 9000 in staging."}])
    try:
        assert await first.retrieve("payments service port 8443 TLS", 5)
        assert [g["id"] for g in await second.retrieve("payments service port", 5)] == ["other"]
        assert first.identity.workspace_id != second.identity.workspace_id
    finally:
        await second.aclose()
        await first.aclose()
    assert first.diagnostics["cleanup_complete"] and second.diagnostics["cleanup_complete"]
    assert not await dev.service.search(first.identity, SearchQuery(trigger="planning", query="payments service port", top_k=50))
    assert not await dev.service.search(second.identity, SearchQuery(trigger="planning", query="payments service port", top_k=50))
    for retriever in (first, second):
        ctx = await dev.service._tenant(retriever.identity)
        async with ctx.store.transaction(read_only=True) as tx:
            assert not await tx.query("SELECT seq FROM memory_queue"), "no queued writes remain after cleanup"


async def test_offline_write_receipt_is_checkpointed_before_server_accepts(offline_retriever) -> None:
    import copy
    retriever = offline_retriever
    receipts = []
    retriever.set_lifecycle_sink(lambda diagnostic: receipts.append(copy.deepcopy(diagnostic)))
    submit = retriever._client.submit_candidates

    async def checked(request):
        from google.protobuf import json_format
        candidate_ids = [json_format.MessageToDict(c.memory_item.value)["id"] for c in request.candidates]
        assert set(candidate_ids) <= set(receipts[-1]["submitted_ids"])
        assert receipts[-1]["workspace"] == retriever.identity.workspace_id
        return await submit(request)

    retriever._client.submit_candidates = checked
    await retriever.retrieve("question", 50)
    await retriever.aclose()
    assert receipts[-1]["cleanup_complete"]


def test_live_factory_honors_frozen_pipeline_deadline(monkeypatch) -> None:
    seen = {}
    def capture(corpus, **kwargs):
        seen.update(kwargs)
        return object()
    monkeypatch.setattr(eval_retriever, "LiveMemoryRetriever", capture)
    eval_retriever.live_retriever([], env={"AR_MEMORY_EVAL_WAIT_S": "1200"})
    assert seen["wait_s"] == 1200.0
    assert seen["tenant"] == "evals"
    eval_retriever.live_retriever([], env={"AR_MEMORY_EVAL_TENANT": "evalmem20261106"})
    assert seen["tenant"] == "evalmem20261106"
    with pytest.raises(ValueError, match="valid tenant"):
        eval_retriever.live_retriever([], env={"AR_MEMORY_EVAL_TENANT": "_platform"})


async def test_isolated_tenant_is_used_by_authenticated_memory_requests(address: str, dev: Backend) -> None:
    retriever = eval_retriever.LiveMemoryRetriever(CORPUS, env=_env(address), tenant="evalmem20261106", single_writer_verified=True)
    try:
        assert await retriever.retrieve("payments service port 8443 TLS", 5)
        assert retriever.identity.tenant_id == "evalmem20261106"
        assert retriever.diagnostics["tenant"] == "evalmem20261106"
    finally:
        await retriever.aclose()
    assert retriever.diagnostics["cleanup_complete"]
    assert not await dev.service.search(retriever.identity, SearchQuery(trigger="planning", query="payments service port", top_k=50))
