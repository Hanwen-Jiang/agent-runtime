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


def test_the_key_can_come_from_its_own_secret_as_a_file() -> None:
    """不用为 Jev 改 SOPS 里的种子：单独一个 Secret，挂成文件，Worker 从 AR_JUDGE_KEY_FILE 读；不进环境变量。"""
    for worker in workers(render(CLOUD_VALUES, set_values=["worker.judge=jev", "worker.judgeKeySecret.name=agent-runtime-jev"])):
        spec = pod_spec(worker)
        env = env_of(spec["containers"][0])
        assert env["AR_JUDGE_KEY_FILE"]["value"] == "/etc/agentruntime/judge/api-key"
        assert not any("secretKeyRef" in str(v) and "jev" in str(v) for v in env.values()), "密钥不进环境变量"
        (volume,) = [v for v in spec["volumes"] if v["name"] == "judge-key"]
        assert volume["secret"]["secretName"] == "agent-runtime-jev"
        assert any(m["name"] == "judge-key" and m.get("readOnly") for m in spec["containers"][0]["volumeMounts"])


def test_without_a_key_secret_nothing_is_mounted() -> None:
    for worker in workers(render(CLOUD_VALUES, set_values=["worker.judge=jev"])):
        spec = pod_spec(worker)
        assert "AR_JUDGE_KEY_FILE" not in env_of(spec["containers"][0]) and all(v["name"] != "judge-key" for v in spec["volumes"])
