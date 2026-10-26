"""一个很小的 Kubernetes API 客户端（只用标准库）：K8s 的 Sandbox 适配器建、查、删沙箱 Pod 用（计划 docs/plan/cloud-deploy.md D-05）。
不引入 kubernetes 的 Python 客户端（设计书 4.15 之外的依赖）；只做 REST 的 JSON 请求与 pods/proxy 的地址。

连接方式：
- 集群内：ServiceAccount 的令牌与 CA（/var/run/secrets/kubernetes.io/serviceaccount），地址取 KUBERNETES_SERVICE_HOST、_PORT；
  令牌每次请求重新读（kubelet 会轮换它）。
- 集群外（开发、测试）：JSON 格式的 kubeconfig（`kubectl config view --raw --minify -o json`），支持 CA、客户端证书与令牌。
  客户端证书要落成文件才能装进 ssl，写在只有本进程用的临时目录里，close() 时删掉。

错误：404 是 not_found；409 是 conflict；400、422 是 invalid；401、403 是 denied；连不上、429、5xx 是 transient。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentruntime_core.errors import ConflictError, DeniedError, InvalidError, NotFoundError, ProviderError, TransientError

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class KubeConfigError(Exception):
    pass


class KubeAPI:
    def __init__(self, server: str, ssl_context: ssl.SSLContext, *, token: str = "", token_file: Path | None = None, temp_dir: Path | None = None) -> None:
        self.server = server.rstrip("/")
        self.ssl_context = ssl_context
        self._token = token
        self._token_file = token_file
        self._temp_dir = temp_dir

    @classmethod
    def load(cls, kubeconfig: str | os.PathLike[str] | None = None) -> KubeAPI:
        """给了 kubeconfig 就用它；否则在集群内用 ServiceAccount。"""
        if kubeconfig:
            return cls.from_kubeconfig(kubeconfig)
        return cls.in_cluster()

    @classmethod
    def in_cluster(cls) -> KubeAPI:
        host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if not host or not (SERVICE_ACCOUNT / "token").exists():
            raise KubeConfigError("不在集群里（没有 KUBERNETES_SERVICE_HOST 或 ServiceAccount 令牌），要给 kubeconfig")
        context = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / "ca.crt"))
        if ":" in host:
            host = f"[{host}]"
        return cls(f"https://{host}:{port}", context, token_file=SERVICE_ACCOUNT / "token")

    @classmethod
    def from_kubeconfig(cls, path: str | os.PathLike[str]) -> KubeAPI:
        try:
            config = json.loads(Path(path).read_text("utf-8"))
        except (OSError, ValueError) as err:
            raise KubeConfigError(f"读不了 kubeconfig（要 JSON 格式：kubectl config view --raw --minify -o json）：{err}") from None
        current = config.get("current-context")
        context = _named(config.get("contexts"), current, "context")
        cluster = _named(config.get("clusters"), context.get("cluster"), "cluster")
        user = _named(config.get("users"), context.get("user"), "user")
        server = cluster.get("server") or ""
        if not server.startswith("https://"):
            raise KubeConfigError("kubeconfig 的 server 要是 https 地址")
        temp_dir = Path(tempfile.mkdtemp(prefix="ar-kube-"))
        try:
            ssl_context = ssl.create_default_context()
            if cluster.get("certificate-authority-data"):
                ssl_context.load_verify_locations(cadata=base64.b64decode(cluster["certificate-authority-data"]).decode())
            elif cluster.get("certificate-authority"):
                ssl_context.load_verify_locations(cafile=cluster["certificate-authority"])
            cert, key = user.get("client-certificate-data"), user.get("client-key-data")
            if cert and key:
                cert_file, key_file = temp_dir / "client.crt", temp_dir / "client.key"
                cert_file.write_bytes(base64.b64decode(cert))
                key_file.write_bytes(base64.b64decode(key))
                os.chmod(key_file, 0o600)
                ssl_context.load_cert_chain(str(cert_file), str(key_file))
            elif user.get("client-certificate") and user.get("client-key"):
                ssl_context.load_cert_chain(user["client-certificate"], user["client-key"])
        except (OSError, ValueError, ssl.SSLError) as err:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise KubeConfigError(f"kubeconfig 里的证书装不上：{err}") from None
        return cls(server, ssl_context, token=user.get("token") or "", temp_dir=temp_dir)

    def auth_headers(self) -> dict[str, str]:
        token = self._token
        if self._token_file is not None:
            token = self._token_file.read_text("utf-8").strip()
        return {"Authorization": f"Bearer {token}"} if token else {}

    def pod_proxy_url(self, namespace: str, pod: str, port: int) -> str:
        """经 API Server 转发到 Pod 端口的地址（pods/proxy）；后面接路径。"""
        return f"{self.server}/api/v1/namespaces/{namespace}/pods/{pod}:{port}/proxy"

    async def request(
        self, method: str, path: str, body: Any = None, *, content_type: str = "application/json", timeout: float = 30
    ) -> tuple[int, bytes]:
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        request = urllib.request.Request(self.server + path, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", content_type)
        for k, v in self.auth_headers().items():
            request.add_header(k, v)

        def send() -> tuple[int, bytes]:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=self.ssl_context))
            try:
                with opener.open(request, timeout=timeout) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as err:
                return err.code, err.read()
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
                raise TransientError("kube_unavailable", f"连不上 Kubernetes API（{type(err).__name__}）") from None

        return await asyncio.to_thread(send)

    async def json(self, method: str, path: str, body: Any = None, *, content_type: str = "application/json") -> dict[str, Any]:
        status, data = await self.request(method, path, body, content_type=content_type)
        if not 200 <= status < 300:
            raise error_for(status, data)
        return json.loads(data) if data else {}

    def close(self) -> None:
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None


def error_for(status: int, body: bytes) -> ProviderError:
    try:
        message = str(json.loads(body).get("message") or "")
    except (ValueError, AttributeError):
        message = ""
    message = message[:300]
    if status == 404:
        return NotFoundError("kube_not_found", message or "不存在")
    if status == 409:
        return ConflictError("kube_conflict", message or "冲突")
    if status in (400, 422):
        return InvalidError("kube_invalid", message or "Kubernetes 拒绝了这个请求")
    if status in (401, 403):
        return DeniedError("kube_denied", message or "没有权限")
    return TransientError("kube_unavailable", message or f"Kubernetes API 暂时不可用（HTTP {status}）")


def _named(items: Any, name: Any, field: str) -> Mapping[str, Any]:
    for item in items or []:
        if isinstance(item, dict) and item.get("name") == name and isinstance(item.get(field), dict):
            return item[field]
    raise KubeConfigError(f"kubeconfig 里找不到 {field} {name!r}")


def label_selector(labels: Mapping[str, str]) -> str:
    return urllib.parse.quote(",".join(f"{k}={v}" for k, v in labels.items()), safe="")
