// Package localingress 是内置令牌表的 Ingress（开发实现）。语义见 providers/ingress。
//
// 令牌是本地签名的 base64url(JSON 载荷).base64url(HMAC-SHA256)。限流按（租户、route）计滑动窗口。
package localingress

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
)

var slug = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)

// Options 是构造参数；Limit 为 0 时按每分钟 60 次。
type Options struct {
	SigningKey []byte
	Clock      clock.Clock
	Limit      int
	Window     time.Duration
}

type claims struct {
	Tenant  string `json:"t"`
	User    string `json:"u"`
	Expires int64  `json:"exp"`
}

// Provider 是内置 Ingress。
type Provider struct {
	mu     sync.Mutex
	opts   Options
	clock  clock.Clock
	admits map[string][]time.Time
}

// New 构造一个内置 Ingress。
func New(opts Options) *Provider {
	if opts.Limit == 0 {
		opts.Limit, opts.Window = 60, time.Minute
	}
	c := opts.Clock
	if c == nil {
		c = clock.System{}
	}
	return &Provider{opts: opts, clock: c, admits: map[string][]time.Time{}}
}

func (p *Provider) sign(payload string) string {
	mac := hmac.New(sha256.New, p.opts.SigningKey)
	mac.Write([]byte(payload))
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

// IssueToken 签发令牌（开发专用）。
func (p *Provider) IssueToken(tenantID, userID string, ttl time.Duration) string {
	raw, _ := json.Marshal(claims{Tenant: tenantID, User: userID, Expires: p.clock.Now().Add(ttl).Unix()})
	payload := base64.RawURLEncoding.EncodeToString(raw)
	return payload + "." + p.sign(payload)
}

func denied(msg string) error { return providererr.New(providererr.Denied, "unauthenticated", msg) }

func (p *Provider) Authenticate(_ context.Context, creds ingress.Credentials) (ingress.Identity, error) {
	payload, sig, ok := strings.Cut(creds.BearerToken, ".")
	if !ok || payload == "" {
		return ingress.Identity{}, denied("缺少或无法识别的凭据")
	}
	if !hmac.Equal([]byte(sig), []byte(p.sign(payload))) {
		return ingress.Identity{}, denied("凭据签名不对")
	}
	raw, err := base64.RawURLEncoding.DecodeString(payload)
	if err != nil {
		return ingress.Identity{}, denied("凭据格式不对")
	}
	var c claims
	if json.Unmarshal(raw, &c) != nil || !slug.MatchString(c.Tenant) {
		return ingress.Identity{}, denied("凭据内容不对")
	}
	expires := time.Unix(c.Expires, 0)
	if !expires.After(p.clock.Now()) {
		return ingress.Identity{}, denied("凭据已过期")
	}
	return ingress.Identity{TenantID: c.Tenant, UserID: c.User, ExpiresAt: expires}, nil
}

func (p *Provider) Admit(_ context.Context, id ingress.Identity, route string) error {
	if id.TenantID == "" {
		return denied("没有认证身份")
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	now := p.clock.Now()
	key := id.TenantID + "\x00" + route
	var kept []time.Time
	for _, t := range p.admits[key] {
		if t.After(now.Add(-p.opts.Window)) {
			kept = append(kept, t)
		}
	}
	if len(kept) >= p.opts.Limit {
		p.admits[key] = kept
		e := providererr.New(providererr.Transient, "rate_limit", "请求过于频繁")
		e.RetryAfter = kept[0].Add(p.opts.Window).Sub(now)
		if e.RetryAfter <= 0 {
			e.RetryAfter = time.Millisecond
		}
		return e
	}
	p.admits[key] = append(kept, now)
	return nil
}

func (p *Provider) Close() error { return nil }
