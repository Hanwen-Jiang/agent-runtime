"""开放注册默认模型白名单的静态验收。

该文件只验证部署 seed 与当前 Model Profile 一致，不改变未挂载白名单时的“不限制”行为。
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "deploy" / "dev" / "default-tenant-policies.json"
PROFILES = ROOT / "deploy" / "dev" / "model-profiles.json"
VALUES = ROOT / "deploy" / "helm" / "agent-runtime" / "values.yaml"
CLOUD_VALUES = ROOT / "deploy" / "helm" / "agent-runtime" / "values-cloud.yaml"


def test_default_policy_references_existing_profiles_and_keeps_expensive_models_out() -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    profiles = json.loads(PROFILES.read_text(encoding="utf-8"))
    allowlist = policy["default"]["model_allowlist"]
    known = {profile["id"] for profile in profiles["profiles"]}

    assert policy["tenants"] == {}
    assert allowlist
    assert set(allowlist) <= known
    assert "a6api/gpt-6.1-sol@2026-11" in allowlist
    assert "a6api/gpt-5.5@2026-10" not in allowlist
    assert "a6api/claude-opus-5-5@2026-10" not in allowlist


def test_default_policy_keeps_embedding_profiles_available() -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    allowlist = set(policy["default"]["model_allowlist"])
    assert {
        "a6api/text-embedding-3-large@2026-10",
        "a6api/text-embedding-3-small@2026-10",
    } <= allowlist


def test_cloud_values_explicitly_enable_the_builtin_policy() -> None:
    base = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    cloud = yaml.safe_load(CLOUD_VALUES.read_text(encoding="utf-8"))
    assert base["modelGateway"]["defaultTenantPolicies"]["enabled"] is False
    assert cloud["modelGateway"]["defaultTenantPolicies"]["enabled"] is True
    assert base["modelGateway"]["defaultTenantPolicies"]["modelBudget"] == {"dailyUsd": 3, "monthlyUsd": 20}
    assert set(base["modelGateway"]["defaultTenantPolicies"]["modelAllowlist"]) == set(
        json.loads(POLICY.read_text(encoding="utf-8"))["default"]["model_allowlist"]
    )
