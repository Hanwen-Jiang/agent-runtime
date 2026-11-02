"""docs/plan/feedback-2026-11-01.md N-07：Model Profile 的单价与长度从 models.dev 取（scripts/sync_model_profiles.py）。

用一份很小的 models.dev 数据离线跑：改单价与长度、改了的才改日期、找不到的不动、加新 Profile、种子与 Profile 文件一致、--check 报出入；
仓库里的两个文件彼此一致，格式是手写的那种（一个键一行）。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "sync_model_profiles.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("sync_model_profiles", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync = _module()

CATALOG = {
    "openai": {"models": {
        "gpt-5.5": {"cost": {"input": 5, "output": 30, "cache_read": 0.5}, "limit": {"context": 1050000, "input": 922000, "output": 128000}, "tool_call": True, "structured_output": True},
        "gpt-6.1-sol": {"cost": {"input": 2, "output": 10, "cache_read": 0.1}, "limit": {"context": 1050000, "input": 922000, "output": 128000}, "tool_call": True, "structured_output": True},
        "text-embedding-3-small": {"cost": {"input": 0.02, "output": 0}, "limit": {"context": 8191, "output": 1536}},
    }},
    "anthropic": {"models": {"claude-sonnet-5-5": {"cost": {"input": 2, "output": 10, "cache_read": 0.1}, "limit": {"context": 1000000, "output": 128000}}}},
}


def profile(model: str, vendor: str = "openai", klass: str = "strong", cost: dict[str, float] | None = None) -> dict[str, Any]:
    return {
        "id": f"a6api/{model}@2026-10", "vendor": vendor, "model": model, "class": klass,
        "tiers": [{"name": "medium", "effort": "medium", "thinking_budget": 8192}],
        "capabilities": ["generate", "structured", "tools", "stream"],
        "cost": cost or {"in_per_m": 1.75, "out_per_m": 14.0, "cache_read_per_m": 0.175},
        "constraints": {"context_window": 400000, "max_output": 128000},
        "freshness": {"price_at": "2026-10-21"},
    }


def test_prices_and_lengths_come_from_models_dev() -> None:
    doc, changed, missing = sync.sync({"version": 1, "profiles": [profile("gpt-5.5")]}, CATALOG, "2026-11-02")
    (p,) = doc["profiles"]
    assert p["cost"] == {"in_per_m": 5.0, "out_per_m": 30.0, "cache_read_per_m": 0.5}
    assert p["constraints"] == {"context_window": 922000, "max_output": 128000}, "窗口取 input 与 context 中较小的"
    assert p["freshness"] == {"price_at": "2026-11-02"} and len(changed) == 1 and not missing


def test_unchanged_profiles_keep_their_date_and_unknown_ones_are_left_alone() -> None:
    same = profile("claude-sonnet-5-5", vendor="anthropic", cost={"in_per_m": 2.0, "out_per_m": 10.0, "cache_read_per_m": 0.1})
    same["constraints"] = {"context_window": 1000000, "max_output": 128000}
    unknown = profile("someone-elses-model", vendor="mistral")
    doc, changed, missing = sync.sync({"version": 1, "profiles": [same, unknown]}, CATALOG, "2026-11-02")
    assert doc["profiles"] == [same, unknown] and not changed and missing == [unknown["id"]]


def test_embedding_models_keep_max_output_zero() -> None:
    emb = profile("text-embedding-3-small", klass="embed")
    (p,) = sync.sync({"version": 1, "profiles": [emb]}, CATALOG, "2026-11-02")[0]["profiles"]
    assert p["constraints"]["max_output"] == 0 and p["cost"]["in_per_m"] == 0.02


def test_release_date_is_real_metadata_not_price_refresh_date() -> None:
    base = sync.synced(profile("gpt-5.5"), CATALOG["openai"]["models"]["gpt-5.5"])
    base["freshness"] = {"price_at": "2026-10-21"}
    entry = {**CATALOG["openai"]["models"]["gpt-5.5"], "release_date": "2026-04-23"}
    catalog = {"openai": {"models": {"gpt-5.5": entry}}}
    doc, changed, _ = sync.sync({"version": 1, "profiles": [base]}, catalog, "2026-11-03")
    p = doc["profiles"][0]
    assert p["released_at"] == "2026-04-23" and len(changed) == 1
    assert p["freshness"]["price_at"] == "2026-10-21", "release metadata is not a new price observation"
    assert "capability_scores" not in p and "reliability" not in p, "must not invent evals"


@pytest.mark.parametrize("release", [None, "", "2026-02-30", "2026-04", "next week"])
def test_missing_or_invalid_release_date_is_not_inferred(release: str | None) -> None:
    entry = {**CATALOG["openai"]["models"]["gpt-5.5"], "release_date": release}
    base = profile("gpt-5.5")
    assert "released_at" not in sync.synced(base, entry)
    base["released_at"] = "2026-04-23"
    assert sync.synced(base, entry)["released_at"] == "2026-04-23", "missing upstream data must not erase known metadata"


def test_a_new_profile_is_built_from_the_catalog() -> None:
    p = sync.new_profile("openai", "gpt-6.1-sol", "strong", ["medium", "high"], CATALOG["openai"]["models"]["gpt-6.1-sol"], "2026-11-02")
    assert p["id"] == "a6api/gpt-6.1-sol@2026-11" and p["class"] == "strong"
    assert [t["name"] for t in p["tiers"]] == ["medium", "high"] and p["capabilities"] == ["generate", "structured", "tools", "stream"]
    assert p["cost"] == {"in_per_m": 2.0, "out_per_m": 10.0, "cache_read_per_m": 0.1}


def test_the_seed_mirrors_the_profiles_and_keeps_other_entries_in_place() -> None:
    seed = {"entries": [
        {"kind": "node_template", "name": "answer", "document": {}},
        {"kind": "model_profile", "name": "old", "document": {}},
        {"kind": "task_families", "name": "default", "document": {}},
    ]}
    profiles = [profile("gpt-5.5"), profile("gpt-6.1-sol")]
    out = sync.mirror_seed(seed, profiles)
    assert [(e["kind"], e["name"]) for e in out["entries"]] == [
        ("node_template", "answer"), ("model_profile", "a6api/gpt-5.5@2026-10"), ("model_profile", "a6api/gpt-6.1-sol@2026-10"), ("task_families", "default"),
    ]


def test_check_reports_drift_without_writing(tmp_path: Path) -> None:
    profiles = tmp_path / "model-profiles.json"
    seed = tmp_path / "registry-seed.json"
    catalog = tmp_path / "api.json"
    profiles.write_text(sync.dump_profiles({"version": 1, "profiles": [profile("gpt-5.5")]}), "utf-8")
    seed.write_text(json.dumps({"entries": []}, indent=2) + "\n", "utf-8")
    catalog.write_text(json.dumps(CATALOG), "utf-8")
    before = profiles.read_text("utf-8")
    args = ["--catalog", str(catalog), "--profiles", str(profiles), "--seed", str(seed), "--date", "2026-11-02"]
    assert sync.main([*args, "--check"]) == 1 and profiles.read_text("utf-8") == before
    assert sync.main(args) == 0
    assert sync.main([*args, "--check"]) == 0, "同步之后就一致了"
    assert json.loads(seed.read_text("utf-8"))["entries"][0]["name"] == "a6api/gpt-5.5@2026-10"


def test_adding_a_model_that_models_dev_does_not_know_fails(tmp_path: Path) -> None:
    for name in ("model-profiles.json", "registry-seed.json"):
        shutil.copy(REPO / "deploy" / "dev" / name, tmp_path / name)
    catalog = tmp_path / "api.json"
    catalog.write_text(json.dumps(CATALOG), "utf-8")
    code = sync.main(["--catalog", str(catalog), "--profiles", str(tmp_path / "model-profiles.json"), "--seed", str(tmp_path / "registry-seed.json"), "--add", "openai/gpt-99"])
    assert code == 2


@pytest.mark.parametrize("name", ["model-profiles.json"])
def test_the_repository_files_are_in_sync_with_each_other(name: str) -> None:
    profiles = json.loads((REPO / "deploy" / "dev" / name).read_text("utf-8"))
    seed = json.loads((REPO / "deploy" / "dev" / "registry-seed.json").read_text("utf-8"))
    assert [e["document"] for e in seed["entries"] if e["kind"] == "model_profile"] == profiles["profiles"]
    assert sync.dump_profiles(profiles) == (REPO / "deploy" / "dev" / name).read_text("utf-8"), "保持手写的格式"
    assert any(p["model"] == "gpt-6.1-sol" and p["class"] == "strong" for p in profiles["profiles"])
