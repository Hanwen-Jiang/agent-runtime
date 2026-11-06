"""Real agent HTTP + real HTTP object transport, without external credentials."""
from __future__ import annotations
import base64
import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest

def call(agent, route, body):
    return agent.request("POST", route, json.dumps(body).encode())

@pytest.fixture
def objects():
    data = {}
    counts = {"GET": 0, "PUT": 0}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_PUT(self):
            counts["PUT"] += 1
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path in data:
                self.send_error(412); return
            checksum = self.headers.get("x-amz-checksum-sha256")
            if checksum and checksum != base64.b64encode(hashlib.sha256(body).digest()).decode():
                self.send_error(400); return
            data[self.path] = body
            self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()
        def do_GET(self):
            counts["GET"] += 1
            if self.path not in data:
                self.send_error(404); return
            self.send_response(200); self.send_header("Content-Length", str(len(data[self.path]))); self.end_headers()
            self.wfile.write(data[self.path])
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    yield f"http://127.0.0.1:{server.server_port}", data, counts
    server.shutdown(); server.server_close(); thread.join()

def rust_agent(make_agent, impl):
    if impl != "rust":
        pytest.skip("direct transfer is a Rust agent extension")
    return make_agent(extra_env={"AGENT_FILE_LIMIT": str(2 << 20)})

def test_prepare_immutable_bytes_commit_idempotent_and_release(make_agent, impl, objects):
    agent = rust_agent(make_agent, impl)
    base, data, counts = objects
    original = b"immutable file bytes\0" * 1000
    (agent.workdir / "out.bin").write_bytes(original)
    prepared = call(agent, "/v1/transfer/prepare", {"path": "out.bin"})
    assert prepared.status == 200
    meta = prepared.json()
    assert meta["size_bytes"] == len(original)
    (agent.workdir / "out.bin").write_bytes(b"changed after preparation")
    body = {"transfer_id": meta["transfer_id"], "object_key": "deliveries/task/out.bin",
            "target": {"url": base + "/out", "headers": {"If-None-Match": "*", "x-amz-checksum-sha256": base64.b64encode(hashlib.sha256(original).digest()).decode()}}}
    assert call(agent, "/v1/transfer/commit", body).status == 200
    assert call(agent, "/v1/transfer/commit", body).status == 200
    assert counts["PUT"] == 1 and data["/out"] == original
    assert call(agent, "/v1/transfer/release", {"transfer_id": meta["transfer_id"]}).status == 200
    assert call(agent, "/v1/transfer/read", {"transfer_id": meta["transfer_id"]}).status == 404
    assert call(agent, "/v1/transfer/prepare", {"path": "../../etc/passwd"}).status == 400

def test_exec_returns_separate_refs_and_failure_retains_full_output(make_agent, impl, objects):
    agent = rust_agent(make_agent, impl)
    base, data, _ = objects
    result = call(agent, "/v1/exec", {"argv": [sys.executable, "-c", "import sys;sys.stdout.write('A'*20000);sys.stderr.write('B'*12000)"], "timeout_ms": 30000, "offload_inline_bytes": 256})
    assert result.status == 200
    out = result.json()
    for stream, size, letter in [("stdout", 20000, b"A"), ("stderr", 12000, b"B")]:
        assert len(base64.b64decode(out[stream])) < 512
        prepared = out[stream + "_transfer"]
        assert prepared["size_bytes"] == size
        failed = call(agent, "/v1/transfer/commit", {"transfer_id": prepared["transfer_id"], "object_key": stream,
                        "target": {"url": "http://127.0.0.1:1/fail", "headers": {}}})
        assert failed.status == 502
        restored = call(agent, "/v1/transfer/read", {"transfer_id": prepared["transfer_id"]})
        assert restored.status == 200 and restored.body == letter * size
        successful = call(agent, "/v1/transfer/commit", {"transfer_id": prepared["transfer_id"], "object_key": stream,
                        "target": {"url": base + "/" + stream, "headers": {}}})
        assert successful.status == 200 and data["/" + stream] == letter * size

def test_restore_only_downloads_missing_blocks_and_checks_manifest(make_agent, impl, objects):
    agent = rust_agent(make_agent, impl)
    base, data, counts = objects
    content = b"one block shared by two files\0" * 100
    data["/blob"] = content
    sha = hashlib.sha256(content).hexdigest()
    plan = {"restore_id": "1" * 32, "max_bytes": 2 * len(content),
            "blocks": [{"sha256": sha, "size_bytes": len(content), "target": {"url": base + "/blob", "headers": {}}}]}
    first = call(agent, "/v1/restore/blocks", plan)
    second = call(agent, "/v1/restore/blocks", plan)
    assert first.status == second.status == 200
    assert first.json()["downloaded_blocks"] == 1
    assert second.json()["downloaded_blocks"] == 0 and counts["GET"] == 1
    entries = [{"path": name, "type": "file", "mode": 0o644, "size": len(content), "sha256": sha} for name in ["a", "b"]]
    restored = call(agent, "/v1/restore", {"restore_id": "1" * 32, "entries": entries})
    assert restored.status == 200, restored.body
    assert (agent.workdir / "a").read_bytes() == (agent.workdir / "b").read_bytes() == content
    manifest = agent.request("GET", "/v1/manifest").json()["entries"]
    assert {e["path"] for e in manifest} == {"a", "b"}
    assert all(e["sha256"] == sha for e in manifest)

def test_corrupt_block_and_path_escape_never_restore(make_agent, impl, objects, tmp_path):
    agent = rust_agent(make_agent, impl)
    base, data, _ = objects
    data["/corrupt"] = b"wrong"
    plan = {"restore_id": "2" * 32, "max_bytes": 20, "blocks": [{"sha256": hashlib.sha256(b"right").hexdigest(), "size_bytes": 5,
               "target": {"url": base + "/corrupt", "headers": {}}}]}
    assert call(agent, "/v1/restore/blocks", plan).status == 502
    assert list(agent.workdir.iterdir()) == []
    empty = {"restore_id": "3" * 32, "max_bytes": 20, "blocks": []}
    assert call(agent, "/v1/restore/blocks", empty).status == 200
    escape = call(agent, "/v1/restore", {"restore_id": "3" * 32, "entries": [{"path": "../escape", "type": "dir", "mode": 0o755}]})
    assert escape.status == 400
    assert not (agent.workdir.parent / "escape").exists()

def test_large_file_snapshot_is_chunked_and_restore_verifies_order(make_agent, impl, objects):
    agent = rust_agent(make_agent, impl)
    base, data, counts = objects
    # Deliberately exceed AGENT_FILE_LIMIT; snapshot chunks remain bounded.
    chunks = [b"A" * (4 << 20), b"B" * (4 << 20), b"tail"]
    whole = b"".join(chunks)
    (agent.workdir / "large.bin").write_bytes(whole)
    manifest = call(agent, "/v1/snapshot", {}).json()["entries"]
    [entry] = manifest
    assert entry["sha256"] == hashlib.sha256(whole).hexdigest()
    assert entry["chunks"] == [{"sha256": hashlib.sha256(c).hexdigest(), "size_bytes": len(c)} for c in chunks]
    prepared = call(agent, "/v1/transfer/prepare", {"path": "large.bin", "offset": 4 << 20, "length": 4 << 20})
    assert prepared.status == 200 and prepared.json()["sha256"] == hashlib.sha256(chunks[1]).hexdigest()
    (agent.workdir / "large.bin").unlink()
    blocks = []
    for i, chunk in enumerate(chunks):
        data[f"/{i}"] = chunk
        blocks.append({"sha256": hashlib.sha256(chunk).hexdigest(), "size_bytes": len(chunk), "target": {"url": f"{base}/{i}"}})
    plan = {"restore_id": "4" * 32, "max_bytes": len(whole), "blocks": blocks}
    assert call(agent, "/v1/restore/blocks", plan).status == 200
    assert call(agent, "/v1/restore/blocks", plan).json()["downloaded_bytes"] == 0
    assert counts["GET"] == len(chunks)
    assert call(agent, "/v1/restore", {"restore_id": plan["restore_id"], "entries": manifest}).status == 200
    assert (agent.workdir / "large.bin").read_bytes() == whole

    failed = rust_agent(make_agent, impl)
    plan["restore_id"] = "5" * 32
    assert call(failed, "/v1/restore/blocks", plan).status == 200
    entry["chunks"][0], entry["chunks"][1] = entry["chunks"][1], entry["chunks"][0]
    assert call(failed, "/v1/restore", {"restore_id": plan["restore_id"], "entries": manifest}).status == 400
    assert list(failed.workdir.iterdir()) == [], "wrong chunk order must not expose a partial workspace"
