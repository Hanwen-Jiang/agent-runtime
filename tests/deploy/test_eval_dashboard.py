"""TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 11.12 节：评测看板（Eval Dashboard）在 Chart 里的部署。

默认不渲染。打开后：learning 镜像跑只用标准库的静态服务，Pod 没有令牌、Secret 与业务环境变量，根文件系统只读；页面在 ConfigMap 里，
由发布步骤写，Chart 不渲染（否则每次 helm upgrade 都会清掉），卷标为 optional；入口只把这个域名转给这个 Service；NetworkPolicy 进只来自
入口控制器，出一概不放（第 11.16 节：给了 metricsUrl 时只放行到 VictoriaMetrics 与 DNS）。缺 host、入口不是 traefik 时渲染失败。
"""

from __future__ import annotations

from typing import Any

import pytest

from deploy_tests import CHART_DIR, CLOUD_VALUES, RELEASE, by_kind, env_of, pod_spec, render, require_tool, run

HOST = "evals.cgent.link"
ENABLED = ["evalDashboard.enabled=true", f"evalDashboard.host={HOST}"]
NAME = "ar-agent-runtime-eval-dashboard"
MOUNT = "/srv/eval-dashboard"


@pytest.fixture(scope="module")
def manifests() -> list[dict[str, Any]]:
    return render(CLOUD_VALUES, set_values=ENABLED)


def named(manifests: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    [found] = [m for m in by_kind(manifests, kind) if m["metadata"]["name"] == NAME]
    return found


def test_off_by_default() -> None:
    for manifests in (render(), render(CLOUD_VALUES)):
        assert not [m for m in manifests if "eval-dashboard" in m["metadata"]["name"]]


def test_the_server_runs_from_the_learning_image_with_nothing_else(manifests: list[dict[str, Any]]) -> None:
    deployment = named(manifests, "Deployment")
    assert deployment["spec"]["replicas"] == 1
    spec = pod_spec(deployment)
    [container] = spec["containers"]
    assert container["image"].rsplit(":", 1)[0].endswith("/learning")
    assert container["args"] == ["-m", "agentruntime_learning.evals.dashboard_server", "--dir", MOUNT, "--port", "8080"]
    assert spec["automountServiceAccountToken"] is False, "不挂 ServiceAccount 令牌"
    assert env_of(container) == {}, "没有业务环境变量（数据库、对象存储、密钥）"
    assert "envFrom" not in container


def test_the_pod_only_mounts_the_page(manifests: list[dict[str, Any]]) -> None:
    spec = pod_spec(named(manifests, "Deployment"))
    [container] = spec["containers"]
    [volume] = spec["volumes"]
    assert volume["configMap"]["name"] == NAME, "只有页面的 ConfigMap，没有 Secret 与别的卷"
    assert volume["configMap"]["optional"] is True, "还没发布时 Pod 也能起来"
    [mount] = container["volumeMounts"]
    assert mount["mountPath"] == MOUNT and mount["readOnly"] is True
    assert "subPath" not in mount, "subPath 的挂载收不到 ConfigMap 的更新"


def test_the_pod_is_locked_down(manifests: list[dict[str, Any]]) -> None:
    spec = pod_spec(named(manifests, "Deployment"))
    [container] = spec["containers"]
    security = container["securityContext"]
    assert security["readOnlyRootFilesystem"] is True and security["allowPrivilegeEscalation"] is False
    assert security["capabilities"]["drop"] == ["ALL"] and security["runAsNonRoot"] is True
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert container["resources"]["limits"]["memory"] and container["resources"]["limits"]["cpu"], "资源有上限"
    for probe in ("readinessProbe", "livenessProbe"):
        assert container[probe]["httpGet"]["path"] == "/healthz"


def test_the_chart_does_not_own_the_page(manifests: list[dict[str, Any]]) -> None:
    assert not [m for m in by_kind(manifests, "ConfigMap") if m["metadata"]["name"] == NAME], "页面由发布步骤写，helm upgrade 不能清掉它"


def test_the_service_is_internal(manifests: list[dict[str, Any]]) -> None:
    service = named(manifests, "Service")
    assert service["spec"].get("type", "ClusterIP") == "ClusterIP"
    assert [p["name"] for p in service["spec"]["ports"]] == ["http"]


def test_the_route_sends_only_this_host_to_this_service(manifests: list[dict[str, Any]]) -> None:
    route = named(manifests, "IngressRoute")
    [rule] = route["spec"]["routes"]
    assert rule["match"] == f"Host(`{HOST}`)"
    assert [s["name"] for s in rule["services"]] == [NAME]
    assert route["spec"]["entryPoints"] == ["websecure"]
    assert route["spec"]["tls"]["secretName"] == "cloudflare-origin-tls", "复用源站证书（已含 *.cgent.link）"


def test_the_network_policy_allows_only_the_ingress_controller_in_and_nothing_out(manifests: list[dict[str, Any]]) -> None:
    spec = named(manifests, "NetworkPolicy")["spec"]
    assert sorted(spec["policyTypes"]) == ["Egress", "Ingress"]
    assert not spec.get("egress"), "静态页面不需要出网，也连不了数据库"
    [rule] = spec["ingress"]
    assert [p["port"] for p in rule["ports"]] == [8080]
    [source] = rule["from"]
    assert source["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "kube-system"}


def test_with_a_metrics_url_the_pod_may_reach_only_victoriametrics_and_dns() -> None:
    """第 11.16 节：实时的一栏要读 VictoriaMetrics；出方向只放行到它的 8428 与 DNS，环境变量只有它的地址。"""
    url = "http://vm-victoria-metrics-single-server.observability.svc:8428"
    manifests = render(CLOUD_VALUES, set_values=[*ENABLED, f"evalDashboard.metricsUrl={url}"])
    [container] = pod_spec(named(manifests, "Deployment"))["containers"]
    assert {k: v["value"] for k, v in env_of(container).items()} == {"AR_EVAL_METRICS_URL": url}
    egress = named(manifests, "NetworkPolicy")["spec"]["egress"]
    assert len(egress) == 2
    vm, dns = egress
    [peer] = vm["to"]
    assert peer["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "observability"}
    assert peer["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "victoria-metrics-single"}
    assert vm["ports"] == [{"protocol": "TCP", "port": 8428}]
    assert {p["port"] for p in dns["ports"]} == {53}
    assert dns["to"][0]["podSelector"]["matchLabels"] == {"k8s-app": "kube-dns"}


@pytest.mark.parametrize(
    ("sets", "needle"),
    [
        (["evalDashboard.enabled=true"], "evalDashboard.host"),
        ([*ENABLED, "ingress.enabled=false"], "ingress"),
        ([*ENABLED, "ingress.type=ingress"], "traefik"),
    ],
    ids=["no-host", "no-entry", "not-traefik"],
)
def test_bad_settings_fail_to_render(sets: list[str], needle: str) -> None:
    helm = require_tool("helm")
    cmd: list[Any] = [helm, "template", RELEASE, CHART_DIR, "--namespace", "agent-runtime", "--values", CLOUD_VALUES]
    for item in sets:
        cmd += ["--set", item]
    proc = run(cmd, check=False)
    assert proc.returncode != 0, f"应当渲染失败：{sets}"
    assert needle in proc.stderr
