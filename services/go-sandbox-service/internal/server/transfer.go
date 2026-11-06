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
	prepared, err := box.PrepareTransfer(ctx, lease.EnvID, m.GetPath())
	if err != nil {
		return nil, fail(err)
	}
	id, err := newTransferID()
	if err != nil {
		return nil, fail(err)
	}
	h.s.transferMu.Lock()
	defer h.s.transferMu.Unlock()
	now := h.s.opts.Clock.Now()
	for key, v := range h.s.transfers {
		if !v.expires.After(now) {
			delete(h.s.transfers, key)
		}
	}
	if len(h.s.transfers) >= 8192 {
		return nil, fail(providererr.New(providererr.Transient, "transfer_capacity", "too many pending transfers"))
	}
	if h.s.transfers == nil {
		h.s.transfers = map[string]*preparedUpload{}
	}
	h.s.transfers[id] = &preparedUpload{owner: lease.Owner, lease: lease.ID(), key: "deliveries/" + m.GetTaskId() + "/" + id + "/" + name, prepared: prepared, expires: now.Add(5 * time.Minute)}
	return connect.NewResponse(&sandboxv1.PrepareTransferResponse{Transfer: &sandboxv1.PreparedTransfer{TransferId: id, Sha256: prepared.SHA256, SizeBytes: prepared.SizeBytes}}), nil
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
		// A timed-out PUT can still have succeeded. This random key was signed only
		// for these immutable bytes, so a matching object makes the retry idempotent.
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
