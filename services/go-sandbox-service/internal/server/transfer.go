package server

import (
	"connectrpc.com/connect"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	sandboxv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/broker"
	"path"
	"strings"

	"time"
	"unicode"
)

type preparedUpload struct {
	owner, lease, key          string
	id, task, idem, path, name string
	prepared                   sandbox.PreparedTransfer
	expires                    time.Time
	result                     *sandboxv1.ObjectReference
}

func newTransferID() (string, error) {
	b := make([]byte, 16)
	_, err := rand.Read(b)
	return hex.EncodeToString(b), err
}
func safeSegment(s string) bool {
	if strings.ContainsFunc(s, unicode.IsControl) {
		return false
	}
	return s != "" && len(s) <= 255 && s != "." && s != ".." && !strings.ContainsAny(s, "/\\\x00\r\n")
}
func (h *handler) PrepareTransfer(ctx context.Context, req *connect.Request[sandboxv1.PrepareTransferRequest]) (*connect.Response[sandboxv1.PrepareTransferResponse], error) {
	m := req.Msg
	owner := m.GetTenant().GetTenantId()
	if owner == "" || !safeSegment(m.GetTaskId()) {
		return nil, fail(providererr.New(providererr.Denied, "transfer_task_mismatch", "tenant and task required"))
	}
	name := m.GetName()
	if name == "" {
		name = path.Base(m.GetPath())
	}
	if !safeSegment(name) {
		return nil, fail(providererr.New(providererr.Invalid, "invalid_delivery_name", "invalid delivery name"))
	}
	if err := sandbox.CheckPath(m.GetPath()); err != nil {
		return nil, fail(err)
	}
	for _, part := range strings.Split(m.GetPath(), "/") {
		if part == ".git" || part == ".hg" || part == ".svn" {
			return nil, fail(providererr.New(providererr.Invalid, "invalid_delivery_path", "reserved directory"))
		}
	}
	id, err := newTransferID()
	if err != nil {
		return nil, fail(err)
	}
	if key := m.GetIdempotencyKey(); key != "" {
		if len(key) > 1024 {
			return nil, fail(providererr.New(providererr.Invalid, "invalid_idempotency_key", "idempotency key too long"))
		}
		sum := sha256.Sum256([]byte(m.GetTaskId() + "\x00" + key))
		id = hex.EncodeToString(sum[:])
		prior, e := h.loadTransfer(ctx, owner, id)
		if e != nil {
			return nil, fail(e)
		}
		if prior != nil {
			if prior.task != m.GetTaskId() || prior.idem != key || prior.path != m.GetPath() || prior.name != name {
				return nil, fail(providererr.New(providererr.Conflict, "transfer_idempotency_conflict", "transfer inputs changed"))
			}
			return connect.NewResponse(&sandboxv1.PrepareTransferResponse{Transfer: preparedResponse(prior)}), nil
		}
	}
	if m.GetLeaseId() == "" {
		return nil, fail(providererr.New(providererr.NotFound, "transfer_not_found", "no prepared transfer for this invocation"))
	}
	lease, err := h.lease(ctx, m.GetTenant(), m.GetLeaseId())
	if err != nil {
		return nil, fail(err)
	}
	if lease.Record == nil || lease.Record.TaskID != m.GetTaskId() {
		return nil, fail(providererr.New(providererr.Denied, "transfer_task_mismatch", "transfer must belong to original task"))
	}
	box, ok := lease.Box.(sandbox.Transfers)
	if !ok {
		return nil, fail(providererr.New(providererr.Invalid, "transfer_unsupported", "backend has no direct transfer support"))
	}
	prepared, err := box.PrepareTransfer(ctx, lease.EnvID, m.GetPath())
	if err != nil {
		return nil, fail(err)
	}
	retained := false
	defer func() {
		if !retained {
			cleanup, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			_ = box.ReleaseTransfer(cleanup, lease.EnvID, prepared.TransferID)
		}
	}()
	p := &preparedUpload{owner: lease.Owner, id: id, task: m.GetTaskId(), idem: m.GetIdempotencyKey(), lease: lease.ID(), path: m.GetPath(), name: name, key: "deliveries/" + m.GetTaskId() + "/" + id + "/" + name, prepared: prepared, expires: h.s.opts.Clock.Now().Add(10 * time.Minute)}
	retained = true // A lost INSERT acknowledgement may have persisted this identity.
	saved, err := h.storeTransfer(ctx, p)
	if err != nil {
		return nil, fail(err)
	}
	if saved.path != p.path || saved.name != p.name || saved.task != p.task {
		return nil, fail(providererr.New(providererr.Conflict, "transfer_idempotency_conflict", "transfer inputs changed"))
	}
	retained = saved.prepared.TransferID == prepared.TransferID
	return connect.NewResponse(&sandboxv1.PrepareTransferResponse{Transfer: preparedResponse(saved)}), nil
}

func (h *handler) CommitTransfer(ctx context.Context, req *connect.Request[sandboxv1.CommitTransferRequest]) (*connect.Response[sandboxv1.CommitTransferResponse], error) {
	m := req.Msg
	owner := m.GetTenant().GetTenantId()
	if owner == "" || (m.GetLeaseId() == "" && m.GetTaskId() == "") {
		return nil, fail(providererr.New(providererr.Denied, "transfer_task_mismatch", "task or original lease required"))
	}
	p, err := h.loadTransfer(ctx, owner, m.GetTransferId())
	if err != nil {
		return nil, fail(err)
	}
	if p == nil || (m.GetTaskId() != "" && m.GetTaskId() != p.task) || (m.GetLeaseId() != "" && m.GetLeaseId() != p.lease) {
		return nil, fail(providererr.New(providererr.NotFound, "transfer_not_found", "unknown transfer"))
	}
	if h.s.opts.Objects == nil {
		return nil, fail(providererr.New(providererr.Invalid, "transfer_unsupported", "object store unavailable"))
	}
	objects, err := h.s.opts.Objects.Bind(&commonv1.TenantContext{TenantId: owner})
	if err != nil {
		return nil, fail(err)
	}
	head, headErr := objects.Head(ctx, p.key)
	if p.result != nil {
		// A durable receipt outlives both the agent and object retention. It is
		// authority for the original bytes, not proof that the object still
		// exists. Recovery must never register a fresh dangling delivery or
		// silently re-upload a different version of the source file.
		if headErr != nil {
			return nil, fail(headErr)
		}
		if !matchesPreparedObject(head, p.prepared) {
			return nil, fail(providererr.New(providererr.Transient, "transfer_mismatch", "prepared object size differs"))
		}
		return connect.NewResponse(&sandboxv1.CommitTransferResponse{Object: p.result}), nil
	}
	var result *sandboxv1.ObjectReference
	if headErr == nil {
		if !matchesPreparedObject(head, p.prepared) {
			return nil, fail(providererr.New(providererr.Transient, "transfer_mismatch", "prepared object size differs"))
		}
		result = &sandboxv1.ObjectReference{ObjectKey: p.key, ObjectUri: objects.URI(p.key), Sha256: p.prepared.SHA256, SizeBytes: p.prepared.SizeBytes}
	} else {
		if providererr.CategoryOf(headErr) != providererr.NotFound {
			return nil, fail(headErr)
		}
		if !p.expires.After(h.s.opts.Clock.Now()) {
			return nil, fail(providererr.New(providererr.NotFound, "transfer_expired", "prepared bytes expired before upload"))
		}
		lease, e := h.b().Get(ctx, m.GetTenant(), p.lease)
		if e != nil {
			return nil, fail(e)
		}
		if lease.Record == nil || lease.Record.TaskID != p.task {
			return nil, fail(providererr.New(providererr.Denied, "transfer_task_mismatch", "original lease task changed"))
		}
		result, err = h.upload(ctx, lease, p.prepared, p.key)
		if err != nil {
			return nil, fail(err)
		}
	}
	if err = h.recordTransferResult(ctx, p, result); err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&sandboxv1.CommitTransferResponse{Object: result}), nil
}

func (h *handler) upload(ctx context.Context, lease *broker.Lease, p sandbox.PreparedTransfer, key string) (*sandboxv1.ObjectReference, error) {
	box, ok := lease.Box.(sandbox.Transfers)
	if !ok || h.s.opts.Objects == nil {
		return nil, providererr.New(providererr.Invalid, "transfer_unsupported", "direct transfer unavailable")
	}
	objects, err := h.s.opts.Objects.Bind(&commonv1.TenantContext{TenantId: lease.Owner})
	if err != nil {
		return nil, err
	}
	signed, err := objects.SignedUpload(ctx, key, 5*time.Minute, p.SHA256, p.SizeBytes)
	if err != nil {
		return nil, err
	}
	proxy, close, err := h.b().OpenTransferNetwork(ctx, lease, signed.URL)
	if err != nil {
		return nil, err
	}
	receipt, uploadErr := box.CommitTransfer(ctx, lease.EnvID, p, key, sandbox.TransferTarget{URL: signed.URL, Headers: signed.Headers, Proxy: proxy})
	closeErr := close()
	if uploadErr != nil {
		// A timed-out PUT can still have succeeded. ObjectStore.SignedUpload is
		// explicitly write-once and binds the body checksum and byte length.
		// R2 enforcement (including equal-length tampering) is verified by
		// TestLiveSignedUploadSecurity; diskobjects verifies the body itself.
		// Therefore this random key's existence proves the only signed bytes
		// arrived. Do not trust an agent-supplied digest or re-read bytes through
		// the Worker. Providers lacking this guarantee must reject SignedUpload.
		if head, headErr := objects.Head(ctx, key); headErr != nil || !matchesPreparedObject(head, p) {
			return nil, uploadErr
		}
		receipt = sandbox.TransferReceipt{ObjectKey: key, SHA256: p.SHA256, SizeBytes: p.SizeBytes}
	}
	if closeErr != nil {
		return nil, closeErr
	}
	head, err := objects.Head(ctx, key)
	if err != nil {
		return nil, err
	}
	if !matchesPreparedObject(head, p) || receipt.SizeBytes != p.SizeBytes || receipt.SHA256 != p.SHA256 {
		_, _ = objects.Delete(ctx, key)
		return nil, providererr.New(providererr.Transient, "transfer_mismatch", "uploaded object differs from prepared bytes")
	}
	_ = box.ReleaseTransfer(ctx, lease.EnvID, p.TransferID)
	return &sandboxv1.ObjectReference{ObjectKey: key, ObjectUri: objects.URI(key), Sha256: p.SHA256, SizeBytes: p.SizeBytes}, nil
}

// Some stores return a trusted write-time digest; a contradictory digest is
// always an integrity failure. S3/R2 SignedUpload currently exposes no digest
// in ObjectInfo, so that case relies on the checksum-bound, write-once URL for
// this unique key (STOR-SEC), not on content length as a cryptographic proof.
func matchesPreparedObject(head objectstore.ObjectInfo, p sandbox.PreparedTransfer) bool {
	return head.Size == p.SizeBytes && (head.SHA256 == "" || head.SHA256 == p.SHA256)
}
