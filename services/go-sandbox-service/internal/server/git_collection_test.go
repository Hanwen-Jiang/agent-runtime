package server_test

import (
	"bytes"
	"context"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	sandboxv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1"
)

// The official task image may contain its compiler/test tools but no git.
// Snapshot and normal file access still work; only the task environment lacks git.
type gitlessTaskEnvironment struct {
	sandbox.Sandbox
	sandbox.Archive
	sandbox.Browse
	gitCalls atomic.Int32
}

func (s *gitlessTaskEnvironment) Exec(ctx context.Context, id string, argv []string, opts sandbox.ExecOptions) (sandbox.ExecResult, error) {
	if len(argv) > 0 && argv[0] == "git" {
		s.gitCalls.Add(1)
		return sandbox.ExecResult{}, providererr.New(providererr.Invalid, "invalid_argument", "找不到 git")
	}
	return s.Sandbox.Exec(ctx, id, argv, opts)
}

func TestGitCollectionFlushesWriterAndUsesBaseImage(t *testing.T) {
	h := newHarness(t, withBackends(localArchive))
	ctx := context.Background()
	tc := tenant("t1", "w1", s1)
	wire := h.mustAcquire(tc, onWorkspace("w1"), reuse())
	h.mustExec(tc, wire, "git init -q -b main && printf 'original\\n' > a.txt && git add a.txt && git -c user.name=T -c user.email=t@example.test commit -qm initial && printf 'changed\\n' >> a.txt && printf 'untracked\\n' > new.txt")
	expected := h.mustExec(tc, wire, "export GIT_INDEX_FILE=.git/proof.index; git read-tree HEAD && git add -A && git diff --cached --binary --no-color --no-ext-diff --no-renames HEAD --; rm -f .git/proof.index")
	b := h.svc.Broker()
	lease, err := b.Get(ctx, tc, wire.LeaseId)
	if err != nil {
		t.Fatal(err)
	}
	original := lease.Box
	originalIndex, err := original.ReadFile(ctx, wire.BackendEnvId, ".git/index")
	if err != nil {
		t.Fatal(err)
	}
	noGit := &gitlessTaskEnvironment{Sandbox: original, Archive: original.(sandbox.Archive), Browse: original.(sandbox.Browse)}
	lease.Box = noGit
	snapshot, err := b.Archiver().Latest(ctx, "t1", "w1")
	if err != nil {
		t.Fatal(err)
	}
	if snapshot != nil {
		t.Fatal("fixture must contain unarchived work")
	}
	response, err := h.reader.GetDiff(ctx, connect.NewRequest(&sandboxv1.GetDiffRequest{Tenant: tc, WorkspaceId: "w1"}))
	if err != nil {
		t.Fatalf("collection depended on git in the official task image: %v", err)
	}
	diff := response.Msg.GetResponse()
	if !strings.Contains(diff.Diff, "+changed") || !strings.Contains(diff.Diff, "+untracked") {
		t.Fatalf("latest work was not collected: %q", diff.Diff)
	}
	if diff.Diff != expected {
		t.Fatalf("collected patch differs from complete Git output: got %q want %q", diff.Diff, expected)
	}
	indexAfter, indexErr := original.ReadFile(ctx, wire.BackendEnvId, ".git/index")
	if indexErr != nil || !bytes.Equal(originalIndex, indexAfter) {
		t.Fatal("collection modified the writer's index")
	}
	if noGit.gitCalls.Load() != 0 {
		t.Fatal("Git was invoked in the task environment")
	}
	if h.envState("t1", wire.LeaseId) != "ready" {
		t.Fatal("collection destroyed the original writer")
	}
	if data, err := original.ReadFile(ctx, wire.BackendEnvId, "a.txt"); err != nil || string(data) != "original\nchanged\n" {
		t.Fatalf("writer content changed: %q %v", data, err)
	}
	if _, err := original.ReadFile(ctx, wire.BackendEnvId, ".git/agentruntime-diff.index"); err == nil {
		t.Fatal("collection wrote temporary index into task environment")
	}
	rows := h.rows("t1", "SELECT env_ref,state,writable,image FROM sandbox_envs")
	if len(rows) != 2 {
		t.Fatalf("want writer and one collection environment: %+v", rows)
	}
	for _, r := range rows {
		if r["env_ref"] != wire.LeaseId && (r["state"] != "destroyed" || r["writable"] != int64(0) || r["image"] != "") {
			t.Fatalf("collection environment must be default, readonly and cleaned: %+v", r)
		}
	}
	lease.Box = original
	if _, err := h.release(tc, wire, false); err != nil {
		t.Fatal(err)
	}
}

func TestGitCollectionWaitsForBusyWriterInsteadOfTakingStaleSnapshot(t *testing.T) {
	h := newHarness(t, withBackends(localArchive))
	tc := tenant("t1", "w1", s1)
	wire := h.mustAcquire(tc, onWorkspace("w1"), reuse(), busyFor(time.Minute))
	_, err := h.reader.GetDiff(context.Background(), connect.NewRequest(&sandboxv1.GetDiffRequest{Tenant: tc, WorkspaceId: "w1"}))
	if _, c := code(t, err); c != "workspace_busy" {
		t.Fatalf("want busy writer refusal, got %v", err)
	}
	if h.envCount("t1") != 1 {
		t.Fatal("busy writer should not spawn a stale collector")
	}
	if _, err := h.release(tc, wire, false); err != nil {
		t.Fatal(err)
	}
}

func TestGitCollectionAtQuotaReplacesOnlyIdleSameSessionReader(t *testing.T) {
	for _, mode := range []string{"idle", "busy", "other_session", "archiving"} {
		t.Run(mode, func(t *testing.T) {
			h := newHarness(t, withBackends(localArchive), withPolicy("t1", map[string]any{"sandbox": map[string]any{"backend": "local", "quota": map[string]any{"max_concurrent": 4}}}))
			ctx := context.Background()
			writerTC := tenant("t1", "w1", s1)
			readerTC := writerTC
			if mode == "other_session" {
				readerTC = tenant("t1", "w1", s2)
			}
			opts := []acquireOpt{onWorkspace("w1"), readOnly(), reuse()}
			if mode == "busy" {
				opts = append(opts, busyFor(time.Hour))
			}
			ro := h.mustAcquire(readerTC, opts...)
			if mode != "busy" {
				if _, err := h.release(readerTC, ro, true); err != nil {
					t.Fatal(err)
				}
			}
			rw := h.mustAcquire(writerTC, onWorkspace("w1"), reuse())
			h.mustExec(writerTC, rw, "git init -q -b main && printf 'baseline\\n' > result.txt && git add result.txt && git -c user.name=T -c user.email=t@example.test commit -qm initial && printf 'latest\\n' >> result.txt")
			if _, err := h.release(writerTC, rw, true); err != nil {
				t.Fatal(err)
			}
			otherTC := tenant("t1", "w2", s2)
			otherRO := h.mustAcquire(otherTC, onWorkspace("w2"), readOnly(), reuse())
			otherRW := h.mustAcquire(otherTC, onWorkspace("w2"), reuse())
			if _, err := h.release(otherTC, otherRO, true); err != nil {
				t.Fatal(err)
			}
			if _, err := h.release(otherTC, otherRW, true); err != nil {
				t.Fatal(err)
			}
			b := h.svc.Broker()
			var clearClaim func()
			if mode == "archiving" {
				rec, err := b.Ledger().Get(ctx, "t1", ro.LeaseId)
				if err != nil {
					t.Fatal(err)
				}
				held, err := b.Ledger().ClaimArchive(ctx, *rec, time.Hour)
				if err != nil {
					t.Fatal(err)
				}
				clearClaim = func() { _ = b.Ledger().FinishArchive(ctx, held, 0, true) }
			}
			// GetDiff often has no session; derive it from this workspace's writer.
			response, err := h.reader.GetDiff(ctx, connect.NewRequest(&sandboxv1.GetDiffRequest{Tenant: tenant("t1", "w1", ""), WorkspaceId: "w1"}))
			if clearClaim != nil {
				clearClaim()
			}
			if mode == "idle" {
				if err != nil || !strings.Contains(response.Msg.Response.Diff, "+latest") {
					t.Fatalf("full quota collection did not preserve latest work: %v %v", response, err)
				}
				if h.envState("t1", ro.LeaseId) != "destroyed" {
					t.Fatal("idle reader was not replaced")
				}
			} else {
				if _, c := code(t, err); c != "sandbox_quota_exceeded" {
					t.Fatalf("must refuse rather than retire %s reader: %v", mode, err)
				}
				if h.envState("t1", ro.LeaseId) != "ready" {
					t.Fatalf("retired %s reader", mode)
				}
			}
			if h.envState("t1", rw.LeaseId) != "ready" || h.envState("t1", otherRO.LeaseId) != "ready" || h.envState("t1", otherRW.LeaseId) != "ready" {
				t.Fatal("collector changed writer or another workspace")
			}
			if got := h.mustExec(writerTC, rw, "cat result.txt"); got != "baseline\nlatest\n" {
				t.Fatalf("writer changed: %q", got)
			}
			if _, err := b.DestroyWorkspace(ctx, "t1", "w1"); err != nil {
				t.Fatal(err)
			}
			if _, err := b.DestroyWorkspace(ctx, "t1", "w2"); err != nil {
				t.Fatal(err)
			}
		})
	}
}
