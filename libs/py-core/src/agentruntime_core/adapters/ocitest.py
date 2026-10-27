"""测试用的 OCI 打包与假镜像仓库（agentruntime_core.adapters.oci_registry 的测试与 Worker 的 D-17 测试共用）。"""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def oci_tar(layers: list[bytes] | None = None, *, nested_index: bool = False, tamper: bool = False) -> bytes:
    """一个最小的 OCI 打包（BuildKit --output type=oci 的结构）。"""
    layers = layers if layers is not None else [b"layer-one", b"layer-two" * 10]
    config = json.dumps({"architecture": "amd64", "os": "linux", "rootfs": {"type": "layers", "diff_ids": []}}).encode()
    manifest = json.dumps({
        "schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": _digest(config), "size": len(config)},
        "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "digest": _digest(layer), "size": len(layer)} for layer in layers],
    }).encode()
    blobs = {_digest(b): b for b in [config, manifest, *layers]}
    entry = {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": _digest(manifest), "size": len(manifest),
             "platform": {"os": "linux", "architecture": "amd64"}}
    index_doc: dict[str, Any] = {"schemaVersion": 2, "manifests": [entry]}
    if nested_index:
        inner = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [entry]}).encode()
        blobs[_digest(inner)] = inner
        index_doc = {"schemaVersion": 2, "manifests": [{"mediaType": "application/vnd.oci.image.index.v1+json", "digest": _digest(inner), "size": len(inner)}]}
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        add("oci-layout", b'{"imageLayoutVersion": "1.0.0"}')
        add("index.json", json.dumps(index_doc).encode())
        for digest, data in blobs.items():
            if tamper and data == layers[0]:
                data = b"x" * len(data)
            add("blobs/sha256/" + digest.split(":", 1)[1], data)
    return out.getvalue()


class FakeRegistry:
    """进程里的 OCI Distribution 仓库（只做推、查、删用到的几个接口），要 Basic 认证。"""

    def __init__(self, username: str = "pusher", password: str = "s3cret") -> None:
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.manifests: dict[tuple[str, str], bytes] = {}
        self.deleted: list[tuple[str, str]] = []
        self.auth = "Basic " + __import__("base64").b64encode(f"{username}:{password}".encode()).decode()
        self._uploads: dict[str, str] = {}
        registry = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return None

            def _deny(self) -> bool:
                if self.headers.get("Authorization") != registry.auth:
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return True
                return False

            def _reply(self, code: int, headers: dict[str, str] | None = None, body: bytes = b"") -> None:
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body and self.command != "HEAD":
                    self.wfile.write(body)

            def do_HEAD(self) -> None:  # noqa: N802
                if self._deny():
                    return
                m = re.fullmatch(r"/v2/(.+)/blobs/(sha256:[0-9a-f]{64})", self.path)
                self._reply(200 if m and (m.group(1), m.group(2)) in registry.blobs else 404)

            def do_POST(self) -> None:  # noqa: N802
                if self._deny():
                    return
                m = re.fullmatch(r"/v2/(.+)/blobs/uploads/", self.path)
                if not m:
                    self._reply(404)
                    return
                upload = f"u{len(registry._uploads)}"
                registry._uploads[upload] = m.group(1)
                self._reply(202, {"Location": f"/v2/{m.group(1)}/blobs/uploads/{upload}"})

            def do_PUT(self) -> None:  # noqa: N802
                if self._deny():
                    return
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                m = re.fullmatch(r"/v2/(.+)/blobs/uploads/(u\d+)\?digest=(.+)", self.path)
                if m:
                    digest = m.group(3).replace("%3A", ":")
                    if _digest(body) != digest:
                        self._reply(400)
                        return
                    registry.blobs[(m.group(1), digest)] = body
                    self._reply(201)
                    return
                m = re.fullmatch(r"/v2/(.+)/manifests/([^/]+)", self.path)
                if m:
                    registry.manifests[(m.group(1), m.group(2))] = body
                    registry.manifests[(m.group(1), _digest(body))] = body
                    self._reply(201, {"Docker-Content-Digest": _digest(body)})
                    return
                self._reply(404)

            def do_DELETE(self) -> None:  # noqa: N802
                if self._deny():
                    return
                m = re.fullmatch(r"/v2/(.+)/manifests/(sha256:[0-9a-f]{64})", self.path)
                if m and (m.group(1), m.group(2)) in registry.manifests:
                    registry.deleted.append((m.group(1), m.group(2)))
                    del registry.manifests[(m.group(1), m.group(2))]
                    self._reply(202)
                else:
                    self._reply(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


__all__ = ["FakeRegistry", "oci_tar"]
