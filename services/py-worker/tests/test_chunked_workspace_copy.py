"""Cross-language chunk manifests survive metadata-only copies and reference cleanup."""
import hashlib
import json
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.providers.sandbox import FileEntry
from agentruntime_core.tenancy import TenantContext
from agentruntime_worker.sandbox.policy import WorkspaceLimits
from agentruntime_worker.workspace.archive import WorkspaceArchiver, blob_key, manifest_key


async def test_chunk_copy_and_source_delete_preserve_live_references(tmp_path):
    objects = LocalDiskObjectStoreProvider(tmp_path / "objects", signing_key=b"chunk-copy-test")
    db = SQLiteRelationalStoreProvider(tmp_path / "database")
    arch = WorkspaceArchiver(db, objects)
    for name in ("source", "copy"):
        await arch.ensure("t1", name, WorkspaceLimits())
    bucket = objects.bind(TenantContext(tenant_id="t1"))
    parts = (b"f" * 20, b"f" * 20)
    whole = b"".join(parts)
    chunks = tuple((hashlib.sha256(part).hexdigest(), len(part)) for part in parts)
    entry = FileEntry(path=".git/HEAD", type="file", mode=0o644, size=len(whole),
                      sha256=hashlib.sha256(whole).hexdigest(), chunks=chunks)
    for sha, part in zip((sha for sha, _ in chunks), parts):
        await bucket.put(blob_key("source", sha), part)
    key = manifest_key("source", "snap")
    await bucket.put(key, json.dumps({"version": 1, "entries": [entry.as_dict()]}).encode())
    async with db.bind(TenantContext(tenant_id="t1")).transaction() as tx:
        await tx.execute("INSERT INTO workspace_snapshots (snapshot_id,workspace_id,seq,manifest_key,files,size_bytes,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7)",
                         ("snap", "source", 1, key, 1, len(whole), "2026-11-07T00:00:00.000000Z"))
    result = await arch.copy_snapshot("t1", "source", "copy", max_bytes=100)
    assert result is not None
    snapshot, head, branch = result
    assert head == "f" * 40 and branch == ""
    assert await arch.entries("t1", snapshot) == [entry]
    assert not (await bucket.list(prefix="workspaces/copy/blobs/")).items
    await arch.delete("t1", "source")
    assert (await bucket.get(blob_key("source", chunks[0][0]))).data == parts[0]
    await arch._prune("t1", "copy")
    assert (await bucket.get(blob_key("source", chunks[0][0]))).data == parts[0]
    await arch.delete("t1", "copy")
    assert not (await bucket.list(prefix="workspaces/source/blobs/")).items
