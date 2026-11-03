"""docs/plan/model-catalog-sync.md（F1，N-07 的延续）：按中转站的 /models 与 models.dev 自动维护 Model Profile 与白名单。

离线跑：一份很小的中转站列表、models.dev 数据、规则、Profile、种子、租户策略与 values.yaml；密钥的用例对着本地一个要求认证的假中转站。
验收的编号对应设计稿第 6 节。实现（scripts/sync_model_catalog.py）还没有时整个文件跳过。
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "sync_model_catalog.py"
if not SCRIPT.exists():
    pytest.skip("scripts/sync_model_catalog.py 还没写：先审验收测试（docs/plan/model-catalog-sync.md）", allow_module_level=True)


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("sync_model_catalog", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cat = _module()

TODAY = "2026-11-03"
LOW = {"name": "low", "effort": "", "thinking_budget": 0}
MEDIUM = {"name": "medium", "effort": "medium", "thinking_budget": 8192}
HIGH = {"name": "high", "effort": "high", "thinking_budget": 32768}
DEFAULT = {"name": "default", "effort": "", "thinking_budget": 0}


def _entry(date: str, inp: float, out: float, cache: float = 0.0, *, output: tuple[str, ...] = ("text",), tools: bool = True) -> dict[str, Any]:
    return {
        "release_date": date,
        "cost": {"input": inp, "output": out, "cache_read": cache},
        "limit": {"context": 400000, "output": 64000},
        "modalities": {"input": ["text"], "output": list(output)},
        "tool_call": tools,
        "structured_output": tools,
    }


CATALOG: dict[str, Any] = {
    "openai": {"models": {
        "gpt-6-sol": _entry("2026-09-22", 2, 10, 0.1),
        "gpt-6.1-sol": _entry("2026-09-29", 2, 10, 0.1),
        "gpt-5.5": _entry("2026-04-23", 5, 30, 0.5),
        "gpt-4o-mini": _entry("2024-07-18", 0.15, 0.6, 0.075),
        "gpt-6-luna": _entry("2026-09-22", 0.1, 0.5, 0.01),
        "text-embedding-3-small": _entry("2024-01-25", 0.02, 0, tools=False),
        "gpt-image-2": _entry("2026-04-21", 5, 30, output=("image",)),
    }},
    "anthropic": {"models": {
        "claude-haiku-5-5": _entry("2026-10-07", 0.1, 0.5, 0.01),
        "claude-sonnet-5-5": _entry("2026-09-28", 2, 10, 0.1),
    }},
    "deepseek": {"models": {"deepseek-v4-flash": _entry("2026-10-17", 0.15, 0.6, 0.003)}},
    "zhipuai": {"models": {
        "glm-5.3-flash": _entry("2026-08-26", 0.12, 0.42),
        "glm-5.3": _entry("2026-08-14", 1.2, 4.2),
    }},
    "xiaomi": {"models": {"mimo-v2.6-pro": _entry("2026-09-22", 0.435, 0.87)}},
    "alibaba": {"models": {"qwen3.8-omni-flash": _entry("2026-09-17", 0.15, 0.47)}},
    # 第三方的条目：不当价格来源，只给“没有一方价格”的报告当参考。
    "hyper": {"models": {
        "deepseek-v4.1-flash": _entry("2026-09-10", 0.33, 1.31),
        "deepseek-v4.1-flash-1017": _entry("2026-10-17", 0.33, 1.31),
    }},
    "above": {"models": {"deepseek-v4.1-flash": _entry("2026-09-10", 0.165, 0.66)}},
    "tencent-token-plan": {"models": {"hy4-preview": _entry("2026-08-28", 0, 0)}},
}

RELAY = [
    "gpt-6-sol", "gpt-6.1-sol", "gpt-5.5", "gpt-4o-mini", "gpt-6-luna", "gpt-image-2",
    "claude-haiku-5-5", "claude-sonnet-5-5",
    "deepseek-v4-flash", "deepseek-v4.1-flash", "deepseek-v4.1-flash-1017", "deepseek-v4.1-flash-expires-on-0910",
    "text-embedding-3-small", "glm-5.3-flash", "glm-5.3", "mimo-v2.6-pro", "qwen3.8-omni-flash",
    "gemini-3.5-flash-low", "hy4-preview",
]


def _profile(pid: str, vendor: str, model: str, klass: str, tiers: list[dict[str, Any]], cost: tuple[float, float, float], released: str) -> dict[str, Any]:
    caps = ["embed"] if klass == "embed" else ["generate", "structured", "tools", "stream"]
    return {
        "id": pid, "vendor": vendor, "model": model, "class": klass, "tiers": tiers, "capabilities": caps,
        "cost": {"in_per_m": cost[0], "out_per_m": cost[1], "cache_read_per_m": cost[2]},
        "constraints": {"context_window": 400000, "max_output": 0 if klass == "embed" else 64000},
        "freshness": {"price_at": "2026-11-02"}, "released_at": released,
    }


def _profiles() -> dict[str, Any]:
    sol = _profile("a6api/gpt-6-sol@2026-10", "openai", "gpt-6-sol", "strong", [MEDIUM, HIGH], (2, 10, 0.1), "2026-09-22")
    sol["capability_scores"] = [{"task_family": "code", "role": "act", "tier": "medium", "score": 0.7, "source": "matrix", "observed_at": "2026-10-30"}]
    sol["reliability"] = {"value": 0.99, "source": "metering", "observed_at": "2026-10-30"}
    return {"version": 1, "profiles": [
        sol,
        _profile("a6api/gpt-5.5@2026-10", "openai", "gpt-5.5", "strong", [MEDIUM, HIGH], (5, 30, 0.5), "2026-04-23"),
        _profile("a6api/gpt-4o-mini@2026-10", "openai", "gpt-4o-mini", "small", [LOW], (0.15, 0.6, 0.075), "2024-07-18"),
        _profile("a6api/claude-haiku-5-5@2026-10", "anthropic", "claude-haiku-5-5", "small", [LOW], (0.1, 0.5, 0.01), "2026-10-07"),
        _profile("a6api/claude-sonnet-5-5@2026-10", "anthropic", "claude-sonnet-5-5", "strong", [MEDIUM, HIGH], (2, 10, 0.1), "2026-09-28"),
        _profile("a6api/deepseek-v4-flash@2026-10", "deepseek", "deepseek-v4-flash", "mid", [LOW], (0.15, 0.6, 0.003), "2026-10-17"),
        _profile("a6api/text-embedding-3-small@2026-10", "openai", "text-embedding-3-small", "embed", [DEFAULT], (0.02, 0, 0), "2024-01-25"),
    ]}


def _rules(**changes: Any) -> dict[str, Any]:
    rules: dict[str, Any] = {
        "version": 1,
        "vendors": {
            "openai": {"provider": "openai", "prefixes": ["gpt-", "text-embedding-"]},
            "anthropic": {"provider": "anthropic", "prefixes": ["claude-"]},
            "google": {"provider": "google", "prefixes": ["gemini-"]},
            "deepseek": {"provider": "deepseek", "prefixes": ["deepseek-"]},
            "zhipuai": {"provider": "zhipuai", "prefixes": ["glm-"]},
            "xiaomi": {"provider": "xiaomi", "prefixes": ["mimo-"]},
            "alibaba": {"provider": "alibaba", "prefixes": ["qwen"]},
            "tencent": {"provider": "tencent", "prefixes": ["hy"]},
        },
        "ignore": ["vision", "image", "audio", "tts", "realtime", "search", "omni", "-exp$", "-low$", "-high$"],
        "aliases": {"openai:gpt-*": "openai:gpt-*-sol", "openai:gpt-*o-mini": "openai:gpt-*-luna"},
        "track": [{"line": "zhipuai:glm-*-flash", "class": "small", "tiers": ["low"]}],
        "disabled": [],
        "price_overrides": {},
        "new_line_max_age_days": 365,
        "allowlists": {
            "default": {"classes": ["small", "mid", "embed"], "strong_max": {"in_per_m": 2, "out_per_m": 10}},
            "tenants": {"gh-hanwen-jiang": {}, "evals": {"candidates": True}, "swebench": {"exclude_vendors": ["anthropic"]}},
        },
    }
    rules.update(changes)
    return rules


def _policies() -> dict[str, Any]:
    budget = {"daily_usd": 100000, "monthly_usd": 1000000}
    return {
        "ceiling": budget,
        "default": {"model_budget": {"daily_usd": 3, "monthly_usd": 20}, "model_allowlist": ["a6api/gpt-4o-mini@2026-10"]},
        "tenants": {
            "evals": {"model_budget": budget, "model_allowlist": ["a6api/gpt-5.5@2026-10"]},
            "swebench": {"model_budget": budget, "model_allowlist": ["a6api/gpt-5.5@2026-10"]},
        },
    }


def _by_id(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["id"]: p for p in document["profiles"]}


def _available(document: dict[str, Any]) -> list[str]:
    return [p["id"] for p in document["profiles"] if p.get("availability", True)]


@pytest.fixture
def planned() -> Any:
    return cat.plan(_profiles(), RELAY, CATALOG, _rules(), TODAY)


# 1. 线与版本的解析；快照、变体、预览与非文字输出不算候选。


@pytest.mark.parametrize(("name", "line", "version"), [
    ("gpt-6.1-sol", "gpt-*-sol", (6, 1)),
    ("gpt-6-sol", "gpt-*-sol", (6,)),
    ("gpt-5.5", "gpt-*", (5, 5)),
    ("gpt-4o-mini", "gpt-*o-mini", (4,)),
    ("claude-opus-5-5", "claude-opus-*", (5, 5)),
    ("claude-fable-5", "claude-fable-*", (5,)),
    ("gemini-3.5-flash-lite", "gemini-*-flash-lite", (3, 5)),
    ("gemini-4-argon", "gemini-*-argon", (4,)),
    ("qwen3.8-max", "qwen*-max", (3, 8)),
    ("kimi-k3.1-pro", "kimi-k*-pro", (3, 1)),
    ("deepseek-v4.1-flash", "deepseek-v*-flash", (4, 1)),
    ("glm-5.3-flashx", "glm-*-flashx", (5, 3)),
    ("LongCat-2.0", "longcat-*", (2, 0)),
    ("text-embedding-3-large", "text-embedding-*-large", (3,)),
])
def test_line_and_version_come_from_the_name(name: str, line: str, version: tuple[int, ...]) -> None:
    assert cat.line_of(name) == (line, version)


@pytest.mark.parametrize("name", [
    "deepseek-v4.1-flash-1017", "deepseek-v4.1-flash-expires-on-0910", "claude-haiku-4-5-20251001",
])
def test_snapshots_are_recognised(name: str) -> None:
    assert cat.is_snapshot(name)


def test_snapshots_variants_previews_and_non_text_models_are_never_candidates(planned: Any) -> None:
    mentioned = json.dumps({"added": planned.added, "unpriced": planned.unpriced, "new_lines": planned.new_lines}, ensure_ascii=False)
    for name in ("deepseek-v4.1-flash-1017", "expires-on", "gpt-image-2", "hy4-preview", "qwen3.8-omni-flash", "gemini-3.5-flash-low"):
        assert name not in mentioned
    assert {p["model"] for p in planned.document["profiles"]} == {p["model"] for p in _profiles()["profiles"]} | {"gpt-6.1-sol", "gpt-6-luna", "glm-5.3-flash"}


# 2. 同一条线出了新版本：加 Profile（继承档位与 Tier，不继承测量），旧版本停用并写明接替者。


def test_new_version_is_added_with_the_old_versions_class_and_tiers(planned: Any) -> None:
    new = _by_id(planned.document)["a6api/gpt-6.1-sol@2026-11"]
    assert new["vendor"] == "openai" and new["model"] == "gpt-6.1-sol"
    assert new["class"] == "strong" and new["tiers"] == [MEDIUM, HIGH]
    assert new["capabilities"] == ["generate", "structured", "tools", "stream"]
    assert new["cost"] == {"in_per_m": 2.0, "out_per_m": 10.0, "cache_read_per_m": 0.1}
    assert new["released_at"] == "2026-09-29" and new["freshness"] == {"price_at": TODAY}
    assert "availability" not in new
    assert "capability_scores" not in new and "reliability" not in new, "测量属于具体模型，新版本用先验，不继承"


def test_old_version_is_switched_off_but_kept(planned: Any) -> None:
    old = _by_id(planned.document)["a6api/gpt-6-sol@2026-10"]
    assert old["availability"] is False
    assert old["capability_scores"] and old["reliability"], "停用不删内容"
    assert "gpt-6.1-sol" in planned.retired["a6api/gpt-6-sol@2026-10"]


def test_new_profiles_are_appended_sorted_by_model(planned: Any) -> None:
    assert planned.added == ["a6api/glm-5.3-flash@2026-11", "a6api/gpt-6-luna@2026-11", "a6api/gpt-6.1-sol@2026-11"]
    assert [p["id"] for p in planned.document["profiles"]][-3:] == planned.added
    assert [p["id"] for p in planned.document["profiles"]][:7] == [p["id"] for p in _profiles()["profiles"]], "已有的顺序不动"


# 3. aliases 接起来的线照样停用旧版本。


def test_aliases_carry_renamed_lines(planned: Any) -> None:
    assert "gpt-6.1-sol" in planned.retired["a6api/gpt-5.5@2026-10"]
    assert "gpt-6-luna" in planned.retired["a6api/gpt-4o-mini@2026-10"]
    luna = _by_id(planned.document)["a6api/gpt-6-luna@2026-11"]
    assert luna["class"] == "small" and luna["tiers"] == [LOW], "继承别名那一头的旧版本（gpt-4o-mini）的档位"


# 4. 没有一方价格的新版本不加，报告附第三方价格的中位数；给了 price_overrides 才加。


def test_newer_version_without_first_party_price_is_reported_not_added(planned: Any) -> None:
    assert "a6api/deepseek-v4-flash@2026-10" in _available(planned.document)
    assert not any(p["model"] == "deepseek-v4.1-flash" for p in planned.document["profiles"])
    [item] = planned.unpriced
    assert item["model"] == "deepseek-v4.1-flash" and item["line"] == "deepseek:deepseek-v*-flash"
    assert item["hint"] == {"in_per_m": pytest.approx(0.2475), "out_per_m": pytest.approx(0.985)}


def test_price_override_adds_the_model_and_version_beats_release_date() -> None:
    rules = _rules(price_overrides={"deepseek-v4.1-flash": {"in_per_m": 0.15, "out_per_m": 0.6, "cache_read_per_m": 0.003, "source": "人定 2026-11-03"}})
    result = cat.plan(_profiles(), RELAY, CATALOG, rules, TODAY)
    new = _by_id(result.document)["a6api/deepseek-v4.1-flash@2026-11"]
    assert new["class"] == "mid" and new["cost"] == {"in_per_m": 0.15, "out_per_m": 0.6, "cache_read_per_m": 0.003}
    assert "deepseek-v4.1-flash" in result.retired["a6api/deepseek-v4-flash@2026-10"]
    assert result.unpriced == []

    # 5. 版本号（4.1 > 4）与发布日（v4-flash 的 10-17 晚于 v4.1-flash 的 09-10）先后不一致：按版本号，写进报告。
    assert result.disagreements == [{"line": "deepseek:deepseek-v*-flash", "by_version": "deepseek-v4.1-flash", "by_date": "deepseek-v4-flash"}]


# 6. 中转站上没有了的停用，再出现时恢复；disabled 里的总是停用。


def test_models_gone_from_the_relay_are_switched_off_and_come_back() -> None:
    without = [m for m in RELAY if m != "claude-sonnet-5-5"]
    gone = cat.plan(_profiles(), without, CATALOG, _rules(), TODAY)
    assert _by_id(gone.document)["a6api/claude-sonnet-5-5@2026-10"]["availability"] is False
    assert "中转站" in gone.retired["a6api/claude-sonnet-5-5@2026-10"]

    back = cat.plan(gone.document, RELAY, CATALOG, _rules(), TODAY)
    assert "availability" not in _by_id(back.document)["a6api/claude-sonnet-5-5@2026-10"]
    assert back.restored == ["a6api/claude-sonnet-5-5@2026-10"]


def test_disabled_profiles_stay_off() -> None:
    result = cat.plan(_profiles(), RELAY, CATALOG, _rules(disabled=["a6api/claude-haiku-5-5@2026-10"]), TODAY)
    assert _by_id(result.document)["a6api/claude-haiku-5-5@2026-10"]["availability"] is False
    assert "a6api/claude-haiku-5-5@2026-10" in result.retired


# 7. 新线只进报告；track 里的新线按规则加。


def test_untracked_new_lines_are_only_reported(planned: Any) -> None:
    lines = {item["line"]: item for item in planned.new_lines}
    assert set(lines) == {"zhipuai:glm-*", "xiaomi:mimo-v*-pro"}
    assert lines["xiaomi:mimo-v*-pro"]["model"] == "mimo-v2.6-pro"
    assert lines["xiaomi:mimo-v*-pro"]["class_guess"] in {"small", "mid", "strong"}


def test_tracked_new_line_is_added_with_the_rule_class(planned: Any) -> None:
    glm = _by_id(planned.document)["a6api/glm-5.3-flash@2026-11"]
    assert glm["vendor"] == "zhipuai" and glm["class"] == "small" and glm["tiers"] == [LOW]


def test_old_new_lines_are_left_out_of_the_report() -> None:
    result = cat.plan(_profiles(), RELAY, CATALOG, _rules(new_line_max_age_days=30), TODAY)
    assert result.new_lines == [], "glm-5.3 与 mimo-v2.6-pro 都早于 30 天"


# 8. 白名单按规则重算。


def test_allowlists_follow_the_rules(planned: Any) -> None:
    policies, notes = cat.allowlists(_policies(), planned.document, _rules())
    order = [p["id"] for p in planned.document["profiles"]]
    default = ["a6api/claude-haiku-5-5@2026-10", "a6api/claude-sonnet-5-5@2026-10", "a6api/deepseek-v4-flash@2026-10",
               "a6api/text-embedding-3-small@2026-10", "a6api/glm-5.3-flash@2026-11", "a6api/gpt-6-luna@2026-11", "a6api/gpt-6.1-sol@2026-11"]
    assert policies["default"]["model_allowlist"] == [i for i in order if i in default]
    assert policies["tenants"]["evals"]["model_allowlist"] == [i for i in order if i in default]
    assert policies["tenants"]["swebench"]["model_allowlist"] == [i for i in order if i in default and "claude" not in i]
    # 其余字段原样；规则里有、文件里没有的租户不新建，只提一句。
    assert policies["ceiling"] == _policies()["ceiling"]
    assert policies["default"]["model_budget"] == {"daily_usd": 3, "monthly_usd": 20}
    assert policies["tenants"]["evals"]["model_budget"] == _policies()["tenants"]["evals"]["model_budget"]
    assert "gh-hanwen-jiang" not in policies["tenants"]
    assert any("gh-hanwen-jiang" in note for note in notes)


def test_free_tier_strong_models_respect_the_price_cap(planned: Any) -> None:
    rules = _rules()
    rules["allowlists"]["default"]["strong_max"] = {"in_per_m": 1, "out_per_m": 5}
    policies, _ = cat.allowlists(_policies(), planned.document, rules)
    assert not {"a6api/claude-sonnet-5-5@2026-10", "a6api/gpt-6.1-sol@2026-11"} & set(policies["default"]["model_allowlist"])
    assert "a6api/gpt-6.1-sol@2026-11" in policies["tenants"]["evals"]["model_allowlist"], "上限只管免费档"


def test_candidate_lines_only_reach_tenants_that_ask_for_them() -> None:
    rules = _rules(track=[*_rules()["track"], {"line": "xiaomi:mimo-v*-pro", "class": "strong", "tiers": ["medium", "high"], "candidate": True}])
    result = cat.plan(_profiles(), RELAY, CATALOG, rules, TODAY)
    mimo = "a6api/mimo-v2.6-pro@2026-11"
    assert mimo in _by_id(result.document)
    policies, _ = cat.allowlists(_policies(), result.document, rules)
    assert mimo in policies["tenants"]["evals"]["model_allowlist"]
    assert mimo not in policies["default"]["model_allowlist"], "没测过的候选不进免费档（单价再低也不行）"
    assert mimo not in policies["tenants"]["swebench"]["model_allowlist"]


# 9～12. 命令行：写文件、种子与 values.yaml 一致、第二遍没有改动、--check、防误删、密钥不外泄。

VALUES_YAML = """# 注释保留
modelGateway:
  profilesConfigMap: agent-runtime-model-profiles
  defaultTenantPolicies:
    enabled: false
    name: ""
    modelAllowlist:
      - a6api/gpt-4o-mini@2026-10
    modelBudget:
      dailyUsd: 3
      monthlyUsd: 20
worker:
  replicas: 1
"""


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    (tmp_path / "relay.json").write_text(json.dumps({"data": [{"id": m, "object": "model"} for m in RELAY]}), "utf-8")
    (tmp_path / "catalog.json").write_text(json.dumps(CATALOG), "utf-8")
    (tmp_path / "rules.json").write_text(json.dumps(_rules()), "utf-8")
    (tmp_path / "profiles.json").write_text(json.dumps(_profiles(), indent=2), "utf-8")
    seed = {"entries": [{"kind": "tenant_policy", "name": "default", "document": {}}] + [{"kind": "model_profile", "name": p["id"], "document": p} for p in _profiles()["profiles"]]}
    (tmp_path / "seed.json").write_text(json.dumps(seed, indent=2), "utf-8")
    (tmp_path / "policies.json").write_text(json.dumps(_policies(), indent=2), "utf-8")
    (tmp_path / "values.yaml").write_text(VALUES_YAML, "utf-8", newline="\n")
    (tmp_path / "none.env").write_text("", "utf-8")
    return tmp_path


def _args(d: Path, *extra: str, relay: str | None = None) -> list[str]:
    source = ["--relay", relay] if relay else ["--relay-models", str(d / "relay.json")]
    return [*source, "--provider-env", str(d / "none.env"), "--catalog", str(d / "catalog.json"), "--rules", str(d / "rules.json"),
            "--profiles", str(d / "profiles.json"), "--seed", str(d / "seed.json"), "--policies", str(d / "policies.json"),
            "--values", str(d / "values.yaml"), "--report", str(d / "report"), "--date", TODAY, *extra]


def _files(d: Path) -> dict[str, str]:
    return {n: (d / n).read_text("utf-8") for n in ("profiles.json", "seed.json", "policies.json", "values.yaml")}


def test_run_writes_profiles_seed_policies_values_and_report(workdir: Path) -> None:
    assert cat.main(_args(workdir)) == 0
    profiles = json.loads((workdir / "profiles.json").read_text("utf-8"))
    assert "a6api/gpt-6.1-sol@2026-11" in _by_id(profiles)
    seed = json.loads((workdir / "seed.json").read_text("utf-8"))
    assert [e["document"] for e in seed["entries"] if e["kind"] == "model_profile"] == profiles["profiles"]
    assert seed["entries"][0]["kind"] == "tenant_policy", "别的种子条目不动"

    default = json.loads((workdir / "policies.json").read_text("utf-8"))["default"]["model_allowlist"]
    values_text = (workdir / "values.yaml").read_text("utf-8")
    assert yaml.safe_load(values_text)["modelGateway"]["defaultTenantPolicies"]["modelAllowlist"] == default
    head, _, _ = VALUES_YAML.partition("    modelAllowlist:\n")
    _, _, tail = VALUES_YAML.partition("    modelBudget:\n")
    assert values_text.startswith(head + "    modelAllowlist:\n") and values_text.endswith("    modelBudget:\n" + tail), "values.yaml 只改这一段"

    [md] = list((workdir / "report").glob("*.md"))
    [js] = list((workdir / "report").glob("*.json"))
    report = json.loads(js.read_text("utf-8"))
    assert set(report) >= {"added", "retired", "restored", "unpriced", "new_lines", "disagreements", "allowlists"}
    text = md.read_text("utf-8")
    assert "gpt-6-sol" in text and "gpt-6.1-sol" in text and "deepseek-v4.1-flash" in text


def test_second_run_changes_nothing_and_check_passes(workdir: Path) -> None:
    assert cat.main(_args(workdir)) == 0
    first = _files(workdir)
    assert cat.main(_args(workdir)) == 0
    assert _files(workdir) == first
    assert cat.main(_args(workdir, "--check")) == 0


def test_check_reports_differences_without_writing(workdir: Path) -> None:
    before = _files(workdir)
    assert cat.main(_args(workdir, "--check")) == 1
    assert _files(workdir) == before


@pytest.mark.parametrize("relay", [["gpt-6.1-sol"], []])
def test_a_relay_list_missing_most_profiles_changes_nothing(workdir: Path, relay: list[str]) -> None:
    (workdir / "relay.json").write_text(json.dumps({"data": [{"id": m} for m in relay]}), "utf-8")
    before = _files(workdir)
    assert cat.main(_args(workdir)) != 0
    assert _files(workdir) == before


def test_an_unreadable_relay_list_changes_nothing(workdir: Path) -> None:
    (workdir / "relay.json").unlink()
    before = _files(workdir)
    assert cat.main(_args(workdir)) != 0
    assert _files(workdir) == before


SECRET = "sk-test-SECRET-9f3a"


@pytest.fixture
def fake_relay() -> Iterator[str]:
    body = json.dumps({"object": "list", "data": [{"id": m, "object": "model", "owned_by": "custom"} for m in RELAY]}).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — http.server 的接口
            ok = self.path == "/v1/models" and self.headers.get("Authorization") == f"Bearer {SECRET}"
            self.send_response(200 if ok else 401)
            self.end_headers()
            self.wfile.write(body if ok else b'{"error": "bad key"}')

        def log_message(self, *_: Any) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()


def test_the_relay_key_never_shows_up(workdir: Path, fake_relay: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("AR_MODEL_PROVIDER_API_KEY", SECRET)
    assert cat.main(_args(workdir, relay=fake_relay)) == 0
    out = capsys.readouterr()
    reports = "".join(p.read_text("utf-8") for p in (workdir / "report").iterdir())
    assert SECRET not in out.out + out.err + reports + "".join(_files(workdir).values())

    monkeypatch.setenv("AR_MODEL_PROVIDER_API_KEY", SECRET + "-wrong")
    assert cat.main(_args(workdir, relay=fake_relay)) != 0
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err, "认证失败的错误信息里也不带密钥"


# 13. 仓库里的规则文件与 Profile、白名单对得上。

RULES = REPO / "deploy" / "dev" / "model-catalog-rules.json"
PROFILES = REPO / "deploy" / "dev" / "model-profiles.json"
POLICY = REPO / "deploy" / "dev" / "default-tenant-policies.json"


def test_repo_rules_refer_to_known_vendors_lines_and_profiles() -> None:
    rules = json.loads(RULES.read_text("utf-8"))
    profiles = json.loads(PROFILES.read_text("utf-8"))["profiles"]
    vendors = set(rules["vendors"])
    assert {p["vendor"] for p in profiles} <= vendors
    lines = [*rules["aliases"], *rules["aliases"].values(), *(t["line"] for t in rules["track"])]
    for line in lines:
        vendor, sep, key = line.partition(":")
        assert sep and vendor in vendors and "*" in key, line
    assert set(rules["disabled"]) <= {p["id"] for p in profiles}
    for profile in profiles:
        assert cat.line_of(profile["model"]) is not None, f"{profile['model']} 解析不出线与版本"


def test_repo_allowlist_is_what_the_rules_give() -> None:
    rules = json.loads(RULES.read_text("utf-8"))
    document = json.loads(PROFILES.read_text("utf-8"))
    policy = json.loads(POLICY.read_text("utf-8"))
    regenerated, _ = cat.allowlists(policy, document, rules)
    assert regenerated == policy, "跑一次 scripts/sync_model_catalog.py"
