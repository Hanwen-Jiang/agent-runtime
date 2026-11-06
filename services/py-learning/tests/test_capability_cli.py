"""能力矩阵的命令行与发布：evals 的 --pin、capability 的 estimate/run/score/publish、capability_scores 的合并与校验。"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import capability_support as cs
from agentruntime_learning.capability import publish as pb
from agentruntime_learning.capability.publication import POLICY_VERSION
from agentruntime_learning.capability.__main__ import main as capability_main
from agentruntime_learning.evals.__main__ import main as evals_main
from agentruntime_learning.evals.worker_subject import ENV_GATEWAY_FACTORY

SYNC = cs.REPO / "scripts" / "sync_model_profiles.py"


@pytest.fixture(autouse=True)
def _fresh_evals() -> None:
    cs.reset()


# --- evals 的 --pin ---


def test_pin_on_eval_run_reports_pinned_and_contaminated_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv(ENV_GATEWAY_FACTORY, "capability_support:serving")
    args = ["run", "--subject", "agentruntime_learning.evals.worker_subject:planner_subject", "--reports-dir", str(tmp_path), "--compare", "none"]
    assert evals_main([*args, "--pin", f"plan={cs.OPUS}"]) == 0
    out = capsys.readouterr().out
    report = json.loads(next(tmp_path.glob("*.json")).read_text("utf-8"))
    assert report["pin"]["role"] == "plan" and report["pin"]["profile"] == cs.OPUS
    assert report["pin"]["pinned_calls"] > 0 and report["pin"]["contaminated_calls"] == 0
    assert report["models"]["plan"] == {cs.OPUS: report["pin"]["pinned_calls"]}, "规划都落在钉的模型上"
    assert set(report["models"]["route"]) == {cs.MINI}, "其他角色照常选档"
    assert f"钉住 plan = {cs.OPUS}" in out

    monkeypatch.setenv(ENV_GATEWAY_FACTORY, "capability_support:serving_switched")
    assert evals_main([*args, "--pin", f"plan={cs.OPUS}", "--reports-dir", str(tmp_path / "switched")]) == 0
    switched = json.loads(next((tmp_path / "switched").glob("*.json")).read_text("utf-8"))
    assert switched["pin"]["contaminated_calls"] == switched["pin"]["pinned_calls"] > 0
    assert "被污染" in capsys.readouterr().out


def test_pin_needs_a_subject_that_goes_through_the_gateway(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert evals_main(["run", "--subject", "baseline", "--reports-dir", str(tmp_path), "--compare", "none", "--pin", f"plan={cs.OPUS}"]) == 2
    assert "Model Gateway" in capsys.readouterr().err
    assert evals_main(["run", "--subject", "baseline", "--pin", "planner=x"]) == 2


def test_pin_on_the_context_eval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv(ENV_GATEWAY_FACTORY, "capability_support:serving_context")
    code = evals_main(["context", "--subject", "agentruntime_learning.evals.worker_subject:context_subject", "--pin", f"transform={cs.MINI}", "--reports-dir", str(tmp_path)])
    assert code == 0
    report = json.loads(next(tmp_path.glob("context-*.json")).read_text("utf-8"))
    assert report["pin"]["role"] == "transform" and report["pin"]["pinned_calls"] > 0 and report["pin"]["contaminated_calls"] == 0
    assert f"钉住 transform = {cs.MINI}" in capsys.readouterr().out


# --- 发布 ---


def score_report(entries: list[tuple[str, float, bool]], *, role: str = "plan", tier: str = "medium", at: str = "2026-11-10") -> dict[str, Any]:
    return {
        "plan": "round1",
        "cells": [
            {
                "task_family": "general.unclassified",
                "role": role,
                "tier": tier,
                "evals": [{"name": "s09", "version": "abc"}, {"name": "bfcl-v4", "version": "v4"}],
                "at": at,
                "leader": entries[0][0],
                "problems": [],
                "publication": {"policy": POLICY_VERSION, "evidence_sha256": "a" * 64, "coverage_ok": True},
                "candidates": [{"candidate": c, "score": s, "publishable": ok, "items": 60, "repeats": 3} for c, s, ok in entries],
            }
        ],
    }


def profile(pid: str, scores: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    p = {
        "id": pid,
        "vendor": "openai",
        "model": pid.split("/")[1].split("@")[0],
        "class": "strong",
        "tiers": [{"name": "medium", "effort": "medium", "thinking_budget": 8192}],
        "capabilities": ["generate"],
        "cost": {"in_per_m": 1.0, "out_per_m": 2.0, "cache_read_per_m": 0.1},
        "freshness": {"price_at": "2026-11-02"},
    }
    if scores is not None:
        p["capability_scores"] = scores
    return p


def entry(role: str = "plan", score: float = 0.6, at: str = "2026-11-01", family: str = "general.unclassified") -> dict[str, Any]:
    return {"task_family": family, "role": role, "tier": "medium", "score": score, "source": "old", "at": at}


def test_only_publishable_scores_become_entries_with_a_traceable_source() -> None:
    patch = pb.patch_from_scores(score_report([("a6api/a@1", 0.7123456, True), ("a6api/b@1", 0.5, False)]))
    assert list(patch) == ["a6api/a@1"]
    (e,) = patch["a6api/a@1"]
    assert e == {"task_family": "general.unclassified", "role": "plan", "tier": "medium", "score": 0.7123,
                 "source": "capmatrix:s09+bfcl-v4@2026-11-10:n=60,r=3:run=round1:evidence=" + "a" * 64, "at": "2026-11-10"}


def test_merge_replaces_the_same_dimension_and_drops_old_entries() -> None:
    today = dt.date(2026, 11, 10)
    profiles = [
        profile("a6api/a@1", [entry("plan", 0.6), entry("judge", 0.4, at="2026-07-01"), entry("route", 0.5, at="2026-09-01")]),
        profile("a6api/b@1", [entry("plan", 0.3, at="2026-08-01")]),
    ]
    patch = {"a6api/a@1": [{**entry("plan", 0.8, at="2026-11-10"), "source": "new"}]}
    merged, notes = pb.merge(profiles, patch, today=today)
    a, b = merged
    assert [(e["role"], e["score"]) for e in a["capability_scores"]] == [("plan", 0.8), ("route", 0.5)], "同维度替换，超过 90 天的删掉"
    assert "capability_scores" not in b, "全删光了就去掉这个键"
    assert any("0.6" in n and "0.8" in n for n in notes) and any("过期" in n for n in notes)
    assert profiles[0]["capability_scores"][0]["score"] == 0.6, "纯函数，不改输入"
    with pytest.raises(pb.PublishError, match="不在文件里"):
        pb.merge(profiles, {"a6api/zzz@1": []}, today=today)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda p: p["capability_scores"].append(dict(p["capability_scores"][0])), "重复"),
        (lambda p: p["capability_scores"][0].update(tier="high"), "不存在的 Tier"),
        (lambda p: p["capability_scores"][0].update(score=1.5), "score"),
        (lambda p: p["capability_scores"][0].update(role="planner"), "role"),
        (lambda p: p["capability_scores"][0].update(task_family="general"), "task_family"),
        (lambda p: p["capability_scores"][0].update(source=""), "source"),
        (lambda p: p["capability_scores"][0].update(at="2026-11"), "at"),
    ],
)
def test_published_profiles_follow_the_schema_and_the_go_rules(change: Any, message: str) -> None:
    good = profile("a6api/a@1", [entry()])
    assert pb.profile_problems(good) == []
    bad = json.loads(json.dumps(good))
    change(bad)
    problems = pb.profile_problems(bad)
    assert problems and any(message in p for p in problems), problems


def _copies(tmp_path: Path) -> tuple[Path, Path]:
    profiles, seed = tmp_path / "model-profiles.json", tmp_path / "registry-seed.json"
    shutil.copy(cs.REPO / "deploy" / "dev" / "model-profiles.json", profiles)
    shutil.copy(cs.REPO / "deploy" / "dev" / "registry-seed.json", seed)
    return profiles, seed


def test_publish_writes_both_files_in_the_same_format(tmp_path: Path) -> None:
    profiles_path, seed_path = _copies(tmp_path)
    before = next(p for p in json.loads(profiles_path.read_text("utf-8"))["profiles"] if p["id"] == cs.SONNET)
    existing = {(e["task_family"], e["role"], e["tier"]): e for e in before.get("capability_scores", [])}
    report = score_report([(cs.SONNET, 0.71, True), (cs.OPUS, 0.69, True)])
    report["cells"].append(score_report([(cs.SONNET, 0.6, True)], role="judge")["cells"][0])
    dry = pb.publish(report, profiles_path=profiles_path, seed_path=seed_path, today=dt.date(2026, 11, 10), dry_run=True)
    assert dry["changed"] and profiles_path.read_bytes() == (cs.REPO / "deploy" / "dev" / "model-profiles.json").read_bytes()
    pb.publish(report, profiles_path=profiles_path, seed_path=seed_path, today=dt.date(2026, 11, 10))
    document = json.loads(profiles_path.read_text("utf-8"))
    sonnet = next(p for p in document["profiles"] if p["id"] == cs.SONNET)
    after = {(e["task_family"], e["role"], e["tier"]): e for e in sonnet["capability_scores"]}
    assert after[("general.unclassified", "judge", "medium")]["score"] == 0.6
    assert after[("general.unclassified", "plan", "medium")]["score"] == 0.71
    untouched = {k: v for k, v in existing.items() if k not in {
        ("general.unclassified", "judge", "medium"), ("general.unclassified", "plan", "medium"),
    }}
    assert {k: after[k] for k in untouched} == untouched, "发布保留其他已验证维度的全部证据"
    assert set(after) == set(existing) | {("general.unclassified", r, "medium") for r in ("judge", "plan")}
    seed = json.loads(seed_path.read_text("utf-8"))
    assert [e["document"] for e in seed["entries"] if e["kind"] == "model_profile"] == document["profiles"], "种子与 Profile 文件一致"
    import importlib.util

    spec = importlib.util.spec_from_file_location("sync_model_profiles", SYNC)
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    assert sync.dump_profiles(document) == profiles_path.read_text("utf-8"), "保持手写的格式"
    assert '      "capability_scores": [\n        {' in profiles_path.read_text("utf-8")
    again = pb.publish(report, profiles_path=profiles_path, seed_path=seed_path, today=dt.date(2026, 11, 10))
    assert not again["changed"], "幂等"


def test_publish_refuses_entries_the_registry_would_reject(tmp_path: Path) -> None:
    profiles_path, seed_path = _copies(tmp_path)
    report = score_report([(cs.MINI, 0.7, True)])  # gpt-4o-mini 没有 medium
    before = profiles_path.read_bytes()
    with pytest.raises(pb.PublishError, match="不存在的 Tier"):
        pb.publish(report, profiles_path=profiles_path, seed_path=seed_path, today=dt.date(2026, 11, 10))
    assert profiles_path.read_bytes() == before


# --- capability 命令行 ---


def test_the_capability_cli_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv(ENV_GATEWAY_FACTORY, "capability_support:serving")
    skill = {cs.SONNET: 0.9, cs.OPUS: 0.5}
    plan = cs.write_plan(
        tmp_path / "plan.json",
        name="cli",
        n=100,
        r=1,
        cells=[{"role": "plan", "tier": "medium", "evals": [{"eval": "capability_support:make", "options": {"skill": skill, "pool": 100}}], "candidates": list(skill)}],
    )
    out = tmp_path / "out"
    from test_deepeval_acceptance import frozen

    freeze_path = tmp_path / "freeze.json"
    observed = frozen()
    observed["subject_models"] = list(skill)
    observed["registry_revisions"] = [
        {"scope": "platform", "kind": "model_profile", "name": pid, "version": "v2", "document_sha256": "e" * 64}
        for pid in skill
    ]
    freeze_path.write_text(json.dumps(observed), "utf-8")
    exported = json.loads((cs.REPO / "deploy/dev/model-profiles.json").read_text("utf-8"))
    exported["registry_revisions"] = {p["id"]: {"scope": "platform", "version": "v2", "sha256": "e" * 64} for p in exported["profiles"]}
    profile_path = tmp_path / "registry-profiles.json"
    profile_path.write_text(json.dumps(exported), "utf-8")
    assert capability_main(["estimate", "--plan", str(plan)]) == 0
    assert "合计还要约" in capsys.readouterr().out
    assert capability_main(["run", "--plan", str(plan), "--out", str(out), "--freeze-config", str(freeze_path), "--profiles", str(profile_path)]) == 0
    printed = capsys.readouterr().out
    assert printed.index("花费估算") < printed.index("这次跑了 200 次")
    assert capability_main(["score", "--out", str(out)]) == 0
    scores = json.loads((out / "scores.json").read_text("utf-8"))
    by = {c["candidate"]: c for c in scores["cells"][0]["candidates"]}
    assert by[cs.SONNET]["p"] == pytest.approx(0.9) and by[cs.SONNET]["publishable"] and by[cs.OPUS]["publishable"]
    profiles_path, seed_path = _copies(tmp_path)
    assert capability_main(["publish", "--out", str(out), "--profiles", str(profiles_path), "--seed", str(seed_path), "--dry-run"]) == 0
    patch = json.loads((out / "capability-patch.json").read_text("utf-8"))
    assert set(patch) == set(skill) and patch[cs.SONNET][0]["source"].startswith("capmatrix:synthetic@")
    assert "--dry-run" in capsys.readouterr().out
    assert capability_main(["run", "--plan", str(tmp_path / "missing.json"), "--out", str(out)]) == 2
