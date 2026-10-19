"""S-04 验收：Stage Runner 的纯函数（设计书 3.2.3；附录 B.2、D.1、D.5、E、F.3；计划 S-04 难点）。

- 确定的 Node id：展开后的子图用“父 id + 序号”，重放和 trace 才对得上。
- 只传 result：输入引用只能指向上游 result、Task State 或常量。
- 单一写者、只读约束与扇出上限所依赖的 effect 排序与校验。
"""

from __future__ import annotations

import copy
import json
import re

import pytest

from agentruntime_worker.workflows import stage_plan as sp

from .support import SCHEMAS_DIR

TASK_ID = re.compile(r"^tsk_[0-7][0-9A-HJKMNP-TV-Z]{25}$")


def examples(schema: str) -> dict[str, dict]:
    data = json.loads((SCHEMAS_DIR / "examples" / f"{schema}.examples.json").read_text("utf-8"))
    return {e["name"]: copy.deepcopy(e["instance"]) for e in data["valid"]}


def atomic(node_id: str, *, after: list[str] | None = None, effect: str = "none", inputs: dict | None = None, **extra) -> dict:
    node = {
        "id": node_id,
        "kind": "atomic",
        "template": "t@v1",
        "executor": "program",
        "effect": effect,
        "role": "transform" if effect in ("none", "read") else "act",
        **extra,
    }
    if after is not None:
        node["after"] = after
    if inputs is not None:
        node["inputs"] = inputs
    return node


def stage(*nodes: dict) -> dict:
    return {"seq": 1, "goal": "g", "exit_when": "e", "nodes": list(nodes)}


# --- effect ---


def test_effect_order() -> None:
    ranks = [sp.effect_rank(e) for e in sp.EFFECT_ORDER]
    assert ranks == sorted(ranks) and len(set(ranks)) == 4
    with pytest.raises(ValueError):
        sp.effect_rank("delete_everything")


def test_node_effect_for_atomic_and_goal_nodes() -> None:
    assert sp.node_effect(atomic("a", effect="read"), "write_irreversible") == "read"
    assert sp.node_effect({"id": "g", "kind": "goal", "mode": "sub_agent"}, "write_irreversible") == "read", "sub_agent 默认只读"
    assert sp.node_effect({"id": "g", "kind": "goal", "mode": "tool_loop"}, "read") == "read"
    assert sp.node_effect({"id": "g", "kind": "goal", "mode": "tool_loop", "effect_ceiling": "write_reversible"}, "write_irreversible") == "write_reversible"


# --- Stage 校验 ---


@pytest.mark.parametrize("name", list(examples("stage")))
def test_schema_examples_are_valid_stages(name: str) -> None:
    assert sp.validate_stage(examples("stage")[name], max_nodes=50) == []


@pytest.mark.parametrize(
    ("bad", "needle"),
    [
        (stage(atomic("a"), atomic("a")), "a"),
        (stage(atomic("a", after=["missing"])), "missing"),
        (stage(atomic("a", after=["b"]), atomic("b", after=["a"])), "环"),
        (stage(atomic("a.1")), "a.1"),
        (stage(atomic("a"), atomic("b", inputs={"x": "$a.result.v"})), "a"),  # 引用的不是祖先
        (stage(atomic("a", effect="write_reversible", idempotency_key="k", quality={"type": "revise", "judge": "j@v1", "max_rounds": 2})), "quality"),
    ],
)
def test_invalid_stages_are_rejected(bad: dict, needle: str) -> None:
    errors = sp.validate_stage(bad, max_nodes=50)
    assert errors, "应当报错"
    assert any(needle in e for e in errors), errors


def test_reference_to_a_transitive_ancestor_is_allowed() -> None:
    ok = stage(atomic("a"), atomic("b", after=["a"]), atomic("c", after=["b"], inputs={"x": "$a.result.v"}))
    assert sp.validate_stage(ok, max_nodes=50) == []


def test_stage_larger_than_the_inline_limit_is_rejected() -> None:
    big = stage(*(atomic(f"n{i}") for i in range(11)))
    assert sp.validate_stage(big, max_nodes=10)
    assert sp.validate_stage(big, max_nodes=11) == []


# --- 输入引用 ---


RESULTS = {"q_region": {"rows": [{"region": "east", "rate": 0.12}]}, "analyze.3": {"text": "华东"}}
STATE = {"constraints": {"quarter": "2026Q3"}, "facts": ["华东退款率高"]}


def test_resolve_references_to_results_task_state_and_constants() -> None:
    value = {
        "rows": "$q_region.result.rows",
        "first_rate": "$q_region.result.rows.0.rate",
        "nested": ["$task.constraints.quarter", {"facts": "$task.facts"}],
        "expanded": "$analyze.3.result.text",
        "whole": "$q_region.result",
        "literal": "$$not_a_reference",
        "constant": 42,
    }
    resolved = sp.resolve_inputs(value, results=RESULTS, task_state=STATE)
    assert resolved == {
        "rows": [{"region": "east", "rate": 0.12}],
        "first_rate": 0.12,
        "nested": ["2026Q3", {"facts": ["华东退款率高"]}],
        "expanded": "华东",
        "whole": RESULTS["q_region"],
        "literal": "$not_a_reference",
        "constant": 42,
    }


def test_goal_and_item_references() -> None:
    resolved = sp.resolve_inputs(
        {"q": "$goal.quarter", "ticket": "$item.id", "whole": "$item"},
        results={},
        task_state={},
        goal_inputs={"quarter": "2026Q3"},
        item={"id": "T-1"},
    )
    assert resolved == {"q": "2026Q3", "ticket": "T-1", "whole": {"id": "T-1"}}


@pytest.mark.parametrize(
    "ref",
    [
        "$missing.result",  # 不存在的 Node
        "$q_region.result.nope",  # 不存在的字段
        "$q_region.rationale",  # 只能引用 result
        "$q_region.meta.cost_usd",
        "$task.constraints.region",
        "$goal.x",  # 没有 Goal 输入
    ],
)
def test_unresolvable_references_raise(ref: str) -> None:
    with pytest.raises(sp.UnresolvedReference):
        sp.resolve_inputs({"x": ref}, results=RESULTS, task_state=STATE)


# --- 确定的 Node id ---


def test_child_ids() -> None:
    assert sp.child_id("analyze", 3) == "analyze.3"
    assert sp.child_id("triage", 2, 1) == "triage.2.1"


def test_reindex_subgraph_numbers_nodes_and_rewrites_references() -> None:
    recipe = [
        atomic("fetch", inputs={"q": "$goal.quarter"}),
        atomic("clean", after=["fetch"], inputs={"rows": "$fetch.result.rows"}),
        atomic("summarize", after=["clean"], inputs={"rows": "$clean.result", "facts": "$task.facts"}),
    ]
    nodes, output = sp.reindex_subgraph("report", recipe, "summarize")
    assert [n["id"] for n in nodes] == ["report.1", "report.2", "report.3"]
    assert nodes[1]["after"] == ["report.1"] and nodes[1]["inputs"] == {"rows": "$report.1.result.rows"}
    assert nodes[2]["inputs"] == {"rows": "$report.2.result", "facts": "$task.facts"}
    assert nodes[0]["inputs"] == {"q": "$goal.quarter"}, "$goal 引用留到执行时用 Goal 的输入解析"
    assert output == "report.3"
    assert recipe[1]["id"] == "clean", "不改原 Recipe"
    with pytest.raises(ValueError):
        sp.reindex_subgraph("report", [atomic("x"), atomic("x")], "x")
    with pytest.raises(ValueError):
        sp.reindex_subgraph("report", [atomic("x", after=["y"])], "x")


def test_expand_per_item_numbers_chains_by_item() -> None:
    chain = [
        atomic("classify", inputs={"ticket": "$item.id"}),
        atomic("extract", after=["classify"], inputs={"label": "$classify.result.label", "ticket": "$item"}),
    ]
    chains, collect = sp.expand_per_item("triage", chain, [{"id": "T-1"}, {"id": "T-2"}], "extract")
    assert [[n["id"] for n in c] for c in chains] == [["triage.1.1", "triage.1.2"], ["triage.2.1", "triage.2.2"]]
    assert chains[1][0]["inputs"] == {"ticket": "T-2"}
    assert chains[1][1]["after"] == ["triage.2.1"]
    assert chains[1][1]["inputs"] == {"label": "$triage.2.1.result.label", "ticket": {"id": "T-2"}}
    assert collect == ["triage.1.2", "triage.2.2"]


def test_batches() -> None:
    assert sp.batches(10, 3) == [range(0, 3), range(3, 6), range(6, 9), range(9, 10)]
    assert sp.batches(0, 3) == []
    with pytest.raises(ValueError):
        sp.batches(3, 0)


def test_ulid_task_id_is_deterministic_and_well_formed() -> None:
    a = sp.ulid_task_id(1_790_000_000_000, 0x1234_5678_9ABC_DEF0_1234)
    assert a == sp.ulid_task_id(1_790_000_000_000, 0x1234_5678_9ABC_DEF0_1234)
    assert TASK_ID.match(a)
    assert sp.ulid_task_id(1_790_000_000_001, 0) > sp.ulid_task_id(1_790_000_000_000, 2**80 - 1), "时间戳在前，按时间排序"


def test_subworkflow_ids() -> None:
    assert sp.subworkflow_id("tsk_X", 2, "read_tickets") == "tsk_X/r2/read_tickets"
    assert sp.subworkflow_id("tsk_X", 1, "triage", "batch-3") == "tsk_X/r1/triage/batch-3"


# --- Hook ---


def test_default_node_hooks_follow_appendix_f3() -> None:
    assert sp.default_node_hooks(atomic("a"), "read") == {"before_node": ["budget_check"], "after_node": ["audit_log"]}
    assert sp.default_node_hooks(atomic("a", effect="read"), "read") == {"before_node": ["budget_check"], "after_node": ["trace_result", "audit_log"]}
    assert sp.default_node_hooks(atomic("a", effect="write_reversible"), "write_irreversible") == {
        "before_node": ["budget_check", "idempotency_check", "single_writer_lock"],
        "after_node": ["checkpoint", "audit_log"],
    }
    assert sp.default_node_hooks(atomic("a", effect="write_irreversible"), "write_irreversible") == {
        "before_node": ["budget_check", "idempotency_check"],
        "after_node": ["audit_log"],
    }


def test_apply_patch() -> None:
    doc = {"a": {"b": 1, "secret": "x"}, "list": [1, 2]}
    patched = sp.apply_patch(
        doc,
        [
            {"op": "test", "path": "/a/b", "value": 1},
            {"op": "replace", "path": "/a/b", "value": 2},
            {"op": "remove", "path": "/a/secret"},
            {"op": "add", "path": "/list/-", "value": 3},
            {"op": "copy", "from": "/a/b", "path": "/c"},
            {"op": "move", "from": "/c", "path": "/d"},
            {"op": "add", "path": "/e~1f", "value": True},
        ],
    )
    assert patched == {"a": {"b": 2}, "list": [1, 2, 3], "d": 2, "e/f": True}
    assert doc == {"a": {"b": 1, "secret": "x"}, "list": [1, 2]}, "不改原文档"
    with pytest.raises(sp.PatchError):
        sp.apply_patch(doc, [{"op": "test", "path": "/a/b", "value": 9}])
    with pytest.raises(sp.PatchError):
        sp.apply_patch(doc, [{"op": "remove", "path": "/missing"}])


# --- 投票、剪枝、重复错误 ---


def test_vote_picks_the_majority_and_breaks_ties_by_index() -> None:
    assert sp.vote([{"a": 1, "b": 2}, {"x": 0}, {"b": 2, "a": 1}]) == 0, "键的顺序不影响计票"
    assert sp.vote(["x", "y", "y"]) == 1
    assert sp.vote(["x", "y"]) == 0


def test_prune_keeps_the_best_scores() -> None:
    assert sp.prune([0.2, 0.9, 0.5, 0.9], 2) == [1, 3]
    assert sp.prune([0.2, 0.9, 0.5], 1) == [1]
    assert sp.prune([0.1], 3) == [0]


def test_repeated_error_detection() -> None:
    err = {"tool": "run_tests", "args": {}, "error": "AssertionError: 1 != 2"}
    ok = {"tool": "run_tests", "args": {}, "observation": "passed"}
    other = {"tool": "run_tests", "args": {}, "error": "SyntaxError"}
    assert sp.repeated_error([err, err, err])
    assert sp.repeated_error([ok, err, err, err])
    assert not sp.repeated_error([err, err])
    assert not sp.repeated_error([err, other, err])
    assert not sp.repeated_error([err, err, ok])
