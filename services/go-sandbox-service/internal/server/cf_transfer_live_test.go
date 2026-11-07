package server_test

import (
	"connectrpc.com/connect"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/s3objects"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	sandboxv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
	"os"
	"strings"
	"testing"
	"time"
)

func TestLiveCloudflareServiceDirectOutputAndQuota(t *testing.T) {
	if os.Getenv("AR_LIVE_CF_TRANSFER") != "1" {
		t.Skip("explicit isolated live CF/R2 acceptance required")
	}
	raw := make([]byte, 16)
	if _, e := rand.Read(raw); e != nil {
		t.Fatal(e)
	}
	objects, e := s3objects.New(s3objects.Options{Endpoint: os.Getenv("AR_S3_ENDPOINT"), Bucket: os.Getenv("AR_S3_BUCKET"), AccessKeyID: os.Getenv("AR_S3_ACCESS_KEY_ID"), SecretAccessKey: secret.New(os.Getenv("AR_S3_SECRET_ACCESS_KEY")), Region: os.Getenv("AR_S3_REGION"), Prefix: "cf-service-accept/" + hex.EncodeToString(raw) + "/"})
	if e != nil {
		t.Fatal(e)
	}
	cleanupBucket, cleanupErr := objects.Bind(tenant("t1", "", s1))
	if cleanupErr != nil {
		t.Fatal(cleanupErr)
	}
	t.Cleanup(func() {
		c, end := context.WithTimeout(context.Background(), 30*time.Second)
		defer end()
		page, e := cleanupBucket.List(c, objectstore.ListOptions{})
		if e != nil {
			t.Error("test object inventory failed")
			return
		}
		for _, k := range page.Items {
			if _, e = cleanupBucket.Delete(c, k.Key); e != nil {
				t.Error("test object cleanup failed")
			}
		}
	})
	backend, _ := json.Marshal(map[string]any{"default": "cf", "backends": map[string]any{"cf": map[string]any{"kind": "cloudflare", "isolation": "microvm", "workspace": "archive", "bridge_url": os.Getenv("AR_CLOUDFLARE_BRIDGE_URL"), "runtimes": []string{"python", "universal"}}}})
	h := newHarness(t, withBackends(string(backend)), withPolicy("t1", map[string]any{"sandbox": map[string]any{"backend": "cf", "quota": map[string]any{"daily_egress_bytes": 60000}}}), func(h *harness) { h.directObjects = objects })
	h.secrets.SeedSecret("", "sandbox/cloudflare-bridge-token", os.Getenv("AR_CLOUDFLARE_BRIDGE_TOKEN"))
	tc := tenant("t1", "", s1)
	lease := h.mustAcquire(tc, taskID("cf-accept-task"))
	t.Cleanup(func() {
		if _, e := h.release(tc, lease, false); e != nil {
			t.Error("CF service lease cleanup", e)
		}
	})
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	result, e := h.worker.Exec(ctx, connect.NewRequest(&sandboxv1.ExecRequest{Tenant: tc, LeaseId: lease.LeaseId, Argv: []string{"python", "-c", "print('x'*20000,end='')"}, TimeoutMs: 10000, OffloadInlineBytes: 256}))
	if e != nil {
		t.Fatal(e)
	}
	if len(result.Msg.Stdout) > 256+64 || result.Msg.StdoutObject == nil || result.Msg.StdoutPending != nil {
		t.Fatalf("output did not offload: excerpt=%d object=%v pending=%v", len(result.Msg.Stdout), result.Msg.StdoutObject != nil, result.Msg.StdoutPending != nil)
	}
	bucket, e := objects.Bind(tc)
	if e != nil {
		t.Fatal(e)
	}
	key := result.Msg.StdoutObject.ObjectKey
	t.Cleanup(func() {
		ctx, end := context.WithTimeout(context.Background(), 20*time.Second)
		defer end()
		if _, e := bucket.Delete(ctx, key); e != nil {
			t.Error("object cleanup")
		}
	})
	obj, e := bucket.Get(ctx, key)
	if e != nil || string(obj.Data) != strings.Repeat("x", 20000) {
		t.Fatal("direct object bytes differ")
	}
	rows := h.rows("t1", `SELECT reserved_bytes,actual_bytes,acknowledged FROM sandbox_transfer_usage`)
	if len(rows) != 1 || ledger.Int(rows[0], "reserved_bytes") != 60000 || ledger.Int(rows[0], "actual_bytes") != 20000 || ledger.Int(rows[0], "acknowledged") != 1 {
		t.Fatalf("accounting: %v", rows)
	}
	// Remaining 40k cannot admit another 60k conservative retry reservation.
	denied, e := h.worker.Exec(ctx, connect.NewRequest(&sandboxv1.ExecRequest{Tenant: tc, LeaseId: lease.LeaseId, Argv: []string{"python", "-c", "print('y'*20000,end='')"}, TimeoutMs: 10000, OffloadInlineBytes: 256}))
	if e != nil {
		t.Fatal(e)
	}
	if len(denied.Msg.Stdout) > 256+64 || denied.Msg.StdoutObject != nil || denied.Msg.StdoutPending == nil {
		t.Fatal("quota failure must return bounded pending output")
	}
	rows = h.rows("t1", `SELECT egress_bytes FROM sandbox_usage`)
	if len(rows) != 1 || ledger.Int(rows[0], "egress_bytes") != 20000 {
		t.Fatalf("failed admission changed actual charge: %v", rows)
	}
	t.Log("real CF Service→agent→gateway→R2: bounded output, exact 20k actual/60k reservation settlement, quota denial and pending output verified")
}
