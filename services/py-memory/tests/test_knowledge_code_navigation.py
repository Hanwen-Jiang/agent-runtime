import pytest

from agentruntime_memory.retrieval.engine import _prioritize_symbol


def candidates():
    return [
        {"id": "caller", "anchor": {"kind": "code_range", "symbol": "submit"}},
        {"id": "page", "anchor": {"kind": "paragraph", "symbol": "Run"}},
        {"id": "definition", "anchor": {"kind": "code_range", "symbol": "Run", "qualified_symbol": "Jobs.Run"}},
    ]


@pytest.mark.parametrize("query", [
    "Jobs.Run", "Where is `Jobs.Run` defined?", "Find the definition of `Jobs.Run`.",
    "定位 `Jobs.Run` 的定义。", "`Jobs.Run` 的定义在哪一行？",
])
def test_explicit_definition_navigation_promotes_an_existing_matching_code_hit(query):
    hits = _prioritize_symbol(candidates(), query)
    assert [hit["id"] for hit in hits] == ["definition", "caller", "page"]
    assert [hit["id"] for hit in hits if hit.get("symbol_exact_match")] == ["definition"]


@pytest.mark.parametrize("query", [
    "How does `Jobs.Run` retry work?", "Show callers of `Jobs.Run`.",
    "Find the definition of `Jobs.Run` and `submit`.",
    "Find the definition of `Jobs.Run()`.", "`Jobs.Run` 的调用方在哪？",
    "Where is `Missing` defined?", "Change the definition of `Jobs.Run`.",
])
def test_other_requests_keep_the_retriever_order(query):
    assert _prioritize_symbol(candidates(), query) == candidates()
