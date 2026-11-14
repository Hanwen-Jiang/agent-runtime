package server_test

import (
	"context"
	"io"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/broker"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
)

type pausedArchive struct {
	sandbox.Sandbox
	sandbox.Archive
	entered chan struct{}
	proceed chan struct{}
}

func (p *pausedArchive) ExportArchive(ctx context.Context, id string, out io.Writer, max int64) (int64, error) {
	close(p.entered)
	select {
	case <-ctx.Done():
		return 0, ctx.Err()
	case <-p.proceed:
	}
	return p.Archive.ExportArchive(ctx, id, out, max)
}

func TestReconcileCannotDestroyAnArchiveInFlight(t *testing.T) {
	for _, release := range []bool{false, true} {
		name := "snapshot"
		if release {
			name = "release"
		}
		t.Run(name, func(t *testing.T) {
			h := newHarness(t, withBackends(localArchive))
			ctx := context.Background()
			tc := tenant("t1", "w1", "")
			wire := h.mustAcquire(tc, onWorkspace("w1"))
			h.mustExec(tc, wire, "printf 'retained' > result.txt")
			b := h.svc.Broker()
			original, err := b.Get(ctx, tc, wire.LeaseId)
			if err != nil {
				t.Fatal(err)
			}
			copy := *original
			pause := &pausedArchive{Sandbox: copy.Box, Archive: copy.Box.(sandbox.Archive), entered: make(chan struct{}), proceed: make(chan struct{})}
			copy.Box = pause
			done := make(chan error, 1)
			go func() {
				if release {
					done <- b.Release(ctx, &copy, false, 0)
				} else {
					_, err := b.Snapshot(ctx, &copy)
					done <- err
				}
			}()
			<-pause.entered
			// A different broker has no process-local lock in common with the caller.
			other := broker.New(broker.Options{Registry: b.Registry(), Policies: b.Policies(), Ledger: b.Ledger(), Archiver: b.Archiver(), Store: h.store, Credentials: broker.NewCredentials(b.Registry(), "", "")})
			h.clock.Advance(11 * time.Minute)
			report, err := other.Reconcile(ctx, 10*time.Minute, time.Hour)
			state := h.envState("t1", wire.LeaseId)
			close(pause.proceed)
			archiveErr := <-done
			if err != nil || report.Reaped != 0 || state != "ready" {
				t.Fatalf("reconcile destroyed active archive: state=%s report=%+v err=%v archive=%v", state, report, err, archiveErr)
			}
			if archiveErr != nil {
				t.Fatal(archiveErr)
			}
			if release {
				if h.envState("t1", wire.LeaseId) != "destroyed" {
					t.Fatal("release must finish destruction")
				}
			} else {
				if _, err := h.release(tc, wire, false); err != nil {
					t.Fatal(err)
				}
			}
			latest, err := b.Archiver().Latest(ctx, "t1", "w1")
			if err != nil || latest == nil {
				t.Fatalf("archive lost: %v", err)
			}
			restored := h.mustAcquire(tc, onWorkspace("w1"))
			if got := h.mustExec(tc, restored, "cat result.txt"); got != "retained" {
				t.Fatalf("data not preserved: %q", got)
			}
			if _, err := h.release(tc, restored, false); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestCancelledArchiveReleasesClaimAndCanRetry(t *testing.T) {
	h := newHarness(t, withBackends(localArchive))
	ctx := context.Background()
	tc := tenant("t1", "w1", "")
	wire := h.mustAcquire(tc, onWorkspace("w1"), busyFor(time.Hour))
	b := h.svc.Broker()
	lease, err := b.Get(ctx, tc, wire.LeaseId)
	if err != nil {
		t.Fatal(err)
	}
	copy := *lease
	pause := &pausedArchive{Sandbox: copy.Box, Archive: copy.Box.(sandbox.Archive), entered: make(chan struct{}), proceed: make(chan struct{})}
	copy.Box = pause
	cancelled, cancel := context.WithCancel(ctx)
	done := make(chan error, 1)
	go func() { _, e := b.Snapshot(cancelled, &copy); done <- e }()
	select {
	case <-pause.entered:
	case e := <-done:
		t.Fatalf("caller BusyFor must not reject its own snapshot: %v", e)
	}
	cancel()
	if err := <-done; err == nil {
		t.Fatal("expected cancellation")
	}
	rec, err := b.Ledger().Get(ctx, "t1", wire.LeaseId)
	if err != nil || rec.ArchiveToken != "" || rec.ArchiveUntil != "" {
		t.Fatalf("claim leaked after cancel: %+v %v", rec, err)
	}
	if _, err := b.Snapshot(ctx, lease); err != nil {
		t.Fatalf("retry failed: %v", err)
	}
	if _, err := h.release(tc, wire, false); err != nil {
		t.Fatal(err)
	}
}

func TestStaleReaperCannotClaimRenewedEnvironment(t *testing.T) {
	h := newHarness(t, withBackends(localArchive))
	ctx := context.Background()
	tc := tenant("t1", "w1", "")
	wire := h.mustAcquire(tc, onWorkspace("w1"))
	b := h.svc.Broker()
	old, err := b.Ledger().Get(ctx, "t1", wire.LeaseId)
	if err != nil {
		t.Fatal(err)
	}
	h.clock.Advance(time.Minute)
	if _, err := h.release(tc, wire, true); err != nil {
		t.Fatal(err)
	}
	if _, err := b.DestroyRecord(ctx, *old, "idle expired", true, false); err == nil {
		t.Fatal("stale GC must fail its CAS")
	}
	if h.envState("t1", wire.LeaseId) != "ready" {
		t.Fatal("stale GC destroyed renewed environment")
	}
	if _, err := h.release(tc, wire, false); err != nil {
		t.Fatal(err)
	}
}

func TestSameSessionAcquireCannotReuseAnArchivingEnvironment(t *testing.T) {
	h := newHarness(t, withBackends(localArchive))
	ctx := context.Background()
	tc := tenant("t1", "w1", s1)
	wire := h.mustAcquire(tc, onWorkspace("w1"), reuse())
	b := h.svc.Broker()
	lease, err := b.Get(ctx, tc, wire.LeaseId)
	if err != nil {
		t.Fatal(err)
	}
	task := "workspace-transfer-" + wire.LeaseId
	rec, err := b.Ledger().Touch(ctx, *lease.Record, ledger.TouchArgs{TaskID: task})
	if err != nil {
		t.Fatal(err)
	}
	lease.Record = &rec
	grant, err := b.Ledger().CreateTransferGrant(ctx, "t1", task, "blobs.example.test")
	if err != nil {
		t.Fatal(err)
	}
	copy := *lease
	pause := &pausedArchive{Sandbox: copy.Box, Archive: copy.Box.(sandbox.Archive), entered: make(chan struct{}), proceed: make(chan struct{})}
	copy.Box = pause
	done := make(chan error, 1)
	go func() { _, err := b.Snapshot(ctx, &copy); done <- err }()
	<-pause.entered
	other := broker.New(broker.Options{Registry: b.Registry(), Policies: b.Policies(), Ledger: b.Ledger(), Archiver: b.Archiver(), Store: h.store, Credentials: broker.NewCredentials(b.Registry(), "", "")})
	_, acquireErr := other.Acquire(ctx, tc, broker.AcquireArgs{Spec: sandbox.Spec{WorkspaceID: "w1"}, Writable: true, Reuse: true, TaskID: "new-task"})
	// A late Touch with a record read before the archive started must also fail.
	_, touchErr := b.Ledger().Touch(ctx, rec, ledger.TouchArgs{TaskID: "new-task"})
	grants, grantErr := b.Ledger().GrantsForTask(ctx, "t1", task)
	close(pause.proceed)
	archiveErr := <-done
	if acquireErr == nil || touchErr == nil || grantErr != nil || len(grants) != 1 || grants[0].GrantID != grant {
		t.Fatalf("archive reused or grant revoked: acquire=%v touch=%v grants=%+v err=%v", acquireErr, touchErr, grants, grantErr)
	}
	if archiveErr != nil {
		t.Fatal(archiveErr)
	}
	if _, err := h.release(tc, wire, false); err != nil {
		t.Fatal(err)
	}
}
