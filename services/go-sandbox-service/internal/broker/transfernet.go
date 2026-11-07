package broker

import (
	"context"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
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
	task, err := b.transferAuditTask(ctx, lease)
	if err != nil {
		return target, close, err
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

// Internal workspace reads have no model Task. The egress gateway resolves
// grants through sandbox_envs.task_id, so persist a per-lease audit identity
// before issuing a transfer grant. Never use an empty tenant-wide identity or
// overwrite a real Task attached concurrently. This creates no tasks/usage row.
func (b *Broker) transferAuditTask(ctx context.Context, lease *Lease) (string, error) {
	if lease.Record == nil || lease.Record.Owner != lease.Owner {
		return "", providererr.New(providererr.Denied, "transfer_lease_required", "storage transfer requires an authorized lease")
	}
	if lease.Record.TaskID != "" {
		return lease.Record.TaskID, nil
	}
	task := "workspace-transfer-" + lease.ID()
	err := b.ledger.Tx(ctx, lease.Owner, false, func(ctx context.Context, tx relational.Tx) error {
		if _, err := tx.Execute(ctx, `UPDATE sandbox_envs SET task_id=$1 WHERE env_ref=$2 AND state='ready' AND task_id=''`, task, lease.ID()); err != nil {
			return err
		}
		row, found, err := tx.QueryOne(ctx, `SELECT task_id,state FROM sandbox_envs WHERE env_ref=$1`, lease.ID())
		if err != nil {
			return err
		}
		if !found || ledger.Str(row, "state") != "ready" || ledger.Str(row, "task_id") != task {
			return providererr.New(providererr.Conflict, "transfer_lease_changed", "storage transfer lease no longer matches its audit identity")
		}
		return nil
	})
	if err != nil {
		return "", err
	}
	lease.Record.TaskID = task
	return task, nil
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
		policy, e := b.policies.For(ctx, lease.Owner)
		if e != nil {
			return "", close, e
		}
		spec, _ := b.reg.Spec(lease.Backend)
		vars, e := b.creds.ExecEnv(ctx, *lease.Record, spec, policy.Quota)
		if e != nil {
			return "", close, e
		}
		proxy = vars["HTTPS_PROXY"]
	}
	if proxy == "" || lease.Record == nil {
		return "", close, providererr.New(providererr.Invalid, "transfer_gateway_required", "storage transfer requires an audited gateway")
	}
	task, err := b.transferAuditTask(ctx, lease)
	if err != nil {
		return "", close, err
	}
	id, err := b.ledger.CreateTransferGrant(ctx, lease.Owner, task, strings.ToLower(u.Hostname()))
	if err != nil {
		return "", close, err
	}
	return proxy, func() error {
		cleanup, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		return b.ledger.RevokeTransferGrant(cleanup, lease.Owner, id)
	}, nil
}
