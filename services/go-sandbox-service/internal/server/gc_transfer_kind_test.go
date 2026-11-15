package server_test

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

type gcTransferProbe struct {
	mu    sync.Mutex
	puts  int
	proxy bool
	urls  map[string]string
}

type gcTransferObjects struct {
	objectstore.Provider
	probe *gcTransferProbe
}

func (p gcTransferObjects) Bind(tc *commonv1.TenantContext) (objectstore.Store, error) {
	store, err := p.Provider.Bind(tc)
	if err != nil {
		return nil, err
	}
	return gcTransferStore{Store: store, probe: p.probe}, nil
}

type gcTransferStore struct {
	objectstore.Store
	probe *gcTransferProbe
}

func (s gcTransferStore) SignedUpload(ctx context.Context, key string, ttl time.Duration, sum string, size int64) (objectstore.SignedUpload, error) {
	signed, err := s.Store.SignedUpload(ctx, key, ttl, sum, size)
	if err != nil {
		return signed, err
	}
	s.probe.mu.Lock()
	url := fmt.Sprintf("https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/fixture/%d", len(s.probe.urls))
	s.probe.urls[url] = signed.URL
	s.probe.mu.Unlock()
	signed.URL = url
	return signed, nil
}

type gcTransferProvider struct {
	sandbox.Provider
	probe *gcTransferProbe
}

func (p gcTransferProvider) Bind(tc *commonv1.TenantContext) (sandbox.Sandbox, error) {
	box, err := p.Provider.Bind(tc)
	if err != nil {
		return nil, err
	}
	return &gcTransferBox{Sandbox: box, Archive: box.(sandbox.Archive), Transfers: box.(sandbox.Transfers),
		BlockTransfers: box.(sandbox.BlockTransfers), SnapshotTransfers: box.(sandbox.SnapshotTransfers), probe: p.probe}, nil
}

type gcTransferBox struct {
	sandbox.Sandbox
	sandbox.Archive
	sandbox.Transfers
	sandbox.BlockTransfers
	sandbox.SnapshotTransfers
	probe *gcTransferProbe
}

func (b *gcTransferBox) CommitTransfer(ctx context.Context, env string, p sandbox.PreparedTransfer, key string, target sandbox.TransferTarget) (sandbox.TransferReceipt, error) {
	b.probe.mu.Lock()
	b.probe.puts++
	b.probe.proxy = b.probe.proxy || target.Proxy != ""
	localURL := b.probe.urls[target.URL]
	b.probe.mu.Unlock()
	if target.Proxy != "" {
		return sandbox.TransferReceipt{}, providererr.New(providererr.Invalid, "invalid_grants", "host grants require direct HTTPS")
	}
	if localURL == "" {
		return sandbox.TransferReceipt{}, fmt.Errorf("fixture signed target missing")
	}
	// The invariant is checked against the scoped HTTPS contract first. Only
	// the successful test transport is redirected to its local object fixture.
	target.URL = localURL
	return b.Transfers.CommitTransfer(ctx, env, p, key, target)
}

func TestGarbageCollectionPreservesHostTransferKindAndLatestBytes(t *testing.T) {
	objects, _ := objectServer(t)
	probe := &gcTransferProbe{urls: map[string]string{}}
	// Run the real local agent behind the Firecracker registry contract. The
	// transport spy checks the host-only no-proxy invariant without a real VM.
	h := newHarness(t,
		withBackends(`{"default":"firecracker","backends":{"firecracker":{"kind":"firecracker","isolation":"microvm","workspace":"archive","manager_url":"http://127.0.0.1:1","egress_gateway_url":"http://gateway.test:8095"}}}`),
		withPolicy("t1", map[string]any{"sandbox": map[string]any{"backend": "firecracker"}}),
		withGateways("", "http://gateway.test:8095"),
		withWrappedProvider("firecracker", func(p sandbox.Provider) sandbox.Provider { return gcTransferProvider{Provider: p, probe: probe} }),
		func(h *harness) { h.directObjects = gcTransferObjects{Provider: objects, probe: probe} })
	ctx := context.Background()
	tc := tenant("t1", "w1", s1)
	wire := h.mustAcquire(tc, onWorkspace("w1"))
	h.mustExec(tc, wire, "printf 'latest uncommitted bytes' > result.txt")
	b := h.svc.Broker()
	rec, err := b.Ledger().Get(ctx, "t1", wire.LeaseId)
	if err != nil {
		t.Fatal(err)
	}
	h.clock.Advance(11 * time.Minute)
	if _, err = b.DestroyRecord(ctx, *rec, "idle expired", true, false); err != nil {
		t.Fatalf("GC archive failed through the host transfer contract: %v", err)
	}
	if h.envState("t1", wire.LeaseId) != "destroyed" {
		t.Fatal("successful GC did not retire the original environment")
	}
	probe.mu.Lock()
	puts, proxy := probe.puts, probe.proxy
	probe.mu.Unlock()
	if puts == 0 || proxy {
		t.Fatalf("GC did not use direct host transfers: puts=%d proxy=%v", puts, proxy)
	}
	restored := h.mustAcquire(tc, onWorkspace("w1"))
	if got := h.mustExec(tc, restored, "cat result.txt"); got != "latest uncommitted bytes" {
		t.Fatalf("GC restored stale content: %q", got)
	}
	if _, err = h.release(tc, restored, false); err != nil {
		t.Fatal(err)
	}
}
