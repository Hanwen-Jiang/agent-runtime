"""Chunk manifests must retain storage keys across the Worker copy/GC boundary."""
import hashlib
from agentruntime_core.providers.sandbox import FileEntry


def test_chunk_manifest_roundtrip_preserves_order_and_legacy_shape():
    parts = (b"a", b"bc", b"a")
    hashes = tuple((hashlib.sha256(part).hexdigest(), len(part)) for part in parts)
    entry = FileEntry(path="large", type="file", mode=0o644, size=4,
                      sha256=hashlib.sha256(b"".join(parts)).hexdigest(), chunks=hashes)
    decoded = FileEntry.from_dict(entry.as_dict())
    assert decoded == entry and decoded.blocks == hashes
    legacy = FileEntry(path="old", type="file", mode=0o644, size=1, sha256=hashes[0][0])
    assert "chunks" not in legacy.as_dict()
    assert FileEntry.from_dict(legacy.as_dict()).blocks == ((legacy.sha256, 1),)
    assert FileEntry(path="dir", type="dir", mode=0o755).blocks == ()
