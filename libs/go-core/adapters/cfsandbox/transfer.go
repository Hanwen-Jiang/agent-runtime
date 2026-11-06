package cfsandbox

import (
	"context"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
)

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
