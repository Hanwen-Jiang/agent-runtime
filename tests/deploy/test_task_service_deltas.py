"""DELTA 的部署（docs/plan/all-plans-dag-2026-11-03.md 7.1）：逐字增量的内部端口。

- 默认打开，但要共用的 SecretStore 文件（secrets.storeClaim）：调用方的令牌由 task-service 启动时写进去，Worker 从同一份文件读。
- task-service 多一个内部端口（AR_INTERNAL_ADDR）与只在集群内的 Service；入口永远不路由到它。
- NetworkPolicy：内部端口只放本命名空间里的 Worker（沙箱、别的命名空间都进不来）；Task API 照旧谁都能连，Registry 只放本命名空间。
- 每个 Worker 池拿到 AR_TASK_SERVICE_INTERNAL_URL；按租户关掉的开关渲染成 AR_DELTA_DISABLED_TENANTS。
"""

from __future__ import annotations

from typing import Any

from deploy_tests import CLOUD_VALUES, by_kind, env_of, pod_spec, render

PORT = 8097


def task_service(manifests: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    [ts] = [m for m in by_kind(manifests, "Deployment") if m["metadata"]["name"].endswith("-task-service")]
    return pod_spec(ts)["containers"][0], env_of(pod_spec(ts)["containers"][0])


def workers(manifests: list[dict[str, Any]]) -> list[dict[str, dict[str, Any]]]:
    return [env_of(pod_spec(m)["containers"][0]) for m in by_kind(manifests, "Deployment") if "-worker-" in m["metadata"]["name"]]


def test_the_cloud_values_open_the_internal_port_to_workers_only() -> None:
    manifests = render(CLOUD_VALUES)
    container, env = task_service(manifests)
    assert env["AR_INTERNAL_ADDR"]["value"] == f"0.0.0.0:{PORT}"
    assert {"name": "internal", "containerPort": PORT} in container["ports"]
    assert "AR_DELTA_DISABLED_TENANTS" not in env, "没有关掉的租户"

    [service] = [m for m in by_kind(manifests, "Service") if m["metadata"]["name"].endswith("-task-service-internal")]
    assert service["spec"]["type"] == "ClusterIP" and service["spec"]["ports"] == [{"name": "internal", "port": PORT, "targetPort": "internal"}]
    url = f"http://{service['metadata']['name']}:{PORT}"
    pools = workers(manifests)
    assert pools and all(w["AR_TASK_SERVICE_INTERNAL_URL"]["value"] == url for w in pools), "每个 Worker 池都推得到"

    [policy] = [m for m in by_kind(manifests, "NetworkPolicy") if m["metadata"]["name"].endswith("-task-service")]
    assert policy["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "task-service"
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    rules = {r["ports"][0]["port"]: r for r in policy["spec"]["ingress"]}
    assert "from" not in rules[8080], "Task API 照旧谁都能连（入口、coding-web）"
    assert rules[8092]["from"] == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "agent-runtime"}}}]
    [peer] = rules[PORT]["from"]
    assert set(peer) == {"podSelector"} and peer["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "worker", "只放本命名空间的 Worker"

    for m in manifests:
        if m["kind"] in ("Ingress", "IngressRoute", "HTTPRoute"):
            assert "internal" not in repr(m) and str(PORT) not in repr(m), "入口不路由到内部端口"


def test_disabled_tenants_and_the_off_switch() -> None:
    _, env = task_service(render(CLOUD_VALUES, set_values=["taskService.deltas.disabledTenants={t9,swebench}"]))
    assert env["AR_DELTA_DISABLED_TENANTS"]["value"] == "t9,swebench"
    manifests = render(CLOUD_VALUES, set_values=["taskService.deltas.enabled=false"])
    _, env = task_service(manifests)
    assert "AR_INTERNAL_ADDR" not in env and all("AR_TASK_SERVICE_INTERNAL_URL" not in w for w in workers(manifests))
    assert not [m for m in manifests if m["metadata"]["name"].endswith(("-task-service-internal",)) or (m["kind"] == "NetworkPolicy" and m["metadata"]["name"].endswith("-task-service"))]


def test_without_a_shared_secret_store_there_is_no_internal_port() -> None:
    manifests = render()
    _, env = task_service(manifests)
    assert "AR_INTERNAL_ADDR" not in env and all("AR_TASK_SERVICE_INTERNAL_URL" not in w for w in workers(manifests)), "令牌没处共享时不开"
