package cfsandbox

import (
	"context"
	"encoding/json"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/sandboxagent"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"net/http"
	"sync"
	"time"
)

const batchConcurrencyFallback = 8

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

func runBounded(count, limit int, fn func(int) error) error {
	if count == 0 {
		return nil
	}
	if limit <= 0 || limit > count {
		limit = count
	}
	sem := make(chan struct{}, limit)
	var (
		wg       sync.WaitGroup
		mu       sync.Mutex
		firstErr error
	)
	for i := 0; i < count; i++ {
		wg.Add(1)
		sem <- struct{}{}
		go func(idx int) {
			defer wg.Done()
			defer func() { <-sem }()
			if err := fn(idx); err != nil {
				mu.Lock()
				if firstErr == nil {
					firstErr = err
				}
				mu.Unlock()
			}
		}(i)
	}
	wg.Wait()
	return firstErr
}

func (b *box) GrantTransferTargets(ctx context.Context, envID string, reqs []sandbox.TransferGrantRequest) ([]sandbox.TransferTarget, error) {
	if len(reqs) == 0 {
		return nil, nil
	}
	if _, err := b.endpoint(envID); err != nil {
		return nil, err
	}
	if len(reqs) == 1 {
		t, err := b.GrantTransferTarget(ctx, envID, reqs[0].ID, reqs[0].Task, reqs[0].Method, reqs[0].SizeBytes, reqs[0].Expires, reqs[0].Target)
		if err != nil {
			return nil, err
		}
		return []sandbox.TransferTarget{t}, nil
	}
	items := make([]map[string]any, len(reqs))
	for i, r := range reqs {
		items[i] = map[string]any{
			"id":            r.ID,
			"task_id":       r.Task,
			"method":        r.Method,
			"size_bytes":    r.SizeBytes,
			"expires_at_ms": r.Expires.UnixMilli(),
			"target":        r.Target,
		}
	}
	status, body, err := b.p.call(ctx, http.MethodPost, "/v1/sandboxes/"+envID+"/transfer-grants:batch", b.owner, map[string]any{
		"action": "grant",
		"items":  items,
	})
	if err != nil {
		return nil, err
	}
	if status == http.StatusNotFound || status == http.StatusMethodNotAllowed {
		out := make([]sandbox.TransferTarget, len(reqs))
		err := runBounded(len(reqs), batchConcurrencyFallback, func(i int) error {
			r := reqs[i]
			t, e := b.GrantTransferTarget(ctx, envID, r.ID, r.Task, r.Method, r.SizeBytes, r.Expires, r.Target)
			if e == nil {
				out[i] = t
			}
			return e
		})
		return out, err
	}
	if err = sandboxagent.RaiseFor(status, body); err != nil {
		return nil, err
	}
	var result struct {
		URLs map[string]string `json:"urls"`
	}
	if json.Unmarshal(body, &result) != nil || len(result.URLs) < len(reqs) {
		return nil, providererr.New(providererr.Transient, "transfer_bad_response", "invalid scoped batch transfer URLs")
	}
	out := make([]sandbox.TransferTarget, len(reqs))
	for i, r := range reqs {
		u := result.URLs[r.ID]
		if u != "http://ar-transfer.internal/"+r.ID {
			return nil, providererr.New(providererr.Transient, "transfer_bad_response", "invalid scoped transfer URL")
		}
		out[i] = sandbox.TransferTarget{URL: u}
	}
	return out, nil
}

func (b *box) RevokeTransferTargets(ctx context.Context, envID string, ids []string) (map[string]int64, error) {
	if len(ids) == 0 {
		return map[string]int64{}, nil
	}
	if !envIDPattern.MatchString(envID) {
		return nil, providererr.New(providererr.Invalid, "invalid_env", "invalid environment")
	}
	if len(ids) == 1 {
		n, err := b.RevokeTransferTarget(ctx, envID, ids[0])
		if err != nil {
			return nil, err
		}
		return map[string]int64{ids[0]: n}, nil
	}
	status, body, err := b.p.call(ctx, http.MethodPost, "/v1/sandboxes/"+envID+"/transfer-grants:batch", b.owner, map[string]any{
		"action": "revoke",
		"ids":    ids,
	})
	if err != nil {
		return nil, err
	}
	if status == http.StatusNotFound || status == http.StatusMethodNotAllowed {
		var mu sync.Mutex
		out := make(map[string]int64, len(ids))
		err := runBounded(len(ids), batchConcurrencyFallback, func(i int) error {
			n, e := b.RevokeTransferTarget(ctx, envID, ids[i])
			if e == nil {
				mu.Lock()
				out[ids[i]] = n
				mu.Unlock()
			}
			return e
		})
		return out, err
	}
	if err = sandboxagent.RaiseFor(status, body); err != nil {
		return nil, err
	}
	var result struct {
		Bytes   map[string]int64 `json:"bytes"`
		Revoked bool             `json:"revoked"`
	}
	if json.Unmarshal(body, &result) != nil || !result.Revoked || result.Bytes == nil {
		return nil, providererr.New(providererr.Transient, "transfer_bad_response", "invalid batch transfer accounting")
	}
	out := make(map[string]int64, len(ids))
	for _, id := range ids {
		n, ok := result.Bytes[id]
		if !ok || n < 0 {
			return nil, providererr.New(providererr.Transient, "transfer_bad_response", "invalid batch transfer accounting")
		}
		out[id] = n
	}
	return out, nil
}

func (b *box) AcknowledgeTransferTargets(ctx context.Context, envID string, ids []string) error {
	if len(ids) == 0 {
		return nil
	}
	if !envIDPattern.MatchString(envID) {
		return providererr.New(providererr.Invalid, "invalid_env", "invalid environment")
	}
	if len(ids) == 1 {
		return b.AcknowledgeTransferTarget(ctx, envID, ids[0])
	}
	status, body, err := b.p.call(ctx, http.MethodPost, "/v1/sandboxes/"+envID+"/transfer-grants:batch", b.owner, map[string]any{
		"action": "ack",
		"ids":    ids,
	})
	if err != nil {
		return err
	}
	if status == http.StatusNotFound || status == http.StatusMethodNotAllowed {
		return runBounded(len(ids), batchConcurrencyFallback, func(i int) error {
			return b.AcknowledgeTransferTarget(ctx, envID, ids[i])
		})
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
