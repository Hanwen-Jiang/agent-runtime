// Package ingresstest 是 Ingress 的一致性测试。只在 Go 侧有。
package ingresstest

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
)

// Harness 由实现所在包提供。
type Harness interface {
	Provider() ingress.Ingress
	Advance(ctx context.Context, d time.Duration)
	// IssueToken 为租户与用户签发一个有效期为 ttl 的令牌。
	IssueToken(tenantID, userID string, ttl time.Duration) string
	// RateLimit 是每个租户、每个 route 的限流配置。
	RateLimit() (limit int, window time.Duration)
}

// Run 跑完整的 Ingress 一致性测试。
func Run(t *testing.T, newHarness func(t *testing.T) Harness) {
	ctx := context.Background()
	conformance.Case(t, "ValidTokenYieldsIdentity", func(t *testing.T) {
		h := newHarness(t)
		token := h.IssueToken(conformance.TenantAID, "u-alice", time.Hour)
		id := conformance.Must(h.Provider().Authenticate(ctx, ingress.Credentials{BearerToken: token}))
		if id.TenantID != conformance.TenantAID || id.UserID != "u-alice" {
			t.Fatalf("identity = %+v", id)
		}
	})
	conformance.Case(t, "BadCredentialsAreDenied", func(t *testing.T) {
		h := newHarness(t)
		token := h.IssueToken(conformance.TenantAID, "u-alice", time.Hour)
		tampered := token[:len(token)-1] + string(rune('a'+(token[len(token)-1]+1)%26))
		for _, bad := range []string{"", "garbage", tampered} {
			_, err := h.Provider().Authenticate(ctx, ingress.Credentials{BearerToken: bad})
			conformance.ExpectError(t, err, providererr.Denied, "")
		}
	})
	conformance.Case(t, "ExpiredTokenIsDenied", func(t *testing.T) {
		h := newHarness(t)
		token := h.IssueToken(conformance.TenantAID, "u-alice", time.Minute)
		h.Advance(ctx, 61*time.Second)
		_, err := h.Provider().Authenticate(ctx, ingress.Credentials{BearerToken: token})
		conformance.ExpectError(t, err, providererr.Denied, "")
	})
	conformance.Case(t, "AdmitLimitsPerTenant", func(t *testing.T) {
		h := newHarness(t)
		limit, window := h.RateLimit()
		a := conformance.Must(h.Provider().Authenticate(ctx, ingress.Credentials{BearerToken: h.IssueToken(conformance.TenantAID, "u-alice", time.Hour)}))
		b := conformance.Must(h.Provider().Authenticate(ctx, ingress.Credentials{BearerToken: h.IssueToken(conformance.TenantBID, "u-bob", time.Hour)}))
		for range limit {
			conformance.NoError(t, h.Provider().Admit(ctx, a, "submit_task"))
		}
		err := h.Provider().Admit(ctx, a, "submit_task")
		conformance.ExpectError(t, err, providererr.Transient, "rate_limit")
		var e *providererr.Error
		if !errors.As(err, &e) || e.RetryAfter <= 0 {
			t.Fatal("rate limit must carry RetryAfter")
		}
		conformance.NoError(t, h.Provider().Admit(ctx, b, "submit_task"))
		h.Advance(ctx, window+time.Second)
		conformance.NoError(t, h.Provider().Admit(ctx, a, "submit_task"))
	})
	conformance.Case(t, "TenantContextComesFromIdentity", func(t *testing.T) {
		id := ingress.Identity{TenantID: conformance.TenantAID, UserID: "u-alice"}
		tc := conformance.Must(ingress.TenantContextFor(id, "", "w-1", "", nil))
		if tc.GetTenantId() != conformance.TenantAID || tc.GetUserId() != "u-alice" || tc.GetWorkspaceId() != "w-1" {
			t.Fatalf("tenant context = %v", tc)
		}
		conformance.Must(ingress.TenantContextFor(id, conformance.TenantAID, "w-1", "", nil))
		_, err := ingress.TenantContextFor(id, conformance.TenantBID, "w-1", "", nil)
		conformance.ExpectError(t, err, providererr.Denied, "tenant_mismatch")
		_, err = ingress.TenantContextFor(ingress.Identity{}, "", "", "", nil)
		conformance.ExpectError(t, err, providererr.Denied, "")
		_, err = ingress.TenantContextFor(id, "", "../w", "", nil)
		conformance.ExpectError(t, err, providererr.Invalid, "malformed_tenant")
	})
}
