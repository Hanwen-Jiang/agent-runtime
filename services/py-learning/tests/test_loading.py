"""S-08：读取评测集时的校验（标注格式、DAG、参照输出与标注一致、Catalog 引用）。"""

from __future__ import annotations

import json

import pytest

from agentruntime_learning.evals.cases import EvalSet, EvalSetError, load_eval_set
from agentruntime_learning.evals.model import TemplateEntry

from support import atomic, checkpoint, reviewer_sample, stage, task_case, verdict


def problems(make_set, task_cases, samples=None) -> list[str]:
    with pytest.raises(EvalSetError) as exc:
        load_eval_set(make_set(task_cases, samples))
    return exc.value.problems


def test_a_valid_set_loads(make_set) -> None:
    root = make_set([task_case("t_one"), task_case("t_two", split="holdout")], [reviewer_sample("r_one")])
    es = load_eval_set(root)
    assert isinstance(es, EvalSet)
    assert [c.id for c in es.task_cases] == ["t_one", "t_two"]
    assert [s.id for s in es.reviewer_samples] == ["r_one"]
    assert es.catalog.templates["pay_order@v1"] == TemplateEntry(executor="program", effect="write_irreversible")
    assert es.catalog.recipes["refund_analysis@v3"] == "analytics.attribution"
    assert es.catalog.tools["card_charge"] == "write_irreversible"
    assert len(es.version) == 64


def test_version_is_stable_and_changes_with_content(make_set) -> None:
    root = make_set([task_case("t_one")])
    assert load_eval_set(root).version == load_eval_set(root).version
    path = root / "task_cases" / "t_one.json"
    data = json.loads(path.read_text("utf-8"))
    data["description"] = "改过的说明"
    path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    assert load_eval_set(root).version != load_eval_set(make_set([task_case("t_one")])).version


def test_only_keeps_one_split_and_unconfirmed_lists_drafts(make_set) -> None:
    confirmed = {"status": "confirmed", "confirmed_by": "reviewer", "confirmed_at": "2026-10-21"}
    root = make_set(
        [task_case("t_dev", review=confirmed), task_case("t_hold", split="holdout")],
        [reviewer_sample("r_dev"), reviewer_sample("r_hold", split="holdout", review=confirmed)],
    )
    es = load_eval_set(root)
    dev = es.only("dev")
    assert [c.id for c in dev.task_cases] == ["t_dev"] and [s.id for s in dev.reviewer_samples] == ["r_dev"]
    assert sorted(es.unconfirmed()) == ["r_dev", "t_hold"]
    assert es.task_cases[0].confirmed and not es.task_cases[1].confirmed


def test_a_file_must_match_the_case_schema(make_set) -> None:
    broken = task_case("t_broken")
    del broken["planner"]
    found = problems(make_set, [broken])
    assert any("t_broken.json" in p and "planner" in p for p in found), found


def test_confirmed_labels_name_the_reviewer_and_date(make_set) -> None:
    found = problems(make_set, [task_case("t_one", review={"status": "confirmed"})])
    assert any("t_one.json" in p for p in found), found


def test_id_must_match_the_file_name(make_set) -> None:
    root = make_set([task_case("t_one")])
    (root / "task_cases" / "t_one.json").rename(root / "task_cases" / "t_other.json")
    with pytest.raises(EvalSetError) as exc:
        load_eval_set(root)
    assert any("t_other.json" in p and "t_one" in p for p in exc.value.problems), exc.value.problems


def test_ids_are_unique_across_task_cases_and_reviewer_samples(make_set) -> None:
    found = problems(make_set, [task_case("same_id")], [reviewer_sample("same_id")])
    assert any("same_id" in p and "重复" in p for p in found), found


def test_kind_must_match_the_directory(make_set) -> None:
    root = make_set([task_case("t_one")])
    sample = reviewer_sample("r_misplaced")
    (root / "task_cases" / "r_misplaced.json").write_text(json.dumps(sample, ensure_ascii=False), "utf-8")
    with pytest.raises(EvalSetError) as exc:
        load_eval_set(root)
    assert any("r_misplaced.json" in p for p in exc.value.problems), exc.value.problems


def test_fixture_stages_must_be_valid_dags(make_set) -> None:
    bad_stage = stage(atomic("answer", "answer@v1", executor="model", effect="none", role="deliver", after=["missing"]))
    found = problems(make_set, [task_case("t_bad_round", rounds=[checkpoint(stage_=bad_stage)])])
    assert any("t_bad_round.json" in p and "missing" in p for p in found), found
    dup = stage(atomic("q", "sql_query@v1"), atomic("q", "sql_query@v1"))
    found = problems(make_set, [task_case("t_bad_reference", reference_stage=dup)])
    assert any("t_bad_reference.json" in p and "重复" in p for p in found), found


def test_results_only_cover_nodes_in_the_stage(make_set) -> None:
    results = {"ghost": {"role": "fetch", "status": "succeeded"}}
    found = problems(make_set, [task_case("t_ghost", rounds=[checkpoint("continue", results=results)])])
    assert any("t_ghost.json" in p and "ghost" in p for p in found), found


def test_reference_verdict_and_acceptable_agree_with_the_label(make_set) -> None:
    found = problems(make_set, [task_case("t_mismatch", rounds=[checkpoint("done", reference=verdict("continue"))])])
    assert any("t_mismatch.json" in p for p in found), found
    found = problems(make_set, [task_case("t_acceptable", rounds=[checkpoint("done", acceptable=["continue"])])])
    assert any("t_acceptable.json" in p for p in found), found


def test_reference_task_plan_agrees_with_the_triage_label(make_set) -> None:
    case = task_case("t_plan", triage="full", reference_plan={"triage": {"level": "replan"}, "horizon": "per_stage"})
    found = problems(make_set, [case])
    assert any("t_plan.json" in p and "replan" in p for p in found), found


def test_reference_outputs_only_use_the_catalog(make_set) -> None:
    invented = stage(atomic("answer", "made_up@v1", executor="model", effect="none", role="deliver"))
    found = problems(make_set, [task_case("t_invented", reference_stage=invented)])
    assert any("t_invented.json" in p and "made_up@v1" in p for p in found), found


def test_every_problem_is_listed_at_once(make_set) -> None:
    one = task_case("t_one")
    del one["rounds"]
    two = task_case("t_two")
    del two["planner"]
    found = problems(make_set, [one, two])
    assert any("t_one.json" in p for p in found) and any("t_two.json" in p for p in found), found
