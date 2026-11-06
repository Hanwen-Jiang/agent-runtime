package fcsandbox_test

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/fcsandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/s3objects"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
)

// This opt-in test requires a dedicated real fc-manager instance, a fresh local
// cache capped at 96 KiB, and live R2 credentials. It never configures production.
func TestLiveFirecrackerBlockCacheAndTenantIsolation(t *testing.T) {
	if os.Getenv("AR_LIVE_FC_BLOB") != "1" {
		t.Skip("dedicated real FC+R2 harness required")
	}
	endpoint, token, state := os.Getenv("AR_FC_TEST_URL"), os.Getenv("AR_FC_TEST_TOKEN"), os.Getenv("AR_FC_TEST_STATE")
	if endpoint == "" || len(token) < 32 || state == "" {
		t.Fatal("dedicated FC harness missing")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	var salt [16]byte
	if _, err := rand.Read(salt[:]); err != nil {
		t.Fatal("random unavailable")
	}
	objects, err := s3objects.New(s3objects.Options{Endpoint: os.Getenv("AR_S3_ENDPOINT"), Bucket: os.Getenv("AR_S3_BUCKET"), AccessKeyID: os.Getenv("AR_S3_ACCESS_KEY_ID"), SecretAccessKey: secret.New(os.Getenv("AR_S3_SECRET_ACCESS_KEY")), Region: os.Getenv("AR_S3_REGION"), Prefix: "fc-blob-live/" + hex.EncodeToString(salt[:]) + "/"})
	if err != nil {
		t.Fatal("R2 setup failed")
	}
	oa, err := objects.Bind(conformance.TenantA())
	if err != nil {
		t.Fatal("scope A")
	}
	ob, err := objects.Bind(conformance.TenantB())
	if err != nil {
		t.Fatal("scope B")
	}
	p, err := fcsandbox.New(fcsandbox.Config{ManagerURL: endpoint, DiskMB: 256}, token, nil)
	if err != nil {
		t.Fatal("FC setup failed")
	}
	a, err := p.Bind(conformance.TenantA())
	if err != nil {
		t.Fatal("FC scope A")
	}
	b, err := p.Bind(conformance.TenantB())
	if err != nil {
		t.Fatal("FC scope B")
	}
	spec := sandbox.Spec{Runtime: "python", CPUMillis: 1000, MemoryMB: 256}
	ea, err := a.Create(ctx, spec)
	if err != nil {
		t.Fatal("VM A failed")
	}
	t.Cleanup(func() {
		c, x := context.WithTimeout(context.Background(), 15*time.Second)
		defer x()
		if err := a.Destroy(c, ea.ID); err != nil {
			t.Error("VM A cleanup")
		}
	})
	eb, err := b.Create(ctx, spec)
	if err != nil {
		t.Fatal("VM B failed")
	}
	t.Cleanup(func() {
		c, x := context.WithTimeout(context.Background(), 15*time.Second)
		defer x()
		if err := b.Destroy(c, eb.ID); err != nil {
			t.Error("VM B cleanup")
		}
	})
	ownerA, ownerB := conformance.TenantA().TenantId, conformance.TenantB().TenantId
	call := func(owner, method, path string, body any) (int, map[string]any) {
		t.Helper()
		var in io.Reader
		if body != nil {
			raw, e := json.Marshal(body)
			if e != nil {
				t.Fatal("encode")
			}
			in = bytes.NewReader(raw)
		}
		req, e := http.NewRequestWithContext(ctx, method, endpoint+path, in)
		if e != nil {
			t.Fatal("request")
		}
		req.Header.Set("Authorization", "Bearer "+token)
		req.Header.Set("X-Sandbox-Owner", owner)
		req.Header.Set("Content-Type", "application/json")
		r, e := http.DefaultClient.Do(req)
		if e != nil {
			t.Fatal("FC request failed")
		}
		defer r.Body.Close()
		data, _ := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		v := map[string]any{}
		_ = json.Unmarshal(data, &v)
		return r.StatusCode, v
	}
	stats := func() map[string]any {
		s, v := call("_platform", "GET", "/v1/blob-cache", nil)
		if s != 200 {
			t.Fatal("cache stats")
		}
		return v
	}
	if s, _ := call(ownerA, "GET", "/v1/blob-cache", nil); s != 403 {
		t.Fatal("tenant could read platform cache metrics")
	}
	content := bytes.Repeat([]byte("cache-data-byte!"), 4096)
	sum := sha256.Sum256(content)
	digest := hex.EncodeToString(sum[:])
	put := func(s objectstore.Store, key string, data []byte) {
		t.Helper()
		if _, e := s.Put(ctx, key, data, objectstore.PutOptions{IfAbsent: true}); e != nil {
			t.Fatal("R2 seed")
		}
		t.Cleanup(func() {
			c, x := context.WithTimeout(context.Background(), 15*time.Second)
			defer x()
			if _, e := s.Delete(c, key); e != nil {
				t.Error("R2 cleanup")
			}
		})
	}
	put(oa, "shared", content)
	put(ob, "shared", content)
	grant := func(owner, id string, s objectstore.Store, key string) (sandbox.TransferTarget, int) {
		t.Helper()
		url, e := s.TemporaryURL(ctx, key, time.Minute, "GET")
		if e != nil {
			t.Fatal("sign")
		}
		status, v := call(owner, "POST", "/v1/envs/"+id+"/blob-grants", map[string]any{"objects": []any{map[string]any{"method": "GET", "sha256": digest, "size_bytes": len(content), "target": map[string]any{"url": url, "headers": map[string]string{}}}}})
		if status != 200 {
			return sandbox.TransferTarget{}, status
		}
		raw, _ := json.Marshal(v["objects"])
		var out []sandbox.TransferTarget
		_ = json.Unmarshal(raw, &out)
		if len(out) != 1 {
			t.Fatal("grant response")
		}
		return out[0], status
	}
	ga, status := grant(ownerA, ea.ID, oa, "shared")
	if status != 200 {
		t.Fatal("grant A")
	}
	if _, status = grant(ownerB, ea.ID, ob, "shared"); status != 404 {
		t.Fatal("cross-tenant grant issuance accepted")
	}
	block := func(target sandbox.TransferTarget) []sandbox.RestoreBlock {
		return []sandbox.RestoreBlock{{SHA256: digest, SizeBytes: int64(len(content)), Target: target}}
	}
	// Direct agent requests keep grant identity stable and expose actual download counts.
	restore := func(owner, id, restoreID string, target sandbox.TransferTarget) map[string]any {
		s, v := call(owner, "POST", "/v1/envs/"+id+"/agent/v1/restore/blocks", map[string]any{"restore_id": restoreID, "max_bytes": 1 << 20, "blocks": block(target)})
		if s != 200 {
			t.Fatalf("real vsock restore HTTP %d", s)
		}
		return v
	}
	before := stats()
	first := restore(ownerA, ea.ID, strings.Repeat("a", 32), ga)
	second := restore(ownerA, ea.ID, strings.Repeat("a", 32), ga)
	if first["downloaded_blocks"] != float64(1) || second["downloaded_blocks"] != float64(0) {
		t.Fatal("missing-block reuse failed")
	}
	after := stats()
	if after["misses"].(float64)-before["misses"].(float64) != 1 {
		t.Fatal("first fetch count")
	}
	// Replaying A's grant over B's independent vsock connection is denied even
	// though the identical bytes now exist in the node's shared cache.
	s, v := call(ownerB, "POST", "/v1/envs/"+eb.ID+"/agent/v1/exec", map[string]any{"argv": []string{"curl", "--silent", "--output", "/dev/null", "--write-out", "%{http_code}", ga.URL}, "timeout_ms": 10000})
	if s != 200 || v["exit_code"] != float64(0) || v["stdout"] != "NDAz" {
		t.Fatal("cached bytes bypassed VM grant authorization")
	}
	gb, status := grant(ownerB, eb.ID, ob, "shared")
	if status != 200 {
		t.Fatal("grant B")
	}
	restore(ownerB, eb.ID, strings.Repeat("b", 32), gb)
	cached := stats()
	if cached["hits"].(float64) <= after["hits"].(float64) || cached["misses"] != after["misses"] {
		t.Fatal("authorized cross-VM cache reuse failed")
	}
	// Corrupt only this isolated manager's local cache; the durable R2 object
	// remains authoritative and must be fetched again with its expected digest.
	if err := os.WriteFile(filepath.Join(state, "blobs", digest), bytes.Repeat([]byte("x"), len(content)), 0600); err != nil {
		t.Fatal("cache fault injection")
	}
	restore(ownerA, ea.ID, strings.Repeat("c", 32), ga)
	recovered := stats()
	if recovered["corruptions"].(float64) <= cached["corruptions"].(float64) || recovered["misses"].(float64) <= cached["misses"].(float64) {
		t.Fatal("corrupt cache did not refetch")
	}
	entries := []sandbox.FileEntry{{Path: "restored.bin", Type: "file", Mode: 0600, Size: int64(len(content)), SHA256: digest}}
	if a.(sandbox.BlockTransfers).RestoreFinish(ctx, ea.ID, strings.Repeat("c", 32), entries) != nil {
		t.Fatal("materialize")
	}
	got, err := a.ReadFile(ctx, ea.ID, "restored.bin")
	if err != nil || !bytes.Equal(got, content) {
		t.Fatal("restored bytes")
	}
	// Agent -> vsock -> host -> real R2 PUT, then authoritative store verification.
	prep, err := a.(sandbox.Transfers).PrepareTransfer(ctx, ea.ID, "restored.bin")
	if err != nil {
		t.Fatal("prepare upload")
	}
	up, err := oa.SignedUpload(ctx, "uploaded", time.Minute, digest, int64(len(content)))
	if err != nil {
		t.Fatal("sign upload")
	}
	t.Cleanup(func() {
		c, x := context.WithTimeout(context.Background(), 15*time.Second)
		defer x()
		if _, e := oa.Delete(c, "uploaded"); e != nil {
			t.Error("upload cleanup")
		}
	})
	for i := 0; i < 2; i++ {
		if _, err := a.(sandbox.Transfers).CommitTransfer(ctx, ea.ID, prep, "uploaded", sandbox.TransferTarget{URL: up.URL, Headers: up.Headers}); err != nil {
			t.Fatal("real FC upload")
		}
	}
	stored, err := oa.Get(ctx, "uploaded")
	if err != nil || !bytes.Equal(stored.Data, content) {
		t.Fatal("durable FC upload bytes")
	}
	t.Logf("real VM/vsock/R2: missing blocks 1 then 0; cross-VM cached grant denied; authorized cache hit; corruption refetched; immutable PUT retry verified; final stats=%v", stats())
}
