package broker

import (
	"context"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/registry"
	"net"
	"net/url"
	"strings"
	"time"
)

func (b *Broker) OpenTransferNetwork(ctx context.Context, lease *Lease, rawURL string) (string, func() error, error) {
	close := func() error { return nil }
	if lease.Kind == "firecracker" || lease.Kind == "local" {
		return "", close, nil
	}
	u, err := url.Parse(rawURL)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.Fragment != "" || (u.Port() != "" && u.Port() != "443") || net.ParseIP(u.Hostname()) != nil || strings.Contains(u.Hostname(), "*") {
		return "", close, providererr.New(providererr.Invalid, "transfer_target", "storage requires an exact HTTPS hostname")
	}
	proxy := lease.Env["HTTPS_PROXY"]
	if proxy == "" && b.creds != nil && lease.Record != nil {
		spec, _ := b.reg.Spec(lease.Backend)
		vars, e := b.creds.ExecEnv(ctx, *lease.Record, spec, registry.Quota{})
		if e != nil {
			return "", close, e
		}
		proxy = vars["HTTPS_PROXY"]
	}
	if proxy == "" || lease.Record == nil || lease.Record.TaskID == "" {
		return "", close, providererr.New(providererr.Invalid, "transfer_gateway_required", "storage transfer requires an audited gateway")
	}
	id, err := b.ledger.CreateTransferGrant(ctx, lease.Owner, lease.Record.TaskID, strings.ToLower(u.Hostname()))
	if err != nil {
		return "", close, err
	}
	return proxy, func() error {
		cleanup, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		return b.ledger.RevokeTransferGrant(cleanup, lease.Owner, id)
	}, nil
}
