package localsandbox

import (
	"context"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
)

func (b *box) PrepareTransfer(ctx context.Context, envID, path string) (sandbox.PreparedTransfer, error) {
	m, err := b.get(envID)
	if err != nil {
		return sandbox.PreparedTransfer{}, err
	}
	return b.p.client.PrepareTransfer(ctx, b.endpoint(m), path)
}
func (b *box) CommitTransfer(ctx context.Context, envID string, p sandbox.PreparedTransfer, key string, target sandbox.TransferTarget) (sandbox.TransferReceipt, error) {
	m, err := b.get(envID)
	if err != nil {
		return sandbox.TransferReceipt{}, err
	}
	return b.p.client.CommitTransfer(ctx, b.endpoint(m), p, key, target)
}
func (b *box) ReadTransfer(ctx context.Context, envID, id string) ([]byte, error) {
	m, err := b.get(envID)
	if err != nil {
		return nil, err
	}
	return b.p.client.ReadTransfer(ctx, b.endpoint(m), id)
}
func (b *box) ReleaseTransfer(ctx context.Context, envID, id string) error {
	m, err := b.get(envID)
	if err != nil {
		return err
	}
	return b.p.client.ReleaseTransfer(ctx, b.endpoint(m), id)
}
func (b *box) RestoreBatch(ctx context.Context, envID, id string, maxBytes int64, blocks []sandbox.RestoreBlock) error {
	m, err := b.get(envID)
	if err != nil {
		return err
	}
	return b.p.client.RestoreBatch(ctx, b.endpoint(m), id, maxBytes, blocks)
}
func (b *box) RestoreFinish(ctx context.Context, envID, id string, entries []sandbox.FileEntry) error {
	m, err := b.get(envID)
	if err != nil {
		return err
	}
	return b.p.client.RestoreFinish(ctx, b.endpoint(m), id, entries)
}

func (b *box) PrepareRange(ctx context.Context, envID, path string, offset, size int64) (sandbox.PreparedTransfer, error) {
	m, err := b.get(envID)
	if err != nil {
		return sandbox.PreparedTransfer{}, err
	}
	return b.p.client.PrepareRange(ctx, b.endpoint(m), path, offset, size)
}
func (b *box) SnapshotManifest(ctx context.Context, envID string) ([]sandbox.FileEntry, error) {
	m, err := b.get(envID)
	if err != nil {
		return nil, err
	}
	return b.p.client.SnapshotManifest(ctx, b.endpoint(m))
}
