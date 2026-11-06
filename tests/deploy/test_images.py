"""D-01：Runtime 的镜像（docs/plan/cloud-deploy.md）。

静态检查（总是跑）：
- Chart 引用的每个镜像都在 deploy/images/images.json 里，构建脚本给出的镜像名与 Chart 渲染出来的一致。
- 每个 Dockerfile 的运行阶段以 10001 运行；基础镜像写明版本（不用 latest）；Python 依赖按 uv.lock 装（--locked）。
- .dockerignore 挡住密钥（.env、*.key、secrets.json）、开发文件（deploy/dev）与 .git；Dockerfile 不复制这些。
- Python 镜像保留仓库的目录结构：代码按源码位置找 /schemas，换了布局就找不到。
- 打标签时由 CI 推到 GHCR（.github/workflows/images.yml）。

构建与运行（设 AR_TEST_IMAGES=1 才跑：要拉基础镜像、下载依赖，十几分钟）：
- 全部镜像都能构建；以 10001、只读根文件系统（另挂 /tmp）启动：task-service、model-gateway 的 /healthz 返回 200，
  Memory Service 的 gRPC 端口接受连接（它没有 /healthz），Worker 连上 Temporal 开始轮询，sandbox-agent 的 /healthz 返回 200。
- 镜像里我们加进去的部分没有 .env、*.key、secrets.json。

D-14（运行环境）：
- 静态：universal 的每种工具链各占一层（从写死版本的官方镜像复制），缓存目录都在 /tmp；单语言镜像的基础镜像写死版本。
- 构建与运行：在 sandbox-universal 里（断网、只读根文件系统、以 10001 运行）编译并测试五种语言的样例（tests/deploy/samples）；
  sandbox-node、sandbox-go 的 agent 能起来。
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from deploy_tests import CHART_DIR, PRODUCTION_VALUES, REPO_ROOT, containers, render, run

IMAGES_DIR = REPO_ROOT / "deploy" / "images"
MANIFEST = json.loads((IMAGES_DIR / "images.json").read_text("utf-8"))["images"]
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "images.yml"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_images.py"
UID = "10001"
SECRET_NAMES = re.compile(r"(^|/)(\.env(\..*)?|[^/]*\.env|[^/]*\.key|secrets\.json)$")


def dockerfiles() -> dict[str, Path]:
    return {str(p.relative_to(REPO_ROOT).as_posix()): p for p in sorted({REPO_ROOT / s["dockerfile"] for s in MANIFEST.values()})}


def final_stage(text: str) -> str:
    """最后一个 FROM 起的部分（运行阶段）。"""
    return re.split(r"(?mi)^FROM\s", text)[-1]


def chart_values() -> tuple[str, str]:
    values = (CHART_DIR / "values.yaml").read_text("utf-8")
    registry = re.search(r"(?m)^\s+registry:\s*(\S+)", values)
    version = re.search(r'(?m)^appVersion:\s*"?([^"\s]+)"?', (CHART_DIR / "Chart.yaml").read_text("utf-8"))
    assert registry and version
    return registry.group(1), version.group(1)


def build_plan(*args: str) -> list[dict[str, Any]]:
    out = subprocess.run([sys.executable, str(BUILD_SCRIPT), "--list", "--json", *args], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stdout + out.stderr
    return json.loads(out.stdout)["images"]


# --- 静态检查 ---


@pytest.mark.parametrize("values", [None, PRODUCTION_VALUES], ids=["default", "production"])
def test_chart_images_are_all_built(values: Path | None) -> None:
    registry, version = chart_values()
    manifests = render(values) if values else render()
    refs = {c["image"] for _, c in containers(manifests) if c["image"].startswith(registry + "/") or "/agentruntime/" in c["image"]}
    assert refs, "Chart 里没找到 Runtime 自己的镜像"
    names = {ref.rsplit("/", 1)[1].split(":", 1)[0] for ref in refs}
    assert names <= set(MANIFEST), f"Chart 引用了没有 Dockerfile 的镜像：{sorted(names - set(MANIFEST))}"
    if values is None:
        planned = {item["ref"] for item in build_plan()}
        assert refs <= planned, f"构建脚本给出的镜像名与 Chart 不一致：Chart {sorted(refs)}，脚本 {sorted(planned)}"
        assert all(ref.endswith(":" + version) for ref in refs), "默认标签是 Chart 的 appVersion"


def test_the_five_services_and_the_sandbox_images_are_listed() -> None:
    assert set(MANIFEST) == {
        "task-service", "model-gateway", "worker", "memory", "learning",
        "sandbox-agent", "sandbox-python", "sandbox-universal", "sandbox-node", "sandbox-go", "pkg-mirror", "egress-gateway",
        "sandbox-service",
    }
    for name, spec in MANIFEST.items():
        assert (REPO_ROOT / spec["dockerfile"]).is_file(), f"{name} 的 Dockerfile 不存在"


def test_cloudflare_bridge_runs_one_of_our_sandbox_images() -> None:
    """桥跑我们构建的沙箱镜像：要么在这里构建 sandbox-python（Dockerfile，上下文是仓库根），要么是推到 Cloudflare 镜像仓库的
    sandbox-universal（按版本或 git 提交号，不用 latest）。"""
    wrangler = (REPO_ROOT / "deploy" / "cloudflare" / "sandbox-bridge" / "wrangler.jsonc").read_text("utf-8")
    image = re.search(r'"image":\s*"([^"]+)"', wrangler)
    assert image
    if image.group(1).startswith("registry.cloudflare.com/"):
        assert re.fullmatch(r"registry\.cloudflare\.com/[0-9a-f]{32}/agentruntime-sandbox-universal:(\d+\.\d+\.\d+|[0-9a-f]{7,40})", image.group(1))
        return
    context = re.search(r'"image_build_context":\s*"([^"]+)"', wrangler)
    bridge = REPO_ROOT / "deploy" / "cloudflare" / "sandbox-bridge"
    assert context
    assert (bridge / image.group(1)).resolve() == (REPO_ROOT / MANIFEST["sandbox-python"]["dockerfile"]).resolve()
    assert (bridge / context.group(1)).resolve() == REPO_ROOT.resolve(), "构建上下文是仓库根（.dockerignore 在那里）"


@pytest.mark.parametrize("path", list(dockerfiles()), ids=list(dockerfiles()))
def test_dockerfile_runs_as_10001_from_pinned_bases(path: str) -> None:
    text = (REPO_ROOT / path).read_text("utf-8")
    users = re.findall(r"(?mi)^USER\s+(\S+)", final_stage(text))
    assert users and users[-1].split(":")[0] == UID, f"{path} 的运行阶段要以 {UID} 运行"
    bases = re.findall(r"(?mi)^ARG\s+\w+_IMAGE=(\S+)", text) + [b for b in re.findall(r"(?mi)^FROM\s+(\S+)", text) if "$" not in b]
    for base in bases:
        if base == "scratch" or re.fullmatch(r"\w+", base):
            continue  # scratch 与前面阶段的名字
        assert ":" in base and not base.endswith(":latest"), f"{path} 的基础镜像 {base} 要写明版本"
    for forbidden in ("deploy/dev", ".env", "secrets.json", ".git"):
        assert not re.search(rf"(?mi)^(COPY|ADD)\s[^\n]*{re.escape(forbidden)}", text), f"{path} 不能复制 {forbidden}"
    assert not re.search(r"(?mi)^ADD\s", text), f"{path} 用 COPY，不用 ADD（ADD 会自动解包、可拉远程地址）"


UNIVERSAL = IMAGES_DIR / "universal" / "Dockerfile"


def test_universal_has_one_layer_per_toolchain_from_pinned_images() -> None:
    """D-14：每种语言的工具链各占一层，从写死版本的官方镜像复制；升级一种语言只换一层。"""
    text = UNIVERSAL.read_text("utf-8")
    pins = dict(re.findall(r"(?m)^ARG (\w+_IMAGE)=(\S+)$", text))
    for arg, image in {
        "PYTHON_IMAGE": "python:3.13.", "NODE_IMAGE": "node:22.", "GO_IMAGE": "golang:1.27.", "JDK_IMAGE": "eclipse-temurin:21.",
        "MAVEN_IMAGE": "maven:3.", "RUST_IMAGE": "rust:1.",
    }.items():
        assert pins.get(arg, "").startswith(image), f"{arg} 要写死 {image}x 的版本：{pins.get(arg)}"
        assert re.search(r"\d+\.\d+\.\d+", pins[arg]), f"{arg} 写到补丁版本，不跟着浮动的标签走：{pins[arg]}"
    final = final_stage(text)
    for source in ("--from=node /usr/local/bin/node", "--from=go /usr/local/go", "--from=jdk /opt/java/openjdk", "--from=maven /usr/share/maven",
                   "--from=rust /usr/local/rustup", "--from=rust /usr/local/cargo", "--from=agent /out/sandbox-agent"):
        assert f"COPY {source}" in final, f"运行阶段要有 COPY {source}"
    assert "build-essential" in final and "git" in final
    for var in ("PIP_CACHE_DIR=/tmp/", "npm_config_cache=/tmp/", "GOCACHE=/tmp/", "GOMODCACHE=/tmp/", "CARGO_HOME=/tmp/", "maven.repo.local=/tmp/", "HOME=/tmp"):
        assert var in final, f"根文件系统只读：{var.split('=')[0]} 要指到 /tmp"
    assert "GOTOOLCHAIN=local" in final, "Go 不自己去下载别的工具链（沙箱默认断网）"
    assert not re.search(r"curl[^\n|]*\|\s*(ba)?sh", text), "不把下载的脚本管道给 shell"


# sandbox-agent 自己就交给命令的变量（services/rs-sandbox-agent/src/main.rs 的 command_env，与 Go 版相同）与它自己的配置。
AGENT_BASE_ENV = {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE"}


def env_names(stage: str) -> tuple[set[str], set[str]]:
    """运行阶段 ENV 设的变量名，与 AGENT_ENV_PASSTHROUGH 列出的。"""
    body = "\n".join(m.group(1) for m in re.finditer(r"(?ms)^ENV\s+(.*?)(?<!\\)$", stage))
    pairs = dict(re.findall(r'(\w+)=("[^"]*"|\S+)', body))
    return set(pairs), set(pairs.get("AGENT_ENV_PASSTHROUGH", "").strip('"').replace(",", " ").split())


@pytest.mark.parametrize("path", ["deploy/images/universal/Dockerfile", "deploy/images/sandbox.Dockerfile"])
def test_sandbox_images_pass_every_toolchain_variable_to_commands(path: str) -> None:
    """D-15：sandbox-agent 只把基本变量与 AGENT_ENV_PASSTHROUGH 列出的交给命令；镜像 ENV 设的工具链变量都要列上，
    否则 K8s 后端经 agent 跑的命令拿不到（rustc 找不到 RUSTUP_HOME、Maven 写只读的 ~/.m2）。"""
    names, passed = env_names(final_stage((REPO_ROOT / path).read_text("utf-8")))
    assert passed, f"{path} 要声明 AGENT_ENV_PASSTHROUGH"
    missing = {n for n in names if not n.startswith("AGENT_") and n not in AGENT_BASE_ENV} - passed
    assert not missing, f"{path} 的 AGENT_ENV_PASSTHROUGH 漏了 {sorted(missing)}"
    assert not {n for n in passed if n.startswith("AGENT_")}, "AGENT_* 不交给命令"
    assert passed <= names, f"列出的变量要在 ENV 里设：{sorted(passed - names)}"
    for var in ("PIP_INDEX_URL", "npm_config_registry", "GOPROXY"):
        assert var not in names, f"包镜像的地址（{var}）每次执行时注入，不写死在镜像里"


def test_single_language_images_pin_their_base() -> None:
    for name in ("sandbox-node", "sandbox-go"):
        base = MANIFEST[name]["args"].get("BASE_IMAGE", "")
        assert re.search(r":\d+\.\d+\.\d+", base), f"{name} 的基础镜像写到补丁版本：{base!r}"
        assert MANIFEST[name]["dockerfile"] == "deploy/images/sandbox.Dockerfile"


def test_python_images_install_from_the_lockfile() -> None:
    text = (IMAGES_DIR / "python.Dockerfile").read_text("utf-8")
    assert re.search(r"uv sync[^\n]*--locked", text) and "--no-dev" in text, "Python 依赖按 uv.lock 装，不装开发依赖"
    assert "pip install" not in text and "uv lock" not in text, "镜像里不重新解析依赖"
    assert "UV_PYTHON_DOWNLOADS=never" in text, "用基础镜像的 Python，不另外下载"
    assert "PYTHONDONTWRITEBYTECODE=1" in final_stage(text) and "UV_COMPILE_BYTECODE=1" in text, "根文件系统只读：字节码构建时编译好"


def test_python_layout_keeps_schemas_where_the_code_looks_for_them() -> None:
    """Python 镜像把工作区装在 /app、保持仓库布局；代码按源码位置找 /schemas（parents[N]），复制时不能改布局。"""
    from agentruntime_worker.planner import validate

    assert validate.SCHEMAS_DIR.resolve() == (REPO_ROOT / "schemas").resolve()
    text = (IMAGES_DIR / "python.Dockerfile").read_text("utf-8")
    for source in ("libs/py-core/", "proto/gen/python/", "services/py-worker/", "services/py-memory/", "services/py-learning/", "schemas/"):
        assert re.search(rf"(?m)^COPY {re.escape(source)} {re.escape(source)}$", text), f"{source} 要原样复制到 /app/{source}"
    assert "WORKDIR /app" in text and "COPY --from=build /app /app" in text


def test_dockerignore_keeps_secrets_and_dev_files_out() -> None:
    patterns = {line.strip() for line in DOCKERIGNORE.read_text("utf-8").splitlines() if line.strip() and not line.startswith("#")}
    for required in (".git", "**/.env", "**/.env.*", "**/*.env", "**/*.key", "**/secrets.json", "deploy/dev", "**/.venv", "**/node_modules"):
        assert required in patterns, f".dockerignore 要有 {required}"
    assert not [p for p in patterns if p.startswith("!")], "不用 ! 例外：有例外时 BuildKit 会打开被排除的目录（读不了的缓存目录会让构建失败）"
    members = re.findall(r'"([^"]+)"', (REPO_ROOT / "pyproject.toml").read_text("utf-8").split("members = [", 1)[1].split("]", 1)[0])
    dockerfile = (IMAGES_DIR / "python.Dockerfile").read_text("utf-8")
    for member in members:
        assert re.search(rf"(?m)^COPY {re.escape(member)}/(pyproject\.toml)? {re.escape(member)}/$", dockerfile), \
            f"python.Dockerfile 要复制工作区成员 {member}（至少它的 pyproject.toml），uv 才能核对 uv.lock"
    tracked = run(["git", "ls-files"], check=False).stdout.splitlines()
    leaked = [f for f in tracked if SECRET_NAMES.search(f)]
    assert not leaked, f"仓库里不该有这些文件：{leaked}"


def test_images_are_pushed_to_ghcr_on_tags() -> None:
    text = WORKFLOW.read_text("utf-8")
    assert re.search(r"(?ms)^on:\s*\n\s*push:\s*\n\s*tags:\s*\[\s*\"v\*\"\s*\]", text), "打 v* 标签时推送"
    assert re.search(r"(?m)^\s*packages:\s*write", text) and re.search(r"(?m)^\s*contents:\s*read", text)
    assert "scripts/build_images.py" in text and "--push" in text, "CI 与本地用同一个脚本"
    assert not re.search(r":latest\b|--tag\s+\"?latest", text), "不推 latest"
    assert "secrets.GITHUB_TOKEN" in text and "--password-stdin" in text, "令牌经标准输入登录，不出现在命令行里"


def test_build_script_rejects_latest_and_unknown_images() -> None:
    for args in (["--tag", "latest"], ["--only", "nope"]):
        out = subprocess.run([sys.executable, str(BUILD_SCRIPT), "--list", *args], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8")
        assert out.returncode != 0


# --- 构建与运行（AR_TEST_IMAGES=1） ---

BUILD = os.environ.get("AR_TEST_IMAGES") == "1"
TAG = "d01-test"
REGISTRY = "agentruntime-test"


def docker(*args: str, check: bool = True, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    exe = os.environ.get("AR_DOCKER_BIN") or "docker"
    exe = str(Path(exe) / "docker") if Path(exe).is_dir() else exe
    proc = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if check and proc.returncode != 0:
        pytest.fail(f"docker {' '.join(args)} 失败：\n{proc.stdout}\n{proc.stderr}", pytrace=False)
    return proc


def ref(name: str) -> str:
    return f"{REGISTRY}/{name}:{TAG}"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 60) -> int:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=3) as resp:
                return resp.status
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


def wait_tcp(port: int, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=3).close()
            return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


LOCKED_DOWN = ["--user", f"{UID}:{UID}", "--read-only", "--tmpfs", "/tmp:rw,size=64m", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]


@pytest.fixture(scope="module")
def built() -> dict[str, str]:
    if not BUILD:
        pytest.skip("设 AR_TEST_IMAGES=1 才构建、运行镜像（要拉基础镜像、下载依赖）")
    proc = subprocess.run([sys.executable, str(BUILD_SCRIPT), "--registry", REGISTRY, "--tag", TAG], cwd=REPO_ROOT, timeout=3600)
    assert proc.returncode == 0, "镜像构建失败"
    return {name: ref(name) for name in MANIFEST}


@pytest.fixture(scope="module")
def temporal(built: dict[str, str]):  # type: ignore[no-untyped-def]
    """宿主机上的 Temporal 开发服务（PATH 上的 temporal），容器经 host.docker.internal 连。"""
    port = free_port()
    proc = subprocess.Popen(
        ["temporal", "server", "start-dev", "--ip", "0.0.0.0", "--port", str(port), "--ui-port", str(free_port()), "--headless"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_tcp(port)
        yield f"host.docker.internal:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=30)


def run_detached(image: str, *args: str, env: dict[str, str], ports: dict[int, int] | None = None, mounts: tuple[str, ...] = ()) -> str:
    cmd = ["run", "-d", "--rm", *LOCKED_DOWN, "--add-host", "host.docker.internal:host-gateway"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    for host, inner in (ports or {}).items():
        cmd += ["-p", f"127.0.0.1:{host}:{inner}"]
    for m in mounts:
        cmd += ["-v", m]
    return docker(*cmd, image, *args).stdout.strip()


@pytest.fixture
def started():  # type: ignore[no-untyped-def]
    ids: list[str] = []
    yield ids
    for cid in ids:
        docker("rm", "-f", cid, check=False)


def logs(cid: str) -> str:
    out = docker("logs", cid, check=False)
    return out.stdout + out.stderr


def test_task_service_serves_healthz(built: dict[str, str], temporal: str, started: list[str]) -> None:
    port = free_port()
    cid = run_detached(built["task-service"], "serve", ports={port: 8080}, env={
        "AR_TEMPORAL_ADDRESS": temporal, "AR_DEV_SIGNING_KEY": secrets.token_hex(16), "AR_HTTP_ADDR": "0.0.0.0:8080",
        "AR_DATA_DIR": "/tmp/data",
    })
    started.append(cid)
    try:
        assert wait_http(f"http://127.0.0.1:{port}/healthz") == 200
    except OSError:
        pytest.fail(logs(cid), pytrace=False)


def test_model_gateway_serves_healthz(built: dict[str, str], started: list[str], tmp_path: Path) -> None:
    port = free_port()
    # 复制到本地的临时目录再挂：仓库在网络盘上时 Docker 挂不了 UNC 路径。
    profiles = tmp_path / "model-profiles.json"
    shutil.copyfile(REPO_ROOT / "deploy" / "dev" / "mock-model-profiles.json", profiles)
    cid = run_detached(built["model-gateway"], "serve", ports={port: 8091}, mounts=(f"{profiles}:/etc/agentruntime/model-profiles.json:ro",), env={
        "AR_MODEL_PROVIDER": "mock", "AR_MODEL_PROFILES_FILE": "/etc/agentruntime/model-profiles.json", "AR_MODEL_GATEWAY_ADDR": "0.0.0.0:8091",
        "AR_SECRETS_FILE": "/tmp/secrets.json",
    })
    started.append(cid)
    try:
        assert wait_http(f"http://127.0.0.1:{port}/healthz") == 200
    except OSError:
        pytest.fail(logs(cid), pytrace=False)


def test_memory_service_accepts_connections(built: dict[str, str], started: list[str]) -> None:
    port = free_port()
    cid = run_detached(built["memory"], "-m", "agentruntime_memory", ports={port: 50061}, env={
        "AR_DEV_SIGNING_KEY": secrets.token_hex(16), "AR_MEMORY_ADDRESS": "0.0.0.0:50061", "AR_DATA_DIR": "/tmp/memory",
    })
    started.append(cid)
    try:
        wait_tcp(port)
    except OSError:
        pytest.fail(logs(cid), pytrace=False)
    time.sleep(2)
    assert "监听" in logs(cid), logs(cid)


def test_worker_polls_temporal(built: dict[str, str], temporal: str, started: list[str]) -> None:
    cid = run_detached(built["worker"], "-m", "agentruntime_worker", "--queues", "task", env={
        "AR_TEMPORAL_ADDRESS": temporal, "AR_DATA_DIR": "/tmp/data",
    })
    started.append(cid)
    address = temporal.replace("host.docker.internal", "127.0.0.1")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        out = subprocess.run(["temporal", "task-queue", "describe", "--task-queue", "task", "--address", address, "-o", "json"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        if out.returncode == 0 and "identity" in out.stdout:
            return
        if docker("inspect", "-f", "{{.State.Running}}", cid, check=False).stdout.strip() != "true":
            break
        time.sleep(2)
    pytest.fail("Worker 没有在 task 队列上轮询：\n" + logs(cid), pytrace=False)


def test_learning_image_runs_the_pipeline_cli(built: dict[str, str]) -> None:
    out = docker("run", "--rm", *LOCKED_DOWN, built["learning"], "-m", "agentruntime_learning.pipeline", "--help", check=False)
    assert out.returncode == 0, out.stdout + out.stderr


@pytest.mark.parametrize("name", ["sandbox-agent", "sandbox-python", "sandbox-universal", "sandbox-node", "sandbox-go"])
def test_sandbox_agent_serves_healthz(built: dict[str, str], started: list[str], name: str) -> None:
    port = free_port()
    cid = run_detached(built[name], ports={port: 7070}, env={"AGENT_TOKEN": secrets.token_hex(24), "AGENT_ADDR": ":7070", "AGENT_WORKDIR": "/tmp"})
    started.append(cid)
    try:
        assert wait_http(f"http://127.0.0.1:{port}/healthz") == 200
    except OSError:
        pytest.fail(logs(cid), pytrace=False)


SAMPLES = REPO_ROOT / "tests" / "deploy" / "samples"
SAMPLE_SCRIPT = " && ".join([
    "cd /workspace/python && python -m unittest -q",
    "cd /workspace/node && node --test",
    "cd /workspace/go && go test ./...",
    "cd /workspace/java && javac -d /tmp/jout Calc.java && java -ea -cp /tmp/jout Calc && mvn -v > /dev/null",
    "cd /workspace/rust && cargo test --offline -q",
    "echo all-five-ok",
])


def test_universal_compiles_and_tests_five_languages_offline(built: dict[str, str], tmp_path: Path) -> None:
    """D-14 验收 1：断网、只读根文件系统、以 10001 运行，五种语言的样例都能编译、测试通过（gVisor、Kata 上的同一项在集群里做）。"""
    work = tmp_path / "workspace"
    shutil.copytree(SAMPLES, work)  # 复制到本地盘：仓库在网络盘上时 Docker 挂不了 UNC 路径
    if hasattr(os, "chmod"):
        for path in [work, *work.rglob("*")]:
            path.chmod(0o777 if path.is_dir() else 0o666)
    out = docker(
        "run", "--rm", "--network", "none", "--user", f"{UID}:{UID}", "--read-only", "--tmpfs", "/tmp:rw,exec,size=2g", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "-v", f"{work}:/workspace", "--entrypoint", "/bin/sh", built["sandbox-universal"], "-c", SAMPLE_SCRIPT,
        check=False, timeout=1200,
    )
    assert out.returncode == 0 and "all-five-ok" in out.stdout, out.stdout[-4000:] + out.stderr[-4000:]
    assert "java ok" in out.stdout


# 我们加进镜像的部分（基础镜像自带的系统文件不在这里检查）。
OURS = ("app/", "schemas/", "usr/local/bin/app", "usr/local/bin/sandbox-agent", "sandbox-agent", "workspace/")


@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_images_carry_no_secrets_or_dev_files(built: dict[str, str], name: str) -> None:
    cid = docker("create", built[name]).stdout.strip()
    try:
        exported = subprocess.run([os.environ.get("AR_DOCKER_BIN") or "docker", "export", cid], capture_output=True, timeout=600).stdout
    finally:
        docker("rm", "-f", cid, check=False)
    with tarfile.open(fileobj=io.BytesIO(exported)) as tar:
        files = [m.name for m in tar.getmembers() if m.isfile()]  # 只看文件：目录项（例如 app/tests/e2e/）不算
    ours = [f for f in files if f.startswith(OURS)]
    assert ours, f"{name}：镜像里没找到我们加的文件"
    leaked = [f for f in ours if SECRET_NAMES.search(f) or "deploy/dev" in f or "/.git/" in f]
    assert not leaked, f"{name} 里有不该有的文件：{leaked}"
    if name in ("worker", "memory", "learning"):
        assert not [f for f in ours if "/tests/" in f and not f.endswith("pyproject.toml") and "site-packages" not in f], f"{name} 里有测试代码"
