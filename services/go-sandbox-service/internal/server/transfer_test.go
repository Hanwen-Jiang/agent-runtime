package server_test

import (
	"bytes"
	"connectrpc.com/connect"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"fmt"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/s3objects"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	sandboxv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1/sandboxv1connect"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/archive"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

type transferObjects struct {
	mu         sync.Mutex
	data       map[string][]byte
	puts, gets int
	fail       bool
	loseAck    bool
}

func objectServer(t *testing.T) (objectstore.Provider, *transferObjects) {
	state := &transferObjects{data: map[string][]byte{}}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		state.mu.Lock()
		defer state.mu.Unlock()
		if r.URL.Query().Get("list-type") != "" {
			w.Header().Set("Content-Type", "application/xml")
			fmt.Fprint(w, "<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>")
			return
		}
		key := r.URL.Path
		switch r.Method {
		case "PUT":
			if state.fail {
				w.WriteHeader(503)
				return
			}
			data, _ := io.ReadAll(r.Body)
			sum := sha256.Sum256(data)
			if checksum := r.Header.Get("x-amz-checksum-sha256"); checksum != "" && checksum != base64.StdEncoding.EncodeToString(sum[:]) {
				w.WriteHeader(400)
				return
			}
			if _, ok := state.data[key]; ok && r.Header.Get("If-None-Match") == "*" {
				w.WriteHeader(412)
				return
			}
			state.puts++
			state.data[key] = data
			if state.loseAck {
				w.WriteHeader(503)
				return
			}
			w.WriteHeader(200)
		case "GET", "HEAD":
			data, ok := state.data[key]
			if !ok {
				w.WriteHeader(404)
				return
			}
			w.Header().Set("Content-Length", fmt.Sprint(len(data)))
			w.WriteHeader(200)
			if r.Method == "GET" {
				state.gets++
				_, _ = w.Write(data)
			}
		case "DELETE":
			delete(state.data, key)
			w.WriteHeader(204)
		default:
			w.WriteHeader(405)
		}
	}))
	t.Cleanup(srv.Close)
	provider, err := s3objects.New(s3objects.Options{Endpoint: srv.URL, Bucket: "objects", AccessKeyID: "test-access", SecretAccessKey: secret.New("test-secret-for-transfer"), Region: "auto"})
	if err != nil {
		t.Fatal(err)
	}
	return provider, state
}
func TestDirectTransferServiceBindsTenantLeaseAndKeepsPreparedBytes(t *testing.T) {
	objects, state := objectServer(t)
	h := newHarness(t, func(h *harness) { h.directObjects = objects })
	h.mkWorkspace("t1", "w1", map[string]string{"report.txt": "original bytes"})
	tc := tenant("t1", "w1", s1)
	lease := h.mustAcquire(tc, onWorkspace("w1"), taskID("task-transfer"))
	ctx := context.Background()
	pre, err := h.worker.PrepareTransfer(ctx, connect.NewRequest(&sandboxv1.PrepareTransferRequest{Tenant: tc, LeaseId: lease.LeaseId, TaskId: "task-transfer", Path: "report.txt"}))
	if err != nil {
		t.Fatal(err)
	}
	h.mustExec(tc, lease, "printf changed > report.txt")
	request := &sandboxv1.CommitTransferRequest{Tenant: tc, LeaseId: lease.LeaseId, TransferId: pre.Msg.Transfer.TransferId}
	other := sandboxv1.CommitTransferRequest{Tenant: tenant("t2", "w1", s1), LeaseId: request.LeaseId, TransferId: request.TransferId}
	if _, err = h.worker.CommitTransfer(ctx, connect.NewRequest(&other)); err == nil {
		t.Fatal("cross tenant transfer accepted")
	}
	state.mu.Lock()
	state.loseAck = true
	state.mu.Unlock()
	result, err := h.worker.CommitTransfer(ctx, connect.NewRequest(request))
	if err != nil {
		t.Fatal(err)
	}
	repeated, err := h.worker.CommitTransfer(ctx, connect.NewRequest(request))
	if err != nil {
		t.Fatal(err)
	}
	if result.Msg.Object.ObjectUri != repeated.Msg.Object.ObjectUri || state.puts != 1 || state.gets != 0 {
		t.Fatal("repeat uploaded twice")
	}
	bucket, _ := objects.Bind(tc)
	got, err := bucket.Get(ctx, result.Msg.Object.ObjectKey)
	if err != nil || string(got.Data) != "original bytes" {
		t.Fatal("uploaded bytes were changed")
	}
}
func TestDirectExecReferencesAndUploadFailurePending(t *testing.T) {
	objects, state := objectServer(t)
	h := newHarness(t, func(h *harness) { h.directObjects = objects })
	tc := tenant("t1", "", s1)
	lease := h.mustAcquire(tc, taskID("task-output"))
	request := &sandboxv1.ExecRequest{Tenant: tc, LeaseId: lease.LeaseId, Argv: []string{"sh", "-c", "head -c 20000 /dev/zero | tr '\\0' A; head -c 12000 /dev/zero | tr '\\0' B >&2"}, TimeoutMs: 30000, OffloadInlineBytes: 256}
	out, err := h.worker.Exec(context.Background(), connect.NewRequest(request))
	if err != nil {
		t.Fatal(err)
	}
	if out.Msg.StdoutObject == nil || out.Msg.StderrObject == nil || len(out.Msg.Stdout) > 512 || len(out.Msg.Stderr) > 512 {
		t.Fatalf("outputs were not externalized: %d %d", len(out.Msg.Stdout), len(out.Msg.Stderr))
	}
	bucket, _ := objects.Bind(tc)
	stored, err := bucket.Get(context.Background(), out.Msg.StdoutObject.ObjectKey)
	if err != nil || !bytes.Equal(stored.Data, bytes.Repeat([]byte("A"), 20000)) {
		t.Fatal("stored stdout mismatch")
	}
	state.mu.Lock()
	state.fail = true
	state.mu.Unlock()
	fallback, err := h.worker.Exec(context.Background(), connect.NewRequest(request))
	if err != nil {
		t.Fatal(err)
	}
	if fallback.Msg.StdoutObject != nil || fallback.Msg.StdoutPending == nil || len(fallback.Msg.Stdout) > 512 || fallback.Msg.StdoutPending.SizeBytes != 20000 {
		t.Fatal("failed upload returned full bytes or lost recovery reference")
	}
	state.mu.Lock()
	state.fail = false
	state.mu.Unlock()
	recovered, err := h.worker.CommitTransfer(context.Background(), connect.NewRequest(&sandboxv1.CommitTransferRequest{Tenant: tc, TaskId: "task-output", TransferId: fallback.Msg.StdoutPending.TransferId}))
	if err != nil || recovered.Msg.Object.SizeBytes != 20000 {
		t.Fatalf("pending output recovery failed: %v", err)
	}
}
func TestDirectArchiveOnlyUploadsMissingAndRestoresBytes(t *testing.T) {
	objects, state := objectServer(t)
	h := newHarness(t, func(h *harness) { h.directObjects = objects; h.backends = localArchive })
	tc := tenant("t1", "w1", s1)
	lease := h.mustAcquire(tc, onWorkspace("w1"), taskID("task-archive"))
	h.mustExec(tc, lease, "printf shared > a; printf shared > b")
	ctx := context.Background()
	broker := h.svc.Broker()
	inner, err := broker.Get(ctx, tc, lease.LeaseId)
	if err != nil {
		t.Fatal(err)
	}
	arch := broker.Archiver()
	snap, err := arch.Snapshot(ctx, inner.Box, inner.EnvID, "t1", "w1", 1<<20, lease.LeaseId, archive.TransferOptions{Enabled: true})
	if err != nil {
		t.Fatal(err)
	}
	if snap.NewBlobs != 1 {
		t.Fatalf("new blobs %d", snap.NewBlobs)
	}
	before := state.puts
	snap2, err := arch.Snapshot(ctx, inner.Box, inner.EnvID, "t1", "w1", 1<<20, lease.LeaseId, archive.TransferOptions{Enabled: true})
	if err != nil {
		t.Fatal(err)
	}
	if snap2.NewBlobs != 0 || state.puts-before != 1 {
		t.Fatal("unchanged snapshot reuploaded content")
	}
	otherTC := tenant("t1", "", s2)
	other := h.mustAcquire(otherTC, taskID("task-restore"))
	target, err := broker.Get(ctx, otherTC, other.LeaseId)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = arch.Restore(ctx, target.Box, target.EnvID, "t1", "w1", 1<<20, archive.TransferOptions{Enabled: true}); err != nil {
		t.Fatal(err)
	}
	if got := h.mustExec(otherTC, other, "cat a b"); strings.TrimSpace(got) != "sharedshared" {
		t.Fatal("restored bytes mismatch")
	}
}

func TestDirectArchiveRestoresEmptyWorkspaceBeforeFirstRepositoryWrite(t *testing.T) {
	for _, directoriesOnly := range []bool{false, true} {
		t.Run(fmt.Sprint("directories_only=", directoriesOnly), func(t *testing.T) {
			objects, _ := objectServer(t)
			h := newHarness(t, func(h *harness) { h.directObjects = objects; h.backends = localArchive })
			ctx := context.Background()
			tc := tenant("t1", "empty-multi", "")
			first := h.mustAcquire(tc, onWorkspace("empty-multi"), taskID("create-empty"))
			if directoriesOnly {
				h.mustExec(tc, first, "mkdir -p empty/nested")
			}
			broker := h.svc.Broker()
			inner, err := broker.Get(ctx, tc, first.LeaseId)
			if err != nil {
				t.Fatal(err)
			}
			arch := broker.Archiver()
			direct := archive.TransferOptions{Enabled: true}
			snapshot, err := arch.Snapshot(ctx, inner.Box, inner.EnvID, "t1", "empty-multi", 1<<20, first.LeaseId, direct)
			if err != nil {
				t.Fatal(err)
			}
			if snapshot.Files != 0 || snapshot.NewBlobs != 0 {
				t.Fatalf("not a zero-blob workspace: %+v", snapshot)
			}
			// The local harness normally uses tar restoration. Explicitly select
			// the direct path used by production Firecracker so [] vs null crosses
			// the real HTTP boundary into the unmodified Rust agent.
			targetTC := tenant("t1", "", "")
			second := h.mustAcquire(targetTC, taskID("attach-first"))
			target, err := broker.Get(ctx, targetTC, second.LeaseId)
			if err != nil {
				t.Fatal(err)
			}
			restored, err := arch.Restore(ctx, target.Box, target.EnvID, "t1", "empty-multi", 1<<20, direct)
			if err != nil {
				t.Fatal(err)
			}
			if restored == nil || restored.SnapshotID != snapshot.SnapshotID {
				t.Fatal("empty snapshot was not restored")
			}
			if directoriesOnly {
				h.mustExec(targetTC, second, "test -d empty/nested")
			}
			h.mustExec(targetTC, second, "mkdir -p repo && git -C repo init -q -b main && printf attached > repo/README")
			if _, err = arch.Snapshot(ctx, target.Box, target.EnvID, "t1", "empty-multi", 1<<20, second.LeaseId, direct); err != nil {
				t.Fatal(err)
			}
			third := h.mustAcquire(targetTC, taskID("read-first"))
			next, err := broker.Get(ctx, targetTC, third.LeaseId)
			if err != nil {
				t.Fatal(err)
			}
			if _, err = arch.Restore(ctx, next.Box, next.EnvID, "t1", "empty-multi", 1<<20, direct); err != nil {
				t.Fatal(err)
			}
			if got := h.mustExec(targetTC, third, "git -C repo rev-parse --is-inside-work-tree && cat repo/README"); got != "true\nattached" {
				t.Fatalf("first repository contents were not preserved: %q", got)
			}
		})
	}
}

func TestDirectArchiveChunksLargeFilesAndUploadsOnlyChangedBlocks(t *testing.T) {
	objects, _ := objectServer(t)
	h := newHarness(t, func(h *harness) { h.directObjects = objects; h.backends = localArchive })
	tc := tenant("t1", "w-large", s1)
	lease := h.mustAcquire(tc, onWorkspace("w-large"), taskID("task-large"))
	h.mustExec(tc, lease, "dd if=/dev/zero of=large.bin bs=1048576 count=68 2>/dev/null")
	ctx := context.Background()
	broker := h.svc.Broker()
	inner, err := broker.Get(ctx, tc, lease.LeaseId)
	if err != nil {
		t.Fatal(err)
	}
	arch := broker.Archiver()
	legacy, err := arch.Snapshot(ctx, inner.Box, inner.EnvID, "t1", "w-large", 80<<20, lease.LeaseId, archive.TransferOptions{Enabled: true})
	if err != nil {
		t.Fatal(err)
	}
	oldEntries, _, err := arch.Manifest(ctx, "t1", legacy)
	if err != nil || len(oldEntries) != 1 || len(oldEntries[0].Chunks) != 0 {
		t.Fatalf("disabled writer produced chunks: %v %v", oldEntries, err)
	}
	arch.ChunkedSnapshots = true
	for i := 0; i < 4; i++ {
		snap, err := arch.Snapshot(ctx, inner.Box, inner.EnvID, "t1", "w-large", 80<<20, lease.LeaseId, archive.TransferOptions{Enabled: true})
		if err != nil {
			t.Fatal(err)
		}
		want := int64(0)
		if i == 0 {
			want = 1
		}
		if snap.NewBlobs != want {
			t.Fatalf("snapshot %d uploaded %d blobs, want %d", i, snap.NewBlobs, want)
		}
		entries, _, err := arch.Manifest(ctx, "t1", snap)
		if err != nil || len(entries) != 1 || len(entries[0].Chunks) != 17 {
			t.Fatalf("chunk manifest lost: %v %v", entries, err)
		}
	}
	h.mustExec(tc, lease, "printf X | dd of=large.bin bs=1 seek=4194304 conv=notrunc 2>/dev/null")
	snap, err := arch.Snapshot(ctx, inner.Box, inner.EnvID, "t1", "w-large", 80<<20, lease.LeaseId, archive.TransferOptions{Enabled: true})
	if err != nil || snap.NewBlobs != 1 {
		t.Fatalf("changed one block: %+v %v", snap, err)
	}
	expected := h.mustExec(tc, lease, "sha256sum large.bin")
	otherTC := tenant("t1", "", s2)
	other := h.mustAcquire(otherTC, taskID("task-large-restore"))
	target, err := broker.Get(ctx, otherTC, other.LeaseId)
	if err != nil {
		t.Fatal(err)
	}
	arch.ChunkedSnapshots = false // Rollback disables writes, not chunk-compatible reads.
	if _, err = arch.Restore(ctx, target.Box, target.EnvID, "t1", "w-large", 80<<20, archive.TransferOptions{Enabled: true}); err != nil {
		t.Fatal(err)
	}
	if got := h.mustExec(otherTC, other, "sha256sum large.bin"); got != expected {
		t.Fatalf("restored file differs: %s != %s", got, expected)
	}
}

func TestDirectTransferJournalSurvivesLostCommitResponseAndRestart(t *testing.T) {
	objects, state := objectServer(t)
	h := newHarness(t, func(h *harness) { h.directObjects = objects })
	tc := tenant("t1", "w1", s1)
	h.mkWorkspace("t1", "w1", map[string]string{"out.txt": "original bytes"})
	lease := h.mustAcquire(tc, onWorkspace("w1"), taskID("task-journal"))
	ctx := context.Background()
	prepare := &sandboxv1.PrepareTransferRequest{Tenant: tc, LeaseId: lease.LeaseId, TaskId: "task-journal", Path: "out.txt", IdempotencyKey: "tool-call-1"}
	pre, err := h.worker.PrepareTransfer(ctx, connect.NewRequest(prepare))
	if err != nil {
		t.Fatal(err)
	}
	// A real HTTP response is consumed and dropped before reaching the caller.
	dropped := false
	client := &http.Client{Transport: transferRoundTrip(func(req *http.Request) (*http.Response, error) {
		resp, err := h.srv.Client().Transport.RoundTrip(req)
		if err == nil && strings.HasSuffix(req.URL.Path, "/CommitTransfer") && !dropped {
			dropped = true
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			return nil, fmt.Errorf("injected lost response")
		}
		return resp, err
	})}
	unreliable := sandboxv1connect.NewSandboxServiceClient(client, h.srv.URL, connect.WithInterceptors(caller("worker", workerToken)))
	if _, err = unreliable.CommitTransfer(ctx, connect.NewRequest(&sandboxv1.CommitTransferRequest{Tenant: tc, LeaseId: lease.LeaseId, TransferId: pre.Msg.Transfer.TransferId})); err == nil || !dropped {
		t.Fatal("response loss not injected")
	}
	h.restart()
	recovered, err := h.worker.CommitTransfer(ctx, connect.NewRequest(&sandboxv1.CommitTransferRequest{Tenant: tc, TaskId: "task-journal", TransferId: pre.Msg.Transfer.TransferId}))
	if err != nil {
		t.Fatal(err)
	}
	if state.puts != 1 {
		t.Fatalf("recovery uploaded %d objects", state.puts)
	}
	h.mustExec(tc, lease, "printf changed > out.txt")
	prepare.LeaseId = ""
	resumed, err := h.worker.PrepareTransfer(ctx, connect.NewRequest(prepare))
	if err != nil || resumed.Msg.Transfer.TransferId != pre.Msg.Transfer.TransferId {
		t.Fatalf("prepare identity changed: %v", err)
	}
	prepare.Path = "different.txt"
	if _, err = h.worker.PrepareTransfer(ctx, connect.NewRequest(prepare)); err == nil {
		t.Fatal("idempotency key accepted changed input")
	}
	for _, bad := range []*sandboxv1.CommitTransferRequest{
		{Tenant: tc, TaskId: "other-task", TransferId: pre.Msg.Transfer.TransferId},
		{Tenant: tenant("t2", "w1", s1), TaskId: "task-journal", TransferId: pre.Msg.Transfer.TransferId},
		{Tenant: tc, TransferId: pre.Msg.Transfer.TransferId},
		{Tenant: tc, LeaseId: "another-lease", TaskId: "task-journal", TransferId: pre.Msg.Transfer.TransferId},
	} {
		if _, err = h.worker.CommitTransfer(ctx, connect.NewRequest(bad)); err == nil {
			t.Fatal("recovery accepted foreign authority")
		}
	}
	bucket, _ := objects.Bind(tc)
	stored, err := bucket.Get(ctx, recovered.Msg.Object.ObjectKey)
	if err != nil || string(stored.Data) != "original bytes" {
		t.Fatal("immutable delivery changed")
	}
}

type transferRoundTrip func(*http.Request) (*http.Response, error)

func (f transferRoundTrip) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
