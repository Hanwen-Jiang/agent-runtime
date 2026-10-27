"""OCI 镜像的读与推（按项目定制的运行环境，docs/plan/cloud-deploy.md D-17）。只用标准库。

- read_layout：读构建器交来的 OCI 打包（BuildKit 的 --output type=oci，一个 tar：oci-layout、index.json、blobs/sha256/…）。
  只按名字读需要的条目，不解开到磁盘；index 里有多个平台时取 linux/amd64（没写平台的取第一个）。每个 blob 的大小按清单记的算，
  总和超过上限 invalid（image_too_large）；读出来的内容按 digest 核对，对不上 invalid。
- RegistryClient.push：按 OCI Distribution 的协议推到仓库的一个路径（先 HEAD，已有的 blob 不重传；没有的 POST 开上传、
  PUT 带 digest 一次传完），最后 PUT 清单，返回清单的 digest。仓库的凭据（Basic）只在这里用，不进日志与错误信息。
  路径由调用方给（Worker 按租户拼），构建产物里写的名字与注解一概不看：产物只能落到调用方给的路径。
- delete：按 digest 删清单（长期不用的镜像定期清掉；仓库要打开删除）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import IO, Any

from agentruntime_core.errors import InvalidError, NotFoundError, TransientError

INDEX_TYPES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")
MANIFEST_TYPES = ("application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPO = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$")
_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True)
class Blob:
    digest: str
    size: int
    media_type: str = ""


@dataclass
class OciLayout:
    """读好的 OCI 打包：清单的原文与要推的 blob（config 与各层）。blob 的内容按需从 tar 里读。"""

    manifest: bytes
    media_type: str
    blobs: list[Blob]
    _tar: tarfile.TarFile = field(repr=False)

    @property
    def size(self) -> int:
        return sum(b.size for b in self.blobs)

    @property
    def manifest_digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.manifest).hexdigest()

    def open_blob(self, digest: str) -> IO[bytes]:
        member = self._tar.extractfile(_blob_name(digest))
        if member is None:
            raise InvalidError("invalid_image", f"打包里没有 {digest}")
        return member


def _blob_name(digest: str) -> str:
    if not _DIGEST.match(digest):
        raise InvalidError("invalid_image", "digest 的格式不对")
    return "blobs/sha256/" + digest.split(":", 1)[1]


def _read(tar: tarfile.TarFile, name: str, limit: int = 4 << 20) -> bytes:
    try:
        member = tar.getmember(name)
    except KeyError:
        raise InvalidError("invalid_image", f"打包里没有 {name}") from None
    if not member.isfile() or member.size > limit:
        raise InvalidError("invalid_image", f"{name} 不是普通文件或太大")
    data = tar.extractfile(member).read()  # type: ignore[union-attr]
    return data


def _verified(tar: tarfile.TarFile, digest: str) -> bytes:
    data = _read(tar, _blob_name(digest))
    if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
        raise InvalidError("invalid_image", f"{digest} 的内容与 digest 对不上")
    return data


def read_layout(fileobj: IO[bytes], *, max_bytes: int) -> OciLayout:
    """读 OCI 打包；超过 max_bytes（各 blob 之和）invalid（image_too_large）。"""
    try:
        tar = tarfile.open(fileobj=fileobj, mode="r:")
    except tarfile.TarError:
        raise InvalidError("invalid_image", "不是 tar 格式的 OCI 打包") from None
    index = json.loads(_read(tar, "index.json"))
    entries = list(index.get("manifests") or [])
    if not entries:
        raise InvalidError("invalid_image", "index.json 里没有清单")
    entry = _pick(entries)
    media_type = str(entry.get("mediaType") or "")
    data = _verified(tar, str(entry.get("digest")))
    if media_type in INDEX_TYPES or json.loads(data).get("manifests"):
        nested = json.loads(data)
        entry = _pick(list(nested.get("manifests") or []))
        media_type = str(entry.get("mediaType") or "")
        data = _verified(tar, str(entry.get("digest")))
    manifest = json.loads(data)
    media_type = media_type or str(manifest.get("mediaType") or MANIFEST_TYPES[0])
    if media_type not in MANIFEST_TYPES:
        raise InvalidError("invalid_image", f"不认识的清单类型 {media_type}")
    blobs = [Blob(str(manifest["config"]["digest"]), int(manifest["config"]["size"]), str(manifest["config"].get("mediaType") or ""))]
    for layer in manifest.get("layers") or []:
        blobs.append(Blob(str(layer["digest"]), int(layer["size"]), str(layer.get("mediaType") or "")))
    for b in blobs:
        _blob_name(b.digest)
        try:
            member = tar.getmember(_blob_name(b.digest))
        except KeyError:
            raise InvalidError("invalid_image", f"打包里没有 {b.digest}") from None
        if not member.isfile() or member.size != b.size:
            raise InvalidError("invalid_image", f"{b.digest} 的大小与清单不符")
    layout = OciLayout(manifest=data, media_type=media_type, blobs=blobs, _tar=tar)
    if layout.size > max_bytes:
        raise InvalidError("image_too_large", f"镜像有 {layout.size} 字节，超过上限 {max_bytes}")
    return layout


def _pick(entries: list[dict[str, Any]]) -> dict[str, Any]:
    if not entries:
        raise InvalidError("invalid_image", "清单列表是空的")
    for e in entries:
        platform = e.get("platform") or {}
        if platform.get("os") == "linux" and platform.get("architecture") == "amd64":
            return e
    return entries[0]


class _HashingReader(io.RawIOBase):
    """边读边算 sha256，读完核对（推之前不必把整层读进内存）。"""

    def __init__(self, src: IO[bytes]) -> None:
        self.src = src
        self.hash = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        data = self.src.read(len(b))
        self.hash.update(data)
        b[: len(data)] = data
        return len(data)


class RegistryClient:
    """OCI Distribution 仓库的客户端（推、删）。base_url 形如 http://registry.agent-runtime.svc:5000。"""

    def __init__(self, base_url: str, *, username: str = "", password: str = "", timeout: float = 600, pull_host: str = "") -> None:
        self.base = base_url.rstrip("/")
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise InvalidError("invalid_registry", "仓库地址要是 http 或 https")
        # 镜像引用里写的主机（节点拉镜像用的名字，可以与推送用的集群内地址不同，例如单节点的 127.0.0.1:30500）。
        self.host = pull_host or parsed.netloc
        self._auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode() if username else ""
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def reference(self, repo: str, digest: str) -> str:
        return f"{self.host}/{repo}@{digest}"

    def _request(self, method: str, url: str, *, body: Any = None, headers: dict[str, str] | None = None, length: int | None = None) -> tuple[int, dict[str, str], bytes]:
        req = urllib.request.Request(url if url.startswith("http") else self.base + url, data=body, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if length is not None:
            req.add_header("Content-Length", str(length))
        if self._auth:
            req.add_header("Authorization", self._auth)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, {k.lower(): v for k, v in (err.headers or {}).items()}, err.read()
        except (urllib.error.URLError, OSError) as err:
            raise TransientError("registry_unavailable", f"连不上镜像仓库（{type(err).__name__}）") from None

    @staticmethod
    def _check(status: int, what: str) -> None:
        if status in (401, 403):
            raise InvalidError("registry_denied", f"镜像仓库拒绝了{what}（凭据或权限不对）")
        if status == 404:
            raise NotFoundError("registry_not_found", f"镜像仓库里没有{what}")
        if status >= 500 or status == 429:
            raise TransientError("registry_unavailable", f"镜像仓库暂时不可用（{what}，HTTP {status}）")
        if status >= 300:
            raise InvalidError("registry_rejected", f"镜像仓库不接受{what}（HTTP {status}）")

    def _push_sync(self, repo: str, tag: str, layout: OciLayout) -> str:
        if not _REPO.match(repo) or not _TAG.match(tag):
            raise InvalidError("invalid_registry_path", "仓库路径或标签的格式不对")
        for blob in layout.blobs:
            status, _, _ = self._request("HEAD", f"/v2/{repo}/blobs/{blob.digest}")
            if status == 200:
                continue
            status, headers, _ = self._request("POST", f"/v2/{repo}/blobs/uploads/", body=b"", length=0)
            self._check(status, "开始上传")
            location = headers.get("location", "")
            if not location:
                raise InvalidError("registry_rejected", "镜像仓库没给上传地址")
            if not location.startswith("http"):
                location = self.base + location
            sep = "&" if "?" in location else "?"
            reader = _HashingReader(layout.open_blob(blob.digest))
            status, _, _ = self._request(
                "PUT", f"{location}{sep}digest={urllib.parse.quote(blob.digest)}", body=io.BufferedReader(reader), length=blob.size,
                headers={"Content-Type": "application/octet-stream"},
            )
            if "sha256:" + reader.hash.hexdigest() != blob.digest:
                raise InvalidError("invalid_image", f"{blob.digest} 的内容与 digest 对不上")
            self._check(status, "上传的层")
        status, headers, _ = self._request(
            "PUT", f"/v2/{repo}/manifests/{tag}", body=layout.manifest, length=len(layout.manifest), headers={"Content-Type": layout.media_type}
        )
        self._check(status, "清单")
        digest = headers.get("docker-content-digest") or layout.manifest_digest
        if digest != layout.manifest_digest:
            raise InvalidError("registry_rejected", "镜像仓库记下的清单 digest 与上传的不同")
        return digest

    async def push(self, repo: str, tag: str, layout: OciLayout) -> str:
        """推一个镜像，返回清单的 digest。"""
        return await asyncio.to_thread(self._push_sync, repo, tag, layout)

    async def delete(self, repo: str, digest: str) -> bool:
        """删一个清单；本来就没有时返回 False。"""
        if not _REPO.match(repo) or not _DIGEST.match(digest):
            raise InvalidError("invalid_registry_path", "仓库路径或 digest 的格式不对")
        status, _, _ = await asyncio.to_thread(self._request, "DELETE", f"/v2/{repo}/manifests/{digest}")
        if status == 404:
            return False
        self._check(status, "删除")
        return True


__all__ = ["Blob", "INDEX_TYPES", "MANIFEST_TYPES", "OciLayout", "RegistryClient", "read_layout"]
