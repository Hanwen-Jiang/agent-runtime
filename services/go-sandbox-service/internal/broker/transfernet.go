package broker

import (
	"context"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/archive"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
	"net"
	"net/url"
	"regexp"
	"strings"
	"sync"
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
	expires := b.ledger.Now().Add(120 * time.Second)
	ids, err := b.ledger.ReserveTransferBatch(ctx, lease.Owner, lease.Backend, lease.EnvID, task, policy.Quota.DailyEgressBytes, []ledger.TransferReservation{{
		Host:    strings.ToLower(u.Hostname()),
		Size:    3 * size,
		Expires: expires,
	}})
	if err != nil {
		return target, close, err
	}
	id := ids[0]
	close = func() error {
		cleanup, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		n, e := bridge.RevokeTransferTarget(cleanup, lease.EnvID, id)
		if e != nil {
			return e
		}
		if e = b.ledger.SettleAndRevokeTransferBatch(cleanup, lease.Owner, map[string]int64{id: n}); e != nil {
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

func (b *Broker) OpenTransferTargets(ctx context.Context, lease *Lease, method string, items []archive.TransferTargetRequest) ([]sandbox.TransferTarget, func() error, error) {
	noop := func() error { return nil }
	if len(items) == 0 {
		return nil, noop, nil
	}
	if len(items) == 1 {
		t, closeOne, err := b.OpenTransferTarget(ctx, lease, method, items[0].SizeBytes, items[0].Target)
		if err != nil {
			return nil, closeOne, err
		}
		return []sandbox.TransferTarget{t}, closeOne, nil
	}
	if lease.Kind != "cloudflare" {
		targets := make([]sandbox.TransferTarget, len(items))
		closes := make([]func() error, 0, len(items))
		closeAll := func() error {
			var first error
			for _, fn := range closes {
				if err := fn(); err != nil && first == nil {
					first = err
				}
			}
			return first
		}
		for i, item := range items {
			t, closeOne, err := b.OpenTransferTarget(ctx, lease, method, item.SizeBytes, item.Target)
			if err != nil {
				_ = closeAll()
				return nil, noop, err
			}
			targets[i] = t
			closes = append(closes, closeOne)
		}
		return targets, closeAll, nil
	}
	if method != "GET" && method != "PUT" {
		return nil, noop, providererr.New(providererr.Invalid, "transfer_target", "invalid scoped storage transfer")
	}
	bridge, ok := lease.Box.(sandbox.ScopedTransferNetwork)
	if !ok || lease.Record == nil {
		return nil, noop, providererr.New(providererr.Invalid, "transfer_gateway_required", "scoped transfer gateway unavailable")
	}
	expires := b.ledger.Now().Add(120 * time.Second)
	reservations := make([]ledger.TransferReservation, len(items))
	for i, item := range items {
		u, err := url.Parse(item.Target.URL)
		if err != nil || u.Scheme != "https" || u.User != nil || u.Fragment != "" || (u.Port() != "" && u.Port() != "443") || !scopedTransferHost(u.Hostname()) || item.SizeBytes < 0 || item.SizeBytes > 64<<20 {
			return nil, noop, providererr.New(providererr.Invalid, "transfer_target", "invalid scoped storage transfer")
		}
		reservations[i] = ledger.TransferReservation{
			Host:    strings.ToLower(u.Hostname()),
			Size:    3 * item.SizeBytes,
			Expires: expires,
		}
	}
	policy, err := b.policies.For(ctx, lease.Owner)
	if err != nil {
		return nil, noop, err
	}
	task, err := b.transferAuditTask(ctx, lease)
	if err != nil {
		return nil, noop, err
	}
	ids, err := b.ledger.ReserveTransferBatch(ctx, lease.Owner, lease.Backend, lease.EnvID, task, policy.Quota.DailyEgressBytes, reservations)
	if err != nil {
		return nil, noop, err
	}
	batchBridge, hasBatch := lease.Box.(sandbox.ScopedTransferBatchNetwork)
	closeBatch := func() error {
		cleanup, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		var usedMap map[string]int64
		if hasBatch {
			var e error
			usedMap, e = batchBridge.RevokeTransferTargets(cleanup, lease.EnvID, ids)
			if e != nil {
				return e
			}
		} else {
			var mu sync.Mutex
			var wg sync.WaitGroup
			var firstErr error
			usedMap = make(map[string]int64, len(ids))
			for _, id := range ids {
				wg.Add(1)
				go func(gid string) {
					defer wg.Done()
					n, e := bridge.RevokeTransferTarget(cleanup, lease.EnvID, gid)
					mu.Lock()
					if e != nil && firstErr == nil {
						firstErr = e
					} else if e == nil {
						usedMap[gid] = n
					}
					mu.Unlock()
				}(id)
			}
			wg.Wait()
			if firstErr != nil {
				return firstErr
			}
		}
		if e := b.ledger.SettleAndRevokeTransferBatch(cleanup, lease.Owner, usedMap); e != nil {
			return e
		}
		if hasBatch {
			if e := batchBridge.AcknowledgeTransferTargets(cleanup, lease.EnvID, ids); e != nil {
				return e
			}
		} else {
			var wg sync.WaitGroup
			var mu sync.Mutex
			var firstErr error
			for _, id := range ids {
				wg.Add(1)
				go func(gid string) {
					defer wg.Done()
					if e := bridge.AcknowledgeTransferTarget(cleanup, lease.EnvID, gid); e != nil {
						mu.Lock()
						if firstErr == nil {
							firstErr = e
						}
						mu.Unlock()
					}
				}(id)
			}
			wg.Wait()
			if firstErr != nil {
				return firstErr
			}
		}
		return b.ledger.AcknowledgeTransferUsageBatch(cleanup, lease.Owner, ids)
	}
	if hasBatch {
		reqs := make([]sandbox.TransferGrantRequest, len(items))
		for i, item := range items {
			reqs[i] = sandbox.TransferGrantRequest{
				ID:        ids[i],
				Task:      task,
				Method:    method,
				SizeBytes: item.SizeBytes,
				Expires:   expires,
				Target:    item.Target,
			}
		}
		targets, err := batchBridge.GrantTransferTargets(ctx, lease.EnvID, reqs)
		if err != nil {
			_ = closeBatch()
			return nil, closeBatch, err
		}
		return targets, closeBatch, nil
	}
	targets := make([]sandbox.TransferTarget, len(items))
	var wg sync.WaitGroup
	var mu sync.Mutex
	var firstErr error
	for i, item := range items {
		wg.Add(1)
		go func(idx int, it archive.TransferTargetRequest) {
			defer wg.Done()
			t, e := bridge.GrantTransferTarget(ctx, lease.EnvID, ids[idx], task, method, it.SizeBytes, expires, it.Target)
			mu.Lock()
			if e != nil && firstErr == nil {
				firstErr = e
			} else if e == nil {
				targets[idx] = t
			}
			mu.Unlock()
		}(i, item)
	}
	wg.Wait()
	if firstErr != nil {
		_ = closeBatch()
		return nil, closeBatch, firstErr
	}
	return targets, closeBatch, nil
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
