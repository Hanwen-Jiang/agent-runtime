"""BFCL v4（capability.evals.bfcl）：工具定义的转换、AST 判分的各条规则、选题与出错。样本取自真实数据的格式。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentruntime_learning.capability.base import PinningGateway, role_enum
from agentruntime_learning.capability.evals import bfcl
from agentruntime_worker.executor.model_gateway import with_billed_cost

from capability_datasets_support import ScriptGateway, write_jsonl

CANDIDATE = "a6api/gpt-5.5@2026-10"

SIMPLE = {"id": "simple_python_0", "question": [[{"role": "user", "content": "Find the area of a triangle with a base of 10 units and height of 5 units."}]],
          "function": [{"name": "calculate_triangle_area", "description": "Calculate the area of a triangle given its base and height.",
                        "parameters": {"type": "dict", "properties": {"base": {"type": "integer", "description": "The base of the triangle."},
                                                                      "height": {"type": "integer", "description": "The height of the triangle."},
                                                                      "unit": {"type": "string", "description": "The unit of measure (defaults to 'units' if not specified)"}},
                                       "required": ["base", "height"]}}]}
SIMPLE_ANSWER = {"id": "simple_python_0", "ground_truth": [{"calculate_triangle_area": {"base": [10], "height": [5], "unit": ["units", ""]}}]}
FLOATS = {"id": "simple_python_9", "question": [[{"role": "user", "content": "Mean of 1.5 and 2."}]],
          "function": [{"name": "stats.mean", "description": "Mean of numbers.",
                        "parameters": {"type": "dict", "properties": {"numbers": {"type": "array", "items": {"type": "float"}, "description": "Numbers."},
                                                                      "precision": {"type": "float", "description": "Rounding."}},
                                       "required": ["numbers"]}}]}
FLOATS_ANSWER = {"id": "simple_python_9", "ground_truth": [{"stats.mean": {"numbers": [[1.5, 2.0]], "precision": ["", 0.1]}}]}
MULTIPLE = {"id": "multiple_2", "question": [[{"role": "user", "content": "What is the capital of Brazil?"}]],
            "function": [{"name": "country_info.largest_city", "description": "Fetch the largest city.", "parameters": {"type": "dict", "properties": {"country": {"type": "string", "description": "Country."}}, "required": ["country"]}},
                         {"name": "country_info.capital", "description": "Fetch the capital.", "parameters": {"type": "dict", "properties": {"country": {"type": "string", "description": "Country."}}, "required": ["country"]}}]}
MULTIPLE_ANSWER = {"id": "multiple_2", "ground_truth": [{"country_info.capital": {"country": ["Brazil"]}}]}
PARALLEL = {"id": "parallel_0", "question": [[{"role": "user", "content": "Play songs from the artists Taylor Swift and Maroon 5, with a play time of 20 minutes and 15 minutes respectively, on Spotify."}]],
            "function": [{"name": "spotify.play", "description": "Play specific tracks from a given artist for a specific time duration.",
                          "parameters": {"type": "dict", "properties": {"artist": {"type": "string", "description": "The artist whose songs you want to play."},
                                                                        "duration": {"type": "integer", "description": "The duration for which the songs should be played, in minutes."}},
                                         "required": ["artist", "duration"]}}]}
PARALLEL_ANSWER = {"id": "parallel_0", "ground_truth": [{"spotify.play": {"artist": ["Taylor Swift"], "duration": [20]}}, {"spotify.play": {"artist": ["Maroon 5"], "duration": [15]}}]}
IRRELEVANT = {"id": "irrelevance_0", "question": [[{"role": "user", "content": "Calculate the area of a triangle given the base is 10 meters and height is 5 meters."}]],
              "function": [{"name": "determine_body_mass_index", "description": "Calculate body mass index given weight and height.",
                            "parameters": {"type": "dict", "properties": {"weight": {"type": "float", "description": "Weight."}, "height": {"type": "float", "description": "Height."}}, "required": ["weight", "height"]}}]}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    write_jsonl(tmp_path / "BFCL_v4_simple_python.json", [SIMPLE, FLOATS])
    write_jsonl(tmp_path / "possible_answer" / "BFCL_v4_simple_python.json", [SIMPLE_ANSWER, FLOATS_ANSWER])
    write_jsonl(tmp_path / "BFCL_v4_multiple.json", [MULTIPLE])
    write_jsonl(tmp_path / "possible_answer" / "BFCL_v4_multiple.json", [MULTIPLE_ANSWER])
    write_jsonl(tmp_path / "BFCL_v4_parallel.json", [PARALLEL])
    write_jsonl(tmp_path / "possible_answer" / "BFCL_v4_parallel.json", [PARALLEL_ANSWER])
    write_jsonl(tmp_path / "BFCL_v4_irrelevance.json", [IRRELEVANT])
    return tmp_path


def make(root: Path, **kw: object) -> bfcl.BfclEval:
    return bfcl.make(root=root, categories=("simple", "multiple", "parallel", "irrelevance"), **kw)


async def run(ev: bfcl.BfclEval, item_id: str, *calls: tuple[str, dict]) -> tuple[float, dict, ScriptGateway]:
    gw = ScriptGateway(lambda r: {"tool_calls": list(calls)})
    item = next(i for i in ev.items(100, 0) if i.id == item_id)
    res = await ev.run_item(item, gw, candidate=CANDIDATE)
    assert res.error is None, res.error
    return res.score, res.record, gw


def test_items_are_deterministic_nested_and_spread_over_categories(root: Path) -> None:
    ev = make(root)
    first = [i.id for i in ev.items(3, seed=7)]
    assert first == [i.id for i in make(root).items(3, seed=7)]
    assert [i.id for i in ev.items(5, seed=7)][:3] == first, "n 变大只在后面追加"
    assert len({ev.rows()[i]["category"] for i in first}) == 3, "各类别轮流取"
    assert sorted(i.id for i in ev.items(99, seed=1)) == sorted(ev.rows())


async def test_the_request_uses_bfcl_function_calling_tools(root: Path) -> None:
    score, record, gw = await run(make(root), "multiple_2", ("country_info_capital", {"country": "brazil"}))
    assert score == 1.0 and record["check"]["valid"]
    req = gw.requests[0]
    assert req.call.role == role_enum("act") and req.tenant.tenant_id == "evals"
    assert [t.name for t in req.generation.tools] == ["country_info_largest_city", "country_info_capital"], "名字里的 . 换成 _"
    assert req.generation.tools[0].description.endswith("Note that the provided function is in Python 3 syntax.")
    assert req.generation.tools[0].params_schema.value.struct_value["type"] == "object"
    assert [m.text for m in req.generation.messages] == ["What is the capital of Brazil?"]


def test_float_parameters_become_numbers_with_the_bfcl_note() -> None:
    tool = bfcl.to_tool(FLOATS["function"][0])
    props = tool["parameters"]["properties"]
    assert props["precision"] == {"type": "number", "format": "float", "description": "Rounding. This is a float type value."}
    assert props["numbers"]["items"]["type"] == "number"
    assert FLOATS["function"][0]["parameters"]["type"] == "dict", "不改原题"


async def test_integers_come_back_from_struct_floats(root: Path) -> None:
    score, record, _ = await run(make(root), "simple_python_0", ("calculate_triangle_area", {"base": 10, "height": 5}))
    assert score == 1.0 and record["output"] == [{"calculate_triangle_area": {"base": 10, "height": 5}}]
    score, record, _ = await run(make(root), "simple_python_0", ("calculate_triangle_area", {"base": 10.5, "height": 5}))
    assert score == 0.0 and record["check"]["error_type"] == "type_error:simple"


async def test_float_lists_and_optional_parameters(root: Path) -> None:
    assert (await run(make(root), "simple_python_9", ("stats_mean", {"numbers": [1.5, 2]})))[0] == 1.0
    assert (await run(make(root), "simple_python_9", ("stats_mean", {"numbers": [1.5, 2], "precision": 0.1})))[0] == 1.0
    _, record, _ = await run(make(root), "simple_python_9", ("stats_mean", {"numbers": [1.5, 3]}))
    assert record["check"]["error_type"] == "value_error:list/tuple"


@pytest.mark.parametrize(("args", "error_type"), [
    ({"base": 10}, "simple_function_checker:missing_required"),
    ({"base": 10, "height": 5, "colour": "red"}, "simple_function_checker:unexpected_param"),
    ({"base": 10, "height": 6}, "value_error:others"),
    ({"base": 10, "height": 5, "unit": "cm"}, "value_error:string"),
])
async def test_ast_rules_reject(root: Path, args: dict, error_type: str) -> None:
    score, record, _ = await run(make(root), "simple_python_0", ("calculate_triangle_area", args))
    assert score == 0.0 and record["check"]["error_type"] == error_type


async def test_simple_wants_exactly_one_call_with_the_right_name(root: Path) -> None:
    _, record, _ = await run(make(root), "simple_python_0")
    assert record["check"]["error_type"] == "simple_function_checker:wrong_count"
    _, record, _ = await run(make(root), "multiple_2", ("country_info_largest_city", {"country": "Brazil"}))
    assert record["check"]["error_type"] == "simple_function_checker:wrong_func_name"


async def test_parallel_matches_in_any_order_and_normalizes_strings(root: Path) -> None:
    calls = (("spotify_play", {"artist": "maroon 5", "duration": 15}), ("spotify_play", {"artist": "Taylor-Swift", "duration": 20}))
    assert (await run(make(root), "parallel_0", *calls))[0] == 1.0
    assert (await run(make(root), "parallel_0", calls[0]))[0] == 0.0
    _, record, _ = await run(make(root), "parallel_0", calls[0], ("spotify_play", {"artist": "Taylor Swift", "duration": 25}))
    assert record["check"]["error_type"] == "parallel_function_checker_no_order:cannot_find_match"


async def test_irrelevance_passes_only_without_a_call(root: Path) -> None:
    assert (await run(make(root), "irrelevance_0"))[0] == 1.0
    assert (await run(make(root), "irrelevance_0", ("determine_body_mass_index", {"weight": 70, "height": 1.8})))[0] == 0.0


async def test_a_failed_call_is_an_error_with_its_billed_cost(root: Path) -> None:
    ev = make(root)
    gw = ScriptGateway(lambda r: with_billed_cost(RuntimeError("provider down"), 1500))
    res = await ev.run_item(ev.items(1, 0)[0], gw, candidate=CANDIDATE)
    assert res.score == 0.0 and res.error == "RuntimeError: provider down" and res.cost_usd == pytest.approx(0.0015)


async def test_pinning_and_contamination_flow_through(root: Path) -> None:
    ev = make(root)
    item = next(i for i in ev.items(100, 0) if i.id == "multiple_2")
    inner = ScriptGateway(lambda r: {"tool_calls": [("country_info_capital", {"country": "Brazil"})]}, cost_micros=2000)
    res = await ev.run_item(item, PinningGateway(inner, role="act", candidate=CANDIDATE), candidate=CANDIDATE)
    assert inner.requests[0].call.pinned_model_profile == CANDIDATE and not res.contaminated
    assert res.cost_usd == pytest.approx(0.002) and res.record["calls"][0]["model"] == CANDIDATE

    switched = ScriptGateway(lambda r: {"tool_calls": [("country_info_capital", {"country": "Brazil"})]})
    switched.answer = lambda r: _served_by(r, "a6api/other@1")
    res = await ev.run_item(item, PinningGateway(switched, role="act", candidate=CANDIDATE), candidate=CANDIDATE)
    assert res.contaminated


def _served_by(request, profile):  # type: ignore[no-untyped-def]
    from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg

    out = mg.GenerateResponse(model=mg.ModelChoice(model_profile_id=profile))
    out.tool_calls.add(id="c", name="country_info_capital").arguments.update({"country": "Brazil"})
    return out


def test_unknown_category_and_the_pinned_source() -> None:
    with pytest.raises(ValueError):
        bfcl.make(categories=("multi_turn_base",)).rows()
    assert bfcl.make().version == f"gorilla@{bfcl.COMMIT[:7]}" and bfcl.make().role == "act"
