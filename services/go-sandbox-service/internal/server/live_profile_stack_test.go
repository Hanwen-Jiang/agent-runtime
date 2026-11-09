package server_test

import (
	"context"
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/fcsandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/s3objects"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	sandboxv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1"
)

// Opt-in isolated manager + fresh R2 prefix. No model API, production ledger,
// production Sandbox service or existing workspace is used by this proof.
func TestLiveBenchmarkWorkspaceArchiveAndGC(t *testing.T) {
	if os.Getenv("AR_LIVE_FC_PROFILE_STACK") != "1" {
		t.Skip("isolated FC + scoped R2 required")
	}
	ctx := context.Background()
	endpoint := os.Getenv("AR_FC_TEST_URL")
	token, err := os.ReadFile(os.Getenv("AR_FC_TEST_TOKEN_FILE"))
	if err != nil {
		t.Fatal("test credential unavailable")
	}
	var objectConfig struct{ Endpoint, Bucket, AccessKeyID, SecretAccessKey, Region, Prefix string }
	raw, err := os.ReadFile(os.Getenv("AR_FC_OBJECT_CONFIG_FILE"))
	if err != nil || json.Unmarshal(raw, &objectConfig) != nil {
		t.Fatal("test object configuration unavailable")
	}
	objects, err := s3objects.New(s3objects.Options{Endpoint: objectConfig.Endpoint, Bucket: objectConfig.Bucket, AccessKeyID: objectConfig.AccessKeyID, SecretAccessKey: secret.New(objectConfig.SecretAccessKey), Region: objectConfig.Region, Prefix: objectConfig.Prefix})
	if err != nil {
		t.Fatal(err)
	}
	provider, err := fcsandbox.New(fcsandbox.Config{ManagerURL: endpoint, Runtimes: []string{"universal"}, GuestProfile: "benchmark-system", RootDiskMB: 4096, DiskMB: 256, BaseRuntime: os.Getenv("AR_FC_TEST_RUNTIME"), BaseImageDigest: os.Getenv("AR_FC_TEST_DIGEST")}, strings.TrimSpace(string(token)), nil)
	if err != nil {
		t.Fatal(err)
	}
	backend, _ := json.Marshal(map[string]any{"default": "firecracker", "backends": map[string]any{"firecracker": map[string]any{"kind": "firecracker", "workspace": "archive", "isolation": "microvm", "manager_url": endpoint}}})
	h := newHarness(t, withBackends(string(backend)), withPolicy("eval-system-proof", map[string]any{"sandbox": map[string]any{"backend": "firecracker", "idle_timeout_s": 60}}), withWrappedProvider("firecracker", func(sandbox.Provider) sandbox.Provider { return provider }), func(h *harness) { h.directObjects = objects })
	tc := tenant("eval-system-proof", "stack-gc", s1)
	t.Cleanup(func() {
		cleanup, cancel := context.WithTimeout(context.Background(), 120*time.Second)
		defer cancel()
		if _, e := h.svc.Broker().DestroyWorkspace(cleanup, tc.TenantId, tc.WorkspaceId); e != nil {
			t.Error("owned VM cleanup failed")
		}
		if e := h.svc.Broker().Archiver().Delete(cleanup, tc.TenantId, tc.WorkspaceId); e != nil {
			t.Error("owned object cleanup failed")
		}
	})
	first := h.mustAcquire(tc, onWorkspace(tc.WorkspaceId), func(r *sandboxv1.AcquireRequest) { r.Spec.Runtime = "universal" })
	h.mustExec(tc, first, "set -e; git init -q; git config user.email proof@example.invalid; git config user.name proof; printf 'initial\\n' > result.txt; git add result.txt; git commit -qm initial; printf 'latest\\n' > result.txt; python -c \"from pathlib import Path;[(Path('file-%d'%i).write_bytes(bytes([i])*4096)) for i in range(10)]\"")
	if _, err = h.release(tc, first, true); err != nil {
		t.Fatal(err)
	}
	h.clock.Advance(61 * time.Second)
	report, err := h.svc.Reconcile(ctx)
	if err != nil || report.Reaped != 1 || len(report.Errors) > 0 {
		t.Fatalf("GC failed: %+v %v", report, err)
	}
	if h.envState(tc.TenantId, first.LeaseId) != "destroyed" {
		t.Fatal("GC did not retire saved VM")
	}
	second := h.mustAcquire(tc, onWorkspace(tc.WorkspaceId), func(r *sandboxv1.AcquireRequest) { r.Spec.Runtime = "universal" })
	if second.BackendEnvId == first.BackendEnvId {
		t.Fatal("restore did not create a distinct VM")
	}
	if got := h.mustExec(tc, second, "cat result.txt"); got != "latest\n" {
		t.Fatal("latest bytes not restored")
	}
	h.mustExec(tc, second, "python -c \"from pathlib import Path;assert all(Path('file-%d'%i).read_bytes()==bytes([i])*4096 for i in range(10))\"")
	if _, err = h.release(tc, second, false); err != nil {
		t.Fatal(err)
	}
	if _, err = h.svc.Broker().DestroyWorkspace(ctx, tc.TenantId, tc.WorkspaceId); err != nil {
		t.Fatal(err)
	}
	if err = h.svc.Broker().Archiver().Delete(ctx, tc.TenantId, tc.WorkspaceId); err != nil {
		t.Fatal(err)
	}
	t.Logf("real FC GC archive/R2 PUT/new-VM restore/R2 GET passed: %s -> %s; scoped artifacts cleaned", first.BackendEnvId, second.BackendEnvId)
}
