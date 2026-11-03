"""AUTH-01 验收（docs/plan/auth-tokens-2026-11-03.md 第 2 节）：task-service 的 Ingress 配置。

- taskService.ingress.issuers 是签发方的登记（只有公钥，task-service keygen 生成）：渲染进一个 ConfigMap，挂进 task-service，
  AR_INGRESS_ISSUERS_FILE 指向它；没有签发方时不渲染。
- taskService.ingress.devTokens 渲染成 AR_INGRESS_DEV_TOKENS：空时按 profile（development 收开发令牌、production 不收）；
  不收开发令牌时不给 task-service 签名密钥（AR_DEV_SIGNING_KEY）。
- 生产模式既不收开发令牌、又没有签发方时渲染失败（task-service 本身也拒绝启动）；取值拼错了也失败。
- 云上（coding-agent 还在发开发令牌）显式 accept，属于迁移期间；生产样例只收 JWT。
"""

from __future__ import annotations

import json
from typing import Any

from deploy_tests import CLOUD_VALUES, PRODUCTION_VALUES, by_kind, env_of, pod_spec, render

from test_task_service_replicas import render_fails

ISSUERS_FILE = "/etc/agentruntime/ingress/issuers.json"


def task_service(manifests: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    [ts] = [m for m in by_kind(manifests, "Deployment") if m["metadata"]["name"].endswith("-task-service")]
    return ts, env_of(pod_spec(ts)["containers"][0])


def issuers_configmaps(manifests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in by_kind(manifests, "ConfigMap") if m["metadata"]["name"].endswith("-ingress-issuers")]


def test_development_accepts_dev_tokens_and_has_no_issuers() -> None:
    manifests = render()
    _, env = task_service(manifests)
    assert env["AR_INGRESS_DEV_TOKENS"]["value"] == "accept"
    assert "AR_DEV_SIGNING_KEY" in env and "AR_INGRESS_ISSUERS_FILE" not in env
    assert not issuers_configmaps(manifests)


def test_production_example_accepts_only_jwts() -> None:
    manifests = render(PRODUCTION_VALUES)
    ts, env = task_service(manifests)
    assert env["AR_INGRESS_DEV_TOKENS"]["value"] == "refuse"
    assert "AR_DEV_SIGNING_KEY" not in env, "只收 JWT 时 task-service 不持有能签令牌的东西"
    assert env["AR_INGRESS_ISSUERS_FILE"]["value"] == ISSUERS_FILE
    [cm] = issuers_configmaps(manifests)
    doc = json.loads(cm["data"]["issuers.json"])
    assert doc["audience"] == "agent-runtime" and doc["issuers"][0]["iss"] == "coding-agent" and doc["issuers"][0]["keys"]
    assert "PRIVATE" not in cm["data"]["issuers.json"]
    spec = pod_spec(ts)
    [volume] = [v for v in spec["volumes"] if v.get("configMap", {}).get("name") == cm["metadata"]["name"]]
    [mount] = [m for m in spec["containers"][0]["volumeMounts"] if m["name"] == volume["name"]]
    assert ISSUERS_FILE.startswith(mount["mountPath"] + "/") and mount["readOnly"] is True


def test_cloud_accepts_dev_tokens_during_the_migration() -> None:
    _, env = task_service(render(CLOUD_VALUES))
    assert env["AR_PROFILE"]["value"] == "production"
    assert env["AR_INGRESS_DEV_TOKENS"]["value"] == "accept" and "AR_DEV_SIGNING_KEY" in env


def test_production_without_issuers_or_dev_tokens_fails_to_render() -> None:
    err = render_fails("taskService.ingress.devTokens=", values=[CLOUD_VALUES])
    assert "taskService.ingress" in err
    err = render_fails("taskService.ingress.devTokens=yes")
    assert "taskService.ingress.devTokens" in err
