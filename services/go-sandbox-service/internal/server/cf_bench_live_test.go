package server_test

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/s3objects"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	sandboxv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/archive"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
)

func TestLiveCloudflareBridgeWorkspaceBenchmark(t *testing.T) {
	if os.Getenv("AR_BENCH_CF_TRANSFER") != "1" {
		t.Skip("set AR_BENCH_CF_TRANSFER=1 to run isolated CF bridge workspace benchmark")
	}
	raw := make([]byte, 16)
	if _, e := rand.Read(raw); e != nil {
		t.Fatal(e)
	}
	prefix := "cf-bench-20261127/" + hex.EncodeToString(raw) + "/"
	objects, e := s3objects.New(s3objects.Options{
		Endpoint:        os.Getenv("AR_S3_ENDPOINT"),
		Bucket:          os.Getenv("AR_S3_BUCKET"),
		AccessKeyID:     os.Getenv("AR_S3_ACCESS_KEY_ID"),
		SecretAccessKey: secret.New(os.Getenv("AR_S3_SECRET_ACCESS_KEY")),
		Region:          os.Getenv("AR_S3_REGION"),
		Prefix:          prefix,
	})
	if e != nil {
		t.Fatal(e)
	}
	cleanupBucket, err := objects.Bind(tenant("t1", "", s1))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		c, end := context.WithTimeout(context.Background(), 60*time.Second)
		defer end()
		page, e := cleanupBucket.List(c, objectstore.ListOptions{})
		if e != nil {
			t.Errorf("benchmark R2 cleanup list failed: %v", e)
			return
		}
		for _, k := range page.Items {
			if _, de := cleanupBucket.Delete(c, k.Key); de != nil {
				t.Errorf("benchmark R2 cleanup delete %s failed: %v", k.Key, de)
			}
		}
	})

	backend, _ := json.Marshal(map[string]any{
		"default": "cf",
		"backends": map[string]any{
			"cf": map[string]any{
				"kind":       "cloudflare",
				"isolation":  "microvm",
				"workspace":  "archive",
				"bridge_url": os.Getenv("AR_CLOUDFLARE_BRIDGE_URL"),
				"runtimes":   []string{"python", "universal"},
			},
		},
	})
	h := newHarness(t, withBackends(string(backend)), withPolicy("t1", map[string]any{
		"sandbox": map[string]any{
			"backend": "cf",
			"quota":   map[string]any{"daily_egress_bytes": 128 << 20},
		},
	}), func(h *harness) { h.directObjects = objects })
	h.secrets.SeedSecret("", "sandbox/cloudflare-bridge-token", os.Getenv("AR_CLOUDFLARE_BRIDGE_TOKEN"))

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	tc := tenant("t1", "ws-cf-bench", s1)
	lease := h.mustAcquire(tc, onWorkspace("ws-cf-bench"), taskID("cf-bench-snap"))
	t.Cleanup(func() {
		_, _ = h.release(tc, lease, false)
	})

	// Populate 19 distinct source files + 1 MiB payload.bin inside CF container.
	setupScript := `python -c "
import os
os.makedirs('src', exist_ok=True)
for i in range(19):
    with open(f'src/mod_{i:02d}.py', 'w') as f:
        f.write(f'def fn_{i}():\n    return \"item-{i}-\" + \"x\" * {256 + i * 64}\n')
with open('payload.bin', 'wb') as f:
    f.write(os.urandom(1 << 20))
"`
	h.mustExec(tc, lease, setupScript)

	broker := h.svc.Broker()
	inner, err := broker.Get(ctx, tc, lease.LeaseId)
	if err != nil {
		t.Fatal(err)
	}
	arch := broker.Archiver()
	opts := archive.TransferOptions{
		Enabled: true,
		OpenTarget: func(ctx context.Context, method string, size int64, target sandbox.TransferTarget) (sandbox.TransferTarget, func() error, error) {
			return broker.OpenTransferTarget(ctx, inner, method, size, target)
		},
		OpenTargets: func(ctx context.Context, method string, items []archive.TransferTargetRequest) ([]sandbox.TransferTarget, func() error, error) {
			return broker.OpenTransferTargets(ctx, inner, method, items)
		},
	}

	t0 := time.Now()
	coldSnap, err := arch.Snapshot(ctx, inner.Box, inner.EnvID, "t1", "ws-cf-bench", 64<<20, lease.LeaseId, opts)
	coldDur := time.Since(t0)
	if err != nil {
		t.Fatalf("cold snapshot failed: %v", err)
	}
	if coldSnap.NewBlobs != 20 {
		t.Fatalf("expected 20 new blobs on cold snapshot, got %d", coldSnap.NewBlobs)
	}

	// Allow >50ms mtime guard for Rust hash_cache hit.
	time.Sleep(80 * time.Millisecond)

	t1 := time.Now()
	unchangedSnap, err := arch.Snapshot(ctx, inner.Box, inner.EnvID, "t1", "ws-cf-bench", 64<<20, lease.LeaseId, opts)
	unchangedDur := time.Since(t1)
	if err != nil {
		t.Fatalf("unchanged snapshot failed: %v", err)
	}
	if unchangedSnap.NewBlobs != 0 {
		t.Fatalf("expected 0 new blobs on unchanged snapshot, got %d", unchangedSnap.NewBlobs)
	}

	h.mustExec(tc, lease, "printf '# updated delta\n' >> src/mod_00.py")
	time.Sleep(80 * time.Millisecond)

	t2 := time.Now()
	deltaSnap, err := arch.Snapshot(ctx, inner.Box, inner.EnvID, "t1", "ws-cf-bench", 64<<20, lease.LeaseId, opts)
	deltaDur := time.Since(t2)
	if err != nil {
		t.Fatalf("delta snapshot failed: %v", err)
	}
	if deltaSnap.NewBlobs != 1 {
		t.Fatalf("expected 1 new blob on delta snapshot, got %d", deltaSnap.NewBlobs)
	}

	otherTC := tenant("t1", "", s2)
	otherLease := h.mustAcquire(otherTC, taskID("cf-bench-restore"))
	t.Cleanup(func() {
		_, _ = h.release(otherTC, otherLease, false)
	})
	otherInner, err := broker.Get(ctx, otherTC, otherLease.LeaseId)
	if err != nil {
		t.Fatal(err)
	}
	restoreOpts := archive.TransferOptions{
		Enabled: true,
		OpenTarget: func(ctx context.Context, method string, size int64, target sandbox.TransferTarget) (sandbox.TransferTarget, func() error, error) {
			return broker.OpenTransferTarget(ctx, otherInner, method, size, target)
		},
		OpenTargets: func(ctx context.Context, method string, items []archive.TransferTargetRequest) ([]sandbox.TransferTarget, func() error, error) {
			return broker.OpenTransferTargets(ctx, otherInner, method, items)
		},
	}
	t3 := time.Now()
	restored, err := arch.Restore(ctx, otherInner.Box, otherInner.EnvID, "t1", "ws-cf-bench", 64<<20, restoreOpts)
	restoreDur := time.Since(t3)
	if err != nil {
		t.Fatalf("restore failed: %v", err)
	}
	if restored == nil || restored.SnapshotID != deltaSnap.SnapshotID {
		t.Fatalf("unexpected restored snapshot: %+v", restored)
	}

	verifyRes, err := h.worker.Exec(ctx, connect.NewRequest(&sandboxv1.ExecRequest{
		Tenant:    otherTC,
		LeaseId:   otherLease.LeaseId,
		Argv:      []string{"sh", "-c", "wc -c < payload.bin && tail -n 1 src/mod_00.py && ls src | wc -l"},
		TimeoutMs: 15000,
	}))
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("restored verify output=%q", string(verifyRes.Msg.Stdout))

	rows := h.rows("t1", `SELECT COUNT(*) AS n, SUM(CASE WHEN acknowledged=1 THEN 1 ELSE 0 END) AS acked, SUM(actual_bytes) AS total_actual FROM sandbox_transfer_usage`)
	if len(rows) != 1 || ledger.Int(rows[0], "n") == 0 || ledger.Int(rows[0], "n") != ledger.Int(rows[0], "acked") {
		t.Fatalf("unsettled transfer accounting rows: %+v", rows)
	}
	summary := fmt.Sprintf("CF_BRIDGE_BENCH cold_20files_ms=%d unchanged_ms=%d delta_1file_ms=%d restore_20files_ms=%d settled_grants=%d settled_actual_bytes=%d",
		coldDur.Milliseconds(), unchangedDur.Milliseconds(), deltaDur.Milliseconds(), restoreDur.Milliseconds(),
		ledger.Int(rows[0], "n"), ledger.Int(rows[0], "total_actual"))
	t.Log(summary)
	fmt.Println(summary)
}
