package diskobjects_test

import (
	"context"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance/objectstoretest"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/diskobjects"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
)

type harness struct {
	provider *diskobjects.Provider
	clock    *testkit.FakeClock
	faults   *testkit.Faults
}

func (h *harness) Provider() objectstore.Provider             { return h.provider }
func (h *harness) Advance(_ context.Context, d time.Duration) { h.clock.Advance(d) }
func (h *harness) InjectFault(op string, c providererr.Category) error {
	h.faults.FailNext(op, c)
	return nil
}
func (h *harness) Fetch(ctx context.Context, url string) ([]byte, error) {
	return h.provider.FetchURL(ctx, url)
}
func (h *harness) Upload(ctx context.Context, url string, data []byte) error {
	return h.provider.UploadURL(ctx, url, data)
}
func (h *harness) RunLifecycle(ctx context.Context) error { return h.provider.RunLifecycle(ctx) }

func TestConformance(t *testing.T) {
	objectstoretest.Run(t, func(t *testing.T) objectstoretest.Harness {
		h := &harness{clock: testkit.NewFakeClock(), faults: testkit.NewFaults()}
		h.provider = conformance.Must(diskobjects.New(diskobjects.Options{
			Root: t.TempDir(), SigningKey: []byte("conformance-signing-key"), Clock: h.clock, Faults: h.faults,
		}))
		t.Cleanup(func() { _ = h.provider.Close() })
		return h
	})
}
