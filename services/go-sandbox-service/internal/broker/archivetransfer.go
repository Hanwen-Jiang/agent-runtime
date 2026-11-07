package broker

import (
	"context"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/archive"
)

func (b *Broker) transferOptions(lease *Lease) archive.TransferOptions {
	return archive.TransferOptions{Enabled: lease.Kind == "firecracker" || lease.Kind == "cloudflare" || b.HasGateway(lease.Backend),
		OpenTarget: func(ctx context.Context, method string, size int64, target sandbox.TransferTarget) (sandbox.TransferTarget, func() error, error) {
			return b.OpenTransferTarget(ctx, lease, method, size, target)
		}}
}
