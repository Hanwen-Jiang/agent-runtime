"""SWE-bench 基线（TMP 计划第 11.10 节）：Model Gateway 按租户限制可选的模型。

网关的白名单读 AR_DEV_TENANT_POLICIES_FILE（router.FileAllowlist，每次调用都重读）。之前 Chart 不设它，线上的白名单不生效。
Chart 的 modelGateway.tenantPoliciesConfigMap 给了才挂上；不给时与以前一样不限制，不多渲染什么。ConfigMap 由运维创建，同 profilesConfigMap。
"""

from __future__ import annotations

from typing import Any

from deploy_tests import CLOUD_VALUES, by_kind, env_of, pod_spec, render

CONFIG_MAP = "agent-runtime-model-allowlist"
FILE = "/etc/agentruntime/tenant-policies/tenant-policies.json"


def gateway(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    [found] = [m for m in by_kind(manifests, "Deployment") if m["metadata"]["name"].endswith("-model-gateway")]
    return found


def test_off_by_default() -> None:
    for manifests in (render(), render(CLOUD_VALUES)):
        spec = pod_spec(gateway(manifests))
        [container] = spec["containers"]
        assert "AR_DEV_TENANT_POLICIES_FILE" not in env_of(container)
        assert not [v for v in spec["volumes"] if v["name"] == "tenant-policies"]


def test_the_config_map_is_mounted_read_only_when_given() -> None:
    spec = pod_spec(gateway(render(CLOUD_VALUES, set_values=[f"modelGateway.tenantPoliciesConfigMap={CONFIG_MAP}"])))
    [container] = spec["containers"]
    assert env_of(container)["AR_DEV_TENANT_POLICIES_FILE"]["value"] == FILE
    [volume] = [v for v in spec["volumes"] if v["name"] == "tenant-policies"]
    assert volume["configMap"]["name"] == CONFIG_MAP
    [mount] = [m for m in container["volumeMounts"] if m["name"] == "tenant-policies"]
    assert mount["readOnly"] is True and FILE.startswith(mount["mountPath"] + "/")
