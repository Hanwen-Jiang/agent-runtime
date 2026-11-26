"""Unit tests for py-headless-renderer SSRF validation, auth, and concurrency limits."""

from __future__ import annotations

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore

import pytest

APP_PATH = Path(__file__).resolve().parent / "app.py"
SPEC = importlib.util.spec_from_file_location("crawler_headless_app", APP_PATH)
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def test_validate_public_url_blocks_ssrf_and_infra_ports() -> None:
    for blocked in (
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/admin",
        "http://10.0.0.12/",
        "https://user:pass@example.com/",
        "http://pg-rw.data.svc:5432/",
    ):
        with pytest.raises(PermissionError):
            MOD.validate_public_url(blocked, resolver=lambda *_: [(2, 1, 6, "", ("127.0.0.1", 80))])

    url, ips = MOD.validate_public_url(
        "https://example.com/app",
        resolver=lambda *_, **__: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    assert url == "https://example.com/app" and ips == ["93.184.216.34"]


def test_http_handler_auth_ssrf_and_render() -> None:
    async def fake_render(url: str, *, timeout_ms: int = 15000) -> dict[str, object]:
        return {"url": url, "status": 200, "title": "SPA", "html": "<html><title>SPA</title><body><p>Rendered</p></body></html>"}

    handler = MOD.make_handler(
        token="headless-secret",
        semaphore=BoundedSemaphore(2),
        allow_private_for_test=True,
        render_fn=fake_render,
    )
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/healthz", timeout=5) as r:
            assert r.status == 200 and json.loads(r.read())["ok"] is True

        req_noauth = urllib.request.Request(base + "/render", data=b'{"url":"https://example.com"}', method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req_noauth, timeout=5)
        assert exc.value.code == 401

        req_ssrf = urllib.request.Request(
            base + "/render",
            data=json.dumps({"url": "http://169.254.169.254/latest/meta-data/"}).encode(),
            headers={"Authorization": "Bearer headless-secret", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc2:
            urllib.request.urlopen(req_ssrf, timeout=5)
        assert exc2.value.code == 403

        req_ok = urllib.request.Request(
            base + "/render",
            data=json.dumps({"url": "https://example.com/spa"}).encode(),
            headers={"Authorization": "Bearer headless-secret", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_ok, timeout=5) as r:
            body = json.loads(r.read())
            assert r.status == 200 and body["title"] == "SPA" and "Rendered" in body["html"]
    finally:
        srv.shutdown()
