"""评测网站（docs/plan/eval-site.md）：/datasets 的数据。

agentruntime_learning.evals.site_catalog 从 evalsets 目录读出每个数据集的题：题目、答案、来源、人审状态与展开的细节，
按 evalsets/site.json 的说明归类（评测类别、来源类型、框架）。守住的规则：

- 留出集的题只给题量：内容（题目、答案、id、来源）不出现在任何输出里；派生题按 group_id 跟着原题走。
- 每题必须有来源：自编题写自编与工作项，改编与原题写上游与版本；缺来源的题不出现，记进 excluded。
- 草稿照常列出，标未审；已人审的带确认人与日期。
- 能按评测类别（默认）、来源类型、框架、人审状态、数据集分组。

前半部分用 tmp_path 里造的小目录定行为；最后两条对着仓库里真正的 evalsets 跑。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentruntime_learning.evals.site_catalog import GROUPINGS, build_catalog

REPO_EVALSETS = Path(__file__).resolve().parents[1] / "evalsets"
SECRET_PROMPT = "HOLDOUT-ONLY prompt text 7c1e"
SECRET_ANSWER = "HOLDOUT-ONLY answer text 9f2a"


def task_case(cid: str, split: str, review: dict[str, Any], message: str, triage: str = "full", reply: str = "done.") -> dict[str, Any]:
    return {
        "kind": "task", "id": cid, "split": split, "category": "general", "review": review,
        "description": f"case {cid}",
        "planner": {"input": {"message": message, "context": {"risk_before": "none", "effect_ceiling": "none"}},
                    "expect": {"triage": triage, "risk": "none", "shape": {"deliver": True}}},
        "rounds": [{"task_state": {"goal": message}, "stage": {"seq": 1, "goal": "g", "exit_when": "e", "nodes": []},
                    "results": {"reply": {"role": "deliver", "status": "succeeded", "result": {"text": reply}}},
                    "expect": {"verdict": "done", "acceptable": ["done"]}}],
        "rubric": ["r"],
    }


def write(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".jsonl":
        path.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in doc), "utf-8")
    else:
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), "utf-8")


CONFIRMED = {"status": "confirmed", "confirmed_by": "Hanwen-Jiang", "confirmed_at": "2026-10-20"}
DRAFT = {"status": "draft", "notes": "AI 起草"}


@pytest.fixture
def evalsets(tmp_path: Path) -> Path:
    root = tmp_path / "evalsets"
    # 自编、已人审：两条 dev，一条 holdout
    write(root / "own" / "task_cases" / "a.json", task_case("own_a", "dev", CONFIRMED, "plan my week"))
    write(root / "own" / "task_cases" / "b.json", task_case("own_b", "dev", CONFIRMED, "fix the build", triage="fast"))
    write(root / "own" / "task_cases" / "h.json", task_case("own_holdout", "holdout", CONFIRMED, SECRET_PROMPT, reply=SECRET_ANSWER))
    # 改编、草稿：两条有来源，一条缺来源；一条派生题
    write(root / "adapted" / "task_cases" / "x.json", task_case("adp_x", "dev", DRAFT, "visit 3 cities"))
    write(root / "adapted" / "task_cases" / "x_fast.json", task_case("adp_x_followup", "dev", DRAFT, "which city first?", triage="fast"))
    write(root / "adapted" / "task_cases" / "y.json", task_case("adp_no_source", "dev", DRAFT, "no source here"))
    write(root / "adapted" / "sources.jsonl", [
        {"id": "adp_x", "dataset": "NATURAL PLAN", "upstream_repository": "https://github.com/google-deepmind/natural-plan",
         "revision": "ca76db3", "source_id": "trip_planning_example_0", "license": "Apache-2.0",
         "source_fields": {"golden_plan": "Day 1-5 Helsinki"}, "confirmed": False},
        {"id": "adp_x_followup", "dataset": "NATURAL PLAN", "upstream_repository": "https://github.com/google-deepmind/natural-plan",
         "revision": "ca76db3", "source_id": "trip_planning_example_0", "license": "Apache-2.0", "derived_from": "adp_x",
         "group_id": "adp_x", "confirmed": False},
    ])
    write(root / "site.json", {
        "frameworks": [
            {"id": "task-loop", "kind": "authored", "title": {"zh": "Task Loop 评测框架", "en": "Task Loop harness"}, "why": {"zh": "自研", "en": "in-house"}},
        ],
        "datasets": [
            {"name": "own", "path": "own", "source_type": "authored", "framework": "task-loop", "eval_category": "planning",
             "work_item": "S-09", "title": {"zh": "自编", "en": "authored"}},
            {"name": "adapted", "path": "adapted", "source_type": "adapted", "framework": "task-loop", "eval_category": "planning",
             "title": {"zh": "改编", "en": "adapted"}},
        ],
    })
    return root


def every_text(doc: Any) -> str:
    return json.dumps(doc, ensure_ascii=False)


def test_the_index_lists_every_dataset_with_counts(evalsets: Path) -> None:
    catalog = build_catalog(evalsets)
    index = catalog.index()
    by_name = {d["name"]: d for d in index["datasets"]}
    assert set(by_name) == {"own", "adapted"}
    own = by_name["own"]
    assert own["source_type"] == "authored" and own["framework"] == "task-loop" and own["eval_category"] == "planning"
    assert own["counts"] == {"total": 3, "shown": 2, "holdout_hidden": 1, "confirmed": 3, "draft": 0, "excluded": 0}
    assert by_name["adapted"]["counts"] == {"total": 3, "shown": 2, "holdout_hidden": 0, "confirmed": 0, "draft": 3, "excluded": 1}
    assert index["frameworks"][0]["id"] == "task-loop", "框架介绍来自 site.json"
    assert set(GROUPINGS) == {"eval_category", "source_type", "framework", "review", "dataset"}
    assert index["default_grouping"] == "eval_category"


def test_holdout_content_never_leaves_the_server(evalsets: Path) -> None:
    catalog = build_catalog(evalsets)
    outputs = [catalog.index(), *(catalog.dataset(d["name"]) for d in catalog.index()["datasets"])]
    for doc in outputs:
        text = every_text(doc)
        assert SECRET_PROMPT not in text and SECRET_ANSWER not in text, "留出集的题目与答案不出服务端"
        assert "own_holdout" not in text, "连 id 也不给"
    own = catalog.dataset("own")
    assert own["holdout"] == {"count": 1}
    assert {i["id"] for i in own["items"]} == {"own_a", "own_b"}


def test_each_item_has_question_answer_source_and_review(evalsets: Path) -> None:
    catalog = build_catalog(evalsets)
    [item] = [i for i in catalog.dataset("own")["items"] if i["id"] == "own_a"]
    assert item["question"] == "plan my week"
    assert item["answer"]["triage"] == "full", "自编的分诊题：答案是标注的分诊与形状"
    assert item["source"] == {"kind": "authored", "work_item": "S-09"}
    assert item["review"] == {"status": "confirmed", "confirmed_by": "Hanwen-Jiang", "confirmed_at": "2026-10-20"}
    assert item["eval_category"] == "planning" and item["source_type"] == "authored" and item["framework"] == "task-loop"
    assert item["detail"]["rubric"] == ["r"], "展开时有判分标准与完整的用例"
    [adapted] = [i for i in catalog.dataset("adapted")["items"] if i["id"] == "adp_x"]
    assert adapted["source"]["kind"] == "upstream"
    assert adapted["source"]["dataset"] == "NATURAL PLAN" and adapted["source"]["revision"] == "ca76db3"
    assert adapted["source"]["license"] == "Apache-2.0" and adapted["source"]["url"].startswith("https://github.com/google-deepmind/natural-plan")
    assert adapted["review"]["status"] == "draft", "草稿照常列出，标未审"


def test_derived_items_point_at_their_original(evalsets: Path) -> None:
    items = {i["id"]: i for i in build_catalog(evalsets).dataset("adapted")["items"]}
    assert items["adp_x_followup"]["group_id"] == "adp_x"
    assert items["adp_x"]["group_id"] == "adp_x", "原题自己就是一组"


def test_items_without_a_source_are_left_out_and_reported(evalsets: Path) -> None:
    catalog = build_catalog(evalsets)
    assert "adp_no_source" not in every_text(catalog.dataset("adapted")["items"])
    assert {"dataset": "adapted", "id": "adp_no_source", "reason": "no_source"} in catalog.index()["excluded"]


def test_unknown_dataset_is_none(evalsets: Path) -> None:
    catalog = build_catalog(evalsets)
    assert catalog.dataset("nope") is None
    assert catalog.dataset("../own") is None


@pytest.mark.parametrize("grouping", ["eval_category", "source_type", "framework", "review", "dataset"])
def test_items_carry_every_grouping_key(evalsets: Path, grouping: str) -> None:
    catalog = build_catalog(evalsets)
    for d in catalog.index()["datasets"]:
        for item in catalog.dataset(d["name"])["items"]:
            value = item["review"]["status"] if grouping == "review" else item[grouping]
            assert value, f"{item['id']} 缺 {grouping}"


def test_a_dataset_missing_from_site_json_is_not_served(evalsets: Path) -> None:
    write(evalsets / "stray" / "task_cases" / "s.json", task_case("stray_1", "dev", DRAFT, "stray"))
    catalog = build_catalog(evalsets)
    assert "stray" not in {d["name"] for d in catalog.index()["datasets"]}, "没在 site.json 里说明的目录不上页面"


# --- 对着仓库里真正的 evalsets ---


def test_the_repository_evalsets_build_and_every_dataset_is_described() -> None:
    catalog = build_catalog(REPO_EVALSETS)
    index = catalog.index()
    names = {d["name"] for d in index["datasets"]}
    for expected in ("task-loop-tasks", "task-loop-reviewer", "task-loop-context", "session-drafts", "deliverables",
                     "memory-rag", "adapted-natural-plan", "adapted-travelplanner", "adapted-swebench"):
        assert expected in names, f"site.json 要说明 {expected}"
    assert not index["excluded"], f"仓库里的题都要有来源：{index['excluded']}"
    assert {d["source_type"] for d in index["datasets"]} >= {"authored", "adapted"}


def test_no_repository_holdout_content_is_served() -> None:
    catalog = build_catalog(REPO_EVALSETS)
    holdout_ids = set()
    for path in REPO_EVALSETS.rglob("*.json"):
        try:
            doc = json.loads(path.read_text("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(doc, dict) and doc.get("split") == "holdout" and doc.get("id"):
            holdout_ids.add(doc["id"])
    assert holdout_ids, "仓库里有留出集"
    served = every_text([catalog.index(), *(catalog.dataset(d["name"]) for d in catalog.index()["datasets"])])
    leaked = sorted(i for i in holdout_ids if f'"{i}"' in served)
    assert not leaked, f"留出集的题出现在输出里：{leaked}"
