package server_test

import (
	"context"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
)

func TestArchiveFailurePreservesUnpublishedWritesForRetry(t *testing.T) {
	for _, mode := range []string{"release", "forced_gc"} {
		for _, category := range []providererr.Category{providererr.Invalid, providererr.Denied, providererr.NotFound, providererr.Transient} {
			t.Run(mode+"/"+category.String(), func(t *testing.T) {
				h := newHarness(t, withBackends(localArchive))
				ctx := context.Background()
				tc := tenant("t1", "w1", "")
				wire := h.mustAcquire(tc, onWorkspace("w1"))
				h.mustExec(tc, wire, "printf 'unpublished work' > result.txt")
				b := h.svc.Broker()
				h.faults["local"].FailNext("export_archive", category)
				var err error
				if mode == "release" {
					_, err = h.release(tc, wire, false)
				} else {
					rec, e := b.Ledger().Get(ctx, "t1", wire.LeaseId)
					if e != nil {
						t.Fatal(e)
					}
					_, err = b.DestroyRecord(ctx, *rec, "max lifetime", true, true)
				}
				if err == nil {
					t.Fatal("archive failure must remain visible")
				}
				rec, e := b.Ledger().Get(ctx, "t1", wire.LeaseId)
				if e != nil || rec.State != "ready" || rec.ArchiveToken != "" {
					t.Fatalf("archive failure discarded writer or leaked claim: %+v %v", rec, e)
				}
				data, e := h.box("local", "t1").ReadFile(ctx, wire.BackendEnvId, "result.txt")
				if e != nil || string(data) != "unpublished work" {
					t.Fatalf("unpublished content lost: %q %v", data, e)
				}
				// The next archive attempt succeeds without re-running any task/model.
				if _, e := h.release(tc, wire, false); e != nil {
					t.Fatalf("archive retry: %v", e)
				}
				restored := h.mustAcquire(tc, onWorkspace("w1"))
				if got := h.mustExec(tc, restored, "cat result.txt"); got != "unpublished work" {
					t.Fatalf("retry snapshot lost content: %q", got)
				}
				if _, e := h.release(tc, restored, false); e != nil {
					t.Fatal(e)
				}
			})
		}
	}
}
