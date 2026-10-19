// Package secretstoretest 是 SecretStore 的一致性测试，与 Python 侧 agentruntime_core.conformance.secret_store 一一对应。
package secretstoretest

import (
	"context"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/secretstore"
)

// ProbeRole 必须授予租户 A、租户 B 与平台作用域。
const ProbeRole = "conformance-reader"

const (
	platformSecretValue = "platform-db-password-7Q2"
	tenantSecretValue   = "tenant-byok-key-9X4"
)

// Harness 由实现所在包提供。tenantID 为空串表示平台作用域。
type Harness interface {
	Provider() secretstore.Provider
	Advance(ctx context.Context, d time.Duration)
	conformance.Faultable
	MaxTTL() time.Duration
	SeedSecret(tenantID, name, value string)
	GrantRole(tenantID, role string)
}

func prepared(t *testing.T, newHarness func(t *testing.T) Harness) Harness {
	h := newHarness(t)
	for _, id := range []string{conformance.TenantAID, conformance.TenantBID, ""} {
		h.GrantRole(id, ProbeRole)
	}
	h.SeedSecret("", "db-password", platformSecretValue)
	h.SeedSecret(conformance.TenantAID, "model-api-key", tenantSecretValue)
	return h
}

func forms(b []byte) []string {
	return []string{string(b), hex.EncodeToString(b), base64.StdEncoding.EncodeToString(b)}
}

// Run 跑完整的 SecretStore 一致性测试。
func Run(t *testing.T, newHarness func(t *testing.T) Harness) {
	var mu sync.Mutex
	leases := map[string]string{}
	probe := conformance.Probe[secretstore.Store]{
		Operation: "IssueCredential",
		Write: func(ctx context.Context, s secretstore.Store, name string) error {
			c, err := s.IssueCredential(ctx, ProbeRole, 5*time.Minute)
			if err == nil {
				mu.Lock()
				leases[name] = c.LeaseID
				mu.Unlock()
			}
			return err
		},
		Visible: func(ctx context.Context, s secretstore.Store, name string) (bool, error) {
			mu.Lock()
			id := leases[name]
			mu.Unlock()
			_, err := s.LeaseStatus(ctx, id)
			if providererr.CategoryOf(err) == providererr.NotFound {
				return false, nil
			}
			return err == nil, err
		},
	}
	binder := func(t *testing.T) conformance.Binder[secretstore.Store] { return prepared(t, newHarness).Provider() }
	conformance.RunBindingRules(t, binder)
	conformance.RunIsolationRules(t, binder, probe)
	conformance.RunErrorRules(t, func(t *testing.T) (conformance.Binder[secretstore.Store], conformance.Faultable) {
		h := prepared(t, newHarness)
		return h.Provider(), h
	}, probe)

	ctx := context.Background()
	setup := func(t *testing.T) (Harness, secretstore.Store) {
		h := prepared(t, newHarness)
		return h, conformance.Must(h.Provider().Bind(conformance.TenantA()))
	}

	conformance.Case(t, "ReadIsScoped", func(t *testing.T) {
		h, s := setup(t)
		if conformance.Must(s.Read(ctx, "model-api-key")).RevealString() != tenantSecretValue {
			t.Fatal("tenant secret mismatch")
		}
		platform := conformance.Must(h.Provider().BindPlatform("conformance"))
		if conformance.Must(platform.Read(ctx, "db-password")).RevealString() != platformSecretValue {
			t.Fatal("platform secret mismatch")
		}
		b := conformance.Must(h.Provider().Bind(conformance.TenantB()))
		_, err := b.Read(ctx, "model-api-key")
		conformance.ExpectError(t, err, providererr.NotFound, "")
		_, err = s.Read(ctx, "db-password")
		conformance.ExpectError(t, err, providererr.NotFound, "")
		_, err = s.Read(ctx, "missing")
		conformance.ExpectError(t, err, providererr.NotFound, "")
	})
	conformance.Case(t, "CredentialLifecycle", func(t *testing.T) {
		h, s := setup(t)
		c := conformance.Must(s.IssueCredential(ctx, ProbeRole, time.Minute))
		other := conformance.Must(s.IssueCredential(ctx, ProbeRole, time.Minute))
		if c.Role != ProbeRole || c.Secret.IsZero() || c.LeaseID == other.LeaseID || c.Secret.Equal(other.Secret) {
			t.Fatalf("credential = %v", c)
		}
		if conformance.Must(s.LeaseStatus(ctx, c.LeaseID)) != secretstore.LeaseActive {
			t.Fatal("new lease not active")
		}
		conformance.NoError(t, s.Revoke(ctx, other.LeaseID))
		conformance.NoError(t, s.Revoke(ctx, other.LeaseID))
		if conformance.Must(s.LeaseStatus(ctx, other.LeaseID)) != secretstore.LeaseRevoked {
			t.Fatal("revoked lease not revoked")
		}
		h.Advance(ctx, 61*time.Second)
		if conformance.Must(s.LeaseStatus(ctx, c.LeaseID)) != secretstore.LeaseExpired {
			t.Fatal("lease did not expire")
		}
		conformance.ExpectError(t, s.Revoke(ctx, "lease-does-not-exist"), providererr.NotFound, "")
	})
	conformance.Case(t, "OtherTenantCannotRevoke", func(t *testing.T) {
		h, s := setup(t)
		c := conformance.Must(s.IssueCredential(ctx, ProbeRole, 5*time.Minute))
		b := conformance.Must(h.Provider().Bind(conformance.TenantB()))
		conformance.ExpectError(t, b.Revoke(ctx, c.LeaseID), providererr.NotFound, "")
		if conformance.Must(s.LeaseStatus(ctx, c.LeaseID)) != secretstore.LeaseActive {
			t.Fatal("foreign revoke affected the lease")
		}
	})
	conformance.Case(t, "IssueRules", func(t *testing.T) {
		h, s := setup(t)
		_, err := s.IssueCredential(ctx, "crm-admin", time.Minute)
		conformance.ExpectError(t, err, providererr.Denied, "")
		_, err = s.IssueCredential(ctx, ProbeRole, 0)
		conformance.ExpectError(t, err, providererr.Invalid, "")
		_, err = s.IssueCredential(ctx, ProbeRole, h.MaxTTL()+time.Second)
		conformance.ExpectError(t, err, providererr.Invalid, "")
	})
	conformance.Case(t, "DataKeys", func(t *testing.T) {
		h, s := setup(t)
		key := conformance.Must(s.GenerateDataKey(ctx, "claim_check"))
		if len(key.Plaintext.Reveal()) != 32 {
			t.Fatalf("plaintext length = %d", len(key.Plaintext.Reveal()))
		}
		if !conformance.Must(s.UnwrapDataKey(ctx, key.Wrapped, "claim_check")).Equal(key.Plaintext) {
			t.Fatal("unwrap mismatch")
		}
		if conformance.Must(s.GenerateDataKey(ctx, "claim_check")).Plaintext.Equal(key.Plaintext) {
			t.Fatal("data keys must be distinct")
		}
		b := conformance.Must(h.Provider().Bind(conformance.TenantB()))
		_, err := b.UnwrapDataKey(ctx, key.Wrapped, "claim_check")
		conformance.ExpectError(t, err, providererr.Denied, "")
		_, err = s.UnwrapDataKey(ctx, key.Wrapped, "vector_encryption")
		conformance.ExpectError(t, err, providererr.Denied, "")
		corrupted := make([]byte, len(key.Wrapped))
		for i, c := range key.Wrapped {
			corrupted[i] = c ^ 0x5A
		}
		_, err = s.UnwrapDataKey(ctx, corrupted, "claim_check")
		conformance.ExpectError(t, err, providererr.Invalid, "")
		for _, form := range append(forms(key.Plaintext.Reveal()), tenantSecretValue) {
			if strings.Contains(err.Error(), form) {
				t.Fatal("error text echoed secret material")
			}
		}
	})
	conformance.Case(t, "SecretsNeverRenderInPlainText", func(t *testing.T) {
		_, s := setup(t)
		c := conformance.Must(s.IssueCredential(ctx, ProbeRole, time.Minute))
		key := conformance.Must(s.GenerateDataKey(ctx, "claim_check"))
		static := conformance.Must(s.Read(ctx, "model-api-key"))
		rendered := []string{
			fmt.Sprintf("%v %+v %#v", c, c, c), fmt.Sprintf("%v %+v %#v", key, key, key),
			fmt.Sprintf("%v %s %q %x", static, static, static, static),
		}
		var plain []string
		for _, b := range [][]byte{c.Secret.Reveal(), key.Plaintext.Reveal(), static.Reveal()} {
			plain = append(plain, forms(b)...)
		}
		for _, text := range rendered {
			for _, form := range plain {
				if strings.Contains(text, form) {
					t.Fatalf("plain text leaked in %q", text)
				}
			}
		}
	})
}
