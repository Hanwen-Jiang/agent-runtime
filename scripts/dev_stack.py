"""本地拉起整套（T-13，细化见 docs/plan/live-e2e.md 第 2 节第 1 条）：Temporal 开发服务、task-service（Registry 打开）、
Model Gateway、Python Worker。人手动演示与 tests/e2e/live 的真模型用例用同一个脚本。只用标准库（同 check.py）。

    uv run --no-sync python scripts/dev_stack.py up [--root 目录] [--provider mock|openai_compat] [--provider-env 文件]
                                                  [--decisions model|stub] [--tenant-effect 租户=effect]...
                                                  [--coding] [--sandbox host|docker] [--json]
    uv run --no-sync python scripts/dev_stack.py status [--json]      各组件是否在运行、地址与日志
    uv run --no-sync python scripts/dev_stack.py token --tenant t1 --user u1 [--scope task.approve]
    uv run --no-sync python scripts/dev_stack.py kill worker          强杀一个组件（演示“杀 Worker 后恢复”）
    uv run --no-sync python scripts/dev_stack.py start worker         再拉起来
    uv run --no-sync python scripts/dev_stack.py env [与 up 相同的选项] [--json]   只打印各组件的环境变量，不启动
    uv run --no-sync python scripts/dev_stack.py down                 停掉全部（再 down 一次不报错）

- 目录（--root，默认 ~/.agentruntime/dev-stack）里放本次运行的一切：编译出的 task-service 与 model-gateway、日志、业务库、
  开发 SecretStore（shared/secrets.json）、ObjectStore（shared/objects，task-service 与 Worker 共用，Claim-Check 两边要互相取回）、
  平台内工具的工作区（workspaces/<租户>/<workspace>）、状态文件 stack.json。默认不在仓库里，因为 SecretStore 里有供应商的密钥。
- 供应商：openai_compat 时地址与密钥只从 --provider-env（默认 ~/.agentruntime/model-provider.env，格式 NAME = value）读；
  密钥经 model-gateway dev-secret 的标准输入写进开发 SecretStore，不进命令行参数、环境变量与任何输出。mock 时用 deploy/dev/mock-model-profiles.json。
- Registry：task-service 设 AR_REGISTRY_ADDR 打开 Registry，启动前用 registry-seed 种下 deploy/dev/registry-seed.json，
  再按 --tenant-effect 给每个租户种一份租户策略（同时写进开发租户策略文件，task-service 按它给 Task 的 effect 上限）。
- Worker：--decisions model（默认）时 AR_PLANNER、AR_REVIEWER、AR_TOOL_LOOP 都是 model，经 Model Gateway；stub 时都用桩。
  Worker 用跑这个脚本的 Python（--python 可以换），PATH 前面加上它所在的目录，shell_run 里的 python 就是同一个解释器。
- 输出：up、status 加 --json 时最后一行是 JSON（地址、pid、日志位置），不含任何密钥。
- --coding：再编译并拉起 coding agent 的网页服务 coding-web（products/coding，docs/plan/coding-agent-mvp.md），开发登录打开，
  与 Worker 共用工作区；租户首次登录时它把租户的 effect 上限写成 write_reversible。端口默认固定 8095（--coding-port）；
  ~/.agentruntime/oauth.env（或旧的 github-oauth.env）里配了哪几家就打开哪几家的登录（GitHub、Gitee、GitLab、Gitea、Bitbucket、Google；
  OAuth 回调 http://127.0.0.1:<端口>/api/auth/<平台>/callback）。--sandbox docker：Worker 的 shell_run
  在一次性 Docker 容器里跑（不联网、限资源），要 PATH 上有 docker。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path.home() / ".agentruntime" / "dev-stack"
DEFAULT_PROVIDER_ENV = Path.home() / ".agentruntime" / "model-provider.env"
# coding-web 的第三方登录（格式同上）：每一家 CODING_<平台>_CLIENT_ID 与 _CLIENT_SECRET（可选 _URL、_API_URL、_SCOPES、_NAME），
# 平台是 GITHUB、GITEE、GITLAB、GITEA、BITBUCKET、GOOGLE。oauth.env 放全部；github-oauth.env 是只有 GitHub 时的旧文件，两个都读。
DEFAULT_OAUTH_ENVS = (Path.home() / ".agentruntime" / "github-oauth.env", Path.home() / ".agentruntime" / "oauth.env")
OAUTH_PROVIDERS = ("GITHUB", "GITEE", "GITLAB", "GITEA", "BITBUCKET", "GOOGLE")
OAUTH_ENV_NAMES = tuple(f"CODING_{p}_{k}" for p in OAUTH_PROVIDERS for k in ("CLIENT_ID", "CLIENT_SECRET", "URL", "API_URL", "SCOPES", "NAME"))
# OAuth App 的回调地址要固定，coding-web 默认用这个端口。
DEFAULT_CODING_PORT = 8095
SEED_FILE = REPO_ROOT / "deploy" / "dev" / "registry-seed.json"
PROFILES = {"openai_compat": REPO_ROOT / "deploy" / "dev" / "model-profiles.json", "mock": REPO_ROOT / "deploy" / "dev" / "mock-model-profiles.json"}
COMPONENTS = ("temporal", "task-service", "model-gateway", "worker", "coding")
# 报告里多一个 registry：它在 task-service 进程里，有自己的内部端口（T-08）。
REPORTED = ("temporal", "task-service", "registry", "model-gateway", "worker", "coding")
# 只在 up --coding 时才起的组件。
OPTIONAL = {"coding"}
EFFECTS = ("none", "read", "write_reversible", "write_irreversible")
WORKER_QUEUES = "task,decision,model,program"
PROVIDER_KEY_NAME = "model-provider/api-key"
PUBLISH_TOKEN_NAME = "registry/publish-token"
# 子进程一律不继承的环境变量：真实密钥只经 dev-secret 的标准输入进入 SecretStore。
SCRUBBED_ENV = ("AR_MODEL_PROVIDER_API_KEY",)
EXE = ".exe" if os.name == "nt" else ""


class StackError(Exception):
    pass


# --- 小工具 ---


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_until(check: Any, what: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except Exception as exc:  # noqa: BLE001 — 轮询期间的错误只记最后一个
            last = exc
        time.sleep(0.3)
    raise StackError(f"{timeout:.0f} 秒内没有等到：{what}（最后的错误：{last!r}）")


def http_ok(url: str) -> bool:
    with urllib.request.urlopen(url, timeout=2) as resp:
        return resp.status == 200


def port_open(address: str) -> bool:
    host, _, port = address.rpartition(":")
    with socket.create_connection((host, int(port)), timeout=2):
        return True


def require(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        raise StackError(f"找不到 {tool}：见 CLAUDE.md 的工具链（PATH 上要有 go、temporal）")
    return path


def read_provider_env(path: Path) -> dict[str, str]:
    """NAME = value 一行一个；# 开头的是注释。"""
    out: dict[str, str] = {}
    for line in path.read_text("utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and not name.strip().startswith("#"):
            out[name.strip()] = value.strip()
    return out


def child_env(extra: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
    env.update(extra)
    return env


def tail(path: Path, lines: int = 40) -> str:
    if not path.exists():
        return ""
    return "\n".join(path.read_text("utf-8", errors="replace").splitlines()[-lines:])


# --- 进程 ---


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def kill(pid: int | None) -> None:
    """连同子进程一起强杀（Windows 上 venv 的 python.exe 是启动器，真正的解释器是它的子进程）。"""
    if not alive(pid):
        return
    assert pid is not None
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 15
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)


def spawn(args: list[str], env: dict[str, str], log: Path, *, stdout: Path | None = None, cwd: Path | None = None) -> int:
    """后台起一个进程，脚本退出后它还在：标准错误写 log；给了 stdout 时标准输出另写一个文件（网关的计量记录）。"""
    log.parent.mkdir(parents=True, exist_ok=True)
    err = log.open("ab")
    out = stdout.open("ab") if stdout is not None else err
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(args, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=err if stdout is not None else subprocess.STDOUT, **kwargs)
    return proc.pid


# --- 整套 ---


class Stack:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.state_file = self.root / "stack.json"
        self.state: dict[str, Any] = json.loads(self.state_file.read_text("utf-8")) if self.state_file.exists() else {}

    # 路径
    @property
    def shared(self) -> Path:
        return self.root / "shared"

    @property
    def secrets_file(self) -> Path:
        return self.shared / "secrets.json"

    @property
    def objects_dir(self) -> Path:
        return self.shared / "objects"

    @property
    def workspace_root(self) -> Path:
        return self.root / "workspaces"

    @property
    def signing_key_file(self) -> Path:
        return self.shared / "signing.key"

    @property
    def policies_file(self) -> Path:
        return self.root / "tenant_policies.json"

    def log(self, name: str) -> Path:
        return self.root / "logs" / f"{name}.log"

    def binary(self, name: str) -> Path:
        return self.root / "bin" / f"{name}{EXE}"

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), "utf-8")

    # 配置
    def configure(self, args: argparse.Namespace) -> None:
        effects: dict[str, str] = {}
        for item in args.tenant_effect or []:
            tenant, sep, effect = item.partition("=")
            if not sep or not tenant or effect not in EFFECTS:
                raise StackError(f"--tenant-effect 要写成 租户=effect（effect 取 {'、'.join(EFFECTS)}），实际 {item!r}")
            effects[tenant] = effect
        provider_env = Path(args.provider_env) if args.provider_env else DEFAULT_PROVIDER_ENV
        if args.provider == "openai_compat" and not provider_env.exists():
            raise StackError(f"找不到供应商的配置 {provider_env}（--provider-env 指定，或用 --provider mock）")
        self.state = {
            "root": str(self.root),
            "options": {
                "provider": args.provider,
                "provider_env": str(provider_env) if args.provider == "openai_compat" else "",
                "decisions": args.decisions,
                "tenant_effects": effects,
                "python": args.python or sys.executable,
                "coding": bool(getattr(args, "coding", False)),
                "sandbox": getattr(args, "sandbox", "host") or "host",
            },
            "ports": {"temporal": free_port(), "temporal_ui": free_port(), "http": free_port(), "registry": free_port(), "gateway": free_port(), "coding": int(getattr(args, "coding_port", 0) or DEFAULT_CODING_PORT)},
            "components": {},
        }

    @property
    def options(self) -> dict[str, Any]:
        return self.state["options"]

    def address(self, name: str) -> str:
        return f"127.0.0.1:{self.state['ports'][name]}"

    @property
    def task_api(self) -> str:
        return f"http://{self.address('http')}"

    @property
    def coding_url(self) -> str:
        return f"http://{self.address('coding')}" if self.options.get("coding") else ""

    def wanted(self, name: str) -> bool:
        return name not in OPTIONAL or bool(self.options.get(name))

    def shared_env(self) -> dict[str, str]:
        return {"AR_OBJECT_STORE_DIR": str(self.objects_dir), "AR_SECRETS_FILE": str(self.secrets_file)}

    def provider_values(self) -> dict[str, str]:
        if self.options["provider"] != "openai_compat":
            return {}
        return read_provider_env(Path(self.options["provider_env"]))

    def signing_key(self) -> str:
        if not self.signing_key_file.exists():
            self.shared.mkdir(parents=True, exist_ok=True)
            self.signing_key_file.write_text(secrets.token_hex(32), "utf-8")
        return self.signing_key_file.read_text("utf-8").strip()

    def envs(self) -> dict[str, dict[str, str]]:
        """各组件的环境变量（不含继承来的那部分）。"""
        python_dir = str(Path(self.options["python"]).parent)
        decisions = "model" if self.options["decisions"] == "model" else "stub"
        gateway = {
            "AR_MODEL_GATEWAY_ADDR": self.address("gateway"),
            "AR_MODEL_PROFILES_FILE": str(PROFILES[self.options["provider"]]),
            "AR_MODEL_PROVIDER": self.options["provider"],
            "AR_SECRETS_FILE": str(self.secrets_file),
        }
        base_url = self.provider_values().get("AR_MODEL_PROVIDER_BASE_URL", "")
        if base_url:
            gateway["AR_MODEL_PROVIDER_BASE_URL"] = base_url
        worker_path = python_dir + os.pathsep + os.environ.get("PATH", "")
        sandbox: dict[str, str] = {}
        if self.options.get("sandbox") == "docker":
            docker = shutil.which("docker")
            if docker:  # docker 的凭据助手与它在同一目录
                worker_path = str(Path(docker).parent) + os.pathsep + worker_path
            sandbox = {"AR_TOOL_SHELL_SANDBOX": "docker"}
        coding = {
            "CODING_ADDR": self.address("coding"),
            "CODING_TASK_API": self.task_api,
            "CODING_WORKSPACE_ROOT": str(self.workspace_root),
            "CODING_DATA_DIR": str(self.root / "coding"),
            "CODING_DEV_LOGIN": "1",
            "CODING_TENANT_POLICIES_FILE": str(self.policies_file),
            "CODING_PUBLIC_URL": f"http://{self.address('coding')}",
            "AR_DEV_SIGNING_KEY": self.signing_key(),
            # 只用来在界面上说明 shell_run 在哪里跑；镜像与 Worker 的默认 AR_TOOL_SHELL_IMAGE 相同。
            "CODING_SANDBOX": "docker" if sandbox else "host",
        }
        if sandbox:
            coding["CODING_SANDBOX_IMAGE"] = os.environ.get("AR_TOOL_SHELL_IMAGE") or "python:3.13-slim"
        for path in DEFAULT_OAUTH_ENVS:
            if path.exists():
                values = read_provider_env(path)
                coding.update({k: values[k] for k in OAUTH_ENV_NAMES if values.get(k)})
        return {
            "temporal": {},
            "task-service": {
                "AR_TEMPORAL_ADDRESS": self.address("temporal"),
                "AR_HTTP_ADDR": self.address("http"),
                "AR_PUBLIC_BASE_URL": self.task_api,
                "AR_DEV_SIGNING_KEY": self.signing_key(),
                "AR_DATA_DIR": str(self.root / "task-service"),
                "AR_REGISTRY_ADDR": self.address("registry"),
                "AR_SCHEMAS_DIR": str(REPO_ROOT / "schemas"),
                "AR_DEV_TENANT_POLICIES_FILE": str(self.policies_file),
                **self.shared_env(),
            },
            "model-gateway": gateway,
            "worker": {
                "AR_TEMPORAL_ADDRESS": self.address("temporal"),
                "AR_DATA_DIR": str(self.root / "worker"),
                "AR_MODEL_GATEWAY_ADDRESS": self.address("gateway"),
                "AR_REGISTRY_ADDRESS": self.address("registry"),
                "AR_PLANNER": decisions,
                "AR_REVIEWER": decisions,
                "AR_TOOL_LOOP": decisions,
                "AR_TOOL_WORKSPACE_ROOT": str(self.workspace_root),
                "PYTHONIOENCODING": "utf-8",
                "PATH": worker_path,
                **sandbox,
                **self.shared_env(),
            },
            "coding": coding,
        }

    # 准备
    def build(self) -> None:
        go = require("go")
        if self.options.get("sandbox") == "docker":
            require("docker")
        targets = [("task-service", "services/go-task-service"), ("model-gateway", "services/go-model-gateway")]
        if self.options.get("coding"):
            targets.append(("coding-web", "products/coding"))
        for name, where in targets:
            proc = subprocess.run(
                [go, "build", "-o", str(self.binary(name)), f"./cmd/{name}"], cwd=REPO_ROOT / where, capture_output=True, text=True, encoding="utf-8", errors="replace"
            )
            if proc.returncode != 0:
                raise StackError(f"编译 {name} 失败：\n{proc.stderr}")

    def dev_secret(self, name: str, value: str) -> None:
        proc = subprocess.run(
            [str(self.binary("model-gateway")), "dev-secret", name],
            input=value + "\n",
            env=child_env({"AR_SECRETS_FILE": str(self.secrets_file)}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if proc.returncode != 0:
            raise StackError(f"model-gateway dev-secret {name} 失败（退出码 {proc.returncode}）：{proc.stderr.replace(value, '***')}")

    def prepare(self) -> None:
        for d in (self.shared, self.objects_dir, self.workspace_root, self.root / "logs", self.root / "task-service", self.root / "worker"):
            d.mkdir(parents=True, exist_ok=True)
        effects = self.options["tenant_effects"]
        self.policies_file.write_text(json.dumps({"tenants": {t: {"effect_ceiling": e} for t, e in sorted(effects.items())}}), "utf-8")
        self.dev_secret(PUBLISH_TOKEN_NAME, secrets.token_hex(24))
        if self.options["provider"] == "openai_compat":
            key = self.provider_values().get("AR_MODEL_PROVIDER_API_KEY", "")
            if not key:
                raise StackError(f"{self.options['provider_env']} 里没有 AR_MODEL_PROVIDER_API_KEY")
            self.dev_secret(PROVIDER_KEY_NAME, key)
        self.seed(SEED_FILE)
        if effects:
            tenants = self.root / "registry-seed.tenants.json"
            entries = [{"kind": "tenant_policy", "tenant": t, "document": {"effect_ceiling": e}} for t, e in sorted(effects.items())]
            tenants.write_text(json.dumps({"entries": entries}), "utf-8")
            self.seed(tenants)

    def seed(self, path: Path) -> None:
        env = child_env({"AR_DATA_DIR": str(self.root / "task-service"), "AR_SCHEMAS_DIR": str(REPO_ROOT / "schemas")})
        proc = subprocess.run([str(self.binary("task-service")), "registry-seed", str(path)], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        if proc.returncode != 0:
            raise StackError(f"registry-seed {path.name} 失败：\n{proc.stderr}")

    # 组件
    def start(self, name: str) -> None:
        envs = self.envs()
        components = self.state.setdefault("components", {})
        if alive((components.get(name) or {}).get("pid")):
            return
        log = self.log(name)
        if name == "temporal":
            temporal = require("temporal")
            ports = self.state["ports"]
            args = [temporal, "server", "start-dev", "--ip", "127.0.0.1", "--port", str(ports["temporal"]), "--ui-port", str(ports["temporal_ui"]), "--headless", "--log-level", "error"]
            pid = spawn(args, child_env(envs[name]), log)
            components[name] = {"pid": pid, "log": str(log)}
            wait_until(
                lambda: subprocess.run([temporal, "workflow", "list", "--address", self.address("temporal"), "--limit", "1"], capture_output=True, timeout=30).returncode == 0,
                "Temporal 开发服务就绪",
                90,
            )
        elif name == "task-service":
            pid = spawn([str(self.binary(name)), "serve"], child_env(envs[name]), log)
            components[name] = {"pid": pid, "log": str(log)}
            wait_until(lambda: http_ok(self.task_api + "/healthz"), "task-service 就绪", 90)
            wait_until(lambda: port_open(self.address("registry")), "Registry 的端口可连", 30)
        elif name == "model-gateway":
            stdout = self.root / "logs" / "model-gateway.stdout.log"
            pid = spawn([str(self.binary(name)), "serve"], child_env(envs[name]), log, stdout=stdout)
            components[name] = {"pid": pid, "log": str(log), "stdout": str(stdout)}
            wait_until(lambda: http_ok(f"http://{self.address('gateway')}/healthz"), "model-gateway 就绪", 60)
        elif name == "worker":
            args = [self.options["python"], "-m", "agentruntime_worker", "--queues", WORKER_QUEUES]
            pid = spawn(args, child_env(envs[name]), log, cwd=REPO_ROOT)
            components[name] = {"pid": pid, "log": str(log)}
            time.sleep(3)
            if not alive(pid):
                raise StackError(f"Worker 启动失败：\n{tail(log)}")
        elif name == "coding":
            if not self.wanted(name):
                raise StackError("这套整套没有带 --coding 启动")
            (self.root / "coding").mkdir(parents=True, exist_ok=True)
            pid = spawn([str(self.binary("coding-web"))], child_env(envs[name]), log)
            components[name] = {"pid": pid, "log": str(log)}
            wait_until(lambda: http_ok(self.coding_url + "/healthz"), "coding-web 就绪", 30)
        else:
            raise StackError(f"不认识的组件 {name}（{'、'.join(COMPONENTS)}）")
        self.save()

    def kill(self, name: str) -> None:
        if name not in COMPONENTS:
            raise StackError(f"不认识的组件 {name}（{'、'.join(COMPONENTS)}）")
        kill((self.state.get("components", {}).get(name) or {}).get("pid"))

    def down(self) -> None:
        for name in reversed(COMPONENTS):
            kill((self.state.get("components", {}).get(name) or {}).get("pid"))

    # 报告
    def report(self) -> dict[str, Any]:
        comps = self.state.get("components", {})
        out: dict[str, Any] = {}
        for name in REPORTED:
            source = "task-service" if name == "registry" else name
            if not self.wanted(source):
                continue  # 没带 --coding 启动时不报 coding
            entry = comps.get(source) or {}
            item = {"pid": entry.get("pid"), "alive": alive(entry.get("pid")), "log": entry.get("log", str(self.log(source)))}
            if entry.get("stdout"):
                item["stdout"] = entry["stdout"]
            out[name] = item
        return {
            "root": str(self.root),
            "task_api": self.task_api if self.state.get("ports") else "",
            "temporal": self.address("temporal") if self.state.get("ports") else "",
            "registry": self.address("registry") if self.state.get("ports") else "",
            "model_gateway": self.address("gateway") if self.state.get("ports") else "",
            "coding": self.coding_url if self.state.get("ports") else "",
            "workspace_root": str(self.workspace_root),
            "shared_env": self.shared_env(),
            "components": out,
        }

    def diagnostics(self, known_secrets: list[str]) -> str:
        text = "\n".join(f"===== {name} =====\n{tail(self.log(name))}" for name in COMPONENTS if self.wanted(name))
        for s in known_secrets:
            if s:
                text = text.replace(s, "***")
        return text


# --- 命令 ---


def cmd_up(args: argparse.Namespace) -> int:
    stack = Stack(Path(args.root))
    if any(alive(c.get("pid")) for c in stack.state.get("components", {}).values()):
        raise StackError(f"{stack.root} 上已经有在运行的整套：先 down")
    stack.configure(args)
    stack.save()
    try:
        stack.build()
        stack.prepare()
        for name in COMPONENTS:
            if stack.wanted(name):
                stack.start(name)
    except (StackError, subprocess.SubprocessError, OSError) as err:
        known = [stack.provider_values().get("AR_MODEL_PROVIDER_API_KEY", "")]
        stack.down()
        print(f"dev_stack up 失败：{err}\n{stack.diagnostics(known)}", file=sys.stderr)
        return 1
    print_report(stack, args.json, "整套已经起来")
    return 0


def print_report(stack: Stack, as_json: bool, title: str) -> None:
    report = stack.report()
    if as_json:
        print(json.dumps(report, ensure_ascii=False))
        return
    print(title)
    print(f"  Task API   {report['task_api']}")
    print(f"  Temporal   {report['temporal']}")
    print(f"  Registry   {report['registry']}")
    print(f"  Gateway    {report['model_gateway']}")
    if report.get("coding"):
        print(f"  Coding     {report['coding']}  （网页，开发登录）")
    print(f"  工作区     {report['workspace_root']}")
    for name, c in report["components"].items():
        if name in OPTIONAL and not c["pid"]:
            continue
        print(f"  {name:<14}{'运行中' if c['alive'] else '已停止'}  pid {c['pid']}  日志 {c['log']}")


def cmd_status(args: argparse.Namespace) -> int:
    stack = Stack(Path(args.root))
    print_report(stack, args.json, f"{stack.root}")
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    Stack(Path(args.root)).down()
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    Stack(Path(args.root)).kill(args.component)
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    stack = Stack(Path(args.root))
    if not stack.state:
        raise StackError(f"{stack.root} 上还没有 up 过")
    stack.start(args.component)
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    stack = Stack(Path(args.root))
    cmd = [str(stack.binary("task-service")), "dev-token", "--tenant", args.tenant, "--user", args.user, "--ttl", args.ttl]
    for scope in args.scope or []:
        cmd += ["--scope", scope]
    proc = subprocess.run(cmd, env=child_env({"AR_DEV_SIGNING_KEY": stack.signing_key()}), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if proc.returncode != 0:
        raise StackError(f"dev-token 失败：{proc.stderr}")
    print(proc.stdout.strip())
    return 0


def cmd_env(args: argparse.Namespace) -> int:
    stack = Stack(Path(args.root))
    stack.configure(args)
    envs = {name: env for name, env in stack.envs().items() if stack.wanted(name)}
    for env in envs.values():
        if "AR_DEV_SIGNING_KEY" in env:
            env["AR_DEV_SIGNING_KEY"] = "***（在 shared/signing.key）"
        for name in [k for k in env if k.startswith("CODING_") and k.endswith("_CLIENT_SECRET")]:
            env[name] = "***（在 ~/.agentruntime/oauth.env 或 github-oauth.env）"
    if args.json:
        print(json.dumps({"root": str(stack.root), "components": envs}, ensure_ascii=False))
    else:
        for name, env in envs.items():
            print(f"[{name}]")
            for k, v in sorted(env.items()):
                print(f"  {k}={v}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python scripts/dev_stack.py", description="本地拉起整套（T-13）")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--root", default=str(DEFAULT_ROOT), help=f"这次运行的目录（默认 {DEFAULT_ROOT}）")

    def options(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--provider", choices=("openai_compat", "mock"), default="openai_compat")
        sp.add_argument("--provider-env", default="", help=f"供应商的地址与密钥（默认 {DEFAULT_PROVIDER_ENV}）")
        sp.add_argument("--decisions", choices=("model", "stub"), default="model", help="Planner、Reviewer、tool_loop 用真的（model）还是桩")
        sp.add_argument("--tenant-effect", action="append", help="租户=effect 上限，可以写多个")
        sp.add_argument("--python", default="", help="Worker 用的 Python（默认跑这个脚本的）")
        sp.add_argument("--coding", action="store_true", help="再拉起 coding agent 的网页服务 coding-web（products/coding）")
        sp.add_argument("--sandbox", choices=("host", "docker"), default="host", help="Worker 的 shell_run 在宿主机（host）还是一次性 Docker 容器（docker）里跑")
        sp.add_argument("--coding-port", type=int, default=DEFAULT_CODING_PORT, help=f"coding-web 的端口（默认 {DEFAULT_CODING_PORT}；GitHub OAuth 的回调地址要固定）")
        sp.add_argument("--json", action="store_true")

    up = sub.add_parser("up", help="编译、种 Registry、拉起全部组件")
    common(up)
    options(up)
    up.set_defaults(func=cmd_up)
    env = sub.add_parser("env", help="只打印各组件的环境变量（不含密钥），不启动")
    common(env)
    options(env)
    env.set_defaults(func=cmd_env)
    for name, func, text in (("status", cmd_status, "各组件的状态"), ("down", cmd_down, "停掉全部组件")):
        sp = sub.add_parser(name, help=text)
        common(sp)
        if name == "status":
            sp.add_argument("--json", action="store_true")
        sp.set_defaults(func=func)
    for name, func, text in (("kill", cmd_kill, "强杀一个组件"), ("start", cmd_start, "再拉起一个组件")):
        sp = sub.add_parser(name, help=text)
        sp.add_argument("component", choices=COMPONENTS)
        common(sp)
        sp.set_defaults(func=func)
    token = sub.add_parser("token", help="签一个开发用的访问令牌")
    common(token)
    token.add_argument("--tenant", required=True)
    token.add_argument("--user", required=True)
    token.add_argument("--scope", action="append")
    token.add_argument("--ttl", default="2h")
    token.set_defaults(func=cmd_token)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.func(args))
    except StackError as err:
        print(f"dev_stack {args.command}：{err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
