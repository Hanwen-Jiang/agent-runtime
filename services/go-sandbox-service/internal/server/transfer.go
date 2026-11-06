package server

import (
	"connectrpc.com/connect"
	"context"
	"crypto/rand"
	"encoding/hex"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	sandboxv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/broker"
	"path"
	"strings"
	"sync"
	"time"
	"unicode"
)

type preparedUpload struct {
	mu                sync.Mutex
	owner, lease, key string
	prepared          sandbox.PreparedTransfer
	expires           time.Time
	result            *sandboxv1.ObjectReference
	release           func(context.Context) error
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
	lease, err := h.lease(ctx, m.GetTenant(), m.GetLeaseId())
	if err != nil {
		return nil, fail(err)
	}
	if lease.Record == nil || m.GetTaskId() != lease.Record.TaskID || !safeSegment(m.GetTaskId()) {
		return nil, fail(providererr.New(providererr.Denied, "transfer_task_mismatch", "transfer must belong to leased task"))
	}
	name := m.GetName()
	if name == "" {
		name = path.Base(m.GetPath())
	}
	if !safeSegment(name) {
		return nil, fail(providererr.New(providererr.Invalid, "invalid_delivery_name", "invalid delivery name"))
	}
	if err = sandbox.CheckPath(m.GetPath()); err != nil {
		return nil, fail(err)
	}
	for _, part := range strings.Split(m.GetPath(), "/") {
		if part == ".git" || part == ".hg" || part == ".svn" {
			return nil, fail(providererr.New(providererr.Invalid, "invalid_delivery_path", "reserved directory"))
		}
	}
	box, ok := lease.Box.(sandbox.Transfers)
	if !ok {
		return nil, fail(providererr.New(providererr.Invalid, "transfer_unsupported", "backend has no direct transfer support"))
	}
	now := h.s.opts.Clock.Now()
	h.s.transferMu.Lock()
	var expired, expiredHere []*preparedUpload
	for key, v := range h.s.transfers {
		if !v.expires.After(now) {
			delete(h.s.transfers, key)
			if v.lease == lease.ID() {
				expiredHere = append(expiredHere, v)
			} else {
				expired = append(expired, v)
			}
		}
	}
	full := len(h.s.transfers) >= 8192
	h.s.transferMu.Unlock()
	// Agent expiry is an independent safety net; eager release must not hold the
	// global table lock or wait on another VM's in-flight upload.
	if len(expired) > 0 {
		go releaseExpiredUploads(expired)
	}
	// Free this VM's expired slots before asking it to prepare more bytes.
	releaseExpiredUploads(expiredHere)
	if full {
		return nil, fail(providererr.New(providererr.Transient, "transfer_capacity", "too many pending transfers"))
	}
	id, err := newTransferID()
	if err != nil {
		return nil, fail(err)
	}
	prepared, err := box.PrepareTransfer(ctx, lease.EnvID, m.GetPath())
	if err != nil {
		return nil, fail(err)
	}
	release := func(cleanup context.Context) error {
		return box.ReleaseTransfer(cleanup, lease.EnvID, prepared.TransferID)
	}
	retained := false
	defer func() {
		if !retained {
			cleanup, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			_ = release(cleanup)
		}
	}()
	h.s.transferMu.Lock()
	defer h.s.transferMu.Unlock()
	if len(h.s.transfers) >= 8192 {
		return nil, fail(providererr.New(providererr.Transient, "transfer_capacity", "too many pending transfers"))
	}
	if h.s.transfers == nil {
		h.s.transfers = map[string]*preparedUpload{}
	}
	h.s.transfers[id] = &preparedUpload{owner: lease.Owner, lease: lease.ID(), key: "deliveries/" + m.GetTaskId() + "/" + id + "/" + name,
		prepared: prepared, expires: h.s.opts.Clock.Now().Add(5 * time.Minute), release: release}
	retained = true
	return connect.NewResponse(&sandboxv1.PrepareTransferResponse{Transfer: &sandboxv1.PreparedTransfer{TransferId: id, Sha256: prepared.SHA256, SizeBytes: prepared.SizeBytes}}), nil
}
func releaseExpiredUploads(expired []*preparedUpload) {
	if len(expired) == 0 {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	for _, p := range expired {
		p.mu.Lock()
		if p.release != nil {
			_ = p.release(ctx)
		}
		p.mu.Unlock()
	}
}

func (h *handler) CommitTransfer(ctx context.Context, req *connect.Request[sandboxv1.CommitTransferRequest]) (*connect.Response[sandboxv1.CommitTransferResponse], error) {
	m := req.Msg
	lease, err := h.lease(ctx, m.GetTenant(), m.GetLeaseId())
	if err != nil {
		return nil, fail(err)
	}
	h.s.transferMu.Lock()
	p := h.s.transfers[m.GetTransferId()]
	h.s.transferMu.Unlock()
	if p == nil || p.owner != lease.Owner || p.lease != lease.ID() || !p.expires.After(h.s.opts.Clock.Now()) {
		return nil, fail(providererr.New(providererr.NotFound, "transfer_not_found", "unknown or expired transfer"))
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.result == nil {
		p.result, err = h.upload(ctx, lease, p.prepared, p.key)
		if err != nil {
			return nil, fail(err)
		}
	}
	return connect.NewResponse(&sandboxv1.CommitTransferResponse{Object: p.result}), nil
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
		if head, headErr := objects.Head(ctx, key); headErr != nil || head.Size != p.SizeBytes {
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
	if head.Size != p.SizeBytes || receipt.SizeBytes != p.SizeBytes || receipt.SHA256 != p.SHA256 {
		_, _ = objects.Delete(ctx, key)
		return nil, providererr.New(providererr.Transient, "transfer_mismatch", "uploaded object differs from prepared bytes")
	}
	_ = box.ReleaseTransfer(ctx, lease.EnvID, p.TransferID)
	return &sandboxv1.ObjectReference{ObjectKey: key, ObjectUri: objects.URI(key), Sha256: p.SHA256, SizeBytes: p.SizeBytes}, nil
}
