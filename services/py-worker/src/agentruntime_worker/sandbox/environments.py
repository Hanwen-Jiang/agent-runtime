"""按项目定制的运行环境（设计书改动稿 W6 第 3 条；docs/plan/cloud-deploy.md D-17；docs/plan/sandbox-platform.md 第 5 节）。

仓库自带环境定义时，第一次用到就构建一个镜像，按“定义的哈希 + 基础镜像 digest”缓存，之后直接用；构建好之前、构建失败、
超了个数上限时退回 universal，并在 shell_run 的结果里说明原因（不悄悄忽略）。

- 识别（detect）：按顺序找 .devcontainer/devcontainer.json、.devcontainer.json、.tool-versions、根目录的 Dockerfile，用第一个。
  - devcontainer.json（允许注释与结尾逗号）：image（FROM 它）或 build.dockerfile + build.context；features、dockerComposeFile
    还不支持（不支持就退回 universal 并说明）。
  - .tool-versions：python、nodejs、golang、rust、java（temurin-<版本>+<构建号>）写死的版本，生成一个 Dockerfile：在 universal
    之上把对应的工具链换成官方镜像里的那个版本（各占一层，同 deploy/images/universal 的做法），不在构建里现下载工具链。
  - Dockerfile：照原样构建，上下文是仓库根目录。
- 静态检查（check_dockerfile）：用户的 Dockerfile 不可信。FROM、COPY --from、RUN --mount=from 引用的镜像不能在本部署的环境
  镜像仓库里别的租户的路径下，不能指向地址或内网名字；RUN 的 --network=host、--security=insecure、secret 与 ssh 挂载不收；
  # syntax 只认 docker/dockerfile；ADD 的远端地址不能是地址或内网名字。真正的隔离靠构建在 microVM 里（env_builder）。
- 缓存（EnvironmentStore，业务库的 sandbox_images，迁移 204，租户作用域）：building → ready，或 failed（原因）；
  长期不用的（idle_days）清掉：删仓库里的清单、记为 deleted。个数配额在建行的事务里核对（与沙箱环境的配额同一种写法）。
- 构建（EnvironmentManager.run_build，构建工作流的 activity 调它）：起构建器（env_builder.K8sJobBuilder：Kata 里的一次性 Job，
  联网只经包镜像与出网网关），构建器把产物（OCI 打包）交到 Worker 的临时文件里（构建 Pod 只读地提供，Worker 去取），Worker
  按大小上限核对，推到镜像仓库里本租户的路径（<prefix>/<租户>/env:<键>）。仓库的凭据只在 Worker 手里，构建里没有。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import logging
import re
import tempfile
import time
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, fields
from datetime import timedelta
from pathlib import PurePosixPath
from typing import IO, Any, Protocol

from agentruntime_core.dev._common import PLATFORM_ID
from agentruntime_core.errors import DeniedError, InvalidError, NotFoundError, ProviderError
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.sandbox.ledger import SandboxLedger, iso

log = logging.getLogger(__name__)

DEFINITION_PATHS = (".devcontainer/devcontainer.json", ".devcontainer.json", ".tool-versions", "Dockerfile")
MAX_DEFINITION_BYTES = 64 * 1024

# 读工作区里一个文件（相对仓库根的路径）；没有时 None。
FileReader = Callable[[str], Awaitable[bytes | None]]


# --- 识别 ---


@dataclass(frozen=True)
class Definition:
    """仓库里的环境定义。problem 非空表示认出来了但不支持（退回 universal 的原因）。"""

    kind: str  # devcontainer、tool-versions、dockerfile
    path: str
    content: bytes
    dockerfile: str = ""
    # 构建上下文（相对仓库根的目录；"" 表示不需要仓库里的文件）。
    context: str = ""
    problem: str = ""

    def key(self, base_digest: str) -> str:
        """缓存键：定义（含生成或引用的 Dockerfile）的哈希加上基础镜像的 digest。"""
        doc = {
            "kind": self.kind, "path": self.path, "content": hashlib.sha256(self.content).hexdigest(),
            "dockerfile": hashlib.sha256(self.dockerfile.encode()).hexdigest(), "context": self.context, "base": base_digest,
        }
        return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


async def detect(read: FileReader, *, base_image: str) -> Definition | None:
    """找仓库里的环境定义；没有时 None。"""
    for path in DEFINITION_PATHS:
        data = await read(path)
        if data is None:
            continue
        if len(data) > MAX_DEFINITION_BYTES:
            return Definition(kind=_kind(path), path=path, content=data[:1024], problem=f"{path} 太大（上限 {MAX_DEFINITION_BYTES} 字节）")
        if path.endswith("devcontainer.json"):
            return await _devcontainer(read, path, data)
        if path == ".tool-versions":
            dockerfile, problem = tool_versions_dockerfile(data.decode("utf-8", errors="replace"), base_image)
            return Definition(kind="tool-versions", path=path, content=data, dockerfile=dockerfile, problem=problem)
        return Definition(kind="dockerfile", path=path, content=data, dockerfile=data.decode("utf-8", errors="replace"), context=".")
    return None


def _kind(path: str) -> str:
    return "devcontainer" if path.endswith("devcontainer.json") else ("tool-versions" if path == ".tool-versions" else "dockerfile")


def strip_jsonc(text: str) -> str:
    """去掉 JSON 里的 // 与 /* */ 注释和结尾的逗号（devcontainer.json 允许它们），字符串里的不动。"""
    out: list[str] = []
    i, n, in_str = 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _within_repo(base: str, rel: str) -> str | None:
    """base（目录）下的相对路径规范化成相对仓库根的路径；出了仓库返回 None。"""
    parts: list[str] = [p for p in PurePosixPath(base).parts if p not in ("", ".")]
    for part in PurePosixPath(rel).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part.startswith("/") or "\\" in part:
            return None
        else:
            parts.append(part)
    return "/".join(parts) or "."


async def _devcontainer(read: FileReader, path: str, data: bytes) -> Definition:
    def unsupported(problem: str) -> Definition:
        return Definition(kind="devcontainer", path=path, content=data, problem=problem)

    try:
        doc = json.loads(strip_jsonc(data.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        return unsupported(f"{path} 不是合法的 JSON")
    if not isinstance(doc, dict):
        return unsupported(f"{path} 要是一个对象")
    if doc.get("features"):
        return unsupported("devcontainer 的 features 还不支持（要在构建里现下载安装脚本）")
    if doc.get("dockerComposeFile"):
        return unsupported("devcontainer 的 dockerComposeFile 还不支持")
    base_dir = str(PurePosixPath(path).parent)
    build = doc.get("build") or {}
    if isinstance(build, dict) and (build.get("dockerfile") or doc.get("dockerFile")):
        rel = str(build.get("dockerfile") or doc.get("dockerFile"))
        dockerfile_path = _within_repo(base_dir, rel)
        context = _within_repo(base_dir, str(build.get("context") or "."))
        if dockerfile_path is None or context is None:
            return unsupported("devcontainer 的 dockerfile 或 context 出了仓库")
        text = await read(dockerfile_path)
        if text is None:
            return unsupported(f"devcontainer 引用的 {dockerfile_path} 不存在")
        args = build.get("args") or {}
        if not isinstance(args, dict) or not all(isinstance(v, str) for v in args.values()):
            return unsupported("devcontainer 的 build.args 要是字符串")
        dockerfile = text.decode("utf-8", errors="replace")
        if args:
            # 构建参数写成 Dockerfile 开头的 ARG 默认值（构建器不另传参数，键里就包含了它们）。
            prelude = "".join(f"ARG {k}={json.dumps(v)}\n" for k, v in sorted(args.items()) if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k))
            dockerfile = _insert_args(dockerfile, prelude)
        return Definition(kind="devcontainer", path=path, content=data, dockerfile=dockerfile, context=context)
    image = doc.get("image")
    if isinstance(image, str) and image.strip():
        return Definition(kind="devcontainer", path=path, content=data, dockerfile=f"FROM {image.strip()}\nENV HOME=/tmp\n")
    return unsupported("devcontainer.json 里既没有 image 也没有 build.dockerfile")


def _insert_args(dockerfile: str, prelude: str) -> str:
    """ARG 要在第一个 FROM 之前才能给 FROM 用；# syntax 之类的解析指令要留在最前面。"""
    lines = dockerfile.splitlines(keepends=True)
    i = 0
    while i < len(lines) and re.match(r"^\s*#\s*[a-z]+\s*=", lines[i]):
        i += 1
    return "".join(lines[:i]) + prelude + "".join(lines[i:])


# .tool-versions 认得的工具：官方镜像的写法与放进 universal 的方式。版本要写死（三段）。
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_TEMURIN = re.compile(r"^temurin-(\d+\.\d+\.\d+)\+(\d+)(?:\.\d+)*(?:\.LTS)?$")
_PASSTHROUGH = (
    "PIP_CACHE_DIR PIP_DISABLE_PIP_VERSION_CHECK npm_config_cache npm_config_update_notifier npm_config_fund GOPATH GOCACHE GOMODCACHE "
    "GOTOOLCHAIN GOFLAGS JAVA_HOME MAVEN_HOME MAVEN_ARGS RUSTUP_HOME CARGO_HOME"
)


def tool_versions_dockerfile(text: str, base_image: str) -> tuple[str, str]:
    """按 .tool-versions 生成 Dockerfile；返回 (Dockerfile, 不支持的原因)。"""
    tools: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            return "", f".tool-versions 的这一行看不懂：{raw.strip()[:80]}"
        name, version = parts[0].lower(), parts[1]
        name = {"node": "nodejs", "go": "golang"}.get(name, name)
        if name in tools:
            return "", f".tool-versions 里 {name} 写了两次"
        tools[name] = version
    if not tools:
        return "", ".tool-versions 是空的"
    stages: list[str] = []
    steps: list[str] = []
    env: list[str] = []
    passthrough = _PASSTHROUGH
    for name in sorted(tools):
        version = tools[name]
        if name == "java":
            m = _TEMURIN.match(version)
            if not m:
                return "", f"java 要写成 temurin-<版本>+<构建号>（例如 temurin-21.0.5+11.0.LTS），实际 {version}"
            stages.append(f"FROM eclipse-temurin:{m.group(1)}_{m.group(2)}-jdk-jammy AS java")
            steps += ["RUN rm -rf /opt/java/openjdk", "COPY --from=java /opt/java/openjdk /opt/java/openjdk"]
            continue
        if not _SEMVER.match(version):
            return "", f"{name} 的版本要写死成三段（例如 1.2.3），实际 {version}"
        if name == "python":
            stages.append(f"FROM python:{version}-slim-bookworm AS python")
            steps.append("COPY --from=python /usr/local /opt/python")
            env += ["PATH=/opt/python/bin:$PATH", "LD_LIBRARY_PATH=/opt/python/lib"]
            passthrough += " LD_LIBRARY_PATH"
        elif name == "nodejs":
            stages.append(f"FROM node:{version}-bookworm-slim AS nodejs")
            steps += ["RUN rm -rf /usr/local/lib/node_modules", "COPY --from=nodejs /usr/local/bin/node /usr/local/bin/node",
                      "COPY --from=nodejs /usr/local/lib/node_modules /usr/local/lib/node_modules"]
        elif name == "golang":
            stages.append(f"FROM golang:{version}-bookworm AS golang")
            steps += ["RUN rm -rf /usr/local/go", "COPY --from=golang /usr/local/go /usr/local/go"]
        elif name == "rust":
            stages.append(f"FROM rust:{version}-slim-bookworm AS rust")
            steps += ["RUN rm -rf /usr/local/rustup /usr/local/cargo", "COPY --from=rust /usr/local/rustup /usr/local/rustup",
                      "COPY --from=rust /usr/local/cargo /usr/local/cargo"]
        else:
            return "", f".tool-versions 里的 {name} 还不支持（支持 python、nodejs、golang、rust、java）"
    lines = [
        "# syntax=docker/dockerfile:1.7",
        "# 由 Runtime 按 .tool-versions 生成（D-17）：在 universal 之上换成写死的版本，各占一层。",
        *stages,
        f"FROM {base_image}",
        "USER 0:0",
        *steps,
    ]
    if env:
        lines.append("ENV " + " ".join(env))
    lines += [f'ENV AGENT_ENV_PASSTHROUGH="{passthrough}"', "USER 10001:10001", ""]
    return "\n".join(lines), ""


# --- 静态检查 ---

_INTERNAL_SUFFIXES = (".local", ".localhost", ".internal", ".svc", ".cluster.local", ".lan", ".home.arpa")


def _internal_host(host: str) -> bool:
    h = host.lower().strip("[]").rstrip(".")
    h = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    if h in ("localhost",) or any(h.endswith(s) for s in _INTERNAL_SUFFIXES):
        return True
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    # 十进制、八进制、十六进制写的地址（inet_aton 认的写法）
    return bool(re.fullmatch(r"(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+)){0,3}", h))


def _image_host(ref: str) -> str:
    first = ref.split("/", 1)[0]
    if "/" in ref and ("." in first or ":" in first or first == "localhost"):
        return first
    return ""


@dataclass(frozen=True)
class RegistryPath:
    """本部署放构建产物的镜像仓库：host（拉取用的主机名）与路径前缀；租户的镜像在 <host>/<prefix>/<租户>/env。"""

    host: str
    prefix: str

    def repo(self, tenant_id: str) -> str:
        return f"{self.prefix}/{tenant_id}/env" if self.prefix else f"{tenant_id}/env"


def check_dockerfile(text: str, *, tenant_id: str, registry: RegistryPath | None) -> list[str]:
    """用户的 Dockerfile 的静态检查；返回不收的原因（空表示通过）。"""
    problems: list[str] = []
    if len(text.encode()) > MAX_DEFINITION_BYTES:
        return [f"Dockerfile 太大（上限 {MAX_DEFINITION_BYTES} 字节）"]
    for m in re.finditer(r"(?im)^\s*#\s*syntax\s*=\s*(\S+)", text):
        if not re.fullmatch(r"(docker\.io/)?docker/dockerfile(:[\w.-]+)?(@sha256:[0-9a-f]{64})?", m.group(1)):
            problems.append(f"# syntax 只认 docker/dockerfile，不收 {m.group(1)}")
    stages: set[str] = set()
    for instr, args in _instructions(text):
        if instr == "FROM":
            words = [w for w in args.split() if not w.startswith("--")]
            if not words:
                continue
            ref = words[0]
            problems += _check_ref(ref, tenant_id, registry, stages)
            if len(words) >= 3 and words[1].lower() == "as":
                stages.add(words[2].lower())
        elif instr == "COPY":
            for flag in re.findall(r"--from=(\S+)", args):
                problems += _check_ref(flag, tenant_id, registry, stages)
        elif instr == "RUN":
            if re.search(r"--network=host\b", args):
                problems.append("RUN --network=host 不收")
            if re.search(r"--security=insecure\b", args):
                problems.append("RUN --security=insecure 不收")
            for mount in re.findall(r"--mount=(\S+)", args):
                opts = dict(p.split("=", 1) if "=" in p else (p, "") for p in mount.split(","))
                if opts.get("type") in ("secret", "ssh"):
                    problems.append(f"RUN --mount=type={opts['type']} 不收（构建里没有密钥）")
                if opts.get("from"):
                    problems += _check_ref(opts["from"], tenant_id, registry, stages)
        elif instr == "ADD":
            for url in re.findall(r"(https?://\S+)", args):
                host = urllib.parse.urlsplit(url).hostname or ""
                if _internal_host(host):
                    problems.append(f"ADD 的地址 {url[:80]} 指向地址或内网名字")
    return sorted(set(problems))


def _check_ref(ref: str, tenant_id: str, registry: RegistryPath | None, stages: set[str]) -> list[str]:
    if ref.lower() in stages or ref == "scratch" or "$" in ref:
        return [] if "$" not in ref else [f"镜像引用里不能用变量：{ref}"]
    host = _image_host(ref)
    if registry is not None and host.lower() == registry.host.lower():
        own = registry.repo(tenant_id) + "@"
        path = ref.split("/", 1)[1]
        if not (path.startswith(own) or path.startswith(registry.repo(tenant_id) + ":")):
            return [f"不能用环境镜像仓库里别的路径：{ref}"]
        return []
    if host and _internal_host(host):
        return [f"镜像 {ref} 指向地址或内网名字"]
    return []


def _instructions(text: str) -> list[tuple[str, str]]:
    """把 Dockerfile 拆成（指令，参数）：处理续行与注释。"""
    out: list[tuple[str, str]] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not buf and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        parts = buf.strip().split(None, 1)
        if parts:
            out.append((parts[0].upper(), parts[1] if len(parts) > 1 else ""))
        buf = ""
    if buf.strip():
        parts = buf.strip().split(None, 1)
        out.append((parts[0].upper(), parts[1] if len(parts) > 1 else ""))
    return out


# --- 缓存 ---


@dataclass(frozen=True)
class ImageRecord:
    image_key: str
    owner: str
    kind: str
    path: str
    dockerfile: str
    state: str
    created_at: str
    workspace_id: str = ""
    context: str = ""
    base_image: str = ""
    image: str = ""
    digest: str = ""
    size_bytes: int = 0
    reason: str = ""
    ready_at: str = ""
    last_used_at: str = ""
    build_ms: int = 0
    deleted_at: str = ""

    @classmethod
    def from_row(cls, owner: str, row: Mapping[str, Any]) -> ImageRecord:
        names = {f.name for f in fields(cls)} - {"owner"}
        return cls(owner=owner, **{k: row[k] for k in names if k in row})


_IMAGE_COLUMNS = [f.name for f in fields(ImageRecord) if f.name != "owner"]


class EnvironmentStore:
    """sandbox_images 的读写（表在 SandboxLedger 的迁移 204 里）。"""

    def __init__(self, ledger: SandboxLedger) -> None:
        self.ledger = ledger

    async def get(self, owner: str, key: str) -> ImageRecord | None:
        await self.ledger.migrate()
        async with self.ledger.bind(owner).transaction(read_only=True) as tx:
            row = await tx.query_one(f"SELECT {', '.join(_IMAGE_COLUMNS)} FROM sandbox_images WHERE image_key = $1", (key,))
        return ImageRecord.from_row(owner, row) if row else None

    async def all(self, owner: str) -> list[ImageRecord]:
        await self.ledger.migrate()
        async with self.ledger.bind(owner).transaction(read_only=True) as tx:
            rows = await tx.query(f"SELECT {', '.join(_IMAGE_COLUMNS)} FROM sandbox_images ORDER BY created_at")
        return [ImageRecord.from_row(owner, r) for r in rows]

    async def begin_build(self, owner: str, definition: Definition, key: str, *, workspace_id: str, base_image: str, max_images: int) -> ImageRecord:
        """记一行 building；同一个事务里核对个数配额（building 与 ready 都算），超了 denied。已有这一行时原样返回。"""
        await self.ledger.migrate()
        now = iso(self.ledger.clock())
        record = ImageRecord(
            image_key=key, owner=owner, kind=definition.kind, path=definition.path, dockerfile=definition.dockerfile, state="building",
            created_at=now, workspace_id=workspace_id, context=definition.context, base_image=base_image,
        )
        async with self.ledger.bind(owner).transaction() as tx:
            await tx.execute(
                "INSERT INTO sandbox_quota_lock (id, version) VALUES ('images', 1) ON CONFLICT (id) DO UPDATE SET version = sandbox_quota_lock.version + 1"
            )
            existing = await tx.query_one(f"SELECT {', '.join(_IMAGE_COLUMNS)} FROM sandbox_images WHERE image_key = $1", (key,))
            if existing is not None and existing["state"] != "deleted":
                return ImageRecord.from_row(owner, existing)
            row = await tx.query_one("SELECT COUNT(*) AS n FROM sandbox_images WHERE state IN ('building', 'ready')")
            if max_images and int((row or {}).get("n") or 0) >= max_images:
                raise DeniedError("environment_quota_exceeded", f"项目环境的镜像最多 {max_images} 个，先清掉不用的")
            if existing is not None:
                await tx.execute("DELETE FROM sandbox_images WHERE image_key = $1", (key,))
            await tx.execute(
                f"INSERT INTO sandbox_images ({', '.join(_IMAGE_COLUMNS)}) VALUES ({', '.join(f'${i}' for i in range(1, len(_IMAGE_COLUMNS) + 1))})",
                [getattr(record, c) for c in _IMAGE_COLUMNS],
            )
        return record

    async def _set(self, owner: str, key: str, **values: Any) -> None:
        await self.ledger.migrate()
        sets = ", ".join(f"{k} = ${i}" for i, k in enumerate(values, start=1))
        async with self.ledger.bind(owner).transaction() as tx:
            await tx.execute(f"UPDATE sandbox_images SET {sets} WHERE image_key = ${len(values) + 1}", [*values.values(), key])

    async def mark_ready(self, owner: str, key: str, *, image: str, digest: str, size_bytes: int, build_ms: int) -> None:
        now = iso(self.ledger.clock())
        await self._set(owner, key, state="ready", image=image, digest=digest, size_bytes=int(size_bytes), build_ms=int(build_ms), ready_at=now,
                        last_used_at=now, reason="")

    async def mark_failed(self, owner: str, key: str, reason: str, *, build_ms: int = 0) -> None:
        await self._set(owner, key, state="failed", reason=reason[:500], build_ms=int(build_ms))

    async def touch(self, owner: str, key: str) -> None:
        await self._set(owner, key, last_used_at=iso(self.ledger.clock()))

    async def mark_deleted(self, owner: str, key: str) -> None:
        await self._set(owner, key, state="deleted", deleted_at=iso(self.ledger.clock()))


# --- 构建器与仓库的接口（env_builder、agentruntime_core.adapters.oci_registry 实现） ---


@dataclass(frozen=True)
class BuildJob:
    owner: str
    key: str
    dockerfile: str
    # 上下文：相对工作区根的目录（""：不用仓库里的文件）；workspace_id 是哪个工作区。
    context: str
    workspace_id: str
    timeout: timedelta = timedelta(minutes=30)
    max_bytes: int = 8 << 30
    # 构建里联网用的变量（出网代理与包镜像，带短期凭据）；不进 repr 与日志。
    env: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class BuildOutcome:
    ok: bool
    reason: str = ""
    # timed_out、too_large、failed：退回 universal 时告诉用户的分类。
    failure: str = ""


class EnvBuilder(Protocol):
    async def build(self, job: BuildJob, out: IO[bytes]) -> BuildOutcome:
        """构建；成功时把 OCI 打包写进 out（不超过 job.max_bytes，超了 too_large）。"""
        ...


class ImagePusher(Protocol):
    host: str

    def reference(self, repo: str, digest: str) -> str: ...

    async def push(self, repo: str, tag: str, layout: Any) -> str: ...

    async def delete(self, repo: str, digest: str) -> bool: ...


# 把构建交给持久执行（Temporal 的构建工作流）；测试里可以直接在进程里跑。
BuildStarter = Callable[[str, str], Awaitable[None]]


class BuildNetwork(Protocol):
    """构建联网用的环境记录与授权（broker.BuildNetwork 实现）：open 给出构建要的代理与镜像源变量，close 收回。"""

    async def open(self, owner: str, key: str, timeout: timedelta) -> dict[str, str]: ...

    async def close(self, owner: str, key: str) -> None: ...


@dataclass(frozen=True)
class Limits:
    max_images: int = 5
    max_image_bytes: int = 8 << 30
    build_timeout: timedelta = timedelta(minutes=30)
    idle_days: int = 14


@dataclass(frozen=True)
class Resolution:
    """shell_run 这次用哪个镜像：image 为空时用登记的运行环境（universal）。state 是 none（仓库没有定义）、ready、building、
    failed、unsupported、quota；note 写给用户看（退回 universal 的原因）。"""

    state: str
    image: str = ""
    kind: str = ""
    path: str = ""
    key: str = ""
    note: str = ""

    def as_output(self) -> dict[str, Any]:
        out: dict[str, Any] = {"state": self.state, "definition": self.path, "kind": self.kind}
        if self.note:
            out["note"] = self.note
        return out


class EnvironmentManager:
    def __init__(
        self,
        ledger: SandboxLedger,
        *,
        base_image: str,
        base_digest: str = "",
        registry: RegistryPath | None = None,
        builder: EnvBuilder | None = None,
        pusher: ImagePusher | None = None,
        starter: BuildStarter | None = None,
        limits: Limits = Limits(),
        network: BuildNetwork | None = None,
    ) -> None:
        self.ledger = ledger
        self.store = EnvironmentStore(ledger)
        self.base_image = base_image
        self.base_digest = base_digest or base_image
        self.registry = registry
        self.builder = builder
        self.pusher = pusher
        self.starter = starter
        self.limits = limits
        # 构建联网（经出网网关拉基础镜像、经包镜像装依赖）；没有时构建断网。
        self.network = network
        self._last_gc: dict[str, float] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    def _from(self) -> str:
        """生成的 Dockerfile 的 FROM：基础镜像按 digest 固定（去掉标签，带上 digest）。"""
        if not self.base_digest.startswith("sha256:") or "@" in self.base_image:
            return self.base_image
        name = self.base_image
        if name.rfind(":") > name.rfind("/"):
            name = name[: name.rfind(":")]
        return f"{name}@{self.base_digest}"

    async def resolve(self, tenant: TenantContext, read: FileReader) -> Resolution:
        owner = tenant.tenant_id
        definition = await detect(read, base_image=self._from())
        if definition is None or not owner or owner == PLATFORM_ID:
            return Resolution(state="none")
        await self._maybe_gc(owner)
        base = {"kind": definition.kind, "path": definition.path}
        fallback = "，这次先用 universal"
        if definition.problem:
            return Resolution(state="unsupported", note=definition.problem + fallback, **base)
        problems = check_dockerfile(definition.dockerfile, tenant_id=owner, registry=self.registry)
        if problems:
            return Resolution(state="unsupported", note="环境定义没通过检查：" + "；".join(problems) + fallback, **base)
        key = definition.key(self.base_digest)
        record = await self.store.get(owner, key)
        if record is not None and record.state == "ready" and record.image:
            await self.store.touch(owner, key)
            return Resolution(state="ready", image=record.image, key=key, **base)
        if record is not None and record.state == "building":
            return Resolution(state="building", key=key, note="项目环境还在构建" + fallback, **base)
        if record is not None and record.state == "failed":
            return Resolution(state="failed", key=key, note=f"项目环境构建失败：{record.reason}{fallback}；改了环境定义会重新构建", **base)
        if self.builder is None or self.pusher is None or self.starter is None:
            return Resolution(state="unsupported", note="这个部署没有配置环境构建器" + fallback, **base)
        try:
            await self.store.begin_build(owner, definition, key, workspace_id=tenant.workspace_id, base_image=self._from(), max_images=self.limits.max_images)
        except DeniedError as err:
            return Resolution(state="quota", key=key, note=err.message + fallback, **base)
        try:
            await self.starter(owner, key)
        except ProviderError as err:
            await self.store.mark_failed(owner, key, f"起不了构建（{err.code}）")
            return Resolution(state="failed", key=key, note=f"起不了构建（{err.code}）{fallback}", **base)
        return Resolution(state="building", key=key, note="第一次用到这个环境定义，正在构建" + fallback, **base)

    async def run_build(self, owner: str, key: str) -> ImageRecord:
        """构建一个镜像并推进仓库（构建工作流的 activity）；失败记原因。返回最终的记录。"""
        assert self.builder is not None and self.pusher is not None and self.registry is not None
        record = await self.store.get(owner, key)
        if record is None:
            raise NotFoundError("unknown_environment", "没有这个环境定义的构建记录")
        if record.state != "building":
            return record
        started = time.monotonic()
        # 产物先落到临时文件（几个 GB，不进内存），推完就删。
        with tempfile.TemporaryFile() as tmp:
            try:
                env = await self.network.open(owner, key, self.limits.build_timeout) if self.network is not None else {}
                job = BuildJob(
                    owner=owner, key=key, dockerfile=record.dockerfile, context=record.context, workspace_id=record.workspace_id,
                    timeout=self.limits.build_timeout, max_bytes=self.limits.max_image_bytes, env=env,
                )
                outcome = await self.builder.build(job, tmp)
            except ProviderError as err:
                outcome = BuildOutcome(ok=False, reason=f"构建器出错（{err.code}）", failure="failed")
            finally:
                if self.network is not None:
                    with contextlib.suppress(ProviderError):
                        await self.network.close(owner, key)
            elapsed = int((time.monotonic() - started) * 1000)
            if not outcome.ok:
                await self.store.mark_failed(owner, key, outcome.reason or "构建失败", build_ms=elapsed)
                return await self._reload(owner, key)
            try:
                image, digest, size = await self._import(tmp, owner, key)
            except InvalidError as err:
                reason = "镜像超过大小上限" if err.code == "image_too_large" else f"构建产物不对（{err.message}）"
                await self.store.mark_failed(owner, key, reason, build_ms=elapsed)
                return await self._reload(owner, key)
        await self.store.mark_ready(owner, key, image=image, digest=digest, size_bytes=size, build_ms=int((time.monotonic() - started) * 1000))
        return await self._reload(owner, key)

    async def _reload(self, owner: str, key: str) -> ImageRecord:
        record = await self.store.get(owner, key)
        assert record is not None
        return record

    async def _import(self, tmp: IO[bytes], owner: str, key: str) -> tuple[str, str, int]:
        """读构建器交来的打包，按大小上限核对，推到本租户的路径（路径由这里按租户拼，打包里写的名字一概不看）。"""
        from agentruntime_core.adapters.oci_registry import read_layout

        assert self.pusher is not None and self.registry is not None
        tmp.seek(0)
        layout = read_layout(tmp, max_bytes=self.limits.max_image_bytes)
        repo = self.registry.repo(owner)
        digest = await self.pusher.push(repo, key[:32], layout)
        return self.pusher.reference(repo, digest), digest, layout.size

    async def gc(self, owner: str) -> int:
        """清掉长期不用的镜像（删仓库里的清单、记为 deleted）；返回清了几个。"""
        if self.pusher is None or self.registry is None:
            return 0
        cutoff = iso(self.ledger.clock() - timedelta(days=self.limits.idle_days))
        n = 0
        for record in await self.store.all(owner):
            if record.state != "ready" or not record.last_used_at or record.last_used_at > cutoff:
                continue
            try:
                await self.pusher.delete(self.registry.repo(owner), record.digest)
            except NotFoundError:
                pass
            except ProviderError as err:
                log.warning("清不掉项目环境的镜像 %s（%s）", record.image_key[:12], err.code)
                continue
            await self.store.mark_deleted(owner, record.image_key)
            n += 1
        return n

    async def _maybe_gc(self, owner: str) -> None:
        """每个租户每小时最多清一次（随用随清，不另起定时任务）。"""
        now = time.monotonic()
        if now - self._last_gc.get(owner, -1e9) < 3600:
            return
        self._last_gc[owner] = now
        with contextlib.suppress(ProviderError):
            await self.gc(owner)


def same_image(a: str, b: str) -> bool:
    """两个镜像引用是不是同一个（都按 digest 写时比 digest）。"""
    if "@" in a and "@" in b:
        return a.rsplit("@", 1)[1] == b.rsplit("@", 1)[1]
    return a == b


def workspace_reader(root: Any) -> FileReader:
    """从本机的工作区目录读（单节点：Worker 挂着工作区卷）。路径只接受 DEFINITION_PATHS 与它们引用的相对路径。"""
    from pathlib import Path

    base = Path(root).resolve()

    async def read(path: str) -> bytes | None:
        rel = _within_repo(".", path)
        if rel is None or rel == ".":
            return None
        target = (base / rel).resolve()
        if target != base and not target.is_relative_to(base):
            return None
        if not target.is_file() or target.stat().st_size > MAX_DEFINITION_BYTES * 4:
            return None
        return target.read_bytes()

    return read


__all__ = [
    "BuildJob", "BuildOutcome", "BuildStarter", "DEFINITION_PATHS", "Definition", "EnvBuilder", "EnvironmentManager", "EnvironmentStore",
    "ImagePusher", "ImageRecord", "Limits", "RegistryPath", "Resolution", "check_dockerfile", "detect", "same_image", "strip_jsonc",
    "tool_versions_dockerfile", "workspace_reader",
]
