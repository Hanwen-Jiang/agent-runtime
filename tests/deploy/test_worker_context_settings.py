"""TMP 计划第 11.15 节，调优第一轮：Worker 的上下文设置能从 Chart 给。

要对比不同的转存与压缩门槛，得能在集群里改；不给时与以前一样，不多渲染什么。
"""

from __future__ import annotations

from typing import Any

from deploy_tests import CLOUD_VALUES, by_kind, env_of, pod_spec, render

SETTINGS = {"AR_CONTEXT_EFFECTIVE_TOKENS": "worker.contextEffectiveTokens", "AR_CONTEXT_OFFLOAD_TOKENS": "worker.contextOffloadTokens"}


def workers(manifests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = [m for m in by_kind(manifests, "Deployment") if "-worker-" in m["metadata"]["name"]]
    assert found, "有 Worker 的 Deployment"
    return found


def test_off_by_default() -> None:
    for manifests in (render(), render(CLOUD_VALUES)):
        for worker in workers(manifests):
            env = env_of(pod_spec(worker)["containers"][0])
            assert not set(SETTINGS) & set(env), worker["metadata"]["name"]


def test_given_values_reach_every_worker_as_strings() -> None:
    manifests = render(CLOUD_VALUES, set_values=["worker.contextEffectiveTokens=128000", "worker.contextOffloadTokens=8000"])
    for worker in workers(manifests):
        env = env_of(pod_spec(worker)["containers"][0])
        assert env["AR_CONTEXT_EFFECTIVE_TOKENS"]["value"] == "128000"
        assert env["AR_CONTEXT_OFFLOAD_TOKENS"]["value"] == "8000"
