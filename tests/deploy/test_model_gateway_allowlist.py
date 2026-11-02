"""SWE-bench 基线（TMP 计划第 11.10 节）：Model Gateway 按租户限制可选的模型。

网关的白名单读 AR_DEV_TENANT_POLICIES_FILE（router.FileAllowlist，每次调用都重读）。默认 values 不挂载白名单；values-cloud 显式打开内置免费策略。显式传入 modelGateway.tenantPoliciesConfigMap 时使用外部 ConfigMap。
"""

from __future__ import annotations

import json
from typing import Any

from deploy_tests import CLOUD_VALUES, by_kind, env_of, pod_spec, render

CONFIG_MAP = "agent-runtime-model-allowlist"
FILE = "/etc/agentruntime/tenant-policies/tenant-policies.json"


def gateway(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    [found] = [m for m in by_kind(manifests, "Deployment") if m["metadata"]["name"].endswith("-model-gateway")]
    return found


def test_off_by_default() -> None:
    spec = pod_spec(gateway(render()))
    [container] = spec["containers"]
    assert "AR_DEV_TENANT_POLICIES_FILE" not in env_of(container)
    assert not [v for v in spec["volumes"] if v["name"] == "tenant-policies"]


def test_cloud_values_mount_the_builtin_free_policy() -> None:
    manifests = render(CLOUD_VALUES)
    spec = pod_spec(gateway(manifests))
    [container] = spec["containers"]
    assert env_of(container)["AR_DEV_TENANT_POLICIES_FILE"]["value"] == FILE
    [volume] = [v for v in spec["volumes"] if v["name"] == "tenant-policies"]
    assert volume["configMap"]["name"] == "ar-agent-runtime-default-tenant-policies"
    [policy] = [m for m in by_kind(manifests, "ConfigMap") if m["metadata"]["name"] == volume["configMap"]["name"]]
    policy_data = json.loads(policy["data"]["tenant-policies.json"])
    assert "a6api/gpt-6.1-sol@2026-11" in policy_data["default"]["model_allowlist"]
    assert "a6api/claude-opus-5-5@2026-10" not in policy_data["default"]["model_allowlist"]
    assert policy_data["default"]["model_budget"] == {"daily_usd": 3, "monthly_usd": 20}


def test_the_config_map_is_mounted_read_only_when_given() -> None:
    manifests = render(CLOUD_VALUES, set_values=[f"modelGateway.tenantPoliciesConfigMap={CONFIG_MAP}"])
    spec = pod_spec(gateway(manifests))
    [container] = spec["containers"]
    assert env_of(container)["AR_DEV_TENANT_POLICIES_FILE"]["value"] == FILE
    [volume] = [v for v in spec["volumes"] if v["name"] == "tenant-policies"]
    assert volume["configMap"]["name"] == CONFIG_MAP
    [mount] = [m for m in container["volumeMounts"] if m["name"] == "tenant-policies"]
    assert mount["readOnly"] is True and FILE.startswith(mount["mountPath"] + "/")
    assert not [m for m in by_kind(manifests, "ConfigMap") if m["metadata"]["name"] == "ar-agent-runtime-default-tenant-policies"]


def test_model_budget_uses_shared_business_database_and_policy() -> None:
    manifests = render(CLOUD_VALUES)
    gw = pod_spec(gateway(manifests))
    [task] = [m for m in by_kind(manifests, "Deployment") if m["metadata"]["name"].endswith("-task-service")]
    ts = pod_spec(task)
    gw_env = env_of(gw["containers"][0])
    ts_env = env_of(ts["containers"][0])
    assert gw_env["AR_RELATIONAL_DSN"] == ts_env["AR_RELATIONAL_DSN"]
    assert gw_env["AR_DEV_TENANT_POLICIES_FILE"] == ts_env["AR_DEV_TENANT_POLICIES_FILE"]
    assert gw_env["AR_REGISTRY_ADDRESS"]["value"].endswith(":8092")
    for spec in (gw, ts):
        [volume] = [v for v in spec["volumes"] if v["name"] == "tenant-policies"]
        assert volume["configMap"]["name"] == "ar-agent-runtime-default-tenant-policies"
