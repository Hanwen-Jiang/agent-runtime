"""T-15：Helm Chart 的验收（细化见 docs/plan/deploy-telemetry.md 第 2 节第 4 条、第 5 节）。

不真部署：`helm lint` 加 `helm template` 的结构检查。名字（队列、环境变量、端口）从代码里取，与渲染结果比对，
因为 Chart 里写错这些 `helm lint` 发现不了。

约定的标签：`app.kubernetes.io/component` 是 task-service、model-gateway、memory、worker、learning、otel-collector；
Worker 池另有 `agentruntime.io/pool: <池名>`。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from agentruntime_worker import names

from deploy_tests import (
    CHART_DIR,
    COLLECTOR_CONFIG,
    PRODUCTION_VALUES,
    REPO_ROOT,
    by_kind,
    containers,
    env_of,
    pod_spec,
    port_of,
    render,
    require_tool,
    run,
)

COMPONENT = "app.kubernetes.io/component"
POOL = "agentruntime.io/pool"
# Python Worker 服务的队列（session 队列由 task-service 里的 Go Session Worker 服务）。
PYTHON_QUEUES = {names.TASK_QUEUE, names.DECISION_QUEUE, names.MODEL_QUEUE, names.PROGRAM_QUEUE}
WORKFLOW_QUEUES = {names.TASK_QUEUE}
SENSITIVE_ENV = re.compile(r"(?i)(password|secret|token|api_?key|signing_key|auth|dsn|credential)")
# 不是密钥、名字却像的环境变量：它们的值是文件路径或密钥的名字。
NOT_SECRET_ENV = {"AR_SECRETS_FILE", "AR_MODEL_PROVIDER_KEY_NAME"}
OTLP_HTTP_PORT = 4318


@pytest.fixture(scope="module", params=["default", "production"])
def manifests(request: pytest.FixtureRequest) -> list[dict[str, Any]]:
    return render() if request.param == "default" else render(PRODUCTION_VALUES)


def component(manifests: list[dict[str, Any]], kind: str, name: str) -> list[dict[str, Any]]:
    return [m for m in by_kind(manifests, kind) if m["metadata"].get("labels", {}).get(COMPONENT) == name]


def only(items: list[dict[str, Any]], what: str) -> dict[str, Any]:
    assert len(items) == 1, f"{what} 应恰好一个，得到 {len(items)} 个"
    return items[0]


def main_container(workload: dict[str, Any]) -> dict[str, Any]:
    return pod_spec(workload)["containers"][0]


def source_text() -> str:
    """各服务读取配置的代码：环境变量名必须在这里出现过。"""
    parts = []
    for pattern in ("services/**/*.go", "services/**/*.py", "libs/**/*.go", "libs/**/*.py"):
        for p in REPO_ROOT.glob(pattern):
            if "_test" in p.name or "/tests/" in p.as_posix():
                continue
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


# --- lint 与渲染 ---


@pytest.mark.parametrize("values", [None, PRODUCTION_VALUES], ids=["default", "production"])
def test_helm_lint_passes(values: Path | None) -> None:
    helm = require_tool("helm")
    cmd: list[Any] = [helm, "lint", "--strict", CHART_DIR]
    if values is not None:
        cmd += ["--values", values]
    run(cmd)


def test_every_manifest_has_kind_name_and_labels(manifests: list[dict[str, Any]]) -> None:
    assert manifests
    for m in manifests:
        assert m.get("apiVersion") and m.get("kind"), m
        meta = m["metadata"]
        assert meta.get("name"), m
        labels = meta.get("labels", {})
        assert labels.get("app.kubernetes.io/part-of") == "agent-runtime", f"{m['kind']}/{meta['name']} 缺 part-of 标签"


# --- Worker 池与 KEDA ---


def worker_pools(manifests: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {d["metadata"]["labels"][POOL]: d for d in component(manifests, "Deployment", "worker")}


def queues_of(container: dict[str, Any]) -> list[str]:
    args = [*container.get("command", []), *container.get("args", [])]
    assert "--queues" in args, f"Worker 容器要用 --queues 指定队列：{args}"
    return args[args.index("--queues") + 1].split(",")


def test_worker_pools_cover_every_python_queue_once(manifests: list[dict[str, Any]]) -> None:
    pools = worker_pools(manifests)
    served: list[str] = []
    for pool, deployment in pools.items():
        queues = queues_of(main_container(deployment))
        assert queues == [pool], f"按队列分池（设计书 3.6、K.5）：池 {pool} 只服务队列 {pool}，得到 {queues}"
        served += queues
    assert sorted(served) == sorted(PYTHON_QUEUES), "每个队列恰好一个池；队列名取自 agentruntime_worker.names"


def test_each_pool_scales_on_its_own_temporal_queue(manifests: list[dict[str, Any]]) -> None:
    pools = worker_pools(manifests)
    scaled = by_kind(manifests, "ScaledObject")
    for pool, deployment in pools.items():
        targets = [s for s in scaled if s["spec"]["scaleTargetRef"]["name"] == deployment["metadata"]["name"]]
        so = only(targets, f"池 {pool} 的 ScaledObject")
        assert so["apiVersion"].startswith("keda.sh/")
        [trigger] = so["spec"]["triggers"]
        assert trigger["type"] == "temporal"
        meta = trigger["metadata"]
        assert meta["taskQueue"] == pool
        assert meta["queueTypes"] == ("workflow" if pool in WORKFLOW_QUEUES else "activity")
        assert meta["endpoint"] and meta["namespace"]
        assert int(meta["targetQueueSize"]) >= 1
        spec = so["spec"]
        assert 0 <= int(spec["minReplicaCount"]) <= int(spec["maxReplicaCount"])
        assert "replicas" not in deployment["spec"], "副本数交给 KEDA，Deployment 不写死"


def test_temporal_address_is_consistent(manifests: list[dict[str, Any]]) -> None:
    addresses = {env_of(main_container(d))["AR_TEMPORAL_ADDRESS"]["value"] for d in worker_pools(manifests).values()}
    addresses.add(env_of(main_container(only(component(manifests, "Deployment", "task-service"), "task-service")))["AR_TEMPORAL_ADDRESS"]["value"])
    endpoints = {t["metadata"]["endpoint"] for so in by_kind(manifests, "ScaledObject") for t in so["spec"]["triggers"]}
    assert len(addresses) == 1 and endpoints == addresses, "Worker、task-service 与 KEDA 连同一个 Temporal"


# --- task-service 与 Registry 端口 ---


def test_registry_port_is_internal_and_distinct(manifests: list[dict[str, Any]]) -> None:
    ts = only(component(manifests, "Deployment", "task-service"), "task-service 的 Deployment")
    c = main_container(ts)
    env = env_of(c)
    http_port, registry_port = port_of(env["AR_HTTP_ADDR"]["value"]), port_of(env["AR_REGISTRY_ADDR"]["value"])
    assert http_port != registry_port, "Registry 用单独的内部端口（docs/plan/registry.md 第 2 节第 7 条）"
    ports = {p["name"]: p["containerPort"] for p in c["ports"]}
    assert ports == {"http": http_port, "registry": registry_port}
    assert "AR_PINNED_VERSIONS_FILE" not in env, "配了 Registry 就不能再配固定版本文件"
    for svc in by_kind(manifests, "Service"):
        exposes_registry = any(p.get("targetPort") in ("registry", registry_port) for p in svc["spec"]["ports"])
        if exposes_registry:
            assert svc["spec"].get("type", "ClusterIP") == "ClusterIP", "Registry 端口只能在集群内"
    for ingress in by_kind(manifests, "Ingress"):
        text = repr(ingress)
        assert "registry" not in text and str(registry_port) not in text, "Registry 端口不进 Ingress"


@pytest.mark.parametrize("name", ["task-service", "model-gateway"])
def test_services_with_healthz_have_probes(manifests: list[dict[str, Any]], name: str) -> None:
    c = main_container(only(component(manifests, "Deployment", name), name))
    for probe in ("readinessProbe", "livenessProbe"):
        assert c[probe]["httpGet"]["path"] == "/healthz", f"{name} 缺 {probe}"


def test_model_gateway_listens_where_workers_call_it(manifests: list[dict[str, Any]]) -> None:
    gw = main_container(only(component(manifests, "Deployment", "model-gateway"), "model-gateway"))
    listen = port_of(env_of(gw)["AR_MODEL_GATEWAY_ADDR"]["value"])
    svc = only(component(manifests, "Service", "model-gateway"), "model-gateway 的 Service")
    for d in worker_pools(manifests).values():
        target = env_of(main_container(d))["AR_MODEL_GATEWAY_ADDRESS"]["value"]
        assert target == f"{svc['metadata']['name']}:{svc['spec']['ports'][0]['port']}"
    assert svc["spec"]["ports"][0]["targetPort"] in (listen, "grpc")


# --- 环境变量与密钥 ---


def test_env_names_exist_in_the_code(manifests: list[dict[str, Any]]) -> None:
    text = source_text()
    for workload, c in containers(manifests):
        if workload["metadata"]["labels"].get(COMPONENT) == "otel-collector":
            continue
        for name in env_of(c):
            if name.startswith("AR_"):
                assert f'"{name}"' in text, f"{workload['metadata']['name']} 的环境变量 {name} 在代码里没有出现，可能拼错了"


def test_no_secret_values_are_rendered(manifests: list[dict[str, Any]]) -> None:
    assert not by_kind(manifests, "Secret"), "Chart 不创建 Secret：values 里只写已有 Secret 的名字"
    for workload, c in containers(manifests):
        for name, entry in env_of(c).items():
            if SENSITIVE_ENV.search(name) and name not in NOT_SECRET_ENV:
                assert "secretKeyRef" in entry.get("valueFrom", {}), f"{workload['metadata']['name']} 的 {name} 应取自 Secret，不能写明文"
    for cm in by_kind(manifests, "ConfigMap"):
        text = repr(cm.get("data", {}))
        assert not re.search(r"(?i)(sk-[a-z0-9]|BEGIN [A-Z ]*PRIVATE KEY)", text), f"ConfigMap {cm['metadata']['name']} 里像有密钥"


def test_sample_secret_values_never_leak(tmp_path: Path) -> None:
    """有人把密钥填进 values（例如 Langfuse 的认证）也不能进清单：Chart 只认 Secret 的名字。"""
    leaked = "PLAINTEXT-sk-should-not-render"
    values = tmp_path / "leaky.yaml"
    values.write_text(
        "external:\n  langfuse:\n    auth: " + leaked + "\n    authSecret:\n      name: ar-langfuse\n      key: basic-auth\n"
        "secrets:\n  devSigningKey: " + leaked + "\n",
        encoding="utf-8",
    )
    assert leaked not in repr(render(values))


def test_secrets_file_comes_from_a_secret_volume(manifests: list[dict[str, Any]]) -> None:
    for workload, c in containers(manifests):
        env = env_of(c)
        if "AR_SECRETS_FILE" not in env:
            continue
        path = env["AR_SECRETS_FILE"]["value"]
        mounts = {m["mountPath"]: m["name"] for m in c.get("volumeMounts", [])}
        mount = next((mp for mp in mounts if path.startswith(mp.rstrip("/") + "/")), None)
        assert mount, f"{workload['metadata']['name']} 的 AR_SECRETS_FILE 不在任何挂载点下"
        volumes = {v["name"]: v for v in pod_spec(workload).get("volumes", [])}
        assert "secret" in volumes[mounts[mount]], "SecretStore 的文件来自 Secret 卷"


# --- 公共约束 ---


def test_containers_are_locked_down(manifests: list[dict[str, Any]]) -> None:
    for workload, c in containers(manifests):
        where = f"{workload['kind']}/{workload['metadata']['name']}/{c['name']}"
        sc = c.get("securityContext", {})
        pod_sc = pod_spec(workload).get("securityContext", {})
        assert sc.get("runAsNonRoot", pod_sc.get("runAsNonRoot")) is True, f"{where} 要非 root"
        assert sc.get("readOnlyRootFilesystem") is True, f"{where} 要只读根文件系统"
        assert sc.get("allowPrivilegeEscalation") is False, f"{where} 不许提权"
        assert sc.get("capabilities", {}).get("drop") == ["ALL"], f"{where} 要去掉全部 capabilities"
        res = c.get("resources", {})
        for part in ("requests", "limits"):
            assert {"cpu", "memory"} <= set(res.get(part, {})), f"{where} 缺 resources.{part}"


def test_images_are_pinned(manifests: list[dict[str, Any]]) -> None:
    for workload, c in containers(manifests):
        image = c["image"]
        assert ":" in image.rsplit("/", 1)[-1] and not image.endswith(":latest"), f"{workload['metadata']['name']} 的镜像要固定版本：{image}"


# --- Telemetry 与 Learning ---


def test_every_service_exports_to_the_collector(manifests: list[dict[str, Any]]) -> None:
    svc = only(component(manifests, "Service", "otel-collector"), "Collector 的 Service")
    ports = {p["name"]: p["port"] for p in svc["spec"]["ports"]}
    assert ports.get("otlp-http") == OTLP_HTTP_PORT and ports.get("otlp-grpc") == 4317
    expected = f"http://{svc['metadata']['name']}:{OTLP_HTTP_PORT}"
    for name in ("task-service", "model-gateway", "memory", "worker", "learning"):
        workloads = [m for m in manifests if m.get("kind") in ("Deployment", "ScaledJob") and m["metadata"]["labels"].get(COMPONENT) == name]
        assert workloads, f"没有 {name}"
        for w in workloads:
            env = env_of(main_container(w))
            assert env["OTEL_EXPORTER_OTLP_ENDPOINT"]["value"] == expected, f"{w['metadata']['name']} 没指向 Collector"


def test_collector_runs_the_checked_in_config(manifests: list[dict[str, Any]]) -> None:
    deployment = only(component(manifests, "Deployment", "otel-collector"), "Collector 的 Deployment")
    c = main_container(deployment)
    assert "opentelemetry-collector-contrib" in c["image"], "kafka 导出器与 filter 处理器在 contrib 发行版里"
    cm = only(component(manifests, "ConfigMap", "otel-collector"), "Collector 的 ConfigMap")
    [content] = cm["data"].values()
    assert content == COLLECTOR_CONFIG.read_text(encoding="utf-8"), "ConfigMap 原样装入 files/otel-collector.yaml"
    env = env_of(c)
    assert "secretKeyRef" in env["LANGFUSE_AUTH"]["valueFrom"]
    for name in ("TEMPO_ENDPOINT", "LANGFUSE_ENDPOINT", "REDPANDA_BROKERS"):
        assert env[name].get("value"), f"Collector 缺 {name}"


def test_learning_is_a_scaled_job_on_the_spans_topic(manifests: list[dict[str, Any]]) -> None:
    job = only(by_kind(manifests, "ScaledJob"), "Learning Pipeline 的 ScaledJob")
    assert job["metadata"]["labels"][COMPONENT] == "learning"
    [trigger] = job["spec"]["triggers"]
    assert trigger["type"] == "kafka"
    meta = trigger["metadata"]
    assert (meta["topic"], meta["consumerGroup"]) == ("telemetry.spans", "learning")
    collector_env = env_of(main_container(only(component(manifests, "Deployment", "otel-collector"), "Collector")))
    assert meta["bootstrapServers"] == collector_env["REDPANDA_BROKERS"]["value"], "Learning 读的就是 Collector 写的那个 Redpanda"
    assert int(job["spec"]["maxReplicaCount"]) >= 1
    pod = pod_spec(job)
    assert pod["restartPolicy"] in ("Never", "OnFailure")
    args = [*main_container(job).get("command", []), *main_container(job).get("args", [])]
    assert "agentruntime_learning.pipeline" in " ".join(args)
