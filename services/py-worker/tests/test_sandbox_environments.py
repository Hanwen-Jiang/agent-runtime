"""D-17：按项目定制的运行环境（设计书改动稿 W6 第 3 条；docs/plan/cloud-deploy.md D-17 的验收）。

1. 带 .tool-versions 的仓库第一次用时构建、第二次直接用缓存（构建时长记在 sandbox_images.build_ms；真实的耗时要在集群里量）。
2. 恶意的 Dockerfile（访问宿主、内网、别的租户的镜像）：静态检查拒绝，不起构建；构建的 Job 只在 Kata 里、不挂 ServiceAccount
   令牌与宿主路径、不用宿主网络，构建里没有镜像仓库的凭据，取回产物的令牌只给提供产物的那一步；产物只推到本租户的路径。
3. 超过大小上限或超时的构建被终止，环境退回 universal 并说明原因；个数配额；长期不用的镜像清掉。
"""

from __future__ import annotations

import io
import json
import os
import time
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

import pytest

from agentruntime_core.adapters.oci_registry import RegistryClient
from agentruntime_core.adapters.ocitest import FakeRegistry, oci_tar
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.errors import InvalidError, TransientError
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.sandbox.broker import BUILD_BACKEND, BuildNetwork, SandboxBroker
from agentruntime_worker.sandbox.credentials import EnvCredentials
from agentruntime_worker.sandbox.env_builder import BuilderConfig, K8sJobBuilder
from agentruntime_worker.sandbox.environments import (
    BuildJob, BuildOutcome, EnvironmentManager, Limits, RegistryPath, check_dockerfile, detect, strip_jsonc, tool_versions_dockerfile,
    workspace_reader,
)
from agentruntime_worker.sandbox.ledger import SandboxLedger
from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools
from agentruntime_worker.tools.sandboxes import Sandboxes

T1 = TenantContext(tenant_id="t1", user_id="u1", workspace_id="w1", session_id="ses_01HZY2J5K3M4N5P6Q7R8S9T0VW")
BASE = "ghcr.io/agentruntime/sandbox-universal:0.1.0"
BASE_DIGEST = "sha256:" + "ab" * 32
REGISTRY = RegistryPath(host="127.0.0.1:30500", prefix="envs")
TOOL_VERSIONS = "python 3.12.7\nnodejs 20.18.0  # LTS\ngolang 1.23.2\n"
BUILD_HOSTS = ("registry-1.docker.io", "auth.docker.io", "production.cloudflare.docker.com")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class FakeBuilder:
    """不起 Job：按场景把一个 OCI 打包写给 Worker，或报超时、出错。记下拿到的 BuildJob 与构建时的环境表。"""

    def __init__(self, broker: SandboxBroker) -> None:
        self.broker = broker
        self.jobs: list[BuildJob] = []
        self.mode = "ok"
        # mode 为 reason 时原样报这个失败原因（看存下来与给用户看的原因有没有去掉签名之类的东西）。
        self.reason = ""
        self.during: dict[str, Any] = {}

    async def build(self, job: BuildJob, out: IO[bytes]) -> BuildOutcome:
        self.jobs.append(job)
        assert self.broker.ledger is not None and self.broker.grants is not None
        active = await self.broker.ledger.active(job.owner)
        self.during = {
            "envs": [(r.backend, r.task_id, r.state) for r in active],
            "grants": [(g.task_id, g.hosts, g.approved_by) for r in active for g in await self.broker.grants.for_task(job.owner, r.task_id)],
        }
        if self.mode == "timeout":
            return BuildOutcome(ok=False, reason=f"构建超时（{int(job.timeout.total_seconds())} 秒）", failure="timed_out")
        if self.mode == "error":
            raise TransientError("kube_unavailable", "连不上 Kubernetes API")
        if self.mode == "reason":
            return BuildOutcome(ok=False, reason=self.reason, failure="failed")
        layers = [os.urandom(4096)] if self.mode == "big" else [b"tool-versions-layer"]
        out.write(oci_tar(layers))
        return BuildOutcome(ok=True)


@pytest.fixture
def registry() -> Iterator[FakeRegistry]:
    r = FakeRegistry()
    yield r
    r.close()


def setup(tmp_path: Path, registry: FakeRegistry, *, limits: Limits = Limits(), backend: Mapping[str, Any] | None = None) -> tuple[EnvironmentManager, FakeBuilder, list[tuple[str, str]], Clock, SandboxBroker]:
    workspaces = tmp_path / "workspaces"
    (workspaces / "t1" / "w1").mkdir(parents=True, exist_ok=True)
    clock = Clock()

    async def key() -> bytes:
        return bytes(range(32))

    async def lookup(tenant_id: str) -> Mapping[str, Any] | None:
        return {"sandbox": {"backend": "local"}}

    boxes = Sandboxes({"local": dict(backend or {"kind": "local", "isolation": "none", "custom_images": True})}, "local", workspace_root=workspaces, policy=lookup)
    ledger = SandboxLedger(SQLiteRelationalStoreProvider(tmp_path / "relational"), clock=clock)
    broker = SandboxBroker(boxes, ledger, credentials=EnvCredentials(key, mirror_url="http://pkg-mirror.test:8093", egress_url="http://egress.test:8095"))
    started: list[tuple[str, str]] = []

    async def starter(owner: str, key: str) -> None:
        started.append((owner, key))

    manager = EnvironmentManager(
        ledger, base_image=BASE, base_digest=BASE_DIGEST, registry=REGISTRY, builder=None,
        pusher=RegistryClient(registry.url, username="pusher", password="s3cret", pull_host=REGISTRY.host),
        starter=starter, limits=limits, network=BuildNetwork(broker, BUILD_HOSTS),
    )
    builder = FakeBuilder(broker)
    manager.builder = builder
    broker.environments = manager
    return manager, builder, started, clock, broker


def write(tmp_path: Path, files: Mapping[str, str]) -> Path:
    root = tmp_path / "workspaces" / "t1" / "w1"
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, "utf-8", newline="\n")
    return root


# --- 识别与生成 ---


async def test_detection_order_and_devcontainer_forms(tmp_path: Path) -> None:
    root = write(tmp_path, {".tool-versions": TOOL_VERSIONS, "Dockerfile": "FROM python:3.13-slim\n"})
    found = await detect(workspace_reader(root), base_image=BASE)
    assert found is not None and found.kind == "tool-versions" and not found.problem
    write(tmp_path, {".devcontainer/devcontainer.json": """{
      // 开发环境
      "build": {"dockerfile": "../Dockerfile", "context": "..", "args": {"VARIANT": "3.13"},},
      /* 结尾逗号与注释都允许 */
      "name": "has // in a string",
    }""", "Dockerfile": "# syntax=docker/dockerfile:1.7\nFROM python:${VARIANT}-slim\n"})
    found = await detect(workspace_reader(root), base_image=BASE)
    assert found is not None and found.kind == "devcontainer" and found.context == "." and not found.problem
    assert found.dockerfile.startswith("# syntax=docker/dockerfile:1.7\nARG VARIANT=\"3.13\"\nFROM"), "ARG 放在解析指令之后、FROM 之前"
    assert json.loads(strip_jsonc('{"a": "x // y", /* c */ "b": [1, 2,],}')) == {"a": "x // y", "b": [1, 2]}
    for doc, problem in (
        ('{"image": "mcr.microsoft.com/devcontainers/python:3.12", "features": {"ghcr.io/devcontainers/features/node:1": {}}}', "features"),
        ('{"dockerComposeFile": "compose.yml"}', "dockerComposeFile"),
        ('{"build": {"dockerfile": "../../../etc/Dockerfile"}}', "出了仓库"),
        ('{"name": "nothing"}', "既没有 image"),
    ):
        write(tmp_path, {".devcontainer/devcontainer.json": doc})
        found = await detect(workspace_reader(root), base_image=BASE)
        assert found is not None and problem in found.problem, (doc, found)
    write(tmp_path, {".devcontainer/devcontainer.json": '{"image": "mcr.microsoft.com/devcontainers/python:3.12"}'})
    found = await detect(workspace_reader(root), base_image=BASE)
    assert found is not None and found.dockerfile.startswith("FROM mcr.microsoft.com/devcontainers/python:3.12\n")


def test_tool_versions_become_pinned_layers_on_universal() -> None:
    base = "ghcr.io/agentruntime/sandbox-universal@" + BASE_DIGEST
    dockerfile, problem = tool_versions_dockerfile(TOOL_VERSIONS + "rust 1.82.0\njava temurin-21.0.5+11.0.LTS\n", base)
    assert not problem
    for line in ("FROM python:3.12.7-slim-bookworm AS python", "FROM node:20.18.0-bookworm-slim AS nodejs", "FROM golang:1.23.2-bookworm AS golang",
                 "FROM rust:1.82.0-slim-bookworm AS rust", "FROM eclipse-temurin:21.0.5_11-jdk-jammy AS java", f"FROM {base}", "USER 10001:10001"):
        assert line in dockerfile.splitlines(), line
    assert "LD_LIBRARY_PATH" in dockerfile.split("AGENT_ENV_PASSTHROUGH=")[1], "sandbox-agent 要把 Python 的库路径交给命令"
    assert check_dockerfile(dockerfile, tenant_id="t1", registry=REGISTRY) == []
    for text, problem in (("nodejs lts\n", "三段"), ("ruby 3.3.0\n", "还不支持"), ("", "空的"), ("python 3.12.7\npython 3.13.0\n", "两次"),
                          ("java 21\n", "temurin")):
        assert problem in tool_versions_dockerfile(text, base)[1], text


def test_malicious_dockerfiles_are_refused() -> None:
    cases = {
        "FROM 127.0.0.1:30500/envs/t2/env@sha256:" + "0" * 64: "别的路径",
        "FROM 127.0.0.1:30500/envs/t2/env:latest": "别的路径",
        "FROM 10.43.0.10:5000/internal/base:1": "内网",
        "FROM localhost:5000/base": "内网",
        "FROM registry.agent-runtime.svc.cluster.local:5000/x": "内网",
        "FROM python:3.13\nCOPY --from=127.0.0.1:30500/envs/t2/env:x /secret /": "别的路径",
        "FROM python:3.13\nRUN --network=host curl http://169.254.169.254/": "network=host",
        "FROM python:3.13\nRUN --security=insecure mount /dev/sda1 /mnt": "insecure",
        "FROM python:3.13\nRUN --mount=type=secret,id=aws cat /run/secrets/aws": "secret",
        "FROM python:3.13\nRUN --mount=type=ssh git clone git@github.com:x/y": "ssh",
        "FROM python:3.13\nRUN --mount=type=bind,from=10.0.0.5:5000/x,target=/x true": "内网",
        "FROM python:3.13\nADD http://169.254.169.254/latest/meta-data/ /m": "ADD",
        "FROM python:3.13\nADD http://2130706433/x /x": "ADD",
        "# syntax=evil.example.com/frontend:1\nFROM python:3.13": "syntax",
        "FROM ${BASE}": "变量",
    }
    for text, problem in cases.items():
        problems = check_dockerfile(text, tenant_id="t1", registry=REGISTRY)
        assert any(problem in p for p in problems), (text, problems)
    own = "FROM 127.0.0.1:30500/envs/t1/env@sha256:" + "1" * 64 + " AS base\nFROM base\nRUN echo ok\n"
    assert check_dockerfile(own, tenant_id="t1", registry=REGISTRY) == [], "自己路径下的镜像与前面的阶段可以用"


# --- 构建、缓存与退回 ---


async def test_first_use_builds_and_second_use_hits_the_cache(tmp_path: Path, registry: FakeRegistry) -> None:
    """验收 1。"""
    manager, builder, started, _, broker = setup(tmp_path, registry)
    root = write(tmp_path, {".tool-versions": TOOL_VERSIONS})
    t0 = time.monotonic()
    first = await manager.resolve(T1, workspace_reader(root))
    assert first.state == "building" and first.image == "" and "universal" in first.note and started == [("t1", first.key)]
    record = await manager.run_build("t1", first.key)  # 构建工作流的 activity
    first_ms = (time.monotonic() - t0) * 1000
    assert record.state == "ready" and record.image.startswith("127.0.0.1:30500/envs/t1/env@sha256:") and record.build_ms >= 0
    [job] = builder.jobs
    assert job.dockerfile.startswith("# syntax=docker/dockerfile:1.7") and "ghcr.io/agentruntime/sandbox-universal@" + BASE_DIGEST in job.dockerfile
    assert job.env["HTTPS_PROXY"].startswith("http://ar:ar1.") and job.env["PIP_INDEX_URL"]
    assert ("envs/t1/env", first.key[:32]) in registry.manifests and {repo for repo, _ in registry.blobs} == {"envs/t1/env"}, "产物只推到本租户的路径"
    # 构建期间：环境表里一行 env-builder、一条平台批准的授权（只放行登记的主机）；结束后都收回。
    [(backend, task, state)] = builder.during["envs"]
    assert backend == BUILD_BACKEND and state == "ready" and task.startswith("envbuild-")
    assert builder.during["grants"] == [(task, tuple(sorted(BUILD_HOSTS)), "platform:env-builder")]
    assert await broker.ledger.active("t1") == [] and await broker.grants.for_task("t1", task) == []  # type: ignore[union-attr]
    t1 = time.monotonic()
    second = await manager.resolve(T1, workspace_reader(root))
    second_ms = (time.monotonic() - t1) * 1000
    assert second.state == "ready" and second.image == record.image and started == [("t1", first.key)], "第二次直接用缓存，不再构建"
    print(f"第一次（起构建 + 构建 + 推送）{first_ms:.0f} ms，第二次（命中缓存）{second_ms:.0f} ms")
    # 改了定义：新的键，重新构建。
    write(tmp_path, {".tool-versions": TOOL_VERSIONS.replace("20.18.0", "22.11.0")})
    third = await manager.resolve(T1, workspace_reader(root))
    assert third.state == "building" and third.key != first.key


async def test_failed_builds_fall_back_to_universal_with_the_reason(tmp_path: Path, registry: FakeRegistry) -> None:
    """验收 3：超时、超过大小上限、构建器出错都记下原因，退回 universal 并说明；同一个定义不再反复构建。"""
    for mode, reason in (("timeout", "超时"), ("big", "大小上限"), ("error", "构建器出错")):
        manager, builder, started, _, _ = setup(tmp_path / mode, registry, limits=Limits(max_image_bytes=2048, build_timeout=timedelta(minutes=5)))
        root = write(tmp_path / mode, {".tool-versions": TOOL_VERSIONS})
        building = await manager.resolve(T1, workspace_reader(root))
        builder.mode = mode
        record = await manager.run_build("t1", building.key)
        assert record.state == "failed" and reason in record.reason, (mode, record)
        again = await manager.resolve(T1, workspace_reader(root))
        assert again.state == "failed" and reason in again.note and "universal" in again.note and len(started) == 1, (mode, again)
        if mode == "timeout":
            assert builder.jobs[0].timeout == timedelta(minutes=5)


async def test_unsupported_and_malicious_definitions_never_start_a_build(tmp_path: Path, registry: FakeRegistry) -> None:
    manager, _, started, _, _ = setup(tmp_path, registry)
    root = write(tmp_path, {"Dockerfile": "FROM python:3.13\nRUN --network=host curl http://10.0.0.1/\n"})
    resolution = await manager.resolve(T1, workspace_reader(root))
    assert resolution.state == "unsupported" and "network=host" in resolution.note and "universal" in resolution.note and started == []
    (root / "Dockerfile").unlink()
    write(tmp_path, {".tool-versions": "ruby 3.3.0\n"})
    resolution = await manager.resolve(T1, workspace_reader(root))
    assert resolution.state == "unsupported" and "ruby" in resolution.note and started == []


async def test_image_quota_and_idle_cleanup(tmp_path: Path, registry: FakeRegistry) -> None:
    manager, _, started, clock, _ = setup(tmp_path, registry, limits=Limits(max_images=1, idle_days=14))
    root = write(tmp_path, {".tool-versions": TOOL_VERSIONS})
    first = await manager.resolve(T1, workspace_reader(root))
    await manager.run_build("t1", first.key)
    write(tmp_path, {".tool-versions": "golang 1.23.2\n"})
    other = await manager.resolve(T1, workspace_reader(root))
    assert other.state == "quota" and "最多 1 个" in other.note and len(started) == 1
    clock.now += timedelta(days=15)
    assert await manager.gc("t1") == 1, "15 天没用的镜像清掉"
    assert registry.deleted and registry.deleted[0][0] == "envs/t1/env"
    assert (await manager.store.get("t1", first.key)).state == "deleted"  # type: ignore[union-attr]
    retry = await manager.resolve(T1, workspace_reader(root))
    assert retry.state == "building", "清掉之后个数又够了"


# --- 构建的 Job ---


def job_for(**overrides: Any) -> BuildJob:
    values: dict[str, Any] = dict(owner="t1", key="ab" * 32, dockerfile="FROM python:3.13\n", context=".", workspace_id="w1",
                                  timeout=timedelta(minutes=20), max_bytes=4 << 30,
                                  env={"HTTPS_PROXY": "http://ar:ar1.body.sig@egress:8095", "PIP_INDEX_URL": "http://m/t/ar1.a.b/pypi/simple/"})
    values.update(overrides)
    return BuildJob(**values)


def pod_status(build: dict[str, Any] | None = None, *, ready: bool = False, ip: str = "10.42.0.17", **extra: Any) -> dict[str, Any]:
    status: dict[str, Any] = {"podIP": ip, "containerStatuses": [{"name": "serve", "ready": ready}], **extra}
    if build is not None:
        status["initContainerStatuses"] = [{"name": "build", "state": build}]
    return status


class FakeKube:
    """假的 Kubernetes API：Job 的状态固定，Pod 的状态按调用次数依次给出（最后一个一直沿用）。"""

    def __init__(self, job_status: dict[str, Any], pods: list[dict[str, Any]] | None = None, log: str = "") -> None:
        self.job_status = job_status
        self.pods = pods or [pod_status()]
        self.log = log
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[Any] = []

    async def json(self, method: str, path: str, body: Any = None, **_: Any) -> dict[str, Any]:
        self.calls.append((method, path))
        self.bodies.append(body)
        if method == "GET" and "/jobs/" in path:
            return {"status": self.job_status}
        if method == "GET" and "/pods?labelSelector=job-name%3D" in path:
            status = self.pods.pop(0) if len(self.pods) > 1 else self.pods[0]
            return {"items": [{"metadata": {"name": "envb-pod"}, "status": status}]}
        return {}

    async def request(self, method: str, path: str, body: Any = None, **_: Any) -> tuple[int, bytes]:
        self.calls.append((method, path))
        if path.endswith("/log?container=build&tailLines=20"):
            return 200, self.log.encode()
        return 200, b"{}"


class FakeFetch:
    """假的取回：记下地址与令牌，写一个 OCI 打包；给了 size 就写那么多字节（看大小上限）。"""

    def __init__(self, size: int = 0) -> None:
        self.size = size
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, url: str, token: str, out: IO[bytes], limit: int) -> int:
        self.calls.append((url, token))
        data = b"x" * self.size if self.size else oci_tar()
        if len(data) > limit:
            raise InvalidError("image_too_large", "构建产物超过大小上限")
        out.write(data)
        return len(data)


CFG = BuilderConfig(agent_image="ghcr.io/agentruntime/sandbox-agent:0.1.0", workspace_claim="workspaces", poll_interval=timedelta(milliseconds=1))
DONE = {"terminated": {"exitCode": 0, "reason": "Completed"}}


def test_build_job_is_locked_down() -> None:
    builder = K8sJobBuilder(FakeKube({}), BuilderConfig(agent_image="ghcr.io/agentruntime/sandbox-agent:0.1.0", workspace_claim="workspaces"))
    config_map, secret, job = builder.manifests(job_for(), "envb-x", "T" * 43)
    spec = job["spec"]["template"]["spec"]
    assert job["metadata"]["namespace"] == "sandbox-builds" and spec["runtimeClassName"] == "kata"
    assert spec["automountServiceAccountToken"] is False and not spec["hostNetwork"] and not spec["hostPID"] and not spec["hostIPC"]
    assert not [v for v in spec["volumes"] if "hostPath" in v], "不挂宿主路径"
    assert job["spec"]["activeDeadlineSeconds"] == 1200 + 900 and job["spec"]["backoffLimit"] == 0, "构建的超时之外留出取回产物的时间"
    [build] = spec["initContainers"]
    [serve] = spec["containers"]
    assert build["securityContext"] == {"privileged": True}, "特权只给构建（在 Kata 的 VM 里）"
    assert serve["securityContext"]["runAsNonRoot"] is True and serve["securityContext"]["allowPrivilegeEscalation"] is False
    assert "AGENT_TOKEN" not in {e["name"] for e in build["env"]}, "构建拿不到取回产物的令牌"
    serve_env = {e["name"]: e for e in serve["env"]}
    assert serve_env["AGENT_TOKEN"]["valueFrom"]["secretKeyRef"] == {"name": "envb-x", "key": "AGENT_TOKEN"} and serve_env["AGENT_WORKDIR"]["value"] == "/out"
    assert serve["command"] == ["/sandbox-agent"] and [m for m in serve["volumeMounts"] if m["name"] == "out"][0]["readOnly"] is True, "只读地提供产物"
    plain = json.dumps([config_map, job])
    assert "T" * 43 not in plain and "ar1.body.sig" not in plain, "令牌与凭据只在这次构建的 Secret 里"
    assert secret["stringData"]["AGENT_TOKEN"] == "T" * 43 and secret["stringData"]["HTTPS_PROXY"].startswith("http://ar:")
    context = next(v for v in spec["volumes"] if v["name"] == "context")
    mount = next(m for m in build["volumeMounts"] if m["name"] == "context")
    assert context["persistentVolumeClaim"]["readOnly"] is True and mount["readOnly"] is True and mount["subPath"] == "t1/w1", "只读、只有本租户的这个工作区"
    out = next(v for v in spec["volumes"] if v["name"] == "out")
    assert out["emptyDir"]["sizeLimit"] == "4096Mi", "产物超过大小上限时被驱逐"
    assert "--output type=oci,dest=/out/image.tar" in build["command"][2]
    # 别的写法一律拒绝。
    with pytest.raises(ValueError):
        K8sJobBuilder(FakeKube({}), BuilderConfig(agent_image="a", isolation="container", runtime_class="gvisor"))
    with pytest.raises(InvalidError):
        builder.manifests(job_for(context="../other-tenant"), "envb-y", "T" * 43)
    no_context = builder.manifests(job_for(context=""), "envb-z", "T" * 43)[2]["spec"]["template"]["spec"]
    assert next(v for v in no_context["volumes"] if v["name"] == "context")["emptyDir"]


async def test_build_job_outcomes_and_redacted_logs() -> None:
    # 构建那一步正常退出、agent 就绪：按 Pod IP 带着这次的令牌取回，取完删掉 Job、Secret、ConfigMap。
    fetch = FakeFetch()
    ok = FakeKube({}, [pod_status({"running": {}}), pod_status(DONE, ready=False), pod_status(DONE, ready=True)])
    out = io.BytesIO()
    assert (await K8sJobBuilder(ok, CFG, fetch=fetch).build(job_for(), out)).ok
    [(url, token)] = fetch.calls
    secret = next(b for b in ok.bodies if b and b.get("kind") == "Secret")
    assert url == "http://10.42.0.17:7070/v1/files/image.tar" and token == secret["stringData"]["AGENT_TOKEN"] and len(token) >= 32
    assert out.getvalue() == oci_tar()
    deletes = [p for m, p in ok.calls if m == "DELETE"]
    assert len(deletes) == 3 and any("/jobs/" in p for p in deletes) and any("/secrets/" in p for p in deletes), "Job、Secret、ConfigMap 都删"
    # 构建超时：构建那一步还在跑就删 Job；Job 自己的 activeDeadlineSeconds 也算超时。
    slow = await K8sJobBuilder(FakeKube({}, [pod_status({"running": {}})]), CFG, fetch=fetch).build(job_for(timeout=timedelta(milliseconds=20)), io.BytesIO())
    assert not slow.ok and slow.failure == "timed_out"
    expired = FakeKube({"conditions": [{"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}]})
    deadline = await K8sJobBuilder(expired, CFG, fetch=fetch).build(job_for(), io.BytesIO())
    assert not deadline.ok and deadline.failure == "timed_out" and "1200 秒" in deadline.reason
    # 产物超过大小上限：被驱逐，或取回时超了。
    evicted = await K8sJobBuilder(FakeKube({}, [pod_status(reason="Evicted")]), CFG, fetch=fetch).build(job_for(), io.BytesIO())
    assert not evicted.ok and evicted.failure == "too_large"
    big = await K8sJobBuilder(FakeKube({}, [pod_status(DONE, ready=True)]), CFG, fetch=FakeFetch(size=2048)).build(job_for(max_bytes=1024), io.BytesIO())
    assert not big.ok and big.failure == "too_large"
    # 构建失败：附上构建日志的最后几行，去掉凭据。
    leaky = "ERROR: failed to fetch http://ar:ar1.body.sig@egress:8095 while resolving http://m/t/ar1.a.b/pypi/simple/"
    failed_kube = FakeKube({}, [pod_status({"terminated": {"exitCode": 1, "reason": "Error"}})], leaky)
    failed = await K8sJobBuilder(failed_kube, CFG, fetch=fetch).build(job_for(), io.BytesIO())
    assert not failed.ok and failed.failure == "failed" and "构建失败" in failed.reason
    assert "ar1.body.sig" not in failed.reason and "ar1.a.b" not in failed.reason, "日志里的凭据去掉"
    assert len(fetch.calls) == 1, "失败、超时时不去取产物"


# EV-TB（docs/plan/ev-tb-0of5-2026-11-04.md）：拉官方镜像的层失败时，BuildKit 把镜像仓库 CDN 的签名地址整个打进日志，查询串里是
# 签名与密钥对的 ID。它们不是我们的密钥，但同样是凭据：不能进失败原因（存在业务库里、经 shell_run 的 environment.note 给用户看）。
# 编出来的、与真的签名同样形状的值（不用线上看到的）。
SIGNATURE = "Qm9ndXNTaWduYXR1cmVGb3JUZXN0c0-xYz7~AbCdEfGhIjKlMnOpQrStUv-WxYz0123456789~QwErTyUiOpAsDfGhJkLzXcVbNm-Plo9Okm8Ijn7Uhb"
SIGNED_URL = ("https://production.cloudfront.docker.com/registry-v2/docker/registry/v2/blobs/sha256/5f/"
              "5f1e1b7a2c9d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9/data"
              f"?Expires=1730383200&Signature={SIGNATURE}&Key-Pair-Id=K2B7EXAMPLEKEYPAIR")


async def test_build_failure_reasons_drop_signed_urls() -> None:
    log = (f"#5 [1/2] FROM docker.io/swebench/sweb.eval.x86_64.django:latest\n"
           f"ERROR: failed to copy: httpReadSeeker: failed open: failed to do request: Get \"{SIGNED_URL}\": dial tcp: i/o timeout\n"
           f"retrying https://user:hunter2-password@mirror.example/v2/x?token=abcdef0123456789abcdef0123456789ab")
    failed_kube = FakeKube({}, [pod_status({"terminated": {"exitCode": 1, "reason": "Error"}})], log)
    failed = await K8sJobBuilder(failed_kube, CFG, fetch=FakeFetch()).build(job_for(), io.BytesIO())
    assert not failed.ok and "构建失败" in failed.reason
    for leaked in (SIGNATURE, "K2B7EXAMPLEKEYPAIR", "Expires=1730383200", "hunter2-password", "abcdef0123456789abcdef0123456789ab"):
        assert leaked not in failed.reason, f"失败原因里不能有 {leaked[:12]}…：{failed.reason}"
    assert "production.cloudfront.docker.com/registry-v2/docker/registry/v2/blobs/sha256/5f/" in failed.reason, "主机与路径留着，看得出是拉哪一层失败的"
    assert "5f1e1b7a2c9d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9" in failed.reason, "层的 digest（十六进制）不是凭据，留着"
    assert "i/o timeout" in failed.reason and "mirror.example" in failed.reason


async def test_stored_failure_reasons_drop_signature_fragments(tmp_path: Path, registry: FakeRegistry) -> None:
    """构建器给的原因被截过时，签名地址只剩半截（没有 https:// 与 ?）：存下来之前再去一遍看着像令牌的长串。"""
    manager, builder, _, _, _ = setup(tmp_path, registry)
    root = write(tmp_path, {".tool-versions": TOOL_VERSIONS})
    building = await manager.resolve(T1, workspace_reader(root))
    builder.mode = "reason"
    builder.reason = f"构建失败：{SIGNATURE}~Tr4nc4ted~Fr4gm3ntOfAS1gnedUrl-ZxCvBnMaSdFgHjKlQwErTyUiOp1234&Key-Pair: i/o timeout"
    record = await manager.run_build("t1", building.key)
    assert record.state == "failed" and "i/o timeout" in record.reason
    assert SIGNATURE not in record.reason and "Fr4gm3ntOfAS1gnedUrl" not in record.reason, record.reason
    again = await manager.resolve(T1, workspace_reader(root))
    assert again.state == "failed" and SIGNATURE not in again.note and "universal" in again.note


# --- shell_run 里 ---


async def test_shell_run_says_which_environment_it_used(tmp_path: Path, registry: FakeRegistry) -> None:
    manager, _, _, _, broker = setup(tmp_path, registry)
    write(tmp_path, {".tool-versions": TOOL_VERSIONS})
    tools = LocalToolDriverProvider()
    register_platform_tools(tools, PlatformToolsConfig(workspace_root=tmp_path / "workspaces", shell_sandbox="sandbox", session_sandboxes=True),
                            sandboxes=broker, telemetry=ConsoleTelemetryProvider(stream=io.StringIO()))

    async def shell(key: str) -> dict[str, Any]:
        return (await tools.bind(T1).invoke("shell_run", "v1", {"argv": ["python", "-c", "print('hi')"]}, idempotency_key=key, task_id="tsk_x")).output

    first = await shell("k1")
    assert first["exit_code"] == 0 and first["environment"]["state"] == "building" and "universal" in first["environment"]["note"]
    assert first["environment"]["definition"] == ".tool-versions"
    [(owner, key)] = [(o, k) for o, k in [(r.owner, r.image_key) for r in await manager.store.all("t1")]]
    await manager.run_build(owner, key)
    second = await shell("k2")
    # 开发实现不用镜像：要用项目环境的镜像时退回 universal，并说明（集群里的 K8s 后端会用上它）。
    assert second["exit_code"] == 0 and second["environment"]["state"] == "fallback" and "universal" in second["environment"]["note"]
