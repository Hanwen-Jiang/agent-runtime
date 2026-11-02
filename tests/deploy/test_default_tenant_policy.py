"""开放注册默认模型白名单的静态验收。

该文件只验证部署 seed 与当前 Model Profile 一致，不改变未挂载白名单时的“不限制”行为。
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "deploy" / "dev" / "default-tenant-policies.json"
PROFILES = ROOT / "deploy" / "dev" / "model-profiles.json"


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
