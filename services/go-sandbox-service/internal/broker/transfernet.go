package broker

import (
	"context"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/registry"
	"net"
	"net/url"
	"regexp"
	"strings"
	"time"
)

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
