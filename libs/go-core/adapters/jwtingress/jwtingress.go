// Package jwtingress 是生产用的 Ingress（AUTH-01，docs/plan/auth-tokens-2026-11-03.md）：只持签发方的公钥，验 Ed25519
// 签名的标准 JWT（紧凑格式），校验方因此伪造不了令牌。语义见 providers/ingress。只用标准库。
//
// 令牌：头部 alg 必须是 EdDSA、带 kid（crit 一律不认）；载荷 iss 要是登记过的签发方，kid 要是它登记的公钥之一（换密钥时
// 新旧并存），aud 要含本服务（默认 agent-runtime），exp 必填、有效期不超过 MaxTTL（默认 24 小时），nbf 没到不收；sub 是用户、
// tenant_id 是租户（要合乎格式、在签发方允许的范围里），scope 是空格分隔的权限。时间按 Leeway 容差（默认 0）。
//
// 限流按（租户、route）的滑动窗口：给了 Limits（平台作用域的 Cache，生产是 Valkey）时多个副本共用一份计数，否则在进程内。
// 迁移期间可以给 Fallback（开发实现 localingress）：不是 JWT 形状的令牌交给它；JWT 形状却验不过的照样拒绝，不交给它。
package jwtingress

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/cache"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
)

// DefaultAudience 是 Runtime 的受众：签给 Runtime 的令牌 aud 要含它。
const DefaultAudience = "agent-runtime"

// DefaultMaxTTL 是令牌的最长有效期：签得更长的不收（AUTH-02 之后签发方只签 5 分钟的）。
const DefaultMaxTTL = 24 * time.Hour

var (
	slug          = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)
	tenantPrefix  = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,61}\*$`)
	errNoIssuers  = errors.New("没有登记签发方")
	pemBeginBytes = []byte("-----BEGIN")
)

// Issuer 是一个签发方的登记：iss、公钥（kid → base64url 的 32 字节公钥或 PEM），以及它能签的租户（空表示全部；
// 条目是租户 ID，或以 * 结尾的前缀）。
type Issuer struct {
	Issuer  string            `json:"iss"`
	Keys    map[string]string `json:"keys"`
	Tenants []string          `json:"tenants,omitempty"`
}

// Config 是签发方的登记文件（AR_INGRESS_ISSUERS_FILE）。
type Config struct {
	Audience string   `json:"audience,omitempty"`
	Issuers  []Issuer `json:"issuers"`
}

// LoadConfig 读并核对登记文件；不认识的字段报错（拼错了不能悄悄失效）。
func LoadConfig(path string) (Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return Config{}, err
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var cfg Config
	if err := dec.Decode(&cfg); err != nil {
		return Config{}, fmt.Errorf("%s 不是合格的签发方登记：%w", path, err)
	}
	if _, _, err := cfg.compile(); err != nil {
		return Config{}, fmt.Errorf("%s：%w", path, err)
	}
	if cfg.Audience == "" {
		cfg.Audience = DefaultAudience
	}
	return cfg, nil
}

type issuer struct {
	keys    map[string]ed25519.PublicKey
	tenants []string
	anyone  bool
}

func (i *issuer) allows(tenant string) bool {
	if i.anyone {
		return true
	}
	for _, t := range i.tenants {
		if t == tenant || (strings.HasSuffix(t, "*") && strings.HasPrefix(tenant, strings.TrimSuffix(t, "*"))) {
			return true
		}
	}
	return false
}

func (c Config) compile() (map[string]*issuer, string, error) {
	if len(c.Issuers) == 0 {
		return nil, "", errNoIssuers
	}
	out := map[string]*issuer{}
	for _, in := range c.Issuers {
		if strings.TrimSpace(in.Issuer) == "" {
			return nil, "", errors.New("签发方的 iss 不能为空")
		}
		if _, dup := out[in.Issuer]; dup {
			return nil, "", fmt.Errorf("签发方 %s 登记了两次", in.Issuer)
		}
		if len(in.Keys) == 0 {
			return nil, "", fmt.Errorf("签发方 %s 没有公钥", in.Issuer)
		}
		is := &issuer{keys: map[string]ed25519.PublicKey{}, anyone: len(in.Tenants) == 0}
		for kid, encoded := range in.Keys {
			if kid == "" {
				return nil, "", fmt.Errorf("签发方 %s 有一个空的 kid", in.Issuer)
			}
			key, err := ParsePublicKey(encoded)
			if err != nil {
				return nil, "", fmt.Errorf("签发方 %s 的公钥 %s：%w", in.Issuer, kid, err)
			}
			is.keys[kid] = key
		}
		for _, t := range in.Tenants {
			switch {
			case t == "*":
				is.anyone = true
			case slug.MatchString(t) || tenantPrefix.MatchString(t):
				is.tenants = append(is.tenants, t)
			default:
				return nil, "", fmt.Errorf("签发方 %s 的租户范围 %q 不合格式", in.Issuer, t)
			}
		}
		out[in.Issuer] = is
	}
	aud := c.Audience
	if aud == "" {
		aud = DefaultAudience
	}
	return out, aud, nil
}

// Options 是构造参数；Limit 为 0 时按每分钟 60 次。
type Options struct {
	Config   Config
	Clock    clock.Clock
	Leeway   time.Duration
	MaxTTL   time.Duration
	Limits   cache.Cache
	Limit    int
	Window   time.Duration
	Fallback ingress.Ingress
}

// Provider 是验 JWT 的 Ingress。
type Provider struct {
	issuers  map[string]*issuer
	audience string
	clock    clock.Clock
	leeway   time.Duration
	maxTTL   time.Duration
	limits   cache.Cache
	limit    int
	window   time.Duration
	fallback ingress.Ingress

	mu     sync.Mutex
	admits map[string][]time.Time
}

// New 构造 Provider；登记不合格时报错。
func New(opts Options) (*Provider, error) {
	issuers, aud, err := opts.Config.compile()
	if err != nil {
		return nil, err
	}
	p := &Provider{
		issuers: issuers, audience: aud, clock: opts.Clock, leeway: opts.Leeway, maxTTL: opts.MaxTTL, limits: opts.Limits,
		limit: opts.Limit, window: opts.Window, fallback: opts.Fallback, admits: map[string][]time.Time{},
	}
	if p.clock == nil {
		p.clock = clock.System{}
	}
	if p.maxTTL <= 0 {
		p.maxTTL = DefaultMaxTTL
	}
	if p.limit == 0 {
		p.limit, p.window = 60, time.Minute
	}
	return p, nil
}

func denied(msg string) error { return providererr.New(providererr.Denied, "unauthenticated", msg) }

type header struct {
	Alg  string          `json:"alg"`
	Kid  string          `json:"kid"`
	Crit json.RawMessage `json:"crit"`
}

type claims struct {
	Iss    string          `json:"iss"`
	Aud    json.RawMessage `json:"aud"`
	Sub    string          `json:"sub"`
	Tenant string          `json:"tenant_id"`
	Exp    *int64          `json:"exp"`
	Nbf    *int64          `json:"nbf,omitempty"`
	Iat    *int64          `json:"iat,omitempty"`
	Scope  string          `json:"scope,omitempty"`
	Jti    string          `json:"jti,omitempty"`
}

// b64 解码时连末位的填充位也核对（Strict）：不然改了签名的最后一个字符还能解出同样的字节。
var b64 = base64.RawURLEncoding.Strict()

func decodeJSON(segment string, out any) error {
	raw, err := b64.DecodeString(segment)
	if err != nil {
		return err
	}
	return json.Unmarshal(raw, out)
}

func (c claims) hasAudience(aud string) bool {
	var one string
	if json.Unmarshal(c.Aud, &one) == nil {
		return one == aud
	}
	var many []string
	if json.Unmarshal(c.Aud, &many) == nil {
		for _, a := range many {
			if a == aud {
				return true
			}
		}
	}
	return false
}

// Authenticate 验令牌，返回身份。
func (p *Provider) Authenticate(ctx context.Context, creds ingress.Credentials) (ingress.Identity, error) {
	token := creds.BearerToken
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		if p.fallback != nil && len(parts) == 2 {
			return p.fallback.Authenticate(ctx, creds)
		}
		return ingress.Identity{}, denied("缺少或无法识别的凭据")
	}
	var h header
	if decodeJSON(parts[0], &h) != nil {
		return ingress.Identity{}, denied("凭据格式不对")
	}
	if h.Alg != "EdDSA" || h.Kid == "" || h.Crit != nil {
		return ingress.Identity{}, denied("凭据的签名算法或密钥编号不对")
	}
	var c claims
	if decodeJSON(parts[1], &c) != nil {
		return ingress.Identity{}, denied("凭据格式不对")
	}
	is := p.issuers[c.Iss]
	if is == nil {
		return ingress.Identity{}, denied("不认识的签发方")
	}
	key := is.keys[h.Kid]
	sig, err := b64.DecodeString(parts[2])
	if key == nil || err != nil || !ed25519.Verify(key, []byte(parts[0]+"."+parts[1]), sig) {
		return ingress.Identity{}, denied("凭据签名不对")
	}
	if !c.hasAudience(p.audience) {
		return ingress.Identity{}, denied("凭据不是签给本服务的")
	}
	now := p.clock.Now()
	if c.Exp == nil {
		return ingress.Identity{}, denied("凭据没有有效期")
	}
	expires := time.Unix(*c.Exp, 0)
	if !expires.Add(p.leeway).After(now) {
		return ingress.Identity{}, denied("凭据已过期")
	}
	if expires.Sub(now) > p.maxTTL+p.leeway {
		return ingress.Identity{}, denied("凭据的有效期太长")
	}
	if c.Nbf != nil && now.Add(p.leeway).Before(time.Unix(*c.Nbf, 0)) {
		return ingress.Identity{}, denied("凭据还没生效")
	}
	if !slug.MatchString(c.Tenant) || c.Sub == "" {
		return ingress.Identity{}, denied("凭据内容不对")
	}
	if !is.allows(c.Tenant) {
		return ingress.Identity{}, denied("签发方不能给这个租户签凭据")
	}
	return ingress.Identity{TenantID: c.Tenant, UserID: c.Sub, Scopes: strings.Fields(c.Scope), ExpiresAt: expires}, nil
}

// Admit 按（租户、route）限流。
func (p *Provider) Admit(ctx context.Context, id ingress.Identity, route string) error {
	if id.TenantID == "" {
		return denied("没有认证身份")
	}
	if p.limits != nil {
		d, err := p.limits.RateLimit(ctx, "ingress:"+id.TenantID+":"+route, int64(p.limit), p.window, 1)
		if err != nil {
			return err
		}
		if !d.Allowed {
			return rateLimited(d.RetryAfter)
		}
		return nil
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	now := p.clock.Now()
	key := id.TenantID + "\x00" + route
	var kept []time.Time
	for _, t := range p.admits[key] {
		if t.After(now.Add(-p.window)) {
			kept = append(kept, t)
		}
	}
	if len(kept) >= p.limit {
		p.admits[key] = kept
		return rateLimited(kept[0].Add(p.window).Sub(now))
	}
	p.admits[key] = append(kept, now)
	return nil
}

func rateLimited(retryAfter time.Duration) error {
	e := providererr.New(providererr.Transient, "rate_limit", "请求过于频繁")
	e.RetryAfter = retryAfter
	if e.RetryAfter <= 0 {
		e.RetryAfter = time.Millisecond
	}
	return e
}

// Close 关掉 Fallback；Limits 归调用方。
func (p *Provider) Close() error {
	if p.fallback != nil {
		return p.fallback.Close()
	}
	return nil
}

// --- 签发（coding-agent 一类的签发方与开发工具用） ---

// Claims 是要签进令牌的内容；Audience 为空时是 DefaultAudience。
type Claims struct {
	Tenant    string
	User      string
	Scopes    []string
	Audience  string
	TTL       time.Duration
	ID        string
	NotBefore time.Time
}

// Signer 用一把私钥签令牌。
type Signer struct {
	issuer string
	keyID  string
	key    ed25519.PrivateKey
	clock  clock.Clock
}

// NewSigner 构造签发器；clk 为 nil 时用系统时钟。
func NewSigner(issuer, keyID string, key ed25519.PrivateKey, clk clock.Clock) *Signer {
	if clk == nil {
		clk = clock.System{}
	}
	return &Signer{issuer: issuer, keyID: keyID, key: key, clock: clk}
}

// Issue 签一个令牌。
func (s *Signer) Issue(c Claims) (string, error) {
	if !slug.MatchString(c.Tenant) || c.User == "" || c.TTL <= 0 {
		return "", errors.New("签令牌要合格的租户、用户与正的有效期")
	}
	now := s.clock.Now()
	aud := c.Audience
	if aud == "" {
		aud = DefaultAudience
	}
	audJSON, _ := json.Marshal(aud)
	exp, iat := now.Add(c.TTL).Unix(), now.Unix()
	body := claims{Iss: s.issuer, Aud: audJSON, Sub: c.User, Tenant: c.Tenant, Exp: &exp, Iat: &iat, Scope: strings.Join(c.Scopes, " "), Jti: c.ID}
	if !c.NotBefore.IsZero() {
		nbf := c.NotBefore.Unix()
		body.Nbf = &nbf
	}
	h, err := json.Marshal(map[string]string{"alg": "EdDSA", "typ": "JWT", "kid": s.keyID})
	if err != nil {
		return "", err
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return "", err
	}
	input := base64.RawURLEncoding.EncodeToString(h) + "." + base64.RawURLEncoding.EncodeToString(payload)
	return input + "." + base64.RawURLEncoding.EncodeToString(ed25519.Sign(s.key, []byte(input))), nil
}

// --- 密钥 ---

// GenerateKey 生成一对密钥：公钥是 base64url（写进登记文件），私钥是 PKCS#8 的 PEM（交给签发方，不进 Runtime）。
func GenerateKey() (string, []byte, error) {
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return "", nil, err
	}
	der, err := x509.MarshalPKCS8PrivateKey(private)
	if err != nil {
		return "", nil, err
	}
	return EncodePublicKey(public), pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}), nil
}

// EncodePublicKey 是登记文件里公钥的写法：32 字节的 base64url（不带填充）。
func EncodePublicKey(key ed25519.PublicKey) string {
	return base64.RawURLEncoding.EncodeToString(key)
}

// PublicKeyPEM 是公钥的 PEM（SubjectPublicKeyInfo）写法，登记文件也收它。
func PublicKeyPEM(key ed25519.PublicKey) (string, error) {
	der, err := x509.MarshalPKIXPublicKey(key)
	if err != nil {
		return "", err
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der})), nil
}

// ParsePublicKey 收 base64url 的 32 字节公钥或 PEM 的 SubjectPublicKeyInfo。
func ParsePublicKey(s string) (ed25519.PublicKey, error) {
	s = strings.TrimSpace(s)
	if strings.HasPrefix(s, "-----BEGIN") {
		block, _ := pem.Decode([]byte(s))
		if block == nil || block.Type != "PUBLIC KEY" {
			return nil, errors.New("不是 PUBLIC KEY 的 PEM")
		}
		key, err := x509.ParsePKIXPublicKey(block.Bytes)
		if err != nil {
			return nil, err
		}
		pub, ok := key.(ed25519.PublicKey)
		if !ok {
			return nil, errors.New("不是 Ed25519 公钥")
		}
		return pub, nil
	}
	raw, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(s, "="))
	if err != nil || len(raw) != ed25519.PublicKeySize {
		return nil, errors.New("要是 32 字节的 base64url 或 PEM")
	}
	return ed25519.PublicKey(raw), nil
}

// ParsePrivateKey 收 PKCS#8 的 PEM，或 base64url 的 32 字节种子。
func ParsePrivateKey(data []byte) (ed25519.PrivateKey, error) {
	data = bytes.TrimSpace(data)
	if bytes.HasPrefix(data, pemBeginBytes) {
		block, _ := pem.Decode(data)
		if block == nil || block.Type != "PRIVATE KEY" {
			return nil, errors.New("不是 PRIVATE KEY 的 PEM")
		}
		key, err := x509.ParsePKCS8PrivateKey(block.Bytes)
		if err != nil {
			return nil, err
		}
		private, ok := key.(ed25519.PrivateKey)
		if !ok {
			return nil, errors.New("不是 Ed25519 私钥")
		}
		return private, nil
	}
	seed, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(string(data), "="))
	if err != nil || len(seed) != ed25519.SeedSize {
		return nil, errors.New("要是 PKCS#8 的 PEM 或 32 字节种子的 base64url")
	}
	return ed25519.NewKeyFromSeed(seed), nil
}
