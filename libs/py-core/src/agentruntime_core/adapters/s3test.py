"""测试里的 S3 兼容服务（docs/plan/cloud-deploy.md D-09）：只实现 ObjectStore 适配器用到的部分——路径式地址的对象读写、
条件写（If-None-Match: *）、ListObjectsV2、桶的生命周期配置——并按 SigV4 核对每个请求的签名与预签名地址的过期时间。
签名的核对是这里单独写的一份（不调用 adapters.sigv4），时间取注入的时钟，好测预签名地址过期。

真实的 R2、S3、MinIO 设 AR_LIVE_S3=1（另要 AR_S3_ENDPOINT、AR_S3_BUCKET、AR_S3_ACCESS_KEY_ID、AR_S3_SECRET_ACCESS_KEY，
可选 AR_S3_REGION）时再跑一遍一致性测试。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from xml.sax.saxutils import escape

_AUTH = re.compile(r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/s3/aws4_request, SignedHeaders=([^,]+), Signature=([0-9a-f]{64})")


@dataclass
class _Object:
    data: bytes
    headers: dict[str, str]
    modified: datetime


class FakeS3:
    def __init__(self, *, bucket: str, access_key: str, secret: str, region: str = "auto", now: Callable[[], datetime]) -> None:
        self.bucket, self.access_key, self.secret, self.region, self.now = bucket, access_key, secret, region, now
        self.objects: dict[str, _Object] = {}
        self.lifecycle: bytes | None = None
        self.requests: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                fake.handle(self)

            def do_HEAD(self) -> None:  # noqa: N802
                fake.handle(self)

            def do_PUT(self) -> None:  # noqa: N802
                fake.handle(self)

            def do_DELETE(self) -> None:  # noqa: N802
                fake.handle(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def run_lifecycle(self) -> int:
        """按当前生命周期配置清理一次（Expiration/Days，按整天算；存储服务每天做的那一步）。返回清理的个数。"""
        if self.lifecycle is None:
            return 0
        rules = []
        for rule in ET.fromstring(self.lifecycle).iter():
            if rule.tag.rsplit("}", 1)[-1] != "Rule":
                continue
            fields = {el.tag.rsplit("}", 1)[-1]: (el.text or "") for el in rule.iter()}
            if fields.get("Status") == "Enabled":
                rules.append((fields.get("Prefix", ""), timedelta(days=int(fields["Days"]))))
        now = self.now()
        with self._lock:
            gone = [n for n, o in self.objects.items() if any(n.startswith(p) and o.modified + age <= now for p, age in rules)]
            for name in gone:
                del self.objects[name]
        return len(gone)

    # --- 签名 ---

    def _key(self, date: str) -> bytes:
        k = ("AWS4" + self.secret).encode()
        for part in (date, self.region, "s3", "aws4_request"):
            k = hmac.new(k, part.encode(), hashlib.sha256).digest()
        return k

    @staticmethod
    def _enc(s: str) -> str:
        return urllib.parse.quote(s, safe="-_.~")

    def _canonical(self, method: str, path: str, pairs: list[tuple[str, str]], headers: dict[str, str], signed: list[str], payload: str) -> str:
        query = "&".join(f"{self._enc(k)}={self._enc(v)}" for k, v in sorted(pairs))
        lines = "".join(f"{h}:{' '.join(headers.get(h, '').split())}\n" for h in signed)
        return "\n".join([method, path, query, lines, ";".join(signed), payload])

    def _verify(self, h: BaseHTTPRequestHandler, body: bytes) -> str | None:
        """签名不对或过期时返回错误码，对时返回 None。"""
        path, _, raw_query = h.path.partition("?")
        pairs = urllib.parse.parse_qsl(raw_query, keep_blank_values=True)
        headers = {k.lower(): v for k, v in h.headers.items()}
        query = dict(pairs)
        if "X-Amz-Signature" in query:
            when = datetime.strptime(query["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            if self.now() > when + timedelta(seconds=int(query["X-Amz-Expires"])):
                return "AccessDenied"
            access, date = query["X-Amz-Credential"].split("/")[:2]
            signed = query["X-Amz-SignedHeaders"].split(";")
            rest = [(k, v) for k, v in pairs if k != "X-Amz-Signature"]
            canonical = self._canonical(h.command, path, rest, headers, signed, "UNSIGNED-PAYLOAD")
            want = query["X-Amz-Signature"]
            amz_date = query["X-Amz-Date"]
        else:
            m = _AUTH.fullmatch(headers.get("authorization", ""))
            if not m:
                return "AccessDenied"
            access, date, _region, signed_text, want = m.groups()
            signed = signed_text.split(";")
            payload = headers.get("x-amz-content-sha256", "")
            if payload != hashlib.sha256(body).hexdigest():
                return "XAmzContentSHA256Mismatch"
            amz_date = headers.get("x-amz-date", "")
            when = datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            if abs(self.now() - when) > timedelta(minutes=15):
                return "RequestTimeTooSkewed"
            canonical = self._canonical(h.command, path, pairs, headers, signed, payload)
        if access != self.access_key:
            return "InvalidAccessKeyId"
        scope = f"{date}/{self.region}/s3/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
        got = hmac.new(self._key(date), to_sign.encode(), hashlib.sha256).hexdigest()
        return None if hmac.compare_digest(got, want) else "SignatureDoesNotMatch"

    # --- 路由 ---

    def handle(self, h: BaseHTTPRequestHandler) -> None:
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length) if length else b""
        problem = self._verify(h, body)
        if problem:
            return self._xml_error(h, 403, problem)
        path, _, raw_query = h.path.partition("?")
        query = dict(urllib.parse.parse_qsl(raw_query, keep_blank_values=True))
        parts = path.split("/", 2)
        if len(parts) < 2 or parts[1] != self.bucket:
            return self._xml_error(h, 404, "NoSuchBucket")
        name = urllib.parse.unquote(parts[2]) if len(parts) == 3 else ""
        self.requests.append((h.command, name or raw_query))
        with self._lock:
            if not name:
                if "lifecycle" in query:
                    return self._lifecycle(h, body)
                if h.command == "GET" and query.get("list-type") == "2":
                    return self._list(h, query)
                return self._xml_error(h, 400, "InvalidRequest")
            obj = self.objects.get(name)
            if h.command == "PUT":
                if h.headers.get("If-None-Match") == "*" and obj is not None:
                    return self._xml_error(h, 412, "PreconditionFailed")
                kept = {k.lower(): v for k, v in h.headers.items() if k.lower().startswith("x-amz-meta-") or k.lower() == "content-type"}
                self.objects[name] = _Object(body, kept, self.now())
                return self._send(h, 200, b"", {"ETag": '"' + hashlib.md5(body, usedforsecurity=False).hexdigest() + '"'})
            if h.command == "DELETE":
                self.objects.pop(name, None)
                return self._send(h, 204, b"")
            if obj is None:
                return self._xml_error(h, 404, "NoSuchKey")
            headers = {**obj.headers, "Last-Modified": format_datetime(obj.modified.astimezone(UTC), usegmt=True)}
            headers.setdefault("content-type", "application/octet-stream")
            return self._send(h, 200, obj.data, headers, head=h.command == "HEAD")

    def _list(self, h: BaseHTTPRequestHandler, query: dict[str, str]) -> None:
        prefix, limit = query.get("prefix", ""), int(query.get("max-keys", "1000"))
        names = sorted(n for n in self.objects if n.startswith(prefix))
        token = query.get("continuation-token")
        if token:
            after = base64.urlsafe_b64decode(token.encode()).decode()
            names = [n for n in names if n > after]
        page, truncated = names[:limit], len(names) > limit
        items = "".join(f"<Contents><Key>{escape(n)}</Key><Size>{len(self.objects[n].data)}</Size></Contents>" for n in page)
        next_token = f"<NextContinuationToken>{base64.urlsafe_b64encode(page[-1].encode()).decode()}</NextContinuationToken>" if truncated else ""
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<Name>{self.bucket}</Name><Prefix>{escape(prefix)}</Prefix><KeyCount>{len(page)}</KeyCount><MaxKeys>{limit}</MaxKeys>"
            f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>{next_token}{items}</ListBucketResult>"
        )
        self._send(h, 200, xml.encode(), {"Content-Type": "application/xml"})

    def _lifecycle(self, h: BaseHTTPRequestHandler, body: bytes) -> None:
        if h.command == "GET":
            if self.lifecycle is None:
                return self._xml_error(h, 404, "NoSuchLifecycleConfiguration")
            return self._send(h, 200, self.lifecycle, {"Content-Type": "application/xml"})
        if h.command == "PUT":
            md5 = base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode()
            if h.headers.get("Content-MD5") != md5:
                return self._xml_error(h, 400, "InvalidDigest")
            self.lifecycle = body
            return self._send(h, 200, b"")
        if h.command == "DELETE":
            self.lifecycle = None
            return self._send(h, 204, b"")
        return self._xml_error(h, 400, "InvalidRequest")

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, status: int, body: bytes, headers: dict[str, str] | None = None, *, head: bool = False) -> None:
        h.send_response(status)
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        if body and not head:
            h.wfile.write(body)

    def _xml_error(self, h: BaseHTTPRequestHandler, status: int, code: str) -> None:
        body = f'<?xml version="1.0" encoding="UTF-8"?><Error><Code>{code}</Code><Message>{code}</Message></Error>'.encode()
        self._send(h, status, body, {"Content-Type": "application/xml"}, head=h.command == "HEAD")


def fetch(url: str, data: bytes | None = None) -> tuple[int, bytes]:
    """不带签名地访问一个（预签名）地址：data 为 None 时 GET，否则 PUT。返回状态码与响应体。"""
    request = urllib.request.Request(url, data=data, method="GET" if data is None else "PUT")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()
