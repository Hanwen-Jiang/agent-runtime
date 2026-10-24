"""故障注入套件里经真 Model Gateway 的用例共用的配置（T-13，docs/plan/live-e2e.md 第 3 节第 5 条）。

同一档（mid@low）两个 Profile，不同供应商；A 便宜，选档先选它。上游是本地的假端点（e2e_support.fake_openai），按模型注入失败、挂起请求。
"""

from __future__ import annotations

from typing import Any

FAKE_KEY = "sk-test-faults-DO-NOT-LEAK-9e1d"
MID_TIER = "mid@low"
MID_A = "fake/mid-a@2026-10"
MID_B = "fake/mid-b@2026-10"
MODEL_A = "fake-mid-a"
MODEL_B = "fake-mid-b"
# 网关对每个 Profile 的尝试次数（services/go-model-gateway/internal/gateway 的 DefaultRetry.MaxAttempts）。
GATEWAY_ATTEMPTS_PER_PROFILE = 3


def _mid_profile(pid: str, vendor: str, model: str, in_per_m: float, out_per_m: float) -> dict[str, Any]:
    return {
        "id": pid,
        "vendor": vendor,
        "model": model,
        "class": "mid",
        "tiers": [{"name": "low", "effort": "", "thinking_budget": 0}],
        "capabilities": ["generate", "structured", "tools", "stream"],
        "cost": {"in_per_m": in_per_m, "out_per_m": out_per_m, "cache_read_per_m": 0.1},
        "constraints": {"context_window": 128000, "max_output": 16384},
        "freshness": {"price_at": "2026-10-24"},
    }


FAULT_PROFILES: dict[str, Any] = {
    "version": 1,
    "profiles": [_mid_profile(MID_A, "openai", MODEL_A, 1.0, 4.0), _mid_profile(MID_B, "google", MODEL_B, 2.0, 8.0)],
}
