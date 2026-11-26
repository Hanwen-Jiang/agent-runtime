"""Isolated headless Chromium renderer microservice for go-crawler-gateway (Wave 4 / P1)."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import signal
import socket
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import BoundedSemaphore
from typing import Any

logger = logging.getLogger("crawler_headless")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BLOCKED_META_HOSTS = frozenset({
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.goog",
    "10.43.0.1",
    "kubernetes.default",
    "kubernetes.default.svc",
})
BLOCKED_INFRA_PORTS = frozenset({22, 2379, 5432, 6379, 6443, 7233, 8123, 9000, 9093})
CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
MAX_HTML_BYTES = 512_000


def _is_public_ip(raw_ip: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw_ip)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_public_ip(str(ip.ipv4_mapped))
    if not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return False
    if isinstance(ip, ipaddress.IPv4Address) and ip in CGNAT_NET:
        return False
    return True


def validate_public_url(raw_url: str, *, allow_private_for_test: bool = False, resolver: Any = None) -> tuple[str, list[str]]:
    raw = str(raw_url or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("only http/https URLs with host are allowed")
    if parsed.username or parsed.password:
        raise PermissionError("URL credentials are prohibited")
    host = parsed.hostname.lower().strip(".")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if host in BLOCKED_META_HOSTS or host.endswith(".169.254.169.254"):
        raise PermissionError(f"metadata/control-plane host {host!r} is hard-blocked")
    if port in BLOCKED_INFRA_PORTS:
        raise PermissionError(f"infrastructure port {port} is blocked")

    if allow_private_for_test:
        return raw, []

    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        ip_obj = None

    if ip_obj is not None:
        if not _is_public_ip(str(ip_obj)):
            raise PermissionError(f"non-public IP {host!r} is blocked")
        return raw, [str(ip_obj)]

    lookup = resolver or socket.getaddrinfo
    try:
        infos = lookup(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"dns lookup failed for {host!r}: {exc}") from exc
    ips = sorted({str(info[4][0]) for info in infos if info[4]})
    if not ips:
        raise ValueError(f"no resolved IPs for {host!r}")
    for ip in ips:
        if not _is_public_ip(ip):
            raise PermissionError(f"host {host!r} resolved to non-public IP {ip}")
    return raw, ips


async def render_page(target_url: str, *, timeout_ms: int = 15000, wait_until: str = "networkidle") -> dict[str, Any]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        try:
            context = await browser.new_context(
                java_script_enabled=True,
                ignore_https_errors=False,
                user_agent="agentruntime-headless-crawler/1.0",
            )
            page = await context.new_page()

            async def _guard_route(route: Any) -> None:
                req = route.request
                if req.resource_type in ("image", "media", "font", "stylesheet"):
                    await route.abort()
                    return
                try:
                    validate_public_url(req.url)
                except Exception:
                    await route.abort()
                    return
                await route.continue_()

            await page.route("**/*", _guard_route)
            resp = await page.goto(target_url, wait_until=wait_until, timeout=timeout_ms)
            html_body = await page.content()
            title = await page.title()
            return {
                "url": page.url,
                "status": resp.status if resp is not None else 200,
                "title": title,
                "html": html_body[:MAX_HTML_BYTES],
            }
        finally:
            await browser.close()


def make_handler(
    *,
    token: str,
    semaphore: BoundedSemaphore,
    timeout_ms: int = 15000,
    allow_private_for_test: bool = False,
    render_fn: Any = None,
) -> type[BaseHTTPRequestHandler]:
    renderer = render_fn or render_page

    class _Handler(BaseHTTPRequestHandler):
        def _send_json(self, code: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._send_json(HTTPStatus.OK, {"ok": True, "browser": "chromium"})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/render":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if token:
                auth = self.headers.get("Authorization", "")
                got = auth[7:].strip() if auth.startswith("Bearer ") else ""
                if not hmac.compare_digest(got, token):
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "missing or invalid headless token"})
                    return
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0 or length > 65536:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid body size"})
                return
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
                return
            target = str(body.get("url") or "").strip()
            try:
                clean_url, _ = validate_public_url(target, allow_private_for_test=allow_private_for_test)
            except PermissionError as exc:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
                return
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            if not semaphore.acquire(blocking=False):
                self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "headless renderer busy"})
                return
            try:
                out = asyncio.run(renderer(clean_url, timeout_ms=timeout_ms))
                self._send_json(HTTPStatus.OK, out)
            except Exception as exc:  # noqa: BLE001
                logger.warning("render failed for %s: %s", urllib.parse.urlsplit(clean_url).netloc, type(exc).__name__)
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": f"render failed: {exc}"})
            finally:
                semaphore.release()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    return _Handler


def main() -> None:
    addr = os.environ.get("AR_HEADLESS_ADDR", "0.0.0.0:8098").strip()
    host, _, port_str = addr.rpartition(":")
    host = host or "0.0.0.0"
    port = int(port_str or "8098")
    token = os.environ.get("AR_HEADLESS_TOKEN", "").strip()
    concurrency = max(1, int(os.environ.get("AR_HEADLESS_CONCURRENCY", "2") or "2"))
    timeout_ms = max(3000, int(os.environ.get("AR_HEADLESS_TIMEOUT_MS", "15000") or "15000"))

    srv = ThreadingHTTPServer(
        (host, port),
        make_handler(token=token, semaphore=BoundedSemaphore(concurrency), timeout_ms=timeout_ms),
    )

    def _shutdown(*_: Any) -> None:
        srv.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    logger.info("crawler-headless listening on %s:%d (concurrency=%d)", host, port, concurrency)
    srv.serve_forever()


if __name__ == "__main__":
    main()
