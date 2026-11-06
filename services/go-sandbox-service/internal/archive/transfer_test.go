package archive

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/diskobjects"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"path/filepath"
	"testing"
)

func digest(data []byte) string { h := sha256.Sum256(data); return hex.EncodeToString(h[:]) }

func TestChunkManifestRoundtripIntegrityAndGarbageCollection(t *testing.T) {
	ctx := context.Background()
	root := t.TempDir()
	objects, err := diskobjects.New(diskobjects.Options{Root: filepath.Join(root, "objects"), SigningKey: []byte("test-only")})
	if err != nil {
		t.Fatal(err)
	}
	db, err := sqlitestore.New(sqlitestore.Options{Dir: filepath.Join(root, "database")})
	if err != nil {
		t.Fatal(err)
	}
	arch := New(db, objects, nil)
	bucket, err := objects.Bind(&commonv1.TenantContext{TenantId: "t1"})
	if err != nil {
		t.Fatal(err)
	}
	a, b := []byte("first"), []byte("second")
	whole := append(append([]byte{}, a...), b...)
	entry := sandbox.FileEntry{Path: "large", Type: "file", Size: int64(len(whole)), SHA256: digest(whole), Chunks: []sandbox.FileBlock{{SHA256: digest(a), SizeBytes: int64(len(a))}, {SHA256: digest(b), SizeBytes: int64(len(b))}}}
	for _, data := range [][]byte{a, b, []byte("orphan")} {
		if _, err := bucket.Put(ctx, BlobKey("source", digest(data)), data, objectstore.PutOptions{}); err != nil {
			t.Fatal(err)
		}
	}
	snap := Snapshot{WorkspaceID: "source", ManifestKey: ManifestKey("source", "snap")}
	body, _ := json.Marshal(manifestDoc{Version: ManifestVersion, Entries: []map[string]any{entryDict(entry)}})
	if _, err := bucket.Put(ctx, snap.ManifestKey, body, objectstore.PutOptions{}); err != nil {
		t.Fatal(err)
	}
	entries, foreign, err := arch.Manifest(ctx, "t1", snap)
	if err != nil || len(entries) != 1 || len(entries[0].Chunks) != 2 {
		t.Fatalf("chunks lost: %v %v", entries, err)
	}
	got, err := arch.Blob(ctx, "t1", "source", entries[0], foreign)
	if err != nil || string(got) != string(whole) {
		t.Fatalf("assembled bytes: %q %v", got, err)
	}
	if err := arch.deleteUnreferenced(ctx, "t1", "source", []Snapshot{snap}, nil); err != nil {
		t.Fatal(err)
	}
	for _, data := range [][]byte{a, b} {
		if _, err := bucket.Head(ctx, BlobKey("source", digest(data))); err != nil {
			t.Fatalf("referenced chunk deleted: %v", err)
		}
	}
	if _, err := bucket.Head(ctx, BlobKey("source", digest([]byte("orphan")))); err == nil {
		t.Fatal("unreferenced block retained")
	}
	located := map[string]string{digest(a): "source", digest(b): "source"}
	if got, err := arch.Blob(ctx, "t1", "copy", entries[0], located); err != nil || string(got) != string(whole) {
		t.Fatalf("inherited chunks unreadable: %q %v", got, err)
	}
	entries[0].Chunks[0], entries[0].Chunks[1] = entries[0].Chunks[1], entries[0].Chunks[0]
	if _, err := arch.Blob(ctx, "t1", "copy", entries[0], located); err == nil {
		t.Fatal("reordered chunks accepted")
	}
	if _, err := arch.Blob(ctx, "t2", "source", entry, nil); err == nil {
		t.Fatal("cross-tenant chunk access accepted")
	}
}

func TestInvalidChunkManifestRejectedBeforeTransfer(t *testing.T) {
	a := digest([]byte("a"))
	cases := []sandbox.FileEntry{
		{Path: "a", Type: "file", Size: 1, SHA256: a, Chunks: []sandbox.FileBlock{{SHA256: a, SizeBytes: 0}}},
		{Path: "a", Type: "file", Size: 2, SHA256: a, Chunks: []sandbox.FileBlock{{SHA256: a, SizeBytes: 1}}},
		{Path: "a", Type: "file", Size: 1, SHA256: a, Chunks: []sandbox.FileBlock{{SHA256: "../escape", SizeBytes: 1}}},
		{Path: "a", Type: "file", Size: 5 << 20, SHA256: a, Chunks: []sandbox.FileBlock{{SHA256: a, SizeBytes: 5 << 20}}},
	}
	for _, entry := range cases {
		if _, _, err := validEntries([]sandbox.FileEntry{entry}, 8<<20); err == nil {
			t.Fatalf("invalid chunks accepted: %+v", entry)
		}
	}
}
