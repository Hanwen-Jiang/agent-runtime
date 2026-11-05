"""工作区选用的环境配置（ENV-02；人工验收 Q16–37；docs/plan/env-02-implementation-2026-11-03.md）。

应用（coding-agent）保存可复用、与仓库无关的环境配置；建工作区时把一份快照随 CreateWorkspace 带来（workspace.v1.WorkspaceEnvironment，
task-service 校验过，这里存之前再核一遍），存在 workspaces 的行上（迁移 207，sandbox 不碰别的租户）。里面只有普通值与密钥的名字。

- setup、start：Bash，SandboxBroker 在每个新的执行环境上先后跑（broker 的 _prepare）；各自最多 64 KiB。
- env：.env 文本，一行一个 KEY=value（# 开头与空行忽略，行首尾空白去掉，值原样）；最多 64 个、合计 16 KiB；名字同 exec env 的规则，
  不能盖掉平台注入的变量（代理、包镜像、CA 证书包、AGENT_*），名字像机密的拒绝（机密只走网络密钥，Q27）。
- network：open、restricted（hosts，每一项显式 include_subdomains）、deny；空表示旧客户端，不另加限制。执行点在出网网关。
- secret_headers：网络密钥（主机、是否含子域名、SecretStore 里的名字、header 名），值由出网网关读，沙箱拿不到。
- setup 的缓存键：sha256("agentruntime-setup-v1\\0" ‖ sha256(脚本) ‖ "\\0" ‖ 基础镜像 digest)（Q21）。

错误一律 invalid，只说哪一处不对，不带值。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agentruntime_core.errors import DeniedError, InvalidError

from agentruntime_worker.sandbox.grants import check_host

MAX_SCRIPT_BYTES = 64 * 1024
MAX_VARS = 64
MAX_ENV_BYTES = 16 * 1024
MAX_HOSTS = 50
MAX_SECRET_HEADERS = 20
MODES = {"": "", "NETWORK_MODE_UNSPECIFIED": "", "NETWORK_MODE_OPEN": "open", "NETWORK_MODE_RESTRICTED": "restricted", "NETWORK_MODE_DENY": "deny",
         "open": "open", "restricted": "restricted", "deny": "deny"}
# 沙箱信任出网网关 CA 的证书包（有网络密钥时由 broker 写进环境，ENV-02）。
CA_BUNDLE = "/tmp/agentruntime-ca.pem"
CA_ENV = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "GIT_SSL_CAINFO")
# 平台注入的变量（Sandbox Service 的 broker.Reserved，含 CA 证书包）：.env 不能盖掉；与 task-service 的 workspaceapi 一致。
RESERVED = frozenset({
    "PIP_INDEX_URL", "UV_DEFAULT_INDEX", "PIP_TRUSTED_HOST", "NPM_CONFIG_REGISTRY", "GOPROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "ALL_PROXY", "NPM_CONFIG_PROXY", "NPM_CONFIG_HTTPS_PROXY", "NPM_CONFIG_AUDIT", "NPM_CONFIG_FUND", "GONOSUMDB", "GOFLAGS", *CA_ENV,
})
_SECRET_WORDS = frozenset({"SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL", "CREDENTIALS", "APIKEY", "PRIVATEKEY"})
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEADER = re.compile(r"^[a-z0-9-]{1,64}$")
_FORBIDDEN_HEADERS = frozenset({
    "host", "connection", "content-length", "transfer-encoding", "te", "trailer", "upgrade", "keep-alive", "proxy-authorization",
    "proxy-authenticate", "proxy-connection", "cookie",
})
_FIELDS = frozenset({"environment_id", "setup_script", "start_command", "env", "network", "secret_headers"})


def secret_like(name: str) -> bool:
    parts = name.upper().split("_")
    return any(p in _SECRET_WORDS for p in parts) or any(a + b in ("APIKEY", "PRIVATEKEY") for a, b in zip(parts, parts[1:], strict=False))


def parse_dotenv(text: str) -> dict[str, str]:
    """.env 文本 → 变量；格式、个数、字节、保留名与像机密的名字都在这里拒。"""
    if len(text.encode("utf-8")) > MAX_ENV_BYTES:
        raise InvalidError("env_too_large", f".env 合计最多 {MAX_ENV_BYTES} 字节")
    out: dict[str, str] = {}
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, eq, value = line.partition("=")
        if not eq or not _NAME.match(name) or name in out or "\x00" in value:
            raise InvalidError("invalid_env_line", f".env 第 {number} 行要是 KEY=value（名字不重复）")
        upper = name.upper()
        if upper in RESERVED or upper.startswith("AGENT_"):
            raise InvalidError("env_reserved_name", f".env 第 {number} 行的名字是平台保留的")
        if secret_like(name):
            raise InvalidError("env_secret_name", f".env 第 {number} 行的名字像机密：机密请用网络密钥，不要写进 .env")
        out[name] = value
    if len(out) > MAX_VARS:
        raise InvalidError("env_too_large", f".env 最多 {MAX_VARS} 个变量")
    return out


def setup_key(script: str, base_digest: str) -> str:
    """setup 的缓存键（Q21）：脚本内容加基础镜像的 digest。"""
    return hashlib.sha256(b"agentruntime-setup-v1\x00" + hashlib.sha256(script.encode("utf-8")).digest() + b"\x00" + base_digest.encode()).hexdigest()


def _domain(value: Any, code: str, what: str) -> str:
    host = str(value or "")
    try:
        checked = check_host(host)
    except (InvalidError, DeniedError):
        raise InvalidError(code, f"{what}只能是公网域名（小写，不能是 IP、* 或内网名字）") from None
    if checked.startswith("*.") or checked != host:
        raise InvalidError(code, f"{what}只能是公网域名（子域名用 include_subdomains 表示）")
    return checked


@dataclass(frozen=True)
class NetworkHost:
    host: str
    include_subdomains: bool = False

    def covers(self, host: str) -> bool:
        return host == self.host or (self.include_subdomains and host.endswith("." + self.host))


@dataclass(frozen=True)
class SecretHeader:
    host: str
    secret_name: str
    header: str = "authorization"
    include_subdomains: bool = False


@dataclass(frozen=True)
class EnvironmentConfig:
    environment_id: str
    setup_script: str = ""
    start_command: str = ""
    env: str = ""
    network_mode: str = ""
    hosts: tuple[NetworkHost, ...] = ()
    secret_headers: tuple[SecretHeader, ...] = ()
    variables: dict[str, str] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_args(cls, doc: Mapping[str, Any]) -> EnvironmentConfig:
        """WorkspaceEnvironment 的 JSON（protojson 的字段名，或本模块 to_json 的写法）。未知字段拒绝（配置类文档，基线第 8 条）。"""
        if not isinstance(doc, Mapping) or set(doc) - _FIELDS:
            raise InvalidError("invalid_environment", "环境配置里有不认识的字段")
        env_id = str(doc.get("environment_id") or "")
        if not _ID.match(env_id):
            raise InvalidError("invalid_environment", "environment_id 只能是字母、数字、_ 与 -，最多 128 个字符")
        scripts = []
        for key in ("setup_script", "start_command"):
            value = doc.get(key) or ""
            if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_SCRIPT_BYTES or "\x00" in value:
                raise InvalidError("environment_script_too_large", f"安装脚本与启动命令各自最多 {MAX_SCRIPT_BYTES} 字节的文本")
            scripts.append(value)
        dotenv = doc.get("env") or ""
        if not isinstance(dotenv, str):
            raise InvalidError("invalid_environment", "env 要是 .env 文本")
        variables = parse_dotenv(dotenv)
        network = doc.get("network") or {}
        if not isinstance(network, Mapping) or set(network) - {"mode", "hosts"} or str(network.get("mode") or "") not in MODES:
            raise InvalidError("invalid_environment", "network 只有 mode（open、restricted、deny）与 hosts")
        raw_hosts = network.get("hosts") or []
        if not isinstance(raw_hosts, list) or len(raw_hosts) > MAX_HOSTS:
            raise InvalidError("invalid_network_host", f"联网名单最多 {MAX_HOSTS} 个主机")
        hosts = []
        for item in raw_hosts:
            if not isinstance(item, Mapping) or set(item) - {"host", "include_subdomains"}:
                raise InvalidError("invalid_network_host", "联网名单的每一项是 {host, include_subdomains}")
            hosts.append(NetworkHost(_domain(item.get("host"), "invalid_network_host", "联网名单里"), bool(item.get("include_subdomains"))))
        raw_headers = doc.get("secret_headers") or []
        if not isinstance(raw_headers, list) or len(raw_headers) > MAX_SECRET_HEADERS:
            raise InvalidError("invalid_secret_header", f"网络密钥最多 {MAX_SECRET_HEADERS} 个")
        headers: list[SecretHeader] = []
        for item in raw_headers:
            if not isinstance(item, Mapping) or set(item) - {"host", "include_subdomains", "secret_name", "header"}:
                raise InvalidError("invalid_environment", "网络密钥的每一项是 {host, include_subdomains, secret_name, header}（不支持查询参数）")
            name = str(item.get("secret_name") or "")
            header = str(item.get("header") or "authorization").lower()
            if not _SECRET_NAME.match(name) or ".." in name:
                raise InvalidError("invalid_secret_header", "网络密钥的名字只能是字母、数字、.、_ 与 -")
            if not _HEADER.match(header) or header in _FORBIDDEN_HEADERS or header.startswith("proxy-"):
                raise InvalidError("invalid_secret_header", "这个请求头不能用作网络密钥")
            entry = SecretHeader(_domain(item.get("host"), "invalid_secret_header", "网络密钥的主机"), name, header, bool(item.get("include_subdomains")))
            if any(h.host == entry.host and h.header == entry.header for h in headers):
                raise InvalidError("invalid_secret_header", "同一台主机的同一个请求头只能配一个网络密钥")
            headers.append(entry)
        return cls(
            environment_id=env_id, setup_script=scripts[0], start_command=scripts[1], env=dotenv, network_mode=MODES[str(network.get("mode") or "")],
            hosts=tuple(hosts), secret_headers=tuple(headers), variables=variables,
        )

    def to_json(self) -> str:
        """存进 workspaces.environment 的写法（出网网关也读它：network.mode 是小写的 open、restricted、deny）。"""
        doc = {
            "environment_id": self.environment_id, "setup_script": self.setup_script, "start_command": self.start_command, "env": self.env,
            "network": {"mode": self.network_mode, "hosts": [{"host": h.host, "include_subdomains": h.include_subdomains} for h in self.hosts]},
            "secret_headers": [
                {"host": h.host, "include_subdomains": h.include_subdomains, "secret_name": h.secret_name, "header": h.header} for h in self.secret_headers
            ],
        }
        return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def public_json(self) -> str:
        """非机密的部分（一直跟着会话）：environment_id、setup、start、network。"""
        doc = json.loads(self.to_json())
        return json.dumps({k: doc[k] for k in ("environment_id", "setup_script", "start_command", "network")}, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))

    def secrets_json(self) -> str:
        """机密的部分（配置删除时立即删掉，2026-11-03 用户定）：env 与 secret_headers；都没有时为空串。"""
        if not self.env and not self.secret_headers:
            return ""
        doc = json.loads(self.to_json())
        return json.dumps({k: doc[k] for k in ("env", "secret_headers")}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @property
    def has_secrets(self) -> bool:
        return bool(self.env.strip()) or bool(self.secret_headers)

    @classmethod
    def from_parts(cls, public: str, secrets: str) -> EnvironmentConfig:
        try:
            doc = dict(json.loads(public))
            if secrets:
                doc.update(dict(json.loads(secrets)))
        except (ValueError, TypeError):
            raise InvalidError("invalid_environment", "工作区存的环境配置格式不对") from None
        return cls.from_args(doc)

    @classmethod
    def from_json(cls, text: str) -> EnvironmentConfig:
        try:
            doc = json.loads(text)
        except ValueError:
            raise InvalidError("invalid_environment", "工作区存的环境配置格式不对") from None
        return cls.from_args(doc)


@dataclass(frozen=True)
class BoundEnvironment:
    """工作区上的选用配置：谁选的（建工作区的用户；网络密钥按他的名字读）、是否已删除（Q35）。"""

    config: EnvironmentConfig
    owner_user: str = ""
    deleted: bool = False


__all__ = [
    "CA_BUNDLE", "CA_ENV", "MAX_ENV_BYTES", "MAX_SCRIPT_BYTES", "MAX_VARS", "RESERVED", "BoundEnvironment", "EnvironmentConfig", "NetworkHost",
    "SecretHeader", "parse_dotenv", "secret_like", "setup_key",
]
