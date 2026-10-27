"""D-02、D-03、D-04：云上单节点的集群基线、数据层与 Runtime 上线（docs/plan/cloud-deploy.md）。

静态检查（总是跑）。线上的验收要真机器，脚本在 deploy/cluster/verify.sh、deploy/data/restore-drill.sh、scripts/sse_probe.py：
- 集群基线：k3s 加密 Secret、Pod Security 默认值、containerd 模板加了 runsc、Traefik 跳 https 且不压缩不缓冲；sandboxes 是
  restricted；RuntimeClass gvisor 与 kata；装脚本按 versions.conf 装、不直接把网上的脚本接给 sh、gVisor 核对 sha512。
- 数据层：Postgres 的角色都不是超级用户、没有 BYPASSRLS；每天备份到 R2；恢复演练从备份起新集群、不往原处归档。
- Runtime 的 Chart：入口是 Traefik 的 IngressRoute（或 Gateway API、通用 Ingress），不再是 nginx，只转 task-service 的 http 端口；
  Worker 在沙箱命名空间里只能管 Pod，读不了 Secret，别的服务不挂令牌；ObjectStore 的 S3 变量；可写的 SecretStore 加种子。
- 各处对得上：values-cloud.yaml 引用的卷、Secret、RuntimeClass、服务地址都有出处；内存的 requests 总和留得下沙箱。
设 AR_TEST_KIND=1 时在 kind 集群里核对 Worker 的 RBAC（kubectl auth can-i）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from deploy_tests import CLOUD_VALUES, REPO_ROOT, by_kind, containers, env_of, pod_spec, render, run

CLUSTER = REPO_ROOT / "deploy" / "cluster"
DATA = REPO_ROOT / "deploy" / "data"
WORKER = "app.kubernetes.io/component"


def docs(path: Path) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(path.read_text("utf-8")) if d]


def cluster_docs() -> list[dict[str, Any]]:
    return [d for name in ("namespaces", "sandboxes", "runtimeclasses", "workspaces", "storage") for d in docs(CLUSTER / f"{name}.yaml")]


def versions() -> dict[str, str]:
    out = {}
    for line in (CLUSTER / "versions.conf").read_text("utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def cloud_values() -> dict[str, Any]:
    return yaml.safe_load(CLOUD_VALUES.read_text("utf-8"))


@pytest.fixture(scope="module")
def cloud() -> list[dict[str, Any]]:
    return render(CLOUD_VALUES)


def mem_gi(value: str) -> float:
    match = re.fullmatch(r"([0-9.]+)(Mi|Gi|Ki|M|G)?", str(value))
    assert match, value
    number, unit = float(match.group(1)), match.group(2) or ""
    return number * {"Gi": 1, "G": 1, "Mi": 1 / 1024, "M": 1 / 1024, "Ki": 1 / 1024 / 1024, "": 1 / 1024**3}[unit]


SCRIPTS = [CLUSTER / "install.sh", CLUSTER / "verify.sh", DATA / "install.sh", DATA / "restore-drill.sh"]

# --- D-02 集群基线 ---


def test_versions_are_pinned_and_every_script_variable_is_defined() -> None:
    pinned = versions()
    for key, value in pinned.items():
        assert value and value.lower() != "latest" and re.search(r"\d", value), f"{key} 要写明版本"
    for script in SCRIPTS:
        for var in set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*_VERSION|GVISOR_RELEASE)\b", script.read_text("utf-8"))):
            assert var in pinned, f"{script.name} 用了 versions.conf 里没有的 {var}"


@pytest.mark.parametrize("script", SCRIPTS, ids=[s.relative_to(REPO_ROOT).as_posix() for s in SCRIPTS])
def test_scripts_fail_fast_and_never_pipe_downloads_into_a_shell(script: Path) -> None:
    text = script.read_text("utf-8")
    assert text.startswith("#!/usr/bin/env bash\n") and "set -euo pipefail" in text
    assert not re.search(r"curl[^\n|]*\|\s*(sudo\s+)?(ba)?sh\b", text), "先下载到文件再执行"
    assert "\r\n" not in text, "脚本要是 LF 换行"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 上的 bash 是 WSL 的，读不到网络盘；CI（Linux）上检查语法")
@pytest.mark.parametrize("script", SCRIPTS, ids=[s.name for s in SCRIPTS])
def test_scripts_parse(script: Path) -> None:
    bash = shutil.which("bash")
    assert bash, "要有 bash"
    run([bash, "-n", script])


def test_gvisor_binaries_are_checksummed() -> None:
    """gVisor 从 2026-07 起一个版本一个包（runsc、shim 与 gvisor-bin/），要整包解到同一个目录。"""
    text = (CLUSTER / "install.sh").read_text("utf-8")
    assert "gvisor.tar.bz2.sha512" in text and "sha512sum -c gvisor.tar.bz2.sha512" in text
    assert re.search(r"tar -xjf \S*gvisor\.tar\.bz2\"? -C /usr/local/bin", text), "runsc 在自己旁边找 gvisor-bin/"
    assert "INSTALL_K3S_VERSION=\"$K3S_VERSION\"" in text


def test_postgres_images_agree() -> None:
    images = {re.search(r"imageName: (\S+)", (DATA / f).read_text("utf-8")).group(1) for f in ("postgres.yaml", "restore-drill.yaml")}
    assert len(images) == 1, f"恢复演练要用与原集群相同的镜像：{images}"
    assert next(iter(images)).rsplit(":", 1)[1].startswith("17."), "与测试用的 PostgreSQL 17 同一大版本"


def test_k3s_config_encrypts_secrets_and_defaults_pod_security() -> None:
    config = yaml.safe_load((CLUSTER / "k3s" / "config.yaml").read_text("utf-8"))
    assert config["secrets-encryption"] is True
    assert config["write-kubeconfig-mode"] == "0600"
    assert "admission-control-config-file=/etc/rancher/k3s/psa.yaml" in config["kube-apiserver-arg"]
    assert "/etc/rancher/k3s/psa.yaml" in (CLUSTER / "install.sh").read_text("utf-8"), "install.sh 把 psa.yaml 放到 k3s 配置引用的位置"
    psa = yaml.safe_load((CLUSTER / "k3s" / "psa.yaml").read_text("utf-8"))
    defaults = psa["plugins"][0]["configuration"]["defaults"]
    assert defaults["enforce"] in ("baseline", "restricted")


def test_containerd_template_adds_runsc_to_the_k3s_base() -> None:
    text = (CLUSTER / "k3s" / "config-v3.toml.tmpl").read_text("utf-8")
    assert text.lstrip().startswith('{{ template "base" . }}'), "在 k3s 生成的配置之上追加"
    assert re.search(r"runtimes\.runsc\]\s*\n\s*runtime_type = \"io\.containerd\.runsc\.v1\"", text)


def test_traefik_redirects_to_https_and_does_not_buffer_sse() -> None:
    doc = yaml.safe_load((CLUSTER / "k3s" / "traefik-config.yaml").read_text("utf-8"))
    values = yaml.safe_load(doc["spec"]["valuesContent"])
    assert values["ports"]["web"]["redirections"]["entryPoint"]["to"] == "websecure"
    websecure = values["ports"]["websecure"]
    assert websecure["transport"]["respondingTimeouts"]["writeTimeout"] in ("0s", "0", 0), "SSE 是长连接，不设写超时"
    assert websecure["forwardedHeaders"]["trustedIPs"], "只信任 Cloudflare 转发的客户端地址"
    text = doc["spec"]["valuesContent"]
    assert "compress" not in text and "buffering" not in text, "全局不开压缩与缓冲（会攒住 SSE）"


def test_namespaces_and_sandbox_constraints() -> None:
    all_docs = cluster_docs()
    namespaces = {d["metadata"]["name"]: d for d in all_docs if d["kind"] == "Namespace"}
    assert set(namespaces) >= {"agent-runtime", "sandboxes", "data", "observability"}
    labels = namespaces["sandboxes"]["metadata"]["labels"]
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"
    policies = {d["metadata"]["name"]: d for d in all_docs if d["kind"] == "NetworkPolicy"}
    deny = policies["default-deny-all"]["spec"]
    assert deny["podSelector"] == {} and set(deny["policyTypes"]) == {"Ingress", "Egress"} and not deny.get("egress")
    assert {d["kind"] for d in all_docs if d["metadata"].get("namespace") == "sandboxes"} >= {"ResourceQuota", "LimitRange"}
    classes = {d["metadata"]["name"]: d["handler"] for d in all_docs if d["kind"] == "RuntimeClass"}
    assert classes == {"gvisor": "runsc", "kata": "kata-clh-runtime-rs"}, "Kata 4 默认的 Rust 运行时上的 Cloud Hypervisor"


# --- D-03 数据层 ---


def test_postgres_roles_cannot_bypass_row_level_security() -> None:
    cluster = next(d for d in docs(DATA / "postgres.yaml") if d["kind"] == "Cluster")
    spec = cluster["spec"]
    assert spec["enableSuperuserAccess"] is False
    roles = {r["name"]: r for r in spec["managed"]["roles"]}
    assert set(roles) == {"agentruntime", "temporal", "langfuse"}
    for name, role in roles.items():
        assert role["superuser"] is False and role["bypassrls"] is False, f"角色 {name} 不能是超级用户或带 BYPASSRLS（S-12a）"
        assert role["passwordSecret"]["name"] == f"pg-{name}"
    assert spec["bootstrap"]["initdb"]["owner"] == "agentruntime"
    databases = {d["spec"]["name"]: d["spec"]["owner"] for d in docs(DATA / "postgres.yaml") if d["kind"] == "Database"}
    assert databases == {"temporal": "temporal", "temporal_visibility": "temporal", "langfuse": "langfuse"}


def test_postgres_is_backed_up_daily_to_r2_and_the_drill_restores_without_archiving() -> None:
    all_docs = docs(DATA / "postgres.yaml")
    cluster = next(d for d in all_docs if d["kind"] == "Cluster")
    [plugin] = cluster["spec"]["plugins"]
    assert plugin["isWALArchiver"] is True
    store = next(d for d in all_docs if d["kind"] == "ObjectStore")
    assert store["metadata"]["name"] == plugin["parameters"]["barmanObjectName"]
    assert "r2.cloudflarestorage.com" in store["spec"]["configuration"]["endpointURL"]
    assert store["spec"]["retentionPolicy"]
    backup = next(d for d in all_docs if d["kind"] == "ScheduledBackup")
    assert backup["spec"]["cluster"]["name"] == cluster["metadata"]["name"] and backup["spec"]["method"] == "plugin"
    assert len(backup["spec"]["schedule"].split()) == 6, "CloudNativePG 的 cron 带秒"
    [drill] = docs(DATA / "restore-drill.yaml")
    assert "recovery" in drill["spec"]["bootstrap"] and "plugins" not in drill["spec"], "演练集群只读备份，不往原处归档 WAL"
    script = (DATA / "restore-drill.sh").read_text("utf-8")
    assert "count(*)" in script and "md5" in script and "diff" in script, "比对行数与校验和"


def test_data_namespace_only_admits_runtime_and_observability() -> None:
    [policy] = docs(DATA / "networkpolicy.yaml")
    allowed = policy["spec"]["ingress"][0]["from"][0]["namespaceSelector"]["matchExpressions"][0]["values"]
    assert "sandboxes" not in allowed and {"agent-runtime", "observability", "data"} <= set(allowed)


def test_temporal_namespace_retention_is_set_on_install() -> None:
    """Runtime 用的 Temporal 命名空间由 Chart 的 namespace Job 建，带保留期（S-06：短于业务对象的生命周期）。"""
    temporal = yaml.safe_load((DATA / "values" / "temporal.yaml").read_text("utf-8"))
    namespaces = temporal["server"]["config"]["namespaces"]
    assert namespaces["create"] is True
    wanted = cloud_values()["external"]["temporal"]["namespace"]
    [entry] = [n for n in namespaces["namespace"] if n["name"] == wanted]
    assert re.fullmatch(r"\d+d", entry["retention"])
    for store in temporal["server"]["config"]["persistence"]["datastores"].values():
        sql = store["sql"]
        assert sql["existingSecret"] == "pg-temporal" and "password" not in sql, "口令取 Secret，不写在 values 里"
        assert sql["createDatabase"] is False, "库由 CloudNativePG 的 Database 建"


# --- D-04 Runtime 的 Chart ---


def test_cloud_entry_is_a_traefik_route_to_the_task_api_only(cloud: list[dict[str, Any]]) -> None:
    assert not by_kind(cloud, "Ingress")
    routes = by_kind(cloud, "IngressRoute")
    [route] = [r for r in routes if r["metadata"]["name"].endswith("-task-service")]
    others = [r["metadata"]["name"] for r in routes if r is not route]
    assert all(name.endswith("-egress-gateway-preview") for name in others), f"入口只有 Task API 与预览地址（D-16）：{others}"
    [rule] = route["spec"]["routes"]
    [service] = rule["services"]
    assert service["name"].endswith("-task-service") and service["port"] == "http"
    assert "middlewares" not in rule, "不加会缓冲或压缩的中间件"
    assert route["spec"]["tls"]["secretName"] == cloud_values()["ingress"]["traefik"]["tlsSecretName"]


def test_nginx_is_gone_from_the_chart() -> None:
    chart = REPO_ROOT / "deploy" / "helm" / "agent-runtime"
    for path in [*chart.glob("*.yaml"), *(chart / "templates").glob("*")]:
        assert "nginx" not in path.read_text("utf-8").replace("ingress-nginx 2026 年 3 月起停止维护", ""), f"{path.name} 还在用 nginx"


@pytest.mark.parametrize(
    ("sets", "kind"),
    [
        (["ingress.enabled=true", "ingress.type=gateway", "ingress.gateway.parentRefs[0].name=public"], "HTTPRoute"),
        (["ingress.enabled=true", "ingress.type=ingress", "ingress.className=traefik"], "Ingress"),
    ],
    ids=["gateway", "ingress"],
)
def test_other_entry_types_render(sets: list[str], kind: str) -> None:
    manifests = render(set_values=sets)
    [entry] = by_kind(manifests, kind)
    assert "registry" not in repr(entry)


def render_fails(*sets: str, values: Path | None = None) -> str:
    helm = shutil.which("helm") or "helm"
    cmd: list[Any] = [helm, "template", "ar", REPO_ROOT / "deploy" / "helm" / "agent-runtime"]
    if values:
        cmd += ["--values", values]
    for s in sets:
        cmd += ["--set", s]
    proc = run(cmd, check=False)
    assert proc.returncode != 0, f"应当渲染失败：{sets}"
    return proc.stderr


@pytest.mark.parametrize(
    ("sets", "message"),
    [
        (["ingress.enabled=true", "ingress.type=gateway"], "parentRefs"),
        (["ingress.enabled=true", "ingress.type=nginx"], "ingress.type"),
        (["worker.shell.mode=docker"], "docker.sock"),
        (["objectStore.s3.endpoint=https://x.r2.cloudflarestorage.com"], "bucket"),
        (["objectStore.s3.endpoint=http://minio:9000", "objectStore.s3.bucket=b", "objectStore.s3.accessKeyId=a",
          "objectStore.s3.secretAccessKey.name=s"], "https"),
    ],
    ids=["gateway-without-parents", "unknown-entry", "docker-shell", "s3-incomplete", "s3-plain-http"],
)
def test_bad_values_are_refused(sets: list[str], message: str) -> None:
    assert message in render_fails(*sets)


@pytest.mark.parametrize(
    ("sets", "message"),
    [
        (["worker.workspacesClaim="], "workspacesClaim"),
        (["worker.shell.defaultBackend=nope"], "defaultBackend"),
        (["worker.shell.backends.gvisor.namespace=elsewhere"], "sandboxNamespace"),
    ],
    ids=["no-workspaces", "unknown-default", "foreign-namespace"],
)
def test_bad_sandbox_values_are_refused(sets: list[str], message: str) -> None:
    assert message in render_fails(*sets, values=CLOUD_VALUES)


def test_worker_may_only_manage_pods_in_the_sandbox_namespace(cloud: list[dict[str, Any]]) -> None:
    sandbox_ns = cloud_values()["worker"]["shell"]["sandboxNamespace"]
    # 构建的命名空间里另有一个只管构建 Job 的 Role（D-17，test_environment_builds_are_registered_and_confined）。
    [role] = [r for r in by_kind(cloud, "Role") if r["metadata"]["namespace"] == sandbox_ns]
    assert {r["metadata"]["namespace"] for r in by_kind(cloud, "Role")} <= {sandbox_ns, "sandbox-builds"}
    for rule in role["rules"]:
        assert rule["apiGroups"] == [""] and set(rule["resources"]) <= {"pods", "pods/proxy"}, "只管 Pod，读不了 Secret"
        assert set(rule["verbs"]) <= {"create", "get", "list", "patch", "delete", "update"}
    assert not by_kind(cloud, "ClusterRole") and not by_kind(cloud, "ClusterRoleBinding")
    [binding] = [b for b in by_kind(cloud, "RoleBinding") if b["metadata"]["namespace"] == sandbox_ns]
    assert binding["metadata"]["namespace"] == sandbox_ns and binding["roleRef"]["name"] == role["metadata"]["name"]
    [subject] = binding["subjects"]
    for workload, c in containers(cloud):
        spec = pod_spec(workload)
        if workload["metadata"]["labels"].get(WORKER) == "worker":
            assert spec["serviceAccountName"] == subject["name"] and spec["automountServiceAccountToken"] is True
            env = env_of(c)
            assert env["AR_TOOL_SHELL_SANDBOX"]["value"] == "sandbox"
            assert env["AR_SANDBOX_BACKENDS_FILE"]["value"].startswith("/etc/agentruntime/sandbox/")
            assert env["AR_TOOL_WORKSPACE_ROOT"]["value"] == "/workspaces"
        else:
            assert spec.get("automountServiceAccountToken") is False, f"{workload['metadata']['name']} 不调 K8s API，不挂令牌"


def test_api_proxy_backends_also_get_pods_proxy() -> None:
    manifests = render(CLOUD_VALUES, set_values=["worker.shell.backends.gvisor.connect=api_proxy"])
    [role] = [r for r in by_kind(manifests, "Role") if r["metadata"]["namespace"] == "sandboxes"]
    assert any("pods/proxy" in r["resources"] for r in role["rules"])
    [plain] = [r for r in by_kind(render(CLOUD_VALUES), "Role") if r["metadata"]["namespace"] == "sandboxes"]
    assert not any("pods/proxy" in r["resources"] for r in plain["rules"]), "pod_ip 时不给 pods/proxy"


def test_backend_registry_matches_the_cluster(cloud: list[dict[str, Any]]) -> None:
    [cm] = [c for c in by_kind(cloud, "ConfigMap") if c["metadata"]["name"].endswith("-sandbox-backends")]
    registry = json.loads(cm["data"]["backends.json"])
    classes = {d["metadata"]["name"] for d in cluster_docs() if d["kind"] == "RuntimeClass"}
    claims = {(d["metadata"]["namespace"], d["metadata"]["name"]) for d in cluster_docs() if d["kind"] == "PersistentVolumeClaim"}
    images = json.loads((REPO_ROOT / "deploy" / "images" / "images.json").read_text("utf-8"))["images"]
    assert registry["default"] in registry["backends"]
    for name, backend in registry["backends"].items():
        assert backend["kind"] == "k8s" and backend["runtime_class"] in classes, f"后端 {name} 的 RuntimeClass 不存在"
        assert (backend["namespace"], backend["workspace_claim"]) in claims, f"后端 {name} 的工作区卷不在 {backend['namespace']}"
        for key in ("image", "agent_image"):
            assert backend[key].rsplit("/", 1)[1].split(":")[0] in images, f"后端 {name} 的 {key} 不是我们构建的镜像"
    assert {b["isolation"] for b in registry["backends"].values()} == {"container", "microvm"}, "两种隔离强度都有"


def test_network_policy_lets_the_rendered_workers_reach_the_agent(cloud: list[dict[str, Any]]) -> None:
    policy = next(d for d in cluster_docs() if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "worker-to-agent")
    source = policy["spec"]["ingress"][0]["from"][0]
    assert source["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "agent-runtime"
    wanted = source["podSelector"]["matchLabels"]
    workers = [w for w in by_kind(cloud, "Deployment") if w["metadata"]["labels"].get(WORKER) == "worker"]
    assert workers and all(wanted.items() <= w["spec"]["template"]["metadata"]["labels"].items() for w in workers)
    assert policy["spec"]["ingress"][0]["ports"][0]["port"] == 7070


def test_cloud_volumes_exist_in_the_cluster_manifests() -> None:
    values = cloud_values()
    claims = {(d["metadata"]["namespace"], d["metadata"]["name"]) for d in cluster_docs() if d["kind"] == "PersistentVolumeClaim"}
    for claim in (values["objectStore"]["claimName"], values["secrets"]["storeClaim"], values["worker"]["workspacesClaim"]):
        assert ("agent-runtime", claim) in claims, f"values-cloud.yaml 引用的卷 {claim} 不在 deploy/cluster 里"


def test_s3_object_store_env_replaces_the_directory() -> None:
    manifests = render(set_values=[
        "objectStore.s3.endpoint=https://acct.r2.cloudflarestorage.com", "objectStore.s3.bucket=ar-objects",
        "objectStore.s3.accessKeyId=key-id", "objectStore.s3.secretAccessKey.name=agent-runtime-r2", "objectStore.s3.prefix=prod/",
    ])
    seen = 0
    for workload, c in containers(manifests):
        env = env_of(c)
        if "AR_S3_ENDPOINT" in env:
            seen += 1
            assert "AR_OBJECT_STORE_DIR" not in env
            assert env["AR_S3_SECRET_ACCESS_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "agent-runtime-r2"
            assert env["AR_S3_PREFIX"]["value"] == "prod/"
    components = {w["metadata"]["labels"].get(WORKER) for w, c in containers(manifests) if "AR_S3_ENDPOINT" in env_of(c)}
    assert components == {"task-service", "worker"}, "Claim-Check 的两端都换"
    assert seen


def test_cloud_service_addresses_match_the_data_layer() -> None:
    external = cloud_values()["external"]
    install = (DATA / "install.sh").read_text("utf-8")
    for release, namespace, address in [
        ("temporal", "data", external["temporal"]["address"]),
        ("qdrant", "data", external["qdrant"]["url"]),
        ("redpanda", "data", external["redpanda"]["brokers"]),
        ("tempo", "observability", external["tempo"]["endpoint"]),
        ("langfuse", "observability", external["langfuse"]["endpoint"]),
    ]:
        assert re.search(rf"helm upgrade --install {release} \S+ --version \S+ -n {namespace}\b", install), f"{release} 装在 {namespace}"
        assert f".{namespace}.svc" in address and release in address, f"{address} 对不上 {release}（{namespace}）"


# --- 密钥与资源 ---


def secret_refs() -> set[tuple[str, str]]:
    """values-cloud.yaml、数据层的清单与 values 里引用的 Secret（命名空间，名字）。"""
    refs: set[tuple[str, str]] = set()
    values = cloud_values()
    secrets = values["secrets"]
    refs |= {("agent-runtime", secrets["existingSecret"]), ("agent-runtime", secrets["relationalDsn"]["name"])}
    refs.add(("agent-runtime", values["external"]["langfuse"]["authSecret"]["name"]))
    refs.add(("agent-runtime", values["ingress"]["traefik"]["tlsSecretName"]))
    text = (DATA / "postgres.yaml").read_text("utf-8")
    refs |= {("data", n) for n in re.findall(r"(?:passwordSecret: \{name: |secret:\s*\n\s*name: |\{name: )(pg-[a-z]+|r2-[a-z]+)", text)}
    refs |= {("data", n) for n in re.findall(r"existingSecret: (\S+)", (DATA / "values" / "temporal.yaml").read_text("utf-8"))}
    for name in ("langfuse", "grafana"):
        body = (DATA / "values" / f"{name}.yaml").read_text("utf-8")
        refs |= {("observability", n) for n in re.findall(r"(?:existingSecret: |name: )([a-z0-9-]+)", body) if n not in ("Tempo",)}
    return refs


def test_every_referenced_secret_is_in_the_example_with_placeholders_only() -> None:
    example = [d for d in docs(CLUSTER / "secrets" / "secrets.example.yaml") if d["kind"] == "Secret"]
    defined = {(d["metadata"]["namespace"], d["metadata"]["name"]) for d in example}
    missing = secret_refs() - defined
    assert not missing, f"secrets.example.yaml 缺这些 Secret：{sorted(missing)}"
    for d in example:
        for key, value in d.get("stringData", {}).items():
            if key in ("username", "user"):
                continue
            assert "REPLACE_ME" in value, f"{d['metadata']['name']}.{key} 只能写占位符"
    ignored = run(["git", "check-ignore", "deploy/cluster/secrets/secrets.yaml"], check=False)
    assert ignored.returncode == 0, "明文的 secrets.yaml 要被 .gitignore 挡住"
    kept = run(["git", "check-ignore", "deploy/cluster/secrets/secrets.enc.yaml", "deploy/cluster/secrets/.sops.yaml"], check=False)
    assert kept.returncode == 1, "加密后的文件与 SOPS 规则要能提交"


def chart_memory_requests(manifests: list[dict[str, Any]]) -> float:
    total = 0.0
    so = {s["spec"]["scaleTargetRef"]["name"]: int(s["spec"]["maxReplicaCount"]) for s in by_kind(manifests, "ScaledObject")}
    for workload, c in containers(manifests):
        replicas = so.get(workload["metadata"]["name"], workload["spec"].get("replicas", 1) if workload["kind"] == "Deployment" else 1)
        total += mem_gi(c["resources"]["requests"]["memory"]) * replicas
    return total


def data_memory_requests() -> float:
    total = 0.0
    for path in [DATA / "postgres.yaml", *(DATA / "values").glob("*.yaml")]:
        for match in re.finditer(r"requests: \{cpu: [^,]+, memory: ([0-9.]+[MG]i)\}", path.read_text("utf-8")):
            total += mem_gi(match.group(1))
    redpanda = yaml.safe_load((DATA / "values" / "redpanda.yaml").read_text("utf-8"))
    return total + mem_gi(redpanda["resources"]["memory"]["container"]["max"])


def test_memory_requests_leave_room_for_sandboxes(cloud: list[dict[str, Any]]) -> None:
    quota = next(d for d in cluster_docs() if d["kind"] == "ResourceQuota")
    sandboxes = mem_gi(quota["spec"]["hard"]["requests.memory"])
    assert sandboxes >= 16, "沙箱至少留 16 GB（计划 D-03）"
    reserved = 3.5
    total = chart_memory_requests(cloud) + data_memory_requests() + sandboxes + reserved
    assert total <= 60, f"64 GB 的机器上 requests 合计 {total:.1f} GiB，超出预算（deploy/cluster/README.md）"


# --- D-07 Cloudflare 桥 ---


def test_bridge_dependencies_are_pinned_and_locked() -> None:
    """桥的 npm 依赖写明确切版本（不用 ^ 或 ~），package-lock.json 提交进仓库、与 package.json 对得上（npm ci 可复现）。"""
    bridge = REPO_ROOT / "deploy" / "cloudflare" / "sandbox-bridge"
    package = json.loads((bridge / "package.json").read_text("utf-8"))
    lock = json.loads((bridge / "package-lock.json").read_text("utf-8"))
    for group in ("dependencies", "devDependencies"):
        for name, version in package.get(group, {}).items():
            assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"{name} 要写确切版本，得到 {version}"
            assert lock["packages"][f"node_modules/{name}"]["version"] == version, f"package-lock.json 里 {name} 的版本对不上"


# --- D-10 面板 ---


def dashboard() -> dict[str, Any]:
    [cm] = docs(DATA / "dashboards" / "agent-runtime.yaml")
    assert cm["kind"] == "ConfigMap" and cm["metadata"]["namespace"] == "observability"
    grafana = yaml.safe_load((DATA / "values" / "grafana.yaml").read_text("utf-8"))
    assert grafana["sidecar"]["dashboards"]["label"] in cm["metadata"]["labels"], "Grafana 的 sidecar 按这个标签收面板"
    return json.loads(cm["data"]["agent-runtime.json"])


def worker_source() -> str:
    root = REPO_ROOT / "services" / "py-worker" / "src"
    return "\n".join(p.read_text("utf-8") for p in root.rglob("*.py"))


def test_dashboard_queries_spans_and_attributes_the_code_emits() -> None:
    board = dashboard()
    queries = [t["query"] for p in board["panels"] for t in p["targets"] if t.get("query")]
    assert queries
    source = worker_source()
    for query in queries:
        for name in re.findall(r'name = \\?"([^"\\]+)\\?"', query):
            assert f'"{name}"' in source, f"面板查的 span {name} 代码里没有"
        for attr in re.findall(r"span\.([a-z_.]+)", query):
            if attr in ("tenant_id",):
                continue  # 绑定租户的 Telemetry 自动加上
            assert f'"{attr}"' in source, f"面板用的属性 {attr} 代码里没有"
    titles = " ".join(p["title"] for p in board["panels"])
    for wanted in ("Task", "模型花费", "沙箱"):
        assert wanted in titles, f"面板缺“{wanted}”（D-10：任务数、各后端的沙箱时长与花费、模型花费）"


def test_tempo_can_answer_traceql_metrics() -> None:
    tempo = yaml.safe_load((DATA / "values" / "tempo.yaml").read_text("utf-8"))["tempo"]
    assert tempo["metricsGenerator"]["enabled"] is True
    assert "local-blocks" in tempo["overrides"]["defaults"]["metrics_generator"]["processors"]


def test_backends_have_per_tenant_quotas() -> None:
    backends = cloud_values()["worker"]["shell"]["backends"]
    for name, backend in backends.items():
        assert int(backend.get("max_envs_per_scope", 0)) > 0, f"后端 {name} 要有每个租户的沙箱配额（D-10）"


# --- kind 上核对 Worker 的 RBAC（AR_TEST_KIND=1） ---


@pytest.mark.skipif(os.environ.get("AR_TEST_KIND") != "1", reason="设 AR_TEST_KIND=1 才在 kind 集群里核对 RBAC")
def test_worker_rbac_on_a_real_api_server(cloud: list[dict[str, Any]], tmp_path: Path) -> None:
    kubectl = shutil.which("kubectl", path=os.environ.get("AR_DOCKER_BIN")) or shutil.which("kubectl")
    kind = os.environ.get("AR_KIND_BIN") or shutil.which("kind")
    assert kubectl and kind, "要有 kubectl 与 kind"
    cluster = os.environ.get("AR_KIND_CLUSTER", "agentruntime-test")
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text(run([kind, "get", "kubeconfig", "--name", cluster]).stdout, "utf-8")
    k = [kubectl, "--kubeconfig", kubeconfig]
    run([*k, "apply", "-f", CLUSTER / "namespaces.yaml"])
    rbac = [d for d in cloud if d["kind"] in ("ServiceAccount", "Role", "RoleBinding") and "-worker" in d["metadata"]["name"]]
    manifest = tmp_path / "rbac.yaml"
    for d in rbac:
        d["metadata"].setdefault("namespace", "agent-runtime")
    manifest.write_text(yaml.safe_dump_all(rbac), "utf-8")
    run([*k, "apply", "-f", manifest])
    try:
        sa = next(d["metadata"]["name"] for d in rbac if d["kind"] == "ServiceAccount")
        who = f"system:serviceaccount:agent-runtime:{sa}"

        def can(verb: str, resource: str, namespace: str, subresource: str = "") -> bool:
            # pods/exec 这种写法 kubectl 会当成名叫 exec 的 Pod；子资源要用 --subresource。
            extra = ["--subresource", subresource] if subresource else []
            out = run([*k, "auth", "can-i", verb, resource, *extra, "-n", namespace, "--as", who], check=False)
            return out.stdout.strip() == "yes"

        assert can("create", "pods", "sandboxes") and can("delete", "pods", "sandboxes") and can("patch", "pods", "sandboxes")
        assert not can("create", "pods", "agent-runtime"), "Worker 不能在别的命名空间建 Pod"
        assert not can("get", "secrets", "sandboxes") and not can("get", "secrets", "agent-runtime"), "Worker 读不了 Secret"
        assert not can("create", "pods", "sandboxes", "exec"), "不给 exec（agent 经 HTTP）"
        assert not can("get", "pods", "sandboxes", "proxy"), "pod_ip 时不给 pods/proxy"
    finally:
        run([*k, "delete", "-f", manifest, "--ignore-not-found"], check=False)


def test_sandbox_images_are_prepulled_on_every_node(cloud: list[dict[str, Any]]) -> None:
    """D-14：每个节点上预拉 kind=k8s 后端登记里的镜像（运行环境与 agent），满足沙箱命名空间的 restricted，断网。"""
    [ds] = [m for m in by_kind(cloud, "DaemonSet") if m["metadata"]["name"].endswith("-sandbox-prepull")]
    assert ds["metadata"]["namespace"] == "sandboxes"
    spec = ds["spec"]["template"]["spec"]
    pulled = {c["image"] for c in spec["initContainers"]}
    backends = cloud_values()["worker"]["shell"]["backends"]
    wanted = set()
    for b in backends.values():
        if b["kind"] == "k8s":
            wanted |= {b["image"], b["agent_image"], *b.get("images", {}).values()}
    assert wanted <= pulled, f"没有预拉：{sorted(wanted - pulled)}"
    assert spec["automountServiceAccountToken"] is False and spec["securityContext"]["runAsNonRoot"] is True
    assert ds["spec"]["template"]["metadata"]["labels"]["agentruntime.io/network"] == "none", "默认拒绝的网络策略管得到它"
    for c in [*spec["initContainers"], *spec["containers"]]:
        sc = c["securityContext"]
        assert sc["allowPrivilegeEscalation"] is False and sc["readOnlyRootFilesystem"] is True and sc["capabilities"]["drop"] == ["ALL"], c["name"]
        assert "limits" in c["resources"]


# --- D-15 包镜像 ---


def mirror_parts(manifests: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """包镜像的 Deployment、Service、它自己的 NetworkPolicy 与沙箱命名空间里放行到它的那条。"""
    [deployment] = [m for m in by_kind(manifests, "Deployment") if m["metadata"]["name"].endswith("-pkg-mirror")]
    [service] = [m for m in by_kind(manifests, "Service") if m["metadata"]["name"].endswith("-pkg-mirror")]
    policies = {m["metadata"]["name"]: m for m in by_kind(manifests, "NetworkPolicy")}
    own = next(p for name, p in policies.items() if name.endswith("-pkg-mirror"))
    return deployment, service, own, policies["sandboxes-to-pkg-mirror"]


def test_pkg_mirror_runs_from_its_own_volume_with_the_cloud_budgets(cloud: list[dict[str, Any]]) -> None:
    deployment, service, _, _ = mirror_parts(cloud)
    values = cloud_values()["pkgMirror"]
    assert deployment["spec"]["replicas"] == 1 and deployment["spec"]["strategy"]["type"] == "Recreate", "卷是 ReadWriteOnce：单副本、先停后起"
    spec = pod_spec(deployment)
    [c] = spec["containers"]
    env = env_of(c)
    assert c["args"] == ["serve"] and c["image"].rsplit("/", 1)[1].startswith("pkg-mirror:")
    assert env["AR_RELATIONAL_DSN"]["valueFrom"]["secretKeyRef"]["name"] == cloud_values()["secrets"]["relationalDsn"]["name"], "未命中的字节记进与 Worker 同一个业务库"
    assert env["AR_SECRETS_SEED_FILE"], "环境凭据的密钥取自 SecretStore 的平台作用域"
    assert env["AR_PKG_MIRROR_ADDR"]["value"] == ":8093" and env["AR_PKG_MIRROR_ADMIN_ADDR"]["value"] == ":8094"
    budgets = {k: int(env[k]["value"]) for k in ("AR_PKG_MIRROR_BLOB_BUDGET", "AR_PKG_MIRROR_META_BUDGET", "AR_PKG_MIRROR_MAX_FILE")}
    assert budgets == {"AR_PKG_MIRROR_BLOB_BUDGET": 100 << 30, "AR_PKG_MIRROR_META_BUDGET": 5 << 30, "AR_PKG_MIRROR_MAX_FILE": 500 << 20}
    [claim] = [m for m in by_kind(cloud, "PersistentVolumeClaim") if m["metadata"]["name"].endswith("-pkg-mirror")]
    assert mem_gi(claim["spec"]["resources"]["requests"]["storage"]) >= (100 + 5) * 1.1, "卷比两份预算的和大，留出余量"
    assert claim["spec"]["resources"]["requests"]["storage"] == values["storage"]
    mounts = {m["mountPath"]: m["name"] for m in c["volumeMounts"]}
    vols = {v["name"]: v for v in spec["volumes"]}
    assert vols[mounts["/cache"]]["persistentVolumeClaim"]["claimName"] == claim["metadata"]["name"]
    assert spec["automountServiceAccountToken"] is False and c["securityContext"]["readOnlyRootFilesystem"] is True
    assert {p["port"] for p in service["spec"]["ports"]} == {8093, 8094}


def test_workers_point_sandboxes_at_the_mirror_service(cloud: list[dict[str, Any]]) -> None:
    _, service, _, _ = mirror_parts(cloud)
    wanted = f"http://{service['metadata']['name']}.agent-runtime.svc:8093"
    workers = [m for m in by_kind(cloud, "Deployment") if "-worker-" in m["metadata"]["name"]]
    assert workers and all(env_of(c)["AR_PKG_MIRROR_URL"]["value"] == wanted for w in workers for c in pod_spec(w)["containers"])


def test_sandboxes_reach_only_the_mirror_and_the_mirror_reaches_only_the_internet(cloud: list[dict[str, Any]]) -> None:
    """W7：沙箱装依赖只经包镜像；镜像是集群里唯一能访问官方源的组件，它自己连不到私有网段与云元数据地址。"""
    deployment, _, own, outbound = mirror_parts(cloud)
    sandbox_ns = cloud_values()["worker"]["shell"]["sandboxNamespace"]
    mirror_labels = deployment["spec"]["template"]["metadata"]["labels"]
    # 沙箱命名空间一侧：选中适配器建的沙箱 Pod，只出到镜像的 8093 与 DNS。
    from agentruntime_core.adapters.k8s_sandbox import LABELS

    assert outbound["metadata"]["namespace"] == sandbox_ns and outbound["spec"]["policyTypes"] == ["Egress"]
    assert outbound["spec"]["podSelector"]["matchLabels"].items() <= LABELS.items(), "选中的是 K8s 适配器建的沙箱 Pod"
    to_mirror, to_dns = outbound["spec"]["egress"]
    [peer] = to_mirror["to"]
    assert peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "agent-runtime"
    assert peer["podSelector"]["matchLabels"].items() <= mirror_labels.items()
    assert [p["port"] for p in to_mirror["ports"]] == [8093], "管理端口不对沙箱开放"
    assert to_dns["to"][0]["podSelector"]["matchLabels"] == {"k8s-app": "kube-dns"} and {p["port"] for p in to_dns["ports"]} == {53}
    # 镜像一侧：8093 收沙箱与 Runtime 的，8094 只收 Runtime 与 observability 的。
    public, admin = own["spec"]["ingress"]
    sources = lambda rule: {f["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] for f in rule["from"]}  # noqa: E731
    assert [p["port"] for p in public["ports"]] == [8093] and sandbox_ns in sources(public)
    assert [p["port"] for p in admin["ports"]] == [8094] and sandbox_ns not in sources(admin)
    internet = [r for r in own["spec"]["egress"] if any("ipBlock" in t for t in r.get("to", []))]
    [rule] = internet
    block = rule["to"][0]["ipBlock"]
    assert block["cidr"] == "0.0.0.0/0" and {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10"} <= set(block["except"])
    assert {p["port"] for p in rule["ports"]} == {80, 443}, "只出 http 与 https"


def test_pkg_mirror_is_off_by_default_and_needs_the_shared_business_database() -> None:
    manifests = render()
    assert not [m for m in manifests if "pkg-mirror" in m["metadata"]["name"]]
    assert all("AR_PKG_MIRROR_URL" not in env_of(c) for _, c in containers(manifests))
    assert "secrets.relationalDsn" in render_fails("pkgMirror.enabled=true")


def test_secret_example_has_the_credential_key_and_the_mirror_admin_token() -> None:
    text = (CLUSTER / "secrets" / "secrets.example.yaml").read_text("utf-8")
    for name in ("sandbox/env-credential-key", "pkg-mirror/admin-token"):
        assert f'"{name}": "REPLACE_ME"' in text, f"SecretStore 的种子里要有 {name}（占位符）"


def test_worker_runs_shell_in_the_universal_runtime(cloud: list[dict[str, Any]]) -> None:
    """D-14：集群里 shell_run 默认用多语言的 universal；每个 K8s 后端都把它映射到镜像。"""
    workers = [m for m in by_kind(cloud, "Deployment") if "-worker-" in m["metadata"]["name"]]
    assert workers and all(env_of(c)["AR_SANDBOX_RUNTIME"]["value"] == "universal" for w in workers for c in pod_spec(w)["containers"])
    [cm] = [m for m in by_kind(cloud, "ConfigMap") if m["metadata"]["name"].endswith("-sandbox-backends")]
    registry = json.loads(cm["data"]["backends.json"])
    for name, b in registry["backends"].items():
        if b["kind"] == "k8s":
            assert "universal" in b.get("images", {}), f"K8s 后端 {name} 要给出 universal 的镜像"



# --- D-16 出网网关 ---


def gateway_parts(manifests: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """出网网关的 Deployment、Service、它自己的 NetworkPolicy 与沙箱命名空间里放行到它的那条。"""
    [deployment] = [m for m in by_kind(manifests, "Deployment") if m["metadata"]["name"].endswith("-egress-gateway")]
    [service] = [m for m in by_kind(manifests, "Service") if m["metadata"]["name"].endswith("-egress-gateway")]
    policies = {m["metadata"]["name"]: m for m in by_kind(manifests, "NetworkPolicy")}
    own = next(p for name, p in policies.items() if name.endswith("-egress-gateway"))
    return deployment, service, own, policies["sandboxes-egress-gateway"]


def test_egress_gateway_is_a_single_replica_on_the_shared_business_database(cloud: list[dict[str, Any]]) -> None:
    deployment, service, _, _ = gateway_parts(cloud)
    assert deployment["spec"]["replicas"] == 1, "授权的用量计数在进程里：单副本，免得各算各的把上限放大"
    spec = pod_spec(deployment)
    [c] = spec["containers"]
    env = env_of(c)
    assert c["args"] == ["serve"] and c["image"].rsplit("/", 1)[1].startswith("egress-gateway:")
    assert env["AR_RELATIONAL_DSN"]["valueFrom"]["secretKeyRef"]["name"] == cloud_values()["secrets"]["relationalDsn"]["name"], "授权与审计在与 Worker 同一个业务库"
    assert env["AR_EGRESS_ADDR"]["value"] == ":8095" and env["AR_EGRESS_ADMIN_ADDR"]["value"] == ":8096" and env["AR_EGRESS_PREVIEW_ADDR"]["value"] == ":8097"
    assert {"10.42.0.0/16", "10.43.0.0/16"} <= set(env["AR_EGRESS_DENY_CIDRS"]["value"].split(",")), "集群的 Pod 与 Service 网段再拒一遍"
    assert env["AR_EGRESS_PREVIEW_DOMAIN"]["value"] == cloud_values()["egressGateway"]["previewDomain"]
    assert spec["automountServiceAccountToken"] is False and c["securityContext"]["readOnlyRootFilesystem"] is True
    assert {p["port"] for p in service["spec"]["ports"]} == {8095, 8096, 8097}


def test_sandboxes_reach_the_internet_only_through_the_gateway(cloud: list[dict[str, Any]]) -> None:
    """W8：沙箱只出到网关的 8095（另外是包镜像）；网关的管理端口只给 Runtime 与 observability，预览端口只给入口控制器；
    网关自己连不到私有网段与云元数据，能连进沙箱 Pod（预览）。"""
    from agentruntime_core.adapters.k8s_sandbox import LABELS

    deployment, _, own, sandbox_side = gateway_parts(cloud)
    sandbox_ns = cloud_values()["worker"]["shell"]["sandboxNamespace"]
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    assert sandbox_side["metadata"]["namespace"] == sandbox_ns and set(sandbox_side["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    assert sandbox_side["spec"]["podSelector"]["matchLabels"].items() <= LABELS.items()
    [out] = sandbox_side["spec"]["egress"]
    assert out["to"][0]["podSelector"]["matchLabels"].items() <= labels.items() and [p["port"] for p in out["ports"]] == [8095], "管理与预览端口不对沙箱开放"
    [inbound] = sandbox_side["spec"]["ingress"]
    assert inbound["from"][0]["podSelector"]["matchLabels"].items() <= labels.items(), "进沙箱的只有网关（预览）"
    proxy, admin, preview = own["spec"]["ingress"]
    sources = lambda rule: {f["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] for f in rule["from"]}  # noqa: E731
    assert [p["port"] for p in proxy["ports"]] == [8095] and sandbox_ns in sources(proxy)
    assert [p["port"] for p in admin["ports"]] == [8096] and sandbox_ns not in sources(admin)
    assert [p["port"] for p in preview["ports"]] == [8097] and sources(preview) == {"kube-system"}
    [rule] = [r for r in own["spec"]["egress"] if any("ipBlock" in t for t in r.get("to", []))]
    block = rule["to"][0]["ipBlock"]
    assert block["cidr"] == "0.0.0.0/0" and {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10", "127.0.0.0/8"} <= set(block["except"])


def test_preview_route_matches_only_environment_hosts(cloud: list[dict[str, Any]]) -> None:
    [route] = [m for m in by_kind(cloud, "IngressRoute") if m["metadata"]["name"].endswith("-egress-gateway-preview")]
    [r] = route["spec"]["routes"]
    domain = cloud_values()["egressGateway"]["previewDomain"]
    assert r["match"] == "HostRegexp(`^sbe-[0-9a-f]{16}\\." + domain.replace(".", "\\.") + "$`)"
    assert r["services"][0]["port"] == "preview" and route["spec"]["tls"]["secretName"] == cloud_values()["egressGateway"]["previewTLSSecretName"]


def test_workers_and_task_service_point_at_the_gateway(cloud: list[dict[str, Any]]) -> None:
    _, service, _, _ = gateway_parts(cloud)
    workers = [m for m in by_kind(cloud, "Deployment") if "-worker-" in m["metadata"]["name"]]
    proxy = f"http://{service['metadata']['name']}.agent-runtime.svc:8095"
    assert workers and all(env_of(c)["AR_EGRESS_GATEWAY_URL"]["value"] == proxy for w in workers for c in pod_spec(w)["containers"])
    assert all(env_of(c)["AR_PREVIEW_DOMAIN"]["value"] == cloud_values()["egressGateway"]["previewDomain"] for w in workers for c in pod_spec(w)["containers"])
    [ts] = [m for m in by_kind(cloud, "Deployment") if m["metadata"]["name"].endswith("-task-service")]
    assert env_of(pod_spec(ts)["containers"][0])["AR_EGRESS_GATEWAY_ADMIN_URL"]["value"] == f"http://{service['metadata']['name']}.agent-runtime.svc:8096"


def test_egress_gateway_is_off_by_default_and_needs_the_shared_business_database() -> None:
    manifests = render()
    assert not [m for m in manifests if "egress-gateway" in m["metadata"]["name"]]
    assert all("AR_EGRESS_GATEWAY_URL" not in env_of(c) and "AR_EGRESS_GATEWAY_ADMIN_URL" not in env_of(c) for _, c in containers(manifests))
    assert "secrets.relationalDsn" in render_fails("egressGateway.enabled=true")
    text = (CLUSTER / "secrets" / "secrets.example.yaml").read_text("utf-8")
    assert '"egress-gateway/admin-token": "REPLACE_ME"' in text


# --- D-17 按项目定制的运行环境 ---


def test_environment_builds_are_registered_and_confined(cloud: list[dict[str, Any]]) -> None:
    """D-17：后端登记里有 environments 一节；Worker 只在构建的命名空间里管 Job（读不了 Secret）；构建只连网关与包镜像；
    构建的命名空间只许 Kata、不许宿主网络与宿主路径（deploy/cluster/builds.yaml）。"""
    [cm] = [m for m in by_kind(cloud, "ConfigMap") if m["metadata"]["name"].endswith("-sandbox-backends")]
    env = json.loads(cm["data"]["backends.json"])["environments"]
    values = cloud_values()["worker"]["shell"]["environments"]
    assert env["builder"]["runtime_class"] == "kata" and env["builder"]["isolation"] == "microvm" and env["builder"]["namespace"] == "sandbox-builds"
    assert env["registry"]["pull_host"] == values["registry"]["pullHost"] and env["registry"]["url"].endswith("-env-registry.agent-runtime.svc:5000")
    assert env["base_image"] == values["baseImage"] and env["limits"]["max_images"] >= 1
    [role] = [m for m in by_kind(cloud, "Role") if m["metadata"]["namespace"] == "sandbox-builds"]
    rules = {(tuple(r["apiGroups"]), tuple(r["resources"])): set(r["verbs"]) for r in role["rules"]}
    assert rules[(("",), ("configmaps", "secrets"))] == {"create", "delete"}, "读不了 Secret"
    assert rules[(("batch",), ("jobs",))] == {"create", "get", "delete"}
    [policy] = [m for m in by_kind(cloud, "NetworkPolicy") if m["metadata"]["name"] == "builds-egress"]
    ports = sorted(p["port"] for rule in policy["spec"]["egress"] for p in rule.get("ports", []))
    assert ports == [53, 53, 8093, 8095], "构建只连网关、包镜像与 DNS"
    builds = (CLUSTER / "builds.yaml").read_text("utf-8")
    assert "runtimeClassName == 'kata'" in builds and "hostPath" in builds and "default-deny-all" in builds


def test_env_registry_takes_pushes_only_from_the_runtime(cloud: list[dict[str, Any]]) -> None:
    [deployment] = [m for m in by_kind(cloud, "Deployment") if m["metadata"]["name"].endswith("-env-registry")]
    [c] = pod_spec(deployment)["containers"]
    [cm] = [m for m in by_kind(cloud, "ConfigMap") if m["metadata"]["name"].endswith("-env-registry")]
    config = yaml.safe_load(cm["data"]["config.yml"])
    assert config["auth"]["htpasswd"]["path"] == "/auth/htpasswd" and config["storage"]["delete"]["enabled"] is True
    assert {m["mountPath"] for m in c["volumeMounts"]} >= {"/auth", "/etc/docker/registry", "/var/lib/registry"}
    [service] = [m for m in by_kind(cloud, "Service") if m["metadata"]["name"].endswith("-env-registry")]
    assert service["spec"]["type"] == "NodePort" and service["spec"]["ports"][0]["nodePort"] == 30500
    [policy] = [m for m in by_kind(cloud, "NetworkPolicy") if m["metadata"]["name"].endswith("-env-registry")]
    assert policy["spec"]["egress"] == [] and policy["spec"]["ingress"][0]["from"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "agent-runtime"
    text = (CLUSTER / "secrets" / "secrets.example.yaml").read_text("utf-8")
    assert '"sandbox/env-registry": "REPLACE_ME"' in text and "agent-runtime-env-registry" in text
    assert (CLUSTER / "k3s" / "registries.yaml.tmpl").is_file()


def test_environments_need_the_egress_gateway() -> None:
    message = render_fails("worker.shell.mode=sandbox", "worker.workspacesClaim=w", "worker.shell.backends.local.kind=local",
                           "worker.shell.defaultBackend=local", "worker.shell.environments.enabled=true", "worker.shell.environments.registry.pullHost=h:1")
    assert "egressGateway" in message


def test_workers_can_collect_build_outputs_and_builds_read_only_their_workspace(cloud: list[dict[str, Any]]) -> None:
    """构建产物由 Worker 去取（构建 Pod 里的 sandbox-agent，7070）：构建的命名空间只放行 Worker 进来；构建读仓库里的文件
    经工作区目录的第三个静态 PV（只读）；Kata 的 handler 要 privileged_without_host_devices（install.sh 核对）。"""
    builds = docs(CLUSTER / "builds.yaml")
    [ingress] = [d for d in builds if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "worker-to-builder"]
    from agentruntime_worker.sandbox.env_builder import AGENT_PORT, MANAGED_BY

    assert ingress["spec"]["podSelector"]["matchLabels"] == {"app.kubernetes.io/managed-by": MANAGED_BY}
    [rule] = ingress["spec"]["ingress"]
    assert [p["port"] for p in rule["ports"]] == [AGENT_PORT]
    source = rule["from"][0]
    assert source["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "agent-runtime"
    workers = [w for w in by_kind(cloud, "Deployment") if w["metadata"]["labels"].get(WORKER) == "worker"]
    assert workers and all(source["podSelector"]["matchLabels"].items() <= w["spec"]["template"]["metadata"]["labels"].items() for w in workers)
    [cm] = [m for m in by_kind(cloud, "ConfigMap") if m["metadata"]["name"].endswith("-sandbox-backends")]
    claim = json.loads(cm["data"]["backends.json"])["environments"]["builder"]["workspace_claim"]
    manifests = [*cluster_docs(), *builds]
    pvcs = {(d["metadata"]["namespace"], d["metadata"]["name"]): d for d in manifests if d["kind"] == "PersistentVolumeClaim"}
    assert pvcs[("sandbox-builds", claim)]["spec"]["accessModes"] == ["ReadOnlyMany"], "构建只读仓库里的文件"
    pvs = {d["metadata"]["name"]: d for d in manifests if d["kind"] == "PersistentVolume"}
    paths = {pv["spec"]["hostPath"]["path"] for pv in pvs.values() if pv["metadata"]["labels"].get("agentruntime.io/volume") == "workspaces"}
    assert len(paths) == 1 and pvs[pvcs[("sandbox-builds", claim)]["spec"]["volumeName"]]["spec"]["claimRef"]["namespace"] == "sandbox-builds"
    assert "privileged_without_host_devices" in (CLUSTER / "install.sh").read_text("utf-8")