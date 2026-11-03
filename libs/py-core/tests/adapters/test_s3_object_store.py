"""D-09：ObjectStore 的 S3 兼容实现（Cloudflare R2、AWS S3、MinIO；docs/plan/cloud-deploy.md）。

- 与开发实现同一套一致性测试，对着 adapters.s3test 的假服务跑（它单独核对每个请求的 SigV4 签名与预签名地址的过期）。
- SigV4 用 AWS 文档里的例子核对（签名密钥的派生、IAM 的 GET、S3 的 GET/PUT/生命周期/列表、预签名地址）。
- S3 特有的：前缀隔开几个环境；生命周期按整天、只改本作用域的规则（t-a 不碰 t-a-b）；经预签名地址上传的对象没有 sha256 元数据；
  密钥不出现在错误里；签名不对 denied。
- 设 AR_LIVE_S3=1（另要 AR_S3_ENDPOINT、AR_S3_BUCKET、AR_S3_ACCESS_KEY_ID、AR_S3_SECRET_ACCESS_KEY，可选 AR_S3_REGION）
  时对真实的 R2/S3/MinIO 再跑一遍一致性测试，每个测试用一个随机前缀，测完删掉。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

from agentruntime_core.adapters import s3test, sigv4
from agentruntime_core.adapters.s3_object_store import S3ObjectStoreProvider, S3Settings, rule_id_prefix
from agentruntime_core.conformance.common import FaultInjectionUnsupported, expect_error, tenant, tenant_a
from agentruntime_core.conformance.object_store import ObjectStoreConformance
from agentruntime_core.errors import ConflictError, DeniedError, ErrorCategory, InvalidError
from agentruntime_core.providers.object_store import LifecycleRule
from agentruntime_core.testing import FakeClock, FaultInjector

BUCKET = "ar-objects"
ACCESS = "AKIDCONFORMANCE"
SECRET = "conformance-secret-key-do-not-log"


async def _fetch(url: str, data: bytes | None = None, headers: Mapping[str, str] | None = None) -> bytes:
    status, body = await asyncio.to_thread(s3test.fetch, url, data, headers)
    if status in (401, 403):
        raise DeniedError("expired_url", "临时地址无效或已过期")
    if status == 412:
        raise ConflictError("object_exists", "对象已存在")
    if status not in (200, 204):
        raise InvalidError("object_store_invalid", f"HTTP {status}")
    return body


class FakeHarness:
    def __init__(self, *, prefix: str = "") -> None:
        self.clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        self.faults = FaultInjector()
        self.fake = s3test.FakeS3(bucket=BUCKET, access_key=ACCESS, secret=SECRET, now=self.clock.now)
        self.provider = self.make(prefix=prefix)

    def make(self, *, prefix: str = "", secret: str = SECRET) -> S3ObjectStoreProvider:
        return S3ObjectStoreProvider(
            endpoint=self.fake.endpoint, bucket=BUCKET, access_key_id=ACCESS, secret_access_key=secret,
            prefix=prefix, clock=self.clock, faults=self.faults,
        )

    async def advance(self, delta: timedelta) -> None:
        self.clock.advance(delta)
        await asyncio.sleep(0)

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)

    async def fetch(self, url: str) -> bytes:
        return await _fetch(url)

    async def upload(self, url: str, data: bytes) -> None:
        await _fetch(url, data)

    async def upload_signed(self, url: str, data: bytes, headers: Mapping[str, str]) -> None:
        await _fetch(url, data, headers)

    async def run_lifecycle(self) -> None:
        # S3 的生命周期按整天、由存储服务每天跑，不能立即触发；按天清理的行为由下面的 test_lifecycle_* 用假服务的时钟测。
        raise FaultInjectionUnsupported


class TestS3ObjectStore(ObjectStoreConformance):
    @pytest.fixture
    def harness(self):
        h = FakeHarness()
        yield h
        h.fake.stop()


class TestS3ObjectStoreWithPrefix(ObjectStoreConformance):
    """几个环境共用一个桶：带前缀时语义不变。"""

    @pytest.fixture
    def harness(self):
        h = FakeHarness(prefix="staging/")
        yield h
        h.fake.stop()


@pytest.fixture
def fake():
    h = FakeHarness()
    yield h
    h.fake.stop()


# --- SigV4：AWS 文档里的例子 ---

DOC_SECRET_IAM = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
DOC_SECRET_S3 = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
S3_DOC_TIME = datetime(2013, 5, 24, tzinfo=UTC)


def _signature(headers: dict[str, str]) -> str:
    return headers["Authorization"].rsplit("Signature=", 1)[1]


def test_signing_key_matches_the_aws_example() -> None:
    key = sigv4.signing_key(DOC_SECRET_IAM, "20150830", "us-east-1", "iam")
    assert key.hex() == "c4afb1cc5771d871763a393e44b703571b55cc28424d1a5e86da6ed3c154a4b9"


def test_iam_get_matches_the_aws_example() -> None:
    signed = sigv4.sign(
        "GET", "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08",
        {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        payload_hash=sigv4.EMPTY_SHA256, access_key="AKIDEXAMPLE", secret=DOC_SECRET_IAM, region="us-east-1", service="iam",
        now=datetime(2015, 8, 30, 12, 36, tzinfo=UTC),
    )
    assert signed["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-date, Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
    )


S3_DOC_CASES = [
    ("get_object", "GET", "https://examplebucket.s3.amazonaws.com/test.txt", {"Range": "bytes=0-9"}, b"",
     "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"),
    ("put_object", "PUT", "https://examplebucket.s3.amazonaws.com/" + sigv4.quote("test$file.text", path=True),
     {"Date": "Fri, 24 May 2013 00:00:00 GMT", "x-amz-storage-class": "REDUCED_REDUNDANCY"}, b"Welcome to Amazon S3.",
     "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"),
    ("get_lifecycle", "GET", "https://examplebucket.s3.amazonaws.com/?lifecycle", {}, b"",
     "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543"),
    ("list_objects", "GET", "https://examplebucket.s3.amazonaws.com/?max-keys=2&prefix=J", {}, b"",
     "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"),
]


@pytest.mark.parametrize(("method", "url", "headers", "body", "want"), [c[1:] for c in S3_DOC_CASES], ids=[c[0] for c in S3_DOC_CASES])
def test_s3_requests_match_the_aws_examples(method: str, url: str, headers: dict[str, str], body: bytes, want: str) -> None:
    signed = sigv4.sign(
        method, url, headers, payload_hash=hashlib.sha256(body).hexdigest(), access_key="AKIAIOSFODNN7EXAMPLE",
        secret=DOC_SECRET_S3, region="us-east-1", now=S3_DOC_TIME,
    )
    assert _signature(signed) == want


def test_presigned_url_matches_the_aws_example() -> None:
    url = sigv4.presign(
        "GET", "https://examplebucket.s3.amazonaws.com/test.txt", expires=86400, access_key="AKIAIOSFODNN7EXAMPLE",
        secret=DOC_SECRET_S3, region="us-east-1", now=S3_DOC_TIME,
    )
    assert url.endswith("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")
    assert "X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request" in url


# --- S3 特有的规则 ---


async def test_wrong_secret_is_denied_and_never_echoed(fake: FakeHarness) -> None:
    bad = fake.make(secret="a-different-secret-value")
    with expect_error(ErrorCategory.DENIED) as info:
        await bad.bind(tenant_a()).put("k", b"x")
    assert "a-different-secret-value" not in str(info.value) and SECRET not in str(info.value)
    assert "a-different-secret-value" not in repr(bad.__dict__)


async def test_keys_are_laid_out_under_scope_and_prefix(fake: FakeHarness) -> None:
    await fake.provider.bind(tenant_a()).put("reports/华东 q3.csv", b"x")
    await fake.make(prefix="staging/").bind_platform("evals").put("set.json", b"y")
    assert sorted(fake.fake.objects) == ["staging/_platform/set.json", "t-alpha/reports/华东 q3.csv"]
    stored = fake.fake.objects["t-alpha/reports/华东 q3.csv"]
    assert stored.headers["x-amz-meta-ar-sha256"] == hashlib.sha256(b"x").hexdigest()


async def test_prefixes_keep_environments_apart(fake: FakeHarness) -> None:
    prod, staging = fake.make(prefix="prod/").bind(tenant_a()), fake.make(prefix="staging/").bind(tenant_a())
    await prod.put("k", b"prod")
    assert (await staging.list()).items == ()
    with expect_error(ErrorCategory.NOT_FOUND):
        await staging.get("k")


def test_rule_ids_match_the_go_adapter() -> None:
    """同一个桶 Go 与 Python 两边都会写生命周期规则，ID 要一样（Go 侧 TestKeysAreLaidOutLikeThePythonAdapter）。"""
    assert rule_id_prefix("t-a/") == "ar-a90eb0c6bad3bc9105a538b4-"


def test_prefix_must_end_with_a_slash() -> None:
    for bad in ("staging", "/staging/", "a\\b/"):
        with pytest.raises(ValueError):
            S3ObjectStoreProvider(endpoint="https://example.invalid", bucket="b", access_key_id="a", secret_access_key="s", prefix=bad)


def test_settings_from_env(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert S3Settings.from_env({}) is None, "没设 AR_S3_ENDPOINT 时不用 S3"
    with pytest.raises(ValueError) as info:
        S3Settings.from_env({"AR_S3_ENDPOINT": "https://x.r2.cloudflarestorage.com", "AR_S3_SECRET_ACCESS_KEY": "top-secret"})
    assert "AR_S3_BUCKET" in str(info.value) and "AR_S3_ACCESS_KEY_ID" in str(info.value) and "top-secret" not in str(info.value)
    key = tmp_path / "key"
    key.write_text("from-file\n", "utf-8")
    settings = S3Settings.from_env({
        "AR_S3_ENDPOINT": "https://x.r2.cloudflarestorage.com", "AR_S3_BUCKET": "b", "AR_S3_ACCESS_KEY_ID": "a",
        "AR_S3_SECRET_ACCESS_KEY_FILE": str(key), "AR_S3_PREFIX": "prod/",
    })
    assert settings is not None and settings.secret_access_key.reveal() == "from-file" and settings.prefix == "prod/" and settings.region == "auto"
    assert "from-file" not in repr(settings)
    assert settings.provider().prefix == "prod/"


def test_plain_http_endpoints_are_refused() -> None:
    with pytest.raises(ValueError):
        S3ObjectStoreProvider(endpoint="http://r2.example.com", bucket="b", access_key_id="a", secret_access_key="s")


async def test_object_uploaded_through_a_presigned_url_reports_sha256_on_get(fake: FakeHarness) -> None:
    store = fake.provider.bind(tenant_a())
    await fake.upload(await store.temporary_url("up", ttl=timedelta(minutes=5), method="PUT"), b"from-browser")
    assert (await store.head("up")).sha256 == ""
    assert (await store.get("up")).info.sha256 == hashlib.sha256(b"from-browser").hexdigest()


async def test_presigned_url_is_scoped_to_its_key_and_method(fake: FakeHarness) -> None:
    store = fake.provider.bind(tenant_a())
    await store.put("k", b"secret")
    url = await store.temporary_url("k", ttl=timedelta(minutes=5))
    with expect_error(ErrorCategory.DENIED):
        await fake.upload(url, b"overwrite")  # GET 的签名不能拿来 PUT
    with expect_error(ErrorCategory.DENIED):
        await fake.fetch(url.replace("/t-alpha/k?", "/t-beta/k?"))


async def test_signed_upload_binds_key_method_checksum_precondition_and_length(fake: FakeHarness) -> None:
    """STOR-SEC（第 8.2 节）：签进去的头是 content-length、host、if-none-match 与 x-amz-checksum-sha256，载荷不签；
    改路径、改方法、去掉或改动任一个头都被拒（签名不对）。"""
    store = fake.provider.bind(tenant_a())
    data = b"block-content"
    digest = hashlib.sha256(data)
    signed = await store.signed_upload("blocks/aa", ttl=timedelta(minutes=5), sha256=digest.hexdigest(), size=len(data))
    assert "X-Amz-SignedHeaders=content-length%3Bhost%3Bif-none-match%3Bx-amz-checksum-sha256" in signed.url
    assert dict(signed.headers) == {
        "Content-Length": str(len(data)), "If-None-Match": "*", "x-amz-checksum-sha256": base64.b64encode(digest.digest()).decode(),
    }
    with expect_error(ErrorCategory.DENIED):
        await fake.upload_signed(signed.url.replace("/t-alpha/blocks/aa?", "/t-alpha/blocks/bb?"), data, signed.headers)
    with expect_error(ErrorCategory.DENIED):
        await fake.upload_signed(signed.url.replace("/t-alpha/blocks/aa?", "/t-beta/blocks/aa?"), data, signed.headers)
    with expect_error(ErrorCategory.DENIED):
        await fake.fetch(signed.url)  # PUT 的签名不能拿来 GET
    for name in signed.headers:
        if name != "Content-Length":
            with expect_error(ErrorCategory.DENIED):
                await fake.upload_signed(signed.url, data, {k: v for k, v in signed.headers.items() if k != name})
    assert fake.fake.objects == {}
    await fake.upload_signed(signed.url, data, signed.headers)
    assert fake.fake.objects["t-alpha/blocks/aa"].data == data


async def test_fake_store_enforces_the_checksum_header() -> None:
    """假服务本身：PUT 带 x-amz-checksum-sha256 时按内容核对（与 S3 一样），不符报 400 BadDigest。"""
    h = FakeHarness()
    try:
        url = await h.provider.bind(tenant_a()).temporary_url("k", ttl=timedelta(minutes=5), method="PUT")
        wrong = base64.b64encode(hashlib.sha256(b"other").digest()).decode()
        status, body = await asyncio.to_thread(s3test.fetch, url, b"data", {"x-amz-checksum-sha256": wrong})
        assert status == 400 and b"BadDigest" in body
        assert h.fake.objects == {}
    finally:
        h.fake.stop()


async def test_presigned_url_lasts_at_most_seven_days(fake: FakeHarness) -> None:
    store = fake.provider.bind(tenant_a())
    url = await store.temporary_url("k", ttl=timedelta(days=30))
    assert "X-Amz-Expires=604800" in url


async def test_list_limit_above_a_page_is_capped(fake: FakeHarness) -> None:
    store = fake.provider.bind(tenant_a())
    await store.put("a", b"x")
    assert [i.key for i in (await store.list(limit=5000)).items] == ["a"]


async def test_lifecycle_expires_by_whole_days(fake: FakeHarness) -> None:
    store = fake.provider.bind(tenant_a())
    await store.set_lifecycle([LifecycleRule(prefix="tmp/", expire_after=timedelta(hours=1))])
    await store.put("tmp/old", b"x")
    await store.put("keep/old", b"x")
    await fake.advance(timedelta(hours=2))
    fake.fake.run_lifecycle()
    assert (await store.get("tmp/old")).data == b"x", "不足一天按一天：两小时后还在"
    await fake.advance(timedelta(days=1))
    fake.fake.run_lifecycle()
    with expect_error(ErrorCategory.NOT_FOUND):
        await store.get("tmp/old")
    assert (await store.get("keep/old")).data == b"x"


async def test_lifecycle_only_replaces_rules_of_its_own_scope(fake: FakeHarness) -> None:
    short, longer = fake.provider.bind(tenant("t-a")), fake.provider.bind(tenant("t-a-b"))
    platform = fake.provider.bind_platform("evals")
    await longer.set_lifecycle([LifecycleRule(prefix="tmp/", expire_after=timedelta(days=3))])
    await platform.set_lifecycle([LifecycleRule(prefix="", expire_after=timedelta(days=30))])
    await short.set_lifecycle([LifecycleRule(prefix="tmp/", expire_after=timedelta(days=1))])
    await short.set_lifecycle([LifecycleRule(prefix="cache/", expire_after=timedelta(days=2))])
    config = fake.fake.lifecycle.decode()
    assert config.count("<Rule>") == 3
    assert "<Prefix>t-a/cache/</Prefix>" in config and "<Prefix>t-a/tmp/</Prefix>" not in config
    assert "<Prefix>t-a-b/tmp/</Prefix>" in config and "<Prefix>_platform/</Prefix>" in config
    assert not rule_id_prefix("t-a-b/").startswith(rule_id_prefix("t-a/"))
    assert f"<ID>{rule_id_prefix('t-a/')}0</ID>" in config and f"<ID>{rule_id_prefix('t-a-b/')}0</ID>" in config
    await short.set_lifecycle([])
    assert fake.fake.lifecycle.decode().count("<Rule>") == 2
    await longer.set_lifecycle([])
    await platform.set_lifecycle([])
    assert fake.fake.lifecycle is None, "没有规则了就删掉整份配置（S3 不接受空的生命周期配置）"


async def test_lifecycle_rejects_non_positive_age(fake: FakeHarness) -> None:
    with expect_error(ErrorCategory.INVALID):
        await fake.provider.bind(tenant_a()).set_lifecycle([LifecycleRule(prefix="", expire_after=timedelta(0))])


async def test_unreachable_store_is_transient() -> None:
    clock = FakeClock()
    provider = S3ObjectStoreProvider(endpoint="http://127.0.0.1:9", bucket="b", access_key_id="a", secret_access_key="s", clock=clock, timeout_s=2)
    with expect_error(ErrorCategory.TRANSIENT, "object_store_unavailable"):
        await provider.bind(tenant_a()).get("k")


# --- 真实的存储服务 ---

LIVE = os.environ.get("AR_LIVE_S3") == "1"


class LiveHarness:
    """每个测试一个随机前缀，等于一个干净的 provider；advance 真实等待，太长的跳过。"""

    def __init__(self) -> None:
        self.faults = FaultInjector()
        self.prefix = f"conformance/{uuid.uuid4().hex}/"
        self.provider = S3ObjectStoreProvider(
            endpoint=os.environ["AR_S3_ENDPOINT"], bucket=os.environ["AR_S3_BUCKET"],
            access_key_id=os.environ["AR_S3_ACCESS_KEY_ID"], secret_access_key=os.environ["AR_S3_SECRET_ACCESS_KEY"],
            region=os.environ.get("AR_S3_REGION", "auto"), prefix=self.prefix, faults=self.faults,
        )

    async def advance(self, delta: timedelta) -> None:
        if delta > timedelta(minutes=2):
            pytest.skip("真实存储上不等这么久")
        await asyncio.sleep(delta.total_seconds())

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)

    async def fetch(self, url: str) -> bytes:
        return await _fetch(url)

    async def upload(self, url: str, data: bytes) -> None:
        await _fetch(url, data)

    async def upload_signed(self, url: str, data: bytes, headers: Mapping[str, str]) -> None:
        await _fetch(url, data, headers)

    async def run_lifecycle(self) -> None:
        raise FaultInjectionUnsupported

    async def cleanup(self) -> None:
        for client in (self.provider.bind(tenant_a()), self.provider.bind(tenant("t-beta")), self.provider.bind_platform("conformance")):
            page = await client.list()
            await asyncio.gather(*(client.delete(i.key) for i in page.items))
            await client.set_lifecycle([])


@pytest.mark.skipif(not LIVE, reason="设 AR_LIVE_S3=1 与 AR_S3_* 才对真实存储跑")
class TestLiveS3ObjectStore(ObjectStoreConformance):
    @pytest.fixture
    async def harness(self):
        h = LiveHarness()
        yield h
        await h.cleanup()
