package broker

import (
	"context"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/archive"
)

func (b *Broker) transferOptions(lease *Lease) archive.TransferOptions {
	return archive.TransferOptions{Enabled: lease.Kind == "firecracker" || b.HasGateway(lease.Backend),
		OpenNetwork: func(ctx context.Context, url string) (string, func() error, error) {
			return b.OpenTransferNetwork(ctx, lease, url)
		}}
}
