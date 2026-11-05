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


def test_the_sub_agent_rules_are_on_by_default() -> None:
    """N16-R（docs/plan/sub-agent.md 第 12 节；总表第 0 节 V1-SUB 默认打开）：默认 rules，每个 Worker 池都拿到；off 时不渲染；别的取值渲染失败。"""
    for manifests in (render(), render(CLOUD_VALUES)):
        assert all(env_of(pod_spec(w)["containers"][0])["AR_SUB_AGENT_POLICY"]["value"] == "rules" for w in workers(manifests))
    off = render(CLOUD_VALUES, set_values=["worker.subAgentPolicy=off"])
    assert all("AR_SUB_AGENT_POLICY" not in env_of(pod_spec(w)["containers"][0]) for w in workers(off))
    with pytest.raises(pytest.fail.Exception, match="subAgentPolicy"):
        render(CLOUD_VALUES, set_values=["worker.subAgentPolicy=always"])


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


def test_the_model_gateway_can_ship_its_own_image() -> None:
    """TOOLS-KIMI（docs/plan/all-plans-dag-2026-11-03.md）：网关的修复要单独上线，main 上别的改动不跟着走。
    modelGateway.imageTag 只换网关的镜像，别的组件仍跟 image.tag；不给时跟 image.tag。"""
    pinned = images(render(CLOUD_VALUES, set_values=["modelGateway.imageTag=fix1234", "image.tag=base5678"]))
    gateway = [k for k in pinned if k.endswith("-model-gateway")]
    assert len(gateway) == 1 and pinned[gateway[0]].endswith("/model-gateway:fix1234")
    others = {k: v for k, v in pinned.items() if k not in gateway and "/agentruntime/" in v and "coding-web" not in k}
    assert others and all(v.endswith(":base5678") for k, v in others.items() if not k.endswith("-memory")), others
    plain = images(render(CLOUD_VALUES, set_values=["image.tag=base5678"]))
    assert next(v for k, v in plain.items() if k.endswith("-model-gateway")).endswith("/model-gateway:base5678"), "不给时跟 image.tag"
    with pytest.raises(pytest.fail.Exception, match="imageTag"):
        render(CLOUD_VALUES, set_values=["modelGateway.imageTag=1234567"])


def test_a_numeric_image_tag_is_refused_instead_of_rendering_a_wrong_name() -> None:
    """提交号可能全是数字：YAML 把它读成数，镜像名就成了 %!s(float64=...)，Pod 拉不到镜像。"""
    with pytest.raises(pytest.fail.Exception, match="image.tag"):
        render(CLOUD_VALUES, set_values=["image.tag=3608351"])
