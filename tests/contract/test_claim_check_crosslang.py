"""S-06 验收“跨语言往返测试”：Go 与 Python 的 Claim-Check 编解码互相兼容（计划 S-06 难点“跨语言一致”）。

两种语言指向同一个 ObjectStore 目录与同一份密钥文件（开发实现，布局与格式两边相同）：
- Go 编码 → Python 解码；Python 编码 → Go 解码，还原出逐字节相同的载荷。
- 同一载荷两边编码得到同一个引用（确定性序列化 + 内容寻址），对象只有一个。
- 租户 A 的引用，两边都不能用租户 B 的上下文解码。
- 阈值以下的载荷两边都原样保留。
格式本身的测试向量（tests/contract/fixtures/claimcheck/vector.json）由两边各自的单元测试核对。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from google.protobuf import json_format
from temporalio.api.common.v1 import Payload

from agentruntime_core.adapters import claim_check as cc
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
from agentruntime_core.errors import DeniedError
from agentruntime_core.tenancy import TenantContext

from contract_tests.tools import run

T1 = TenantContext(tenant_id="t1")
T2 = TenantContext(tenant_id="t2")


def big_payload(fill: str = "字") -> Payload:
    return Payload(metadata={"encoding": b"json/plain", "note": "跨语言".encode()}, data=('"' + fill * 40_000 + '"').encode())


def small_payload() -> Payload:
    return Payload(metadata={"encoding": b"json/plain"}, data=b'"small"')


class Shared:
    """两种语言共用的 ObjectStore 目录与密钥文件。Python 每次现读密钥文件，Go 写入的密钥加密密钥也看得见。"""

    def __init__(self, root: Path, go: Path) -> None:
        self.objects = root / "objects"
        self.secrets = root / "secrets.json"
        self.root = root
        self.go = go

    def python(self, threshold: int = cc.DEFAULT_THRESHOLD) -> cc.ClaimCheck:
        return cc.ClaimCheck(
            LocalDiskObjectStoreProvider(self.objects, signing_key=b"crosslang"),
            LocalFileSecretStoreProvider(self.secrets),
            threshold=threshold,
        )

    def go_run(self, op: str, tenant: str, payload: Payload, *, check: bool = True) -> tuple[Payload | None, str]:
        src = self.root / f"in-{op}.json"
        dst = self.root / f"out-{op}.json"
        src.write_text(json_format.MessageToJson(payload), "utf-8")
        dst.unlink(missing_ok=True)
        proc = run(
            [self.go, op, "--objects", self.objects, "--secrets", self.secrets, "--tenant", tenant, "--in", src, "--out", dst],
            check=check,
        )
        if proc.returncode != 0:
            return None, proc.stderr
        return json_format.Parse(dst.read_text("utf-8"), Payload()), proc.stderr


@pytest.fixture
def shared(tmp_path: Path, claimcheck_go: Path) -> Shared:
    return Shared(tmp_path, claimcheck_go)


def test_go_encode_python_decode(shared: Shared) -> None:
    original = big_payload()
    ref, _ = shared.go_run("encode", "t1", original)
    assert ref is not None and cc.is_reference(ref)
    [restored] = asyncio.run(shared.python().bind(T1).decode([ref]))
    assert cc.serialize(restored) == cc.serialize(original)


def test_python_encode_go_decode(shared: Shared) -> None:
    original = big_payload("文")
    [ref] = asyncio.run(shared.python().bind(T1).encode([original]))
    assert cc.is_reference(ref)
    restored, _ = shared.go_run("decode", "t1", ref)
    assert restored is not None and cc.serialize(restored) == cc.serialize(original)


def test_both_sides_produce_the_same_reference_for_the_same_payload(shared: Shared) -> None:
    original = big_payload("同")
    go_ref, _ = shared.go_run("encode", "t1", original)
    [py_ref] = asyncio.run(shared.python().bind(T1).encode([original]))
    assert go_ref == py_ref, "确定性序列化 + 内容寻址：两边得到同一个引用"
    page = asyncio.run(LocalDiskObjectStoreProvider(shared.objects, signing_key=b"crosslang").bind(T1).list(prefix=cc.KEY_PREFIX))
    assert len(page.items) == 1


def test_neither_side_decodes_another_tenants_reference(shared: Shared) -> None:
    original = big_payload("租")
    go_ref, _ = shared.go_run("encode", "t1", original)
    assert go_ref is not None
    with pytest.raises(DeniedError):
        asyncio.run(shared.python().bind(T2).decode([go_ref]))
    [py_ref] = asyncio.run(shared.python().bind(T1).encode([big_payload("户")]))
    restored, stderr = shared.go_run("decode", "t2", py_ref, check=False)
    assert restored is None and "denied claim_check_cross_tenant" in stderr


def test_small_payloads_pass_through_on_both_sides(shared: Shared) -> None:
    payload = small_payload()
    go_out, _ = shared.go_run("encode", "t1", payload)
    [py_out] = asyncio.run(shared.python().bind(T1).encode([payload]))
    assert go_out == payload and py_out == payload
