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
	result, err := h.worker.CommitTransfer(ctx, connect.NewRequest(request))
	if err != nil {
		t.Fatal(err)
	}
	repeated, err := h.worker.CommitTransfer(ctx, connect.NewRequest(request))
	if err != nil {
		t.Fatal(err)
	}
	if result.Msg.Object.ObjectUri != repeated.Msg.Object.ObjectUri || state.puts != 1 {
		t.Fatal("repeat uploaded twice")
	}
	bucket, _ := objects.Bind(tc)
	got, err := bucket.Get(ctx, result.Msg.Object.ObjectKey)
	if err != nil || string(got.Data) != "original bytes" {
		t.Fatal("uploaded bytes were changed")
	}
}
func TestDirectExecReferencesAndUploadFailureFallback(t *testing.T) {
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
	if fallback.Msg.StdoutObject != nil || !bytes.Equal(fallback.Msg.Stdout, bytes.Repeat([]byte("A"), 20000)) {
		t.Fatal("failed upload lost stdout")
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
	if _, err = arch.Restore(ctx, target.Box, target.EnvID, "t1", "w-large", 80<<20, archive.TransferOptions{Enabled: true}); err != nil {
		t.Fatal(err)
	}
	if got := h.mustExec(otherTC, other, "sha256sum large.bin"); got != expected {
		t.Fatalf("restored file differs: %s != %s", got, expected)
	}
}
