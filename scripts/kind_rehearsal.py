"""在 kind 上彩排 Runtime 上线（docs/plan/cloud-deploy.md D-04 的验收 2–4，以及 D-05 的集群内路径）。

    uv run --no-sync python scripts/kind_rehearsal.py

要先有：本机构建的镜像（python scripts/build_images.py --registry agentruntime-test --tag d01-test）、kind、kubectl、helm、
Docker Desktop。kind 集群用 scripts/kind_cluster.py 的那个（没有就建，有就复用；设 AR_KIND_GVISOR 时装 gVisor，
沙箱 Pod 才能用 RuntimeClass gvisor，见 kind_cluster）。会下载：KEDA 的 Chart 与镜像、temporalio/temporal 的镜像、Traefik 的
IngressRoute CRD（经 gh 从 traefik-helm-chart 读）。

做的事：装 KEDA、集群里的 Temporal 开发服务与一个最小的 Postgres（Sandbox Service 与 Worker 共用业务库，SBX-01）→ 建 Secret
（随机的开发密钥与业务库的连接串，只在集群里）与 Model Profile 的 ConfigMap →
helm 装 Runtime（values-cloud.yaml 叠 deploy/cluster/kind-rehearsal.values.yaml）→ 给 Registry 种开发种子 → 逐项核对：
  1. 各 Deployment 就绪；KEDA 的 ScaledObject 就绪；IngressRoute 被 API Server 接受。
  2. 提交一个 Task 跑到 done（mock 模型、桩 Planner）。
  3. 100 KB 的命令：Claim-Check 经共用的业务对象卷与可写的 SecretStore 在 task-service 与 Worker 之间往返，跑到 done。
  4. Worker 在集群里经 Sandbox Service 跑 shell_run：服务用自己的 ServiceAccount 在 sandboxes 命名空间建 Pod，经网络策略放行的
     7070 连 agent，命令在 gVisor 里跑（装了 gVisor 时）。
  5. 停掉 decision 池（KEDA 暂停到 0 副本）后提交一批 Task，再删掉 task 池的 Pod（任务进行到一半）；恢复后 KEDA 按积压把 decision
     池扩到不止一个副本，所有 Task 照样跑完。
结果逐项打印，有失败时退出码 1。--keep 保留装好的东西（默认最后删掉 Runtime 的 release，KEDA 与 Temporal 留着方便再跑）。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import kind_cluster as kindtest  # scripts/ 在 sys.path 上（按文件运行）

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy" / "helm" / "agent-runtime"
NS = "agent-runtime"
RELEASE = "ar"
FULL = "ar-agent-runtime"
REGISTRY, TAG = "agentruntime-test", "d01-test"
IMAGES = ["task-service", "model-gateway", "worker", "memory", "sandbox-service", "sandbox-agent", "sandbox-python"]
TEMPORAL_IMAGE = "temporalio/temporal:1.9.1"
TRAEFIK_CRD = ("traefik/traefik-helm-chart", "v41.7.1", "traefik/crds/traefik.io_ingressroutes.yaml")
SERVICE = "/agentruntime.taskapi.v1.TaskService/"
TENANT = "t-rehearsal"

TEMPORAL = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: temporal-frontend, namespace: data}
spec:
  replicas: 1
  selector: {matchLabels: {app: temporal-dev}}
  template:
    metadata: {labels: {app: temporal-dev}}
    spec:
      containers:
        - name: temporal
          image: %s
          args: ["server", "start-dev", "--ip", "0.0.0.0", "--namespace", "agent-runtime", "--headless"]
          ports: [{containerPort: 7233}]
          readinessProbe: {tcpSocket: {port: 7233}, periodSeconds: 3}
---
apiVersion: v1
kind: Service
metadata: {name: temporal-frontend, namespace: data}
spec:
  selector: {app: temporal-dev}
  ports: [{name: grpc, port: 7233, targetPort: 7233}]
""" % TEMPORAL_IMAGE

# kind 的默认存储类叫 standard；deploy/cluster/storage.yaml 写的是 k3s 的 local-path，这里补一个同名的。
LOCAL_PATH = """
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata: {name: local-path}
provisioner: rancher.io/local-path
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
"""


class Rehearsal:
    def __init__(self, cluster: kindtest.Cluster, keep: bool) -> None:
        self.cluster = cluster
        self.keep = keep
        self.kubeconfig = str(cluster.kubeconfig)
        self.helm = shutil.which("helm") or "helm"
        self.results: list[tuple[str, bool, str]] = []
        self._dir = Path(tempfile.mkdtemp(prefix="ar-rehearsal-"))

    # --- 小工具 ---

    def run(self, *args: str, input: str | None = None, check: bool = True, timeout: float = 600) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(list(args), input=input, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        if check and proc.returncode != 0:
            raise RuntimeError(f"{Path(args[0]).name} {' '.join(args[1:4])} 失败：{(proc.stderr or proc.stdout).strip()[-1500:]}")
        return proc

    def k(self, *args: str, **kw) -> subprocess.CompletedProcess[str]:
        return self.run(self.cluster.kubectl, "--kubeconfig", self.kubeconfig, *args, **kw)

    def apply(self, manifest: str, *extra: str) -> None:
        self.k("apply", *extra, "-f", "-", input=manifest)

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, ok, detail))
        print(f"{'通过' if ok else '失败'}：{name}{('（' + detail + '）') if detail else ''}", flush=True)

    # --- 准备 ---

    def images(self) -> None:
        missing = [n for n in IMAGES if self.run(self.cluster.docker, "image", "inspect", f"{REGISTRY}/{n}:{TAG}", check=False).returncode != 0]
        if missing:
            self.run(sys.executable, str(ROOT / "scripts" / "build_images.py"), "--registry", REGISTRY, "--tag", TAG, "--only", ",".join(missing), timeout=3600)
        refs = [f"{REGISTRY}/{n}:{TAG}" for n in IMAGES]
        self.run(self.cluster.kind, "load", "docker-image", *refs, "--name", kindtest.CLUSTER, timeout=1800)

    def platform(self) -> None:
        self.apply(LOCAL_PATH)
        self.k("apply", "-f", str(ROOT / "deploy" / "cluster" / "storage.yaml"))
        self.apply(TEMPORAL)
        self.apply(self.POSTGRES)
        keda = dict(line.split("=", 1) for line in (ROOT / "deploy" / "cluster" / "versions.conf").read_text("utf-8").splitlines()
                    if line.strip() and not line.startswith("#"))["KEDA_VERSION"]
        self.run(self.helm, "repo", "add", "kedacore", "https://kedacore.github.io/charts", "--force-update")
        self.run(self.helm, "--kubeconfig", self.kubeconfig, "upgrade", "--install", "keda", "kedacore/keda", "--version", keda,
                 "-n", "keda", "--create-namespace", "--wait", "--timeout", "10m", timeout=900)
        repo, ref, path = TRAEFIK_CRD
        crd = self.run("gh", "api", "-H", "Accept: application/vnd.github.raw", f"repos/{repo}/contents/{path}?ref={ref}").stdout
        self.apply(crd, "--server-side")
        self.k("-n", "data", "rollout", "status", "deploy/temporal-frontend", "--timeout=300s", timeout=330)
        self.k("-n", "data", "rollout", "status", "deploy/rehearsal-postgres", "--timeout=300s", timeout=330)

    # 最小的业务库（彩排不装 deploy/data 的 Postgres）：应用用的角色不是超级用户、不带 BYPASSRLS（行级安全，S-12a）。
    POSTGRES = """
apiVersion: v1
kind: ConfigMap
metadata: {name: rehearsal-postgres-init, namespace: data}
data:
  init.sql: |
    CREATE ROLE agentruntime LOGIN PASSWORD 'rehearsal' NOSUPERUSER NOBYPASSRLS;
    CREATE DATABASE agentruntime OWNER agentruntime;
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: rehearsal-postgres, namespace: data}
spec:
  selector: {matchLabels: {app: rehearsal-postgres}}
  template:
    metadata: {labels: {app: rehearsal-postgres}}
    spec:
      containers:
        - name: postgres
          image: postgres:17-alpine
          env: [{name: POSTGRES_PASSWORD, value: rehearsal-admin}]
          ports: [{containerPort: 5432}]
          volumeMounts: [{name: init, mountPath: /docker-entrypoint-initdb.d}]
          readinessProbe: {exec: {command: [pg_isready, -U, postgres]}, periodSeconds: 3}
      volumes: [{name: init, configMap: {name: rehearsal-postgres-init}}]
---
apiVersion: v1
kind: Service
metadata: {name: rehearsal-postgres, namespace: data}
spec:
  selector: {app: rehearsal-postgres}
  ports: [{port: 5432}]
"""

    def secrets(self) -> None:
        seed = {"version": 1, "scopes": {"_platform": {"roles": [], "secrets": {
            "model-provider/api-key": "mock-" + secrets.token_hex(8),
            "registry/publish-token": secrets.token_hex(24),
            "sandbox/token-key": secrets.token_hex(32),
        }}}}
        docs = [
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "agent-runtime-secrets", "namespace": NS}, "type": "Opaque",
             "stringData": {"dev-signing-key": secrets.token_hex(32), "secrets.json": json.dumps(seed)}},
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "agent-runtime-dsn", "namespace": NS}, "type": "Opaque",
             "stringData": {"dsn": "postgres://agentruntime:rehearsal@rehearsal-postgres.data.svc:5432/agentruntime?sslmode=disable"}},
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "agent-runtime-langfuse", "namespace": NS}, "type": "Opaque",
             "stringData": {"basic-auth": base64.b64encode(b"pk:sk").decode()}},
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "agent-runtime-model-profiles", "namespace": NS},
             "data": {"model-profiles.json": (ROOT / "deploy" / "dev" / "mock-model-profiles.json").read_text("utf-8")}},
        ]
        # 已有的不覆盖：SecretStore 的种子换了，可写文件里已经生成的 kek 不会跟着换，但签名密钥换了会让旧令牌失效。
        for doc in docs:
            exists = self.k("-n", NS, "get", doc["kind"].lower(), doc["metadata"]["name"], check=False).returncode == 0
            if not exists:
                self.apply(json.dumps(doc))

    def install(self) -> None:
        self.run(self.helm, "--kubeconfig", self.kubeconfig, "upgrade", "--install", RELEASE, str(CHART), "-n", NS,
                 "-f", str(CHART / "values-cloud.yaml"), "-f", str(ROOT / "deploy" / "cluster" / "kind-rehearsal.values.yaml"),
                 "--wait", "--timeout", "10m", timeout=900)
        seed = (ROOT / "deploy" / "dev" / "registry-seed.json").read_text("utf-8")
        self.k("-n", NS, "exec", "-i", f"deploy/{FULL}-task-service", "--", "/usr/local/bin/app", "registry-seed", "/dev/stdin", input=seed)

    # --- Task API ---

    def token(self) -> str:
        out = self.k("-n", NS, "exec", f"deploy/{FULL}-task-service", "--", "/usr/local/bin/app", "dev-token", "--tenant", TENANT, "--user", "u1").stdout
        return out.strip().splitlines()[-1].strip()

    def call(self, method: str, body: dict, token: str) -> dict:
        req = urllib.request.Request(self.base + SERVICE + method, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")

    def submit(self, token: str, message: str) -> str:
        body = {"client_request_id": uuid.uuid4().hex, "message": message, "inputs": {"stub_plan": "answer"}, "session_access": "SESSION_ACCESS_READ_WRITE"}
        return self.call("SubmitTask", body, token)["taskId"]

    def wait_done(self, token: str, task_ids: list[str], timeout: float) -> dict[str, str]:
        deadline, status = time.monotonic() + timeout, {t: "" for t in task_ids}
        while time.monotonic() < deadline:
            for t in task_ids:
                if not status[t].endswith(("DONE", "FAILED", "CANCELLED", "ABORTED")):
                    try:
                        status[t] = self.call("QueryTask", {"task_id": t}, token).get("status", "")
                    except (urllib.error.URLError, OSError):
                        pass
            if all(s.endswith(("DONE", "FAILED", "CANCELLED", "ABORTED")) for s in status.values()):
                break
            time.sleep(3)
        return status

    def port_forward(self) -> subprocess.Popen[str]:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        proc = subprocess.Popen([self.cluster.kubectl, "--kubeconfig", self.kubeconfig, "-n", NS, "port-forward", f"svc/{FULL}-task-service", f"{port}:8080"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(self.base + "/healthz", timeout=3) as resp:
                    if resp.status == 200:
                        return proc
            except OSError:
                time.sleep(1)
        proc.kill()
        raise RuntimeError("连不上 task-service（port-forward）")

    # --- 核对 ---

    def check_ready(self) -> None:
        deploys = json.loads(self.k("-n", NS, "get", "deploy", "-o", "json").stdout)["items"]
        not_ready = [d["metadata"]["name"] for d in deploys if d["spec"].get("replicas", 1) and d["status"].get("readyReplicas", 0) < d["spec"].get("replicas", 1)]
        self.record("Runtime 的各 Deployment 就绪", not not_ready, ", ".join(not_ready))
        objects = json.loads(self.k("-n", NS, "get", "scaledobject", "-o", "json").stdout)["items"]
        bad = [o["metadata"]["name"] for o in objects
               if not any(c["type"] == "Ready" and c["status"] == "True" for c in o.get("status", {}).get("conditions", []))]
        self.record("KEDA 的 ScaledObject 都就绪（连得上 Temporal）", bool(objects) and not bad, ", ".join(bad) or f"{len(objects)} 个")
        routes = self.k("-n", NS, "get", "ingressroute", "-o", "name", check=False).stdout.split()
        self.record("IngressRoute 被 API Server 接受（Traefik 的 CRD）", bool(routes), " ".join(routes))

    def check_tasks(self, token: str) -> None:
        small = self.submit(token, "彩排：上季度退款率为什么上升？")
        big = self.submit(token, "量" * (100 * 1024 // 3))
        status = self.wait_done(token, [small, big], 180)
        self.record("Task 跑到 done（mock 模型、桩 Planner）", status[small].endswith("DONE"), status[small])
        objects = self.k("-n", NS, "exec", f"deploy/{FULL}-worker-program", "--", "python", "-c",
                         f"import json, pathlib; print(sorted({{json.loads(p.read_text())['key'].split('/')[0] for p in pathlib.Path('/objects/{TENANT}/data').glob('*.json')}}))",
                         check=False).stdout.strip()
        self.record("100 KB 的命令经 Claim-Check 在 task-service 与 Worker 之间往返", status[big].endswith("DONE") and "claim-check" in objects,
                    f"{status[big]}；共用卷上的对象：{objects}")

    def check_sandbox(self) -> None:
        script = (
            "import asyncio, json, os\n"
            "from agentruntime_core.tenancy import TenantContext\n"
            "from agentruntime_worker.config import WorkerConfig\n"
            "from agentruntime_worker.tools import dev_tools\n"
            "cfg = WorkerConfig.from_env(os.environ)\n"
            f"os.makedirs(os.path.join(str(cfg.tool_workspace_root), '{TENANT}', 'w1'), exist_ok=True)\n"
            f"t = TenantContext(tenant_id='{TENANT}', workspace_id='w1')\n"
            "args = {'argv': ['python', '-c', \"import os; print(open('/proc/version').read().strip()); print(os.getuid())\"]}\n"
            "out = asyncio.run(dev_tools(cfg).bind(t).invoke('shell_run', 'v1', args, idempotency_key='rehearsal-' + os.urandom(4).hex()))\n"
            "print(json.dumps(out.output))\n"
        )
        proc = self.k("-n", NS, "exec", "-i", f"deploy/{FULL}-worker-program", "--", "python", "-", input=script, check=False, timeout=300)
        try:
            out = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            self.record("Worker 在集群里经 Sandbox Service 跑 shell_run", False, (proc.stderr or proc.stdout)[-600:])
            return
        gvisor = self.cluster.runtime_class == "gvisor"
        ok = out.get("exit_code") == 0 and out.get("sandbox") == "gvisor" and "10001" in out.get("stdout", "") and (not gvisor or "gvisor" in out["stdout"].lower())
        self.record("Worker 在集群里经 Sandbox Service 跑 shell_run（服务的 ServiceAccount 建 Pod、经 7070 连 agent" + ("、gVisor 内核" if gvisor else "") + "）",
                    ok, out.get("stdout", "").strip().replace("\n", " | "))

    def replicas(self, pool: str) -> int:
        return int(self.k("-n", NS, "get", "deploy", f"{FULL}-worker-{pool}", "-o", "jsonpath={.spec.replicas}").stdout or 0)

    # 让积压留得住：桩 Planner 很快，一个副本几秒就消化掉 30 个，HPA 来不及动。用网络策略暂时切断 decision 池到 Temporal 的连接
    # （kind 的 kindnet 执行网络策略），新起的 Pod 拉不到任务，积压一直在；KEDA 按积压扩容后再放开。
    BLOCK = """
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: rehearsal-block-decision, namespace: agent-runtime}
spec:
  podSelector: {matchLabels: {agentruntime.io/pool: decision}}
  policyTypes: [Egress]
  egress:
    - ports: [{protocol: UDP, port: 53}, {protocol: TCP, port: 53}]
"""

    def check_scaling_and_recovery(self, token: str) -> None:
        self.apply(self.BLOCK)
        # 网络策略只拦新连接：已有的 Pod 在优雅退出的那几十秒里还连着 Temporal、照样拉任务，要等它们真的没了再提交。
        self.k("-n", NS, "delete", "pod", "-l", "agentruntime.io/pool=decision", "--wait=true", "--grace-period=5", timeout=180)
        time.sleep(10)
        tasks = [self.submit(token, f"彩排批量 {i}") for i in range(30)]
        time.sleep(10)
        # 任务进行到一半（Task 工作流已经开始、在等规划）：删掉 task 池的 Pod。
        self.k("-n", NS, "delete", "pod", "-l", "agentruntime.io/pool=task", "--wait=false")
        peak, deadline = 0, time.monotonic() + 240
        while time.monotonic() < deadline and peak < 2:
            peak = max(peak, self.replicas("decision"))
            time.sleep(5)
        self.k("-n", NS, "delete", "networkpolicy", "rehearsal-block-decision")
        self.k("-n", NS, "delete", "pod", "-l", "agentruntime.io/pool=decision", "--wait=false")
        status = self.wait_done(token, tasks, 420)
        done = sum(s.endswith("DONE") for s in status.values())
        self.record("积压时 KEDA 把 decision 池扩到不止一个副本", peak >= 2, f"最多 {peak} 个副本（30 个积压、目标每副本 10 个）")
        self.record("任务进行到一半删掉 task 池的 Pod，30 个 Task 照样跑完", done == len(tasks), f"{done}/{len(tasks)} done")
        # 积压消化后缩回最少副本（HPA 缩容有 5 分钟的稳定窗口）。
        deadline = time.monotonic() + 600
        while self.replicas("decision") > 1 and time.monotonic() < deadline:
            time.sleep(10)
        final = self.replicas("decision")
        self.record("积压消化后 decision 池缩回 1 个副本", final == 1, f"{final} 个副本")

    def teardown(self) -> None:
        if not self.keep:
            self.run(self.helm, "--kubeconfig", self.kubeconfig, "uninstall", RELEASE, "-n", NS, check=False, timeout=300)
        shutil.rmtree(self._dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true", help="跑完保留 Runtime 的 release")
    args = parser.parse_args()
    os.environ.setdefault("AR_KIND_REUSE", "1")
    cluster = kindtest.Cluster()
    r = Rehearsal(cluster, args.keep)
    forward = None
    try:
        print("==> 镜像", flush=True)
        r.images()
        print("==> KEDA、Temporal、Traefik 的 CRD", flush=True)
        r.platform()
        print("==> Secret 与 Runtime", flush=True)
        r.secrets()
        r.install()
        r.check_ready()
        forward = r.port_forward()
        token = r.token()
        r.check_tasks(token)
        r.check_sandbox()
        r.check_scaling_and_recovery(token)
    finally:
        if forward is not None:
            forward.kill()
        r.teardown()
        cluster.stop()
    failed = [name for name, ok, _ in r.results if not ok]
    print(f"\n{len(r.results) - len(failed)}/{len(r.results)} 项通过" + (f"；失败：{'、'.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
