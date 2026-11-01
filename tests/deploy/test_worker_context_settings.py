"""TMP 计划第 11.15 节，调优第一轮：Worker 的上下文设置能从 Chart 给。

要对比不同的转存与压缩门槛，得能在集群里改；不给时与以前一样，不多渲染什么。
"""

from __future__ import annotations

from typing import Any

import pytest

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


# --- 调优那次部署遇到的两件事（TMP 计划第 11.15 节） ---


def images(manifests: list[dict[str, Any]]) -> dict[str, str]:
    return {m["metadata"]["name"]: pod_spec(m)["containers"][0]["image"] for m in by_kind(manifests, "Deployment")}


def test_the_memory_service_can_stay_on_its_own_image() -> None:
    """新镜像的 Memory Service 在生产配置不全时拒绝启动：接上 Milvus 与真 embedding 之前，它留在旧镜像上，别的服务照常换。"""
    pinned = images(render(CLOUD_VALUES, set_values=["memory.imageTag=old1234", "image.tag=new5678"]))
    assert next(v for k, v in pinned.items() if k.endswith("-memory")).endswith("/memory:old1234")
    assert next(v for k, v in pinned.items() if "-worker-" in k).endswith("/worker:new5678")
    plain = images(render(CLOUD_VALUES, set_values=["image.tag=new5678"]))
    assert next(v for k, v in plain.items() if k.endswith("-memory")).endswith("/memory:new5678"), "不给时跟 image.tag"


def test_a_numeric_image_tag_is_refused_instead_of_rendering_a_wrong_name() -> None:
    """提交号可能全是数字：YAML 把它读成数，镜像名就成了 %!s(float64=...)，Pod 拉不到镜像。"""
    with pytest.raises(pytest.fail.Exception, match="image.tag"):
        render(CLOUD_VALUES, set_values=["image.tag=3608351"])
