"""沙箱后端登记里 kind = e2b 的后端（e2b 分支，docs/plan/e2b-backend.md 第 3 节、E-02）：自托管的 E2B。

登记的写法（AR_SANDBOX_BACKENDS_FILE 里的一项）：

    "e2b": {"kind": "e2b", "isolation": "microvm",
            "api_url": "http://10.0.0.7:3000", "sandbox_url": "http://10.0.0.7:3002",
            "template": "agentruntime-python", "deployment": "prod",
            "pause_on_idle": true, "egress_proxy": "", "envs": {"PIP_INDEX_URL": "..."}}

- api_url、sandbox_url：E2B 的 api 与 client-proxy（Embed 的 3000、3002，只在内网，见 deploy/e2b）。routing 默认 header。
- template：runtime python 用的模板（scripts/e2b_template.py 构建）；也可以给 templates（runtime → 模板）。
- E2B 的 API key 取自 SecretStore 平台作用域的 sandbox/e2b-api-key；出网网关凭据的派生密钥与 K8s 后端共用 sandbox/token-key。
- isolation 只能是 microvm（每个环境一台 Firecracker VM）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Any

from agentruntime_core.errors import InvalidError
from agentruntime_core.providers.sandbox import SandboxProvider

API_KEY_SECRET = "sandbox/e2b-api-key"
TOKEN_KEY_SECRET = "sandbox/token-key"

SecretReader = Callable[[str], Awaitable[bytes | None]]


def check_spec(name: str, spec: Mapping[str, Any]) -> None:
    """登记文件读进来时就核对，配错了 Worker 起不来，而不是第一次用时才报。"""
    if spec.get("isolation") != "microvm":
        raise ValueError(f"沙箱后端 {name}（e2b）要写明 isolation: microvm")
    if not spec.get("api_url"):
        raise ValueError(f"沙箱后端 {name}（e2b）要给 api_url")
    if spec.get("routing", "header") == "header" and not spec.get("sandbox_url"):
        raise ValueError(f"沙箱后端 {name}（e2b）按头路由时要给 sandbox_url")
    templates = spec.get("templates")
    if templates is not None and not (isinstance(templates, dict) and all(isinstance(v, str) and v for v in templates.values())):
        raise ValueError(f"沙箱后端 {name}（e2b）的 templates 要是 runtime → 模板名")


async def build(name: str, spec: Mapping[str, Any], secret: SecretReader) -> SandboxProvider:
    from agentruntime_core.adapters.e2b_sandbox import E2BSandboxConfig, E2BSandboxProvider

    key = await secret(API_KEY_SECRET)
    if not key:
        raise InvalidError("missing_e2b_api_key", f"SecretStore 的平台作用域里没有 {API_KEY_SECRET}（E2B 后端 {name} 要用）")
    templates = dict(spec.get("templates") or {"python": str(spec.get("template") or "agentruntime-python")})
    fields: dict[str, Any] = {k: str(spec[k]) for k in ("sandbox_url", "routing", "deployment", "workdir", "user", "egress_proxy") if k in spec}
    if "pause_on_idle" in spec:
        fields["pause_on_idle"] = bool(spec["pause_on_idle"])
    if "envs" in spec:
        fields["envs"] = {str(k): str(v) for k, v in dict(spec["envs"]).items()}
    if "request_timeout_s" in spec:
        fields["request_timeout"] = timedelta(seconds=float(spec["request_timeout_s"]))
    config = E2BSandboxConfig(api_url=str(spec["api_url"]), templates=templates, **fields)
    return E2BSandboxProvider(config, key.decode(), token_key=await secret(TOKEN_KEY_SECRET))
