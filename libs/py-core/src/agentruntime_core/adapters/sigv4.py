"""AWS Signature Version 4（只用标准库）：S3 兼容的 ObjectStore 适配器（Cloudflare R2、AWS S3、MinIO）用它签请求与预签名地址
（docs/plan/cloud-deploy.md D-09）。不引入 boto3（设计书 4.15 之外的依赖）。

规则按 AWS 的文档：规范请求（方法、URI、查询串、按名排序的小写头、签名的头、载荷哈希）→ 待签字符串 → 按日期、区域、服务、
aws4_request 逐级 HMAC 派生的密钥签名。测试用文档里的例子核对（tests/adapters/test_s3_object_store.py）。
"""

from __future__ import annotations

import hashlib
import hmac
import urllib.parse
from collections.abc import Mapping
from datetime import UTC, datetime

ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    k = _hmac(("AWS4" + secret).encode("utf-8"), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


def quote(value: str, *, path: bool = False) -> str:
    """URI 编码：只保留 A-Z a-z 0-9 - _ . ~；路径里的 / 保留。"""
    return urllib.parse.quote(value, safe="/-_.~" if path else "-_.~")


def canonical_query(query: str) -> str:
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    return "&".join(f"{quote(k)}={quote(v)}" for k, v in sorted(pairs))


def canonical_request(method: str, path: str, query: str, headers: Mapping[str, str], signed: list[str], payload_hash: str) -> str:
    lines = [f"{name}:{' '.join(str(headers[name]).split())}" for name in signed]
    return "\n".join([method, path or "/", canonical_query(query), "\n".join(lines) + "\n", ";".join(signed), payload_hash])


def _stamp(now: datetime) -> tuple[str, str]:
    now = now.astimezone(UTC)
    return now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")


def sign(
    method: str,
    url: str,
    headers: Mapping[str, str],
    *,
    payload_hash: str,
    access_key: str,
    secret: str,
    region: str,
    service: str = "s3",
    now: datetime,
) -> dict[str, str]:
    """返回要加到请求上的头：x-amz-date、x-amz-content-sha256（s3）与 Authorization。url 的路径要已经按 S3 的规则编码好。"""
    parts = urllib.parse.urlsplit(url)
    amz_date, date = _stamp(now)
    lower = {k.lower(): str(v) for k, v in headers.items()}
    lower["host"] = parts.netloc
    lower["x-amz-date"] = amz_date
    if service == "s3":
        lower["x-amz-content-sha256"] = payload_hash
    signed = sorted(lower)
    request = canonical_request(method, parts.path, parts.query, lower, signed, payload_hash)
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join([ALGORITHM, amz_date, scope, hashlib.sha256(request.encode()).hexdigest()])
    signature = hmac.new(signing_key(secret, date, region, service), to_sign.encode(), hashlib.sha256).hexdigest()
    out = {"x-amz-date": amz_date, "Authorization": f"{ALGORITHM} Credential={access_key}/{scope}, SignedHeaders={';'.join(signed)}, Signature={signature}"}
    if service == "s3":
        out["x-amz-content-sha256"] = payload_hash
    return out


def presign(method: str, url: str, *, expires: int, access_key: str, secret: str, region: str, service: str = "s3", now: datetime) -> str:
    """预签名地址（查询串里带签名）：只签 host 头，载荷不签（UNSIGNED-PAYLOAD）。"""
    parts = urllib.parse.urlsplit(url)
    amz_date, date = _stamp(now)
    scope = f"{date}/{region}/{service}/aws4_request"
    params = urllib.parse.parse_qsl(parts.query, keep_blank_values=True) + [
        ("X-Amz-Algorithm", ALGORITHM),
        ("X-Amz-Credential", f"{access_key}/{scope}"),
        ("X-Amz-Date", amz_date),
        ("X-Amz-Expires", str(expires)),
        ("X-Amz-SignedHeaders", "host"),
    ]
    query = "&".join(f"{quote(k)}={quote(v)}" for k, v in params)
    request = canonical_request(method, parts.path, query, {"host": parts.netloc}, ["host"], UNSIGNED_PAYLOAD)
    to_sign = "\n".join([ALGORITHM, amz_date, scope, hashlib.sha256(request.encode()).hexdigest()])
    signature = hmac.new(signing_key(secret, date, region, service), to_sign.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, f"{query}&X-Amz-Signature={signature}", ""))
