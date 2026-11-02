"""docs/plan/feedback-2026-11-01.md N-09：Worker 的交付快速检查（Jev）能从 Chart 打开。

不给时与以前一样，不渲染 AR_JUDGE；给 jev 时每个 Worker 都有；别的取值渲染失败。密钥不在 values 里，在 SecretStore 的种子里
（secrets.example.yaml 列着 judge/jev-api-key）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deploy_tests import CLOUD_VALUES, by_kind, env_of, pod_spec, render

REPO = Path(__file__).resolve().parents[2]


def workers(manifests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = [m for m in by_kind(manifests, "Deployment") if "-worker-" in m["metadata"]["name"]]
    assert found
    return found


def test_off_by_default() -> None:
    for manifests in (render(), render(CLOUD_VALUES)):
        for worker in workers(manifests):
            assert "AR_JUDGE" not in env_of(pod_spec(worker)["containers"][0])


def test_jev_reaches_every_worker() -> None:
    for worker in workers(render(CLOUD_VALUES, set_values=["worker.judge=jev"])):
        assert env_of(pod_spec(worker)["containers"][0])["AR_JUDGE"]["value"] == "jev"


def test_other_judges_are_refused() -> None:
    with pytest.raises(pytest.fail.Exception, match="worker.judge"):
        render(CLOUD_VALUES, set_values=["worker.judge=gpt"])


def test_the_secret_seed_lists_the_jev_key() -> None:
    assert '"judge/jev-api-key": "REPLACE_ME"' in (REPO / "deploy" / "cluster" / "secrets" / "secrets.example.yaml").read_text("utf-8")
