package broker

import (
	"context"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/registry"
	"net"
	"net/url"
	"regexp"
	"strings"
	"time"
)

// OpenTransferTarget uses the same signed-object protocol on every backend.
// Cloudflare only substitutes an internal per-object gateway capability; no
// public host is allowed in the tenant container's network configuration.
func (b *Broker) OpenTransferTarget(ctx context.Context, lease *Lease, method string, size int64, target sandbox.TransferTarget) (sandbox.TransferTarget, func() error, error) {
	if lease.Kind != "cloudflare" {
		proxy, close, err := b.OpenTransferNetwork(ctx, lease, target.URL)
		target.Proxy = proxy
		return target, close, err
	}
	close := func() error { return nil }
	u, err := url.Parse(target.URL)
	if err != nil || u.Scheme != "https" || u.User != nil || u.Fragment != "" || (u.Port() != "" && u.Port() != "443") || !scopedTransferHost(u.Hostname()) || size < 0 || size > 64<<20 || (method != "GET" && method != "PUT") {
		return target, close, providererr.New(providererr.Invalid, "transfer_target", "invalid scoped storage transfer")
	}
	bridge, ok := lease.Box.(sandbox.ScopedTransferNetwork)
	if !ok || lease.Record == nil {
		return target, close, providererr.New(providererr.Invalid, "transfer_gateway_required", "scoped transfer gateway unavailable")
	}
	policy, err := b.policies.For(ctx, lease.Owner)
	if err != nil {
		return target, close, err
	}
	task := lease.Record.TaskID
	if task == "" {
		// Internal archive reads can acquire an authorized workspace lease
		// without a model Task. Keep a lease-scoped audit identity; it grants
		// no user network access and never enters model_usage.
		task = "workspace-" + lease.ID()
	}
	id, err := b.ledger.CreateTransferGrant(ctx, lease.Owner, task, strings.ToLower(u.Hostname()))
	if err != nil {
		return target, close, err
	}
	expires := b.ledger.Now().Add(120 * time.Second)
	if err = b.ledger.ReserveTransferBytes(ctx, lease.Owner, id, lease.Backend, lease.EnvID, task, 3*size, policy.Quota.DailyEgressBytes, expires); err != nil {
		_ = b.ledger.RevokeTransferGrant(context.WithoutCancel(ctx), lease.Owner, id)
		return target, close, err
	}
	close = func() error {
		cleanup, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		n, e := bridge.RevokeTransferTarget(cleanup, lease.EnvID, id)
		if e != nil {
			return e
		}
		if e = b.ledger.SettleTransferBytes(cleanup, lease.Owner, id, n); e != nil {
			return e
		}
		if e = b.ledger.RevokeTransferGrant(cleanup, lease.Owner, id); e != nil {
			return e
		}
		if e = bridge.AcknowledgeTransferTarget(cleanup, lease.EnvID, id); e != nil {
			return e
		}
		return b.ledger.AcknowledgeTransferUsage(cleanup, lease.Owner, id)
	}
	transformed, err := bridge.GrantTransferTarget(ctx, lease.EnvID, id, task, method, size, expires, target)
	if err != nil {
		_ = close()
		return target, close, err
	}
	return transformed, close, nil
}

func (b *Broker) reconcileTransferUsage(ctx context.Context) error {
	directory, err := b.ledger.Directory(ctx, false)
	if err != nil {
		return err
	}
	owners := map[string]bool{}
	for _, entry := range directory {
		if owners[entry.Owner] {
			continue
		}
		owners[entry.Owner] = true
		rows, e := b.ledger.ExpiredTransferUsage(ctx, entry.Owner)
		if e != nil {
			return e
		}
		for _, row := range rows {
			provider, e := b.reg.Provider(ctx, ledger.Str(row, "backend"))
			if e != nil {
				return e
			}
			box, e := provider.Bind(&commonv1.TenantContext{TenantId: entry.Owner})
			if e != nil {
				return e
			}
			bridge, ok := box.(sandbox.ScopedTransferNetwork)
			if !ok {
				continue
			}
			id := ledger.Str(row, "grant_id")
			if row["actual_bytes"] == nil {
				bytes, e := bridge.RevokeTransferTarget(ctx, ledger.Str(row, "env_id"), id)
				if e != nil {
					return e
				}
				if e = b.ledger.SettleTransferBytes(ctx, entry.Owner, id, bytes); e != nil {
					return e
				}
			}
			if e = b.ledger.RevokeTransferGrant(ctx, entry.Owner, id); e != nil {
				return e
			}
			if e = bridge.AcknowledgeTransferTarget(ctx, ledger.Str(row, "env_id"), id); e != nil {
				return e
			}
			if e = b.ledger.AcknowledgeTransferUsage(ctx, entry.Owner, id); e != nil {
				return e
			}
		}
	}
	return nil
}

// A temporary sandbox grant authorizes a hostname, not a signed URL path. Only
// provider hostnames that identify an account or bucket may receive this grant;
// a shared path-style S3 endpoint would also authorize another tenant's bucket.
var accountR2Host = regexp.MustCompile(`^[a-f0-9]{32}(?:\.(?:eu|fedramp))?\.r2\.cloudflarestorage\.com$`)
var bucketS3Host = regexp.MustCompile(`^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\.s3(?:[.-][a-z]{2}(?:-[a-z]+)+-[0-9]+)?\.amazonaws\.com$`)

func scopedTransferHost(host string) bool {
	host = strings.ToLower(host)
	if strings.Contains(host, "..") || strings.HasSuffix(host, ".") {
		return false
	}
	return accountR2Host.MatchString(host) || bucketS3Host.MatchString(host)
}

func (b *Broker) OpenTransferNetwork(ctx context.Context, lease *Lease, rawURL string) (string, func() error, error) {
	close := func() error { return nil }
	if lease.Kind == "firecracker" || lease.Kind == "local" {
		return "", close, nil
	}
	u, err := url.Parse(rawURL)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.Fragment != "" || (u.Port() != "" && u.Port() != "443") || net.ParseIP(u.Hostname()) != nil || strings.Contains(u.Hostname(), "*") {
		return "", close, providererr.New(providererr.Invalid, "transfer_target", "storage requires an exact HTTPS hostname")
	}
	if !scopedTransferHost(u.Hostname()) {
		return "", close, providererr.New(providererr.Invalid, "transfer_authority_unscoped", "storage transfer hostname must identify a supported account or bucket")
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
