// Package filesecrets 是本地文件 SecretStore（开发实现）。语义见 providers/secretstore。
//
// 文件格式与 Python 侧（agentruntime_core.dev.secret_store）相同：
//
//	{"version": 1, "scopes": {"<租户 ID 或 _platform>": {"secrets": {名字: 值}, "roles": [角色], "kek": base64}}}
//
// kek 是该作用域的密钥加密密钥，首次生成数据密钥时创建并写回文件。短期凭据的租约只在内存里。
// 文件里是明文，只能用于开发。
package filesecrets

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/devutil"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/secretstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Options 是构造参数；MaxTTL 为 0 时按 1 小时。
type Options struct {
	Path   string
	MaxTTL time.Duration
	Clock  clock.Clock
	Faults *testkit.Faults
}

type scopeEntry struct {
	Secrets map[string]string `json:"secrets"`
	Roles   []string          `json:"roles"`
	KEK     string            `json:"kek,omitempty"`
}

type file struct {
	Version int                    `json:"version"`
	Scopes  map[string]*scopeEntry `json:"scopes"`
}

type lease struct {
	owner, role string
	expires     time.Time
	revoked     bool
}

// Provider 是本地文件 SecretStore 的工厂。
type Provider struct {
	mu     sync.Mutex
	opts   Options
	clock  clock.Clock
	data   file
	leases map[string]*lease
}

// New 构造；文件存在时读入。
func New(opts Options) (*Provider, error) {
	if opts.Path == "" {
		return nil, errors.New("filesecrets: Path 必填")
	}
	if opts.MaxTTL == 0 {
		opts.MaxTTL = time.Hour
	}
	c := opts.Clock
	if c == nil {
		c = clock.System{}
	}
	p := &Provider{opts: opts, clock: c, data: file{Version: 1, Scopes: map[string]*scopeEntry{}}, leases: map[string]*lease{}}
	raw, err := os.ReadFile(opts.Path)
	if err == nil {
		if err := json.Unmarshal(raw, &p.data); err != nil {
			return nil, err
		}
	} else if !errors.Is(err, fs.ErrNotExist) {
		return nil, err
	}
	return p, nil
}

func (p *Provider) Bind(t *commonv1.TenantContext) (secretstore.Store, error) {
	s, err := tenancy.TenantScope(t)
	if err != nil {
		return nil, err
	}
	return &store{p: p, owner: devutil.ScopeID(s)}, nil
}

func (p *Provider) BindPlatform(purpose string) (secretstore.Store, error) {
	s, err := tenancy.PlatformScope(purpose)
	if err != nil {
		return nil, err
	}
	return &store{p: p, owner: devutil.ScopeID(s)}, nil
}

func (p *Provider) Close() error { return nil }

func ownerOf(tenantID string) string {
	if tenantID == "" {
		return devutil.PlatformID
	}
	return tenantID
}

// SeedSecret 写入静态密钥；tenantID 为空串表示平台作用域（开发专用）。
func (p *Provider) SeedSecret(tenantID, name, value string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.scope(ownerOf(tenantID)).Secrets[name] = value
	_ = p.save()
}

// GrantRole 把签发凭据的角色授予作用域（开发专用）。
func (p *Provider) GrantRole(tenantID, role string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	e := p.scope(ownerOf(tenantID))
	if !slices.Contains(e.Roles, role) {
		e.Roles = append(e.Roles, role)
		_ = p.save()
	}
}

func (p *Provider) scope(owner string) *scopeEntry {
	e, ok := p.data.Scopes[owner]
	if !ok {
		e = &scopeEntry{Secrets: map[string]string{}, Roles: []string{}}
		p.data.Scopes[owner] = e
	}
	if e.Secrets == nil {
		e.Secrets = map[string]string{}
	}
	return e
}

func (p *Provider) kek(owner string) ([]byte, error) {
	e := p.scope(owner)
	if e.KEK == "" {
		b := make([]byte, 32)
		_, _ = rand.Read(b)
		e.KEK = base64.StdEncoding.EncodeToString(b)
		if err := p.save(); err != nil {
			return nil, err
		}
	}
	return base64.StdEncoding.DecodeString(e.KEK)
}

func (p *Provider) save() error {
	raw, err := json.MarshalIndent(p.data, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(p.opts.Path), 0o700); err != nil {
		return err
	}
	tmp := p.opts.Path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, p.opts.Path)
}

func randomID(prefix string, n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return prefix + hex.EncodeToString(b)
}

type store struct {
	p     *Provider
	owner string
}

func (s *store) enter(op string) error {
	if err := s.p.opts.Faults.Check(op); err != nil {
		return err
	}
	s.p.mu.Lock()
	return nil
}

func (s *store) Read(_ context.Context, name string) (secret.Value, error) {
	if err := s.enter("Read"); err != nil {
		return secret.Value{}, err
	}
	defer s.p.mu.Unlock()
	v, ok := s.p.scope(s.owner).Secrets[name]
	if !ok {
		return secret.Value{}, providererr.New(providererr.NotFound, "not_found", "密钥不存在")
	}
	return secret.New(v), nil
}

func (s *store) IssueCredential(_ context.Context, role string, ttl time.Duration) (secretstore.Credential, error) {
	if err := s.enter("IssueCredential"); err != nil {
		return secretstore.Credential{}, err
	}
	defer s.p.mu.Unlock()
	if !slices.Contains(s.p.scope(s.owner).Roles, role) {
		return secretstore.Credential{}, providererr.New(providererr.Denied, "role_not_granted", "该角色没有授予本作用域")
	}
	if ttl <= 0 || ttl > s.p.opts.MaxTTL {
		return secretstore.Credential{}, providererr.New(providererr.Invalid, "invalid_ttl", "ttl 必须大于 0 且不超过上限")
	}
	id := randomID("lease-", 12)
	expires := s.p.clock.Now().Add(ttl)
	s.p.leases[id] = &lease{owner: s.owner, role: role, expires: expires}
	raw := make([]byte, 18)
	_, _ = rand.Read(raw)
	return secretstore.Credential{
		LeaseID: id, Role: role, ExpiresAt: expires,
		Secret: secret.New("dev-" + base64.RawURLEncoding.EncodeToString(raw)),
	}, nil
}

func (s *store) leaseLocked(id string) (*lease, error) {
	l, ok := s.p.leases[id]
	if !ok || l.owner != s.owner {
		return nil, providererr.New(providererr.NotFound, "not_found", "租约不存在")
	}
	return l, nil
}

func (s *store) LeaseStatus(_ context.Context, id string) (secretstore.LeaseStatus, error) {
	if err := s.enter("LeaseStatus"); err != nil {
		return "", err
	}
	defer s.p.mu.Unlock()
	l, err := s.leaseLocked(id)
	if err != nil {
		return "", err
	}
	switch {
	case l.revoked:
		return secretstore.LeaseRevoked, nil
	case !l.expires.After(s.p.clock.Now()):
		return secretstore.LeaseExpired, nil
	default:
		return secretstore.LeaseActive, nil
	}
}

func (s *store) Revoke(_ context.Context, id string) error {
	if err := s.enter("Revoke"); err != nil {
		return err
	}
	defer s.p.mu.Unlock()
	l, err := s.leaseLocked(id)
	if err != nil {
		return err
	}
	l.revoked = true
	return nil
}

func (s *store) GenerateDataKey(_ context.Context, purpose string) (secretstore.DataKey, error) {
	if purpose == "" {
		return secretstore.DataKey{}, providererr.New(providererr.Invalid, "invalid_argument", "purpose 不能为空")
	}
	if err := s.enter("GenerateDataKey"); err != nil {
		return secretstore.DataKey{}, err
	}
	defer s.p.mu.Unlock()
	kek, err := s.p.kek(s.owner)
	if err != nil {
		return secretstore.DataKey{}, providererr.Wrap(providererr.Transient, "io_error", "保存密钥文件失败", err)
	}
	plaintext := make([]byte, 32)
	_, _ = rand.Read(plaintext)
	return secretstore.DataKey{
		KeyID: randomID("dk-", 8), Plaintext: secret.FromBytes(plaintext), Wrapped: wrap(kek, s.owner, purpose, plaintext),
	}, nil
}

func (s *store) UnwrapDataKey(_ context.Context, wrapped []byte, purpose string) (secret.Value, error) {
	if err := s.enter("UnwrapDataKey"); err != nil {
		return secret.Value{}, err
	}
	defer s.p.mu.Unlock()
	kek, err := s.p.kek(s.owner)
	if err != nil {
		return secret.Value{}, providererr.Wrap(providererr.Transient, "io_error", "保存密钥文件失败", err)
	}
	plain, err := unwrap(kek, s.owner, purpose, wrapped)
	if err != nil {
		return secret.Value{}, err
	}
	return secret.FromBytes(plain), nil
}
