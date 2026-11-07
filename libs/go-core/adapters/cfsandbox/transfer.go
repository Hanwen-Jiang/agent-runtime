package cfsandbox

import (
	"context"
	"encoding/json"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/sandboxagent"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"net/http"
	"time"
)

func (b *box) GrantTransferTarget(ctx context.Context, envID, id, task, method string, size int64, expires time.Time, target sandbox.TransferTarget) (sandbox.TransferTarget, error) {
	if _, err := b.endpoint(envID); err != nil {
		return sandbox.TransferTarget{}, err
	}
	status, body, err := b.p.call(ctx, http.MethodPut, "/v1/sandboxes/"+envID+"/transfer-grants/"+id, b.owner, map[string]any{"task_id": task, "method": method, "size_bytes": size, "expires_at_ms": expires.UnixMilli(), "target": target})
	if err != nil {
		return sandbox.TransferTarget{}, err
	}
	if err = sandboxagent.RaiseFor(status, body); err != nil {
		return sandbox.TransferTarget{}, err
	}
	var result struct {
		URL string `json:"url"`
	}
	if json.Unmarshal(body, &result) != nil || result.URL != "http://ar-transfer.internal/"+id {
		return sandbox.TransferTarget{}, providererr.New(providererr.Transient, "transfer_bad_response", "invalid scoped transfer URL")
	}
	return sandbox.TransferTarget{URL: result.URL}, nil
}
func (b *box) RevokeTransferTarget(ctx context.Context, envID, id string) (int64, error) {
	// Revocation is also allowed after environment teardown, using retained
	// owner-bound transfer accounting. It never reopens the environment.
	if !envIDPattern.MatchString(envID) {
		return 0, providererr.New(providererr.Invalid, "invalid_env", "invalid environment")
	}
	status, body, err := b.p.call(ctx, http.MethodDelete, "/v1/sandboxes/"+envID+"/transfer-grants/"+id, b.owner, nil)
	if err != nil {
		return 0, err
	}
	if err = sandboxagent.RaiseFor(status, body); err != nil {
		return 0, err
	}
	var result struct {
		Bytes   int64 `json:"bytes"`
		Revoked bool  `json:"revoked"`
	}
	if json.Unmarshal(body, &result) != nil || result.Bytes < 0 || !result.Revoked {
		return 0, providererr.New(providererr.Transient, "transfer_bad_response", "invalid transfer accounting")
	}
	return result.Bytes, nil
}
func (b *box) AcknowledgeTransferTarget(ctx context.Context, envID, id string) error {
	if !envIDPattern.MatchString(envID) {
		return providererr.New(providererr.Invalid, "invalid_env", "invalid environment")
	}
	status, body, err := b.p.call(ctx, http.MethodPost, "/v1/sandboxes/"+envID+"/transfer-grants/"+id+"/ack", b.owner, nil)
	if err != nil {
		return err
	}
	return sandboxagent.RaiseFor(status, body)
}

func (b *box) PrepareTransfer(ctx context.Context, envID, path string) (sandbox.PreparedTransfer, error) {
	ep, err := b.endpoint(envID)
	if err != nil {
		return sandbox.PreparedTransfer{}, err
	}
	return b.p.agent.PrepareTransfer(ctx, ep, path)
}
func (b *box) CommitTransfer(ctx context.Context, envID string, p sandbox.PreparedTransfer, key string, target sandbox.TransferTarget) (sandbox.TransferReceipt, error) {
	ep, err := b.endpoint(envID)
	if err != nil {
		return sandbox.TransferReceipt{}, err
	}

	return b.p.agent.CommitTransfer(ctx, ep, p, key, target)
}
func (b *box) ReadTransfer(ctx context.Context, envID, id string) ([]byte, error) {
	ep, err := b.endpoint(envID)
	if err != nil {
		return nil, err
	}
	return b.p.agent.ReadTransfer(ctx, ep, id)
}

func (b *box) ReleaseTransfer(ctx context.Context, envID, id string) error {
	ep, err := b.endpoint(envID)
	if err != nil {
		return err
	}
	return b.p.agent.ReleaseTransfer(ctx, ep, id)
}

func (b *box) RestoreBatch(ctx context.Context, envID, id string, maxBytes int64, blocks []sandbox.RestoreBlock) error {
	ep, err := b.endpoint(envID)
	if err != nil {
		return err
	}
	return b.p.agent.RestoreBatch(ctx, ep, id, maxBytes, blocks)
}
func (b *box) RestoreFinish(ctx context.Context, envID, id string, entries []sandbox.FileEntry) error {
	ep, err := b.endpoint(envID)
	if err != nil {
		return err
	}
	return b.p.agent.RestoreFinish(ctx, ep, id, entries)
}

func (b *box) PrepareRange(ctx context.Context, envID, path string, offset, size int64) (sandbox.PreparedTransfer, error) {
	ep, err := b.endpoint(envID)
	if err != nil {
		return sandbox.PreparedTransfer{}, err
	}
	return b.p.agent.PrepareRange(ctx, ep, path, offset, size)
}
func (b *box) SnapshotManifest(ctx context.Context, envID string) ([]sandbox.FileEntry, error) {
	ep, err := b.endpoint(envID)
	if err != nil {
		return nil, err
	}
	return b.p.agent.SnapshotManifest(ctx, ep)
}
