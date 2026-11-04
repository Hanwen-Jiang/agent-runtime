// Package filesecrets 是本地文件 SecretStore（开发实现）。语义见 providers/secretstore。
//
// 文件格式与 Python 侧（agentruntime_core.dev.secret_store）相同：
//
//	{"version": 1, "scopes": {"<租户 ID 或 _platform>": {"secrets": {名字: 值}, "roles": [角色], "kek": base64}}}
//
// kek 是该作用域的密钥加密密钥，首次生成数据密钥时创建并写回文件。短期凭据的租约只在内存里。
// 多个进程可以共用一份文件（task-service 与 Worker 互相解开对方的 Claim-Check 数据密钥，S-06）：
// 写文件时加锁并先重新读，缺 kek 时先重新读文件再生成，不会覆盖别的进程写进去的内容。
// 绑定的 Store 实现 secretstore.Writer（用户密钥，SEC-API）；Read 每次重新读文件，别的进程刚写进去的、刚删掉的马上看得到
// （出网网关按名字现读，不缓存）。
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
	_ = p.locked(func() error {
		p.scope(ownerOf(tenantID)).Secrets[name] = value
		return p.save()
	})
}

// GrantRole 把签发凭据的角色授予作用域（开发专用）。
func (p *Provider) GrantRole(tenantID, role string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	_ = p.locked(func() error {
		e := p.scope(ownerOf(tenantID))
		if slices.Contains(e.Roles, role) {
			return nil
		}
		e.Roles = append(e.Roles, role)
		return p.save()
	})
}

// MergeSeed 把种子文件（同样的格式，通常是只读挂载的 K8s Secret）并进 path 的密钥文件（docs/plan/cloud-deploy.md D-04）：
// 种子里的密钥覆盖同名的（轮换时改种子），角色取并集；kek 只在密钥文件还没有时才取种子的，已有的不换
// （用它包过的数据密钥要能解开）。与 Python 侧 LocalFileSecretStoreProvider.merge_seed 规则相同。
func MergeSeed(path, seedPath string) error {
	raw, err := os.ReadFile(seedPath)
	if err != nil {
		return providererr.New(providererr.Invalid, "invalid_seed", "读不了密钥的种子文件")
	}
	var seed file
	if err := json.Unmarshal(raw, &seed); err != nil || seed.Scopes == nil {
		return providererr.New(providererr.Invalid, "invalid_seed", "密钥的种子文件格式不对")
	}
	p, err := New(Options{Path: path})
	if err != nil {
		return err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.locked(func() error {
		for owner, entry := range seed.Scopes {
			if entry == nil {
				continue
			}
			mine := p.scope(owner)
			for name, value := range entry.Secrets {
				mine.Secrets[name] = value
			}
			for _, role := range entry.Roles {
				if !slices.Contains(mine.Roles, role) {
					mine.Roles = append(mine.Roles, role)
				}
			}
			slices.Sort(mine.Roles)
			if mine.KEK == "" && entry.KEK != "" {
				mine.KEK = entry.KEK
			}
		}
		return p.save()
	})
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

// kek 返回作用域的密钥加密密钥。别的进程可能已经写进文件（S-06：task-service 与 Worker 共用一份）：
// 内存里没有就加锁、重新读文件，还没有才生成并写回，不会覆盖别人的。
func (p *Provider) kek(owner string) ([]byte, error) {
	if e := p.scope(owner); e.KEK != "" {
		return base64.StdEncoding.DecodeString(e.KEK)
	}
	var kek string
	err := p.locked(func() error {
		e := p.scope(owner)
		if e.KEK == "" {
			b := make([]byte, 32)
			_, _ = rand.Read(b)
			e.KEK = base64.StdEncoding.EncodeToString(b)
			if err := p.save(); err != nil {
				return err
			}
		}
		kek = e.KEK
		return nil
	})
	if err != nil {
		return nil, err
	}
	return base64.StdEncoding.DecodeString(kek)
}

const (
	lockTimeout = 10 * time.Second
	lockStale   = 30 * time.Second
)

// locked 在跨进程文件锁（<文件>.lock，独占创建）里执行 fn；拿到锁后先重新读文件，修改基于最新内容。
// 调用方持有 p.mu。
func (p *Provider) locked(fn func() error) error {
	lock := p.opts.Path + ".lock"
	if err := os.MkdirAll(filepath.Dir(lock), 0o700); err != nil {
		return err
	}
	deadline := time.Now().Add(lockTimeout)
	for {
		f, err := os.OpenFile(lock, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
		if err == nil {
			_ = f.Close()
			break
		}
		if !errors.Is(err, fs.ErrExist) {
			return err
		}
		if info, statErr := os.Stat(lock); statErr == nil && time.Since(info.ModTime()) > lockStale {
			_ = os.Remove(lock) // 持锁的进程崩溃留下的锁
			continue
		}
		if time.Now().After(deadline) {
			return providererr.New(providererr.Transient, "busy", "密钥文件被别的进程锁住")
		}
		time.Sleep(10 * time.Millisecond)
	}
	defer func() { _ = os.Remove(lock) }()
	if err := p.reload(); err != nil {
		return err
	}
	return fn()
}

// reload 重新读密钥文件（调用方持有 p.mu）；文件还没有时保持内存里的内容。写文件是先写临时文件再改名，读到的总是完整的一份。
func (p *Provider) reload() error {
	raw, err := os.ReadFile(p.opts.Path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	fresh := file{Version: 1, Scopes: map[string]*scopeEntry{}}
	if err := json.Unmarshal(raw, &fresh); err != nil {
		return err
	}
	if fresh.Scopes == nil {
		fresh.Scopes = map[string]*scopeEntry{}
	}
	p.data = fresh
	return nil
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
	if err := s.p.reload(); err != nil {
		return secret.Value{}, providererr.Wrap(providererr.Transient, "io_error", "读密钥文件失败", err)
	}
	v, ok := s.p.scope(s.owner).Secrets[name]
	if !ok {
		return secret.Value{}, providererr.New(providererr.NotFound, "not_found", "密钥不存在")
	}
	return secret.New(v), nil
}

var _ secretstore.Writer = (*store)(nil)

// Write 新建或替换本作用域的一个密钥（secretstore.Writer）：在文件锁里先重新读再改，不覆盖别的进程写进去的内容。
func (s *store) Write(_ context.Context, name string, value secret.Value) error {
	if name == "" {
		return providererr.New(providererr.Invalid, "invalid_argument", "密钥名不能为空")
	}
	if err := s.enter("Write"); err != nil {
		return err
	}
	defer s.p.mu.Unlock()
	return ioError(s.p.locked(func() error {
		s.p.scope(s.owner).Secrets[name] = value.RevealString()
		return s.p.save()
	}))
}

// Delete 删掉本作用域的一个密钥（secretstore.Writer）；不存在时返回 false。
func (s *store) Delete(_ context.Context, name string) (bool, error) {
	if err := s.enter("Delete"); err != nil {
		return false, err
	}
	defer s.p.mu.Unlock()
	var existed bool
	err := s.p.locked(func() error {
		secrets := s.p.scope(s.owner).Secrets
		if _, existed = secrets[name]; !existed {
			return nil
		}
		delete(secrets, name)
		return s.p.save()
	})
	return existed, ioError(err)
}

// ioError 把读写文件的错误换成 transient（文本不带文件内容）；已经是 providererr 的（例如锁住了）原样返回。
func ioError(err error) error {
	var pe *providererr.Error
	if err == nil || errors.As(err, &pe) {
		return err
	}
	return providererr.Wrap(providererr.Transient, "io_error", "保存密钥文件失败", err)
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
