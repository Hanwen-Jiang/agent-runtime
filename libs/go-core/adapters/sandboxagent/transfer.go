package sandboxagent

import (
	"context"
	"encoding/json"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"net/http"
	"time"
)

func (c Client) transferCall(ctx context.Context, ep Endpoint, path string, body any, out any) error {
	raw, err := json.Marshal(body)
	if err != nil {
		return err
	}
	status, data, err := c.call(ctx, ep, http.MethodPost, path, raw, "application/json", max(c.timeout(), 10*time.Minute))
	if err != nil {
		return err
	}
	if err = RaiseFor(status, data); err != nil {
		return err
	}
	if err = json.Unmarshal(data, out); err != nil {
		return providererr.New(providererr.Transient, "sandbox_bad_response", "invalid transfer response")
	}
	return nil
}
func (c Client) PrepareTransfer(ctx context.Context, ep Endpoint, path string) (sandbox.PreparedTransfer, error) {
	if err := sandbox.CheckPath(path); err != nil {
		return sandbox.PreparedTransfer{}, err
	}
	var out sandbox.PreparedTransfer
	err := c.transferCall(ctx, ep, "/v1/transfer/prepare", map[string]string{"path": path}, &out)
	if err == nil && (out.TransferID == "" || len(out.SHA256) != 64 || out.SizeBytes < 0 || out.SizeBytes > 64<<20) {
		err = providererr.New(providererr.Transient, "sandbox_bad_response", "invalid prepared transfer")
	}
	return out, err
}
func (c Client) CommitTransfer(ctx context.Context, ep Endpoint, p sandbox.PreparedTransfer, key string, target sandbox.TransferTarget) (sandbox.TransferReceipt, error) {
	var out sandbox.TransferReceipt
	err := c.transferCall(ctx, ep, "/v1/transfer/commit", map[string]any{"transfer_id": p.TransferID, "object_key": key, "target": target}, &out)
	if err == nil && (out.ObjectKey != key || out.SHA256 != p.SHA256 || out.SizeBytes != p.SizeBytes) {
		err = providererr.New(providererr.Transient, "transfer_mismatch", "transfer result differs from prepared bytes")
	}
	return out, err
}
func (c Client) ReadTransfer(ctx context.Context, ep Endpoint, id string) ([]byte, error) {
	raw, _ := json.Marshal(map[string]string{"transfer_id": id})
	status, data, err := c.call(ctx, ep, http.MethodPost, "/v1/transfer/read", raw, "application/json", max(c.timeout(), 10*time.Minute))
	if err != nil {
		return nil, err
	}
	if err = RaiseFor(status, data); err != nil {
		return nil, err
	}
	return data, nil
}

func (c Client) ReleaseTransfer(ctx context.Context, ep Endpoint, id string) error {
	var out map[string]any
	return c.transferCall(ctx, ep, "/v1/transfer/release", map[string]string{"transfer_id": id}, &out)
}

func (c Client) RestoreBatch(ctx context.Context, ep Endpoint, id string, maxBytes int64, blocks []sandbox.RestoreBlock) error {
	if blocks == nil {
		blocks = []sandbox.RestoreBlock{}
	}
	var out map[string]any
	return c.transferCall(ctx, ep, "/v1/restore/blocks", map[string]any{"restore_id": id, "max_bytes": maxBytes, "blocks": blocks}, &out)
}
func (c Client) RestoreFinish(ctx context.Context, ep Endpoint, id string, entries []sandbox.FileEntry) error {
	var out map[string]any
	return c.transferCall(ctx, ep, "/v1/restore", map[string]any{"restore_id": id, "entries": entries}, &out)
}

func (c Client) SnapshotManifest(ctx context.Context, ep Endpoint) ([]sandbox.FileEntry, error) {
	var out struct {
		Entries []sandbox.FileEntry `json:"entries"`
	}
	err := c.transferCall(ctx, ep, "/v1/snapshot", map[string]any{}, &out)
	return out.Entries, err
}
func (c Client) PrepareRange(ctx context.Context, ep Endpoint, path string, offset, size int64) (sandbox.PreparedTransfer, error) {
	if err := sandbox.CheckPath(path); err != nil {
		return sandbox.PreparedTransfer{}, err
	}
	var out sandbox.PreparedTransfer
	err := c.transferCall(ctx, ep, "/v1/transfer/prepare", map[string]any{"path": path, "offset": offset, "length": size}, &out)
	if err == nil && (out.TransferID == "" || len(out.SHA256) != 64 || out.SizeBytes != size) {
		err = providererr.New(providererr.Transient, "sandbox_bad_response", "invalid block preparation")
	}
	return out, err
}
