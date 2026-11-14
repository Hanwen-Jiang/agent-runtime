package server_test

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/broker"
)

func TestIdleExpiredSessionsReuseAtFullQuotaWithoutAnotherEnvironment(t *testing.T) {
	h := newHarness(t, withBackends(localArchive), withPolicy("t1", map[string]any{"sandbox": map[string]any{"backend": "local", "idle_timeout_s": 60, "quota": map[string]any{"max_concurrent": 4}}}))
	ctx := context.Background()
	b := h.svc.Broker()
	tenants := []*commonv1.TenantContext{tenant("t1", "w1", s1), tenant("t1", "w2", s2)}
	writers := make([]string, 2)
	for i, tc := range tenants {
		ro := h.mustAcquire(tc, onWorkspace(tc.WorkspaceId), readOnly(), reuse())
		if _, err := h.release(tc, ro, true); err != nil {
			t.Fatal(err)
		}
		rw := h.mustAcquire(tc, onWorkspace(tc.WorkspaceId), reuse())
		writers[i] = rw.LeaseId
		if _, err := h.release(tc, rw, true); err != nil {
			t.Fatal(err)
		}
	}
	if h.envCount("t1") != 4 {
		t.Fatal("fixture needs two read-only/write pairs")
	}
	h.clock.Advance(61 * time.Second)
	stale, err := b.Ledger().Get(ctx, "t1", writers[0])
	if err != nil {
		t.Fatal(err)
	}
	type result struct {
		lease *broker.Lease
		err   error
	}
	results := make([]result, 2)
	var wg sync.WaitGroup
	for i, tc := range tenants {
		wg.Add(1)
		go func(i int, tc *commonv1.TenantContext) {
			defer wg.Done()
			results[i].lease, results[i].err = b.Acquire(ctx, tc, broker.AcquireArgs{Spec: sandbox.Spec{WorkspaceID: tc.WorkspaceId}, Writable: true, Reuse: true})
		}(i, tc)
	}
	wg.Wait()
	for i, r := range results {
		if r.err != nil || r.lease == nil || !r.lease.Reused || r.lease.ID() != writers[i] {
			t.Fatalf("session %d allocated at full quota instead of renewing its existing environment: %+v %v", i, r.lease, r.err)
		}
	}
	// GC may have inspected the idle-expired row before reuse won its CAS.
	if _, err := b.DestroyRecord(ctx, *stale, "idle expired", true, false); providererr.CategoryOf(err) != providererr.Conflict {
		t.Fatalf("stale GC did not lose: %v", err)
	}
	for i, tc := range tenants {
		ro, err := b.Acquire(ctx, tc, broker.AcquireArgs{Spec: sandbox.Spec{WorkspaceID: tc.WorkspaceId}, Writable: false, Reuse: true})
		if err != nil || ro.ID() != writers[i] {
			t.Fatalf("read must prefer the renewed write environment: %+v %v", ro, err)
		}
		if _, err := b.DestroyWorkspace(ctx, "t1", tc.WorkspaceId); err != nil {
			t.Fatal(err)
		}
	}
	if h.envCount("t1") != 4 {
		t.Fatal("unexpected replacement environment")
	}
}

func TestExpiredMaxLifetimeAndOtherSessionsAreNeverReused(t *testing.T) {
	h := newHarness(t, withBackends(localArchive), withPolicy("t1", map[string]any{"sandbox": map[string]any{"backend": "local", "idle_timeout_s": 60, "max_lifetime_s": 120}}))
	ctx := context.Background()
	tc := tenant("t1", "w1", s1)
	wire := h.mustAcquire(tc, onWorkspace("w1"), reuse())
	b := h.svc.Broker()
	if _, err := h.release(tc, wire, true); err != nil {
		t.Fatal(err)
	}
	h.clock.Advance(61 * time.Second)
	if found, err := b.Ledger().FindReusable(ctx, "t1", "w1", s2, []string{"local"}, true); err != nil || found != nil {
		t.Fatalf("different session reused old environment: %+v %v", found, err)
	}
	h.clock.Advance(60 * time.Second)
	if found, err := b.Ledger().FindReusable(ctx, "t1", "w1", s1, []string{"local"}, true); err != nil || found != nil {
		t.Fatalf("max lifetime ignored: %+v %v", found, err)
	}
	next, err := h.acquire(tc, onWorkspace("w1"), reuse())
	if err != nil || next.LeaseId == wire.LeaseId {
		t.Fatalf("must create a new environment after max lifetime: %+v %v", next, err)
	}
	if _, err := h.release(tc, next, false); err != nil {
		t.Fatal(err)
	}
}

func TestIdleExpiredMissingBackendIsNotRenewedOrReturned(t *testing.T) {
	h := newHarness(t, withPolicy("t1", map[string]any{"sandbox": map[string]any{"backend": "local", "idle_timeout_s": 60}}))
	ctx := context.Background()
	tc := tenant("t1", "w1", s1)
	wire := h.mustAcquire(tc, onWorkspace("w1"), reuse())
	b := h.svc.Broker()
	if _, err := h.release(tc, wire, true); err != nil {
		t.Fatal(err)
	}
	if err := h.box("local", "t1").Destroy(ctx, wire.BackendEnvId); err != nil {
		t.Fatal(err)
	}
	h.clock.Advance(61 * time.Second)
	before, err := b.Ledger().Get(ctx, "t1", wire.LeaseId)
	if err != nil {
		t.Fatal(err)
	}
	for range 2 {
		got, err := h.acquire(tc, onWorkspace("w1"), reuse())
		if err == nil || got != nil {
			t.Fatalf("missing backend was returned as healthy: %+v %v", got, err)
		}
		after, err := b.Ledger().Get(ctx, "t1", wire.LeaseId)
		if err != nil || after.ExpiresAt != before.ExpiresAt || after.ArchiveToken != "" {
			t.Fatalf("failed health renewed or leaked claim: %+v %v", after, err)
		}
	}
	if _, err := h.svc.Reconcile(ctx); err != nil {
		t.Fatal(err)
	}
	if h.envState("t1", wire.LeaseId) != "lost" {
		t.Fatal("authoritative backend listing should mark missing environment lost")
	}
}
