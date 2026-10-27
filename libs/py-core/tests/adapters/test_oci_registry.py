"""OCI 打包的读与推（agentruntime_core.adapters.oci_registry；D-17 的构建产物进仓库）。"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator

import pytest

from agentruntime_core.adapters.oci_registry import RegistryClient, read_layout
from agentruntime_core.adapters.ocitest import FakeRegistry, oci_tar
from agentruntime_core.errors import InvalidError


@pytest.fixture
def registry() -> Iterator[FakeRegistry]:
    r = FakeRegistry()
    yield r
    r.close()


def test_read_layout_finds_the_amd64_manifest_and_its_blobs() -> None:
    for nested in (False, True):
        layout = read_layout(io.BytesIO(oci_tar(nested_index=nested)), max_bytes=1 << 20)
        manifest = json.loads(layout.manifest)
        assert [b.digest for b in layout.blobs] == [manifest["config"]["digest"], *[l["digest"] for l in manifest["layers"]]]
        assert layout.size == sum(b.size for b in layout.blobs) and layout.manifest_digest.startswith("sha256:")


async def test_read_layout_refuses_oversize_tampered_and_non_tar(registry: FakeRegistry) -> None:
    with pytest.raises(InvalidError) as info:
        read_layout(io.BytesIO(oci_tar([b"x" * 4096])), max_bytes=1024)
    assert info.value.code == "image_too_large"
    layout = read_layout(io.BytesIO(oci_tar(tamper=True)), max_bytes=1 << 20)
    with pytest.raises(InvalidError) as info:
        await RegistryClient(registry.url, username="pusher", password="s3cret").push("envs/t1/env", "k", layout)  # 推的时候边读边核对
    assert info.value.code == "invalid_image" and not registry.manifests
    with pytest.raises(InvalidError):
        read_layout(io.BytesIO(b"not a tar"), max_bytes=1 << 20)


async def test_push_puts_blobs_and_manifest_under_the_given_path_only(registry: FakeRegistry) -> None:
    client = RegistryClient(registry.url, username="pusher", password="s3cret", pull_host="127.0.0.1:30500")
    layout = read_layout(io.BytesIO(oci_tar()), max_bytes=1 << 20)
    digest = await client.push("envs/t1/env", "abc123", layout)
    assert digest == layout.manifest_digest
    assert {repo for repo, _ in registry.blobs} == {"envs/t1/env"} and len(registry.blobs) == 3
    assert registry.manifests[("envs/t1/env", "abc123")] == layout.manifest
    assert client.reference("envs/t1/env", digest) == f"127.0.0.1:30500/envs/t1/env@{digest}"
    # 再推一次：已有的 blob 不重传。
    before = dict(registry.blobs)
    assert await client.push("envs/t1/env", "abc123", layout) == digest and registry.blobs == before
    assert await client.delete("envs/t1/env", digest) is True and await client.delete("envs/t1/env", digest) is False


async def test_bad_credentials_and_bad_paths_are_refused(registry: FakeRegistry) -> None:
    layout = read_layout(io.BytesIO(oci_tar()), max_bytes=1 << 20)
    with pytest.raises(InvalidError) as info:
        await RegistryClient(registry.url, username="pusher", password="wrong").push("envs/t1/env", "k", layout)
    assert info.value.code == "registry_denied" and "wrong" not in str(info.value)
    client = RegistryClient(registry.url, username="pusher", password="s3cret")
    for repo, tag in (("../t2/env", "k"), ("envs/T1/env", "k"), ("envs/t1/env", "a/b")):
        with pytest.raises(InvalidError):
            await client.push(repo, tag, layout)
