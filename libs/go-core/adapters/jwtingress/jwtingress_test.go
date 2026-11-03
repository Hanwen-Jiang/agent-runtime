package jwtingress_test

// AUTH-01（docs/plan/auth-tokens-2026-11-03.md）：生产用的 Ingress 只持公钥，验 Ed25519 签名的标准 JWT。
//
//  1. 过 Ingress 的一致性测试（进程内限流与经 Cache 限流各一遍）。
//  2. 只认 alg = EdDSA、带 kid、签名对得上登记的公钥；iss 要登记过，aud 要含本服务，exp 必填，nbf 没到不收，
//     有效期超过上限不收，crit 头不认，租户要合乎格式并在签发方允许的范围里。
//  3. 身份：sub 是用户，tenant_id 是租户，scope（空格分隔）是权限。
//  4. 换密钥：同一个签发方登记多个 kid 时都收；去掉旧 kid 后旧令牌不收。
//  5. 迁移：配了 Fallback 时不是 JWT 的令牌交给它（开发令牌）；JWT 形状的令牌验不过时不交给它。没配时一律拒绝。
//  6. 限流经 Cache 时多个副本共用一份计数。
//  7. 签发方的登记文件：缺字段、重复、坏的公钥、坏的租户范围都报错；aud 默认 agent-runtime。
//  8. 生成的密钥对能签能验；公钥收 base64url 与 PEM 两种写法。

import (
	"context"
	"crypto/ed25519"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/jwtingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance/ingresstest"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/localingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/memcache"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/cache"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
)

const (
	limit  = 3
	window = time.Minute
	iss    = "coding-agent"
	kid    = "k1"
)

func newKey(t *testing.T) ed25519.PrivateKey {
	t.Helper()
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return key
}

func pub(key ed25519.PrivateKey) string {
	return jwtingress.EncodePublicKey(key.Public().(ed25519.PublicKey))
}

func config(keys map[string]ed25519.PrivateKey) jwtingress.Config {
	enc := map[string]string{}
	for k, v := range keys {
		enc[k] = pub(v)
	}
	return jwtingress.Config{Issuers: []jwtingress.Issuer{{Issuer: iss, Keys: enc}}}
}

type harness struct {
	provider *jwtingress.Provider
	signer   *jwtingress.Signer
	clock    *testkit.FakeClock
}

func (h *harness) Provider() ingress.Ingress                  { return h.provider }
func (h *harness) Advance(_ context.Context, d time.Duration) { h.clock.Advance(d) }
func (h *harness) IssueToken(tenantID, userID string, ttl time.Duration) string {
	token, err := h.signer.Issue(jwtingress.Claims{Tenant: tenantID, User: userID, TTL: ttl})
	if err != nil {
		panic(err)
	}
	return token
}
func (h *harness) RateLimit() (int, time.Duration) { return limit, window }

func newHarness(t *testing.T, limits func(*testkit.FakeClock) cache.Cache) *harness {
	t.Helper()
	h := &harness{clock: testkit.NewFakeClock()}
	key := newKey(t)
	opts := jwtingress.Options{Config: config(map[string]ed25519.PrivateKey{kid: key}), Clock: h.clock, Limit: limit, Window: window}
	if limits != nil {
		opts.Limits = limits(h.clock)
	}
	p, err := jwtingress.New(opts)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = p.Close() })
	h.provider, h.signer = p, jwtingress.NewSigner(iss, kid, key, h.clock)
	return h
}

func sharedCache(t *testing.T, clk *testkit.FakeClock) cache.Cache {
	t.Helper()
	c, err := memcache.New(memcache.Options{Clock: clk}).BindPlatform("ingress")
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestConformance(t *testing.T) {
	ingresstest.Run(t, func(t *testing.T) ingresstest.Harness { return newHarness(t, nil) })
}

func TestConformanceWithTheCacheLimiter(t *testing.T) {
	ingresstest.Run(t, func(t *testing.T) ingresstest.Harness {
		return newHarness(t, func(clk *testkit.FakeClock) cache.Cache { return sharedCache(t, clk) })
	})
}

// forge 按给定的头与载荷签一个令牌：sign 为 nil 时用 Ed25519 的 key 签。
func forge(header, claims map[string]any, key ed25519.PrivateKey, sign func(string) []byte) string {
	h, _ := json.Marshal(header)
	c, _ := json.Marshal(claims)
	input := base64.RawURLEncoding.EncodeToString(h) + "." + base64.RawURLEncoding.EncodeToString(c)
	var sig []byte
	if sign != nil {
		sig = sign(input)
	} else {
		sig = ed25519.Sign(key, []byte(input))
	}
	return input + "." + base64.RawURLEncoding.EncodeToString(sig)
}

func TestRejectsTokensItCannotTrust(t *testing.T) {
	ctx := context.Background()
	clk := testkit.NewFakeClock()
	key, other := newKey(t), newKey(t)
	cfg := config(map[string]ed25519.PrivateKey{kid: key})
	cfg.Issuers = append(cfg.Issuers, jwtingress.Issuer{Issuer: "evals-bot", Keys: map[string]string{"e1": pub(other)}, Tenants: []string{"evals"}})
	p, err := jwtingress.New(jwtingress.Options{Config: cfg, Clock: clk})
	if err != nil {
		t.Fatal(err)
	}
	now := clk.Now().Unix()
	head := func(edit func(map[string]any)) map[string]any {
		h := map[string]any{"alg": "EdDSA", "typ": "JWT", "kid": kid}
		if edit != nil {
			edit(h)
		}
		return h
	}
	body := func(edit func(map[string]any)) map[string]any {
		c := map[string]any{"iss": iss, "aud": "agent-runtime", "sub": "u-alice", "tenant_id": conformance.TenantAID, "exp": now + 600, "iat": now}
		if edit != nil {
			edit(c)
		}
		return c
	}
	good := forge(head(nil), body(nil), key, nil)
	if _, err := p.Authenticate(ctx, ingress.Credentials{BearerToken: good}); err != nil {
		t.Fatalf("合格的令牌被拒：%v", err)
	}
	hmacSigned := func(input string) []byte {
		m := hmac.New(sha256.New, []byte(pub(key)))
		m.Write([]byte(input))
		return m.Sum(nil)
	}
	cases := map[string]string{
		"HS256（拿公钥当 HMAC 密钥）": forge(head(func(h map[string]any) { h["alg"] = "HS256" }), body(nil), key, hmacSigned),
		"alg none":            forge(head(func(h map[string]any) { h["alg"] = "none" }), body(nil), key, func(string) []byte { return nil }),
		"没有 kid":              forge(head(func(h map[string]any) { delete(h, "kid") }), body(nil), key, nil),
		"不认识的 kid":            forge(head(func(h map[string]any) { h["kid"] = "k9" }), body(nil), key, nil),
		"别的密钥冒用 kid":          forge(head(nil), body(nil), other, nil),
		"crit 头":              forge(head(func(h map[string]any) { h["crit"] = []string{"exp"} }), body(nil), key, nil),
		"没登记的 iss":            forge(head(nil), body(func(c map[string]any) { c["iss"] = "someone" }), key, nil),
		"aud 不是本服务":           forge(head(nil), body(func(c map[string]any) { c["aud"] = "other-service" }), key, nil),
		"没有 aud":              forge(head(nil), body(func(c map[string]any) { delete(c, "aud") }), key, nil),
		"没有 exp":              forge(head(nil), body(func(c map[string]any) { delete(c, "exp") }), key, nil),
		"已过期":                 forge(head(nil), body(func(c map[string]any) { c["exp"] = now - 1 }), key, nil),
		"nbf 还没到":             forge(head(nil), body(func(c map[string]any) { c["nbf"] = now + 60 }), key, nil),
		"有效期超过上限":             forge(head(nil), body(func(c map[string]any) { c["exp"] = now + int64((25 * time.Hour).Seconds()) }), key, nil),
		"租户格式不对":              forge(head(nil), body(func(c map[string]any) { c["tenant_id"] = "../t" }), key, nil),
		"没有租户":                forge(head(nil), body(func(c map[string]any) { delete(c, "tenant_id") }), key, nil),
		"签发方不能签这个租户":          forge(head(func(h map[string]any) { h["kid"] = "e1" }), body(func(c map[string]any) { c["iss"] = "evals-bot" }), other, nil),
		"多一段":                 good + ".x",
		"开发令牌（没配 Fallback）":   localingress.New(localingress.Options{SigningKey: []byte("k"), Clock: clk}).IssueToken(conformance.TenantAID, "u-alice", time.Hour),
	}
	for name, token := range cases {
		_, err := p.Authenticate(ctx, ingress.Credentials{BearerToken: token})
		if providererr.CategoryOf(err) != providererr.Denied {
			t.Errorf("%s：应为 denied，实际 %v", name, err)
		}
	}
	allowed := forge(head(func(h map[string]any) { h["kid"] = "e1" }), body(func(c map[string]any) { c["iss"] = "evals-bot"; c["tenant_id"] = "evals" }), other, nil)
	if _, err := p.Authenticate(ctx, ingress.Credentials{BearerToken: allowed}); err != nil {
		t.Fatalf("签发方允许的租户被拒：%v", err)
	}
	listed := forge(head(nil), body(func(c map[string]any) { c["aud"] = []string{"other-service", "agent-runtime"} }), key, nil)
	if _, err := p.Authenticate(ctx, ingress.Credentials{BearerToken: listed}); err != nil {
		t.Fatalf("aud 是数组且含本服务时要收：%v", err)
	}
}

func TestIdentityComesFromSubTenantAndScope(t *testing.T) {
	h := newHarness(t, nil)
	token, err := h.signer.Issue(jwtingress.Claims{Tenant: conformance.TenantAID, User: "u-alice", Scopes: []string{"task.approve", "task.read"}, TTL: time.Hour, ID: "j-1"})
	if err != nil {
		t.Fatal(err)
	}
	id, err := h.provider.Authenticate(context.Background(), ingress.Credentials{BearerToken: token})
	if err != nil {
		t.Fatal(err)
	}
	if id.TenantID != conformance.TenantAID || id.UserID != "u-alice" || strings.Join(id.Scopes, ",") != "task.approve,task.read" {
		t.Fatalf("identity = %+v", id)
	}
	if !id.ExpiresAt.Equal(h.clock.Now().Add(time.Hour).Truncate(time.Second)) {
		t.Fatalf("ExpiresAt = %s", id.ExpiresAt)
	}
	parts := strings.Split(token, ".")
	raw, _ := base64.RawURLEncoding.DecodeString(parts[0])
	var header map[string]any
	_ = json.Unmarshal(raw, &header)
	if header["alg"] != "EdDSA" || header["kid"] != kid || header["typ"] != "JWT" {
		t.Fatalf("签出的头 = %v", header)
	}
}

func TestKeyRotationAcceptsEveryRegisteredKid(t *testing.T) {
	ctx := context.Background()
	clk := testkit.NewFakeClock()
	old, current := newKey(t), newKey(t)
	both, err := jwtingress.New(jwtingress.Options{Config: config(map[string]ed25519.PrivateKey{"k1": old, "k2": current}), Clock: clk})
	if err != nil {
		t.Fatal(err)
	}
	oldToken, _ := jwtingress.NewSigner(iss, "k1", old, clk).Issue(jwtingress.Claims{Tenant: conformance.TenantAID, User: "u", TTL: time.Hour})
	newToken, _ := jwtingress.NewSigner(iss, "k2", current, clk).Issue(jwtingress.Claims{Tenant: conformance.TenantAID, User: "u", TTL: time.Hour})
	for _, token := range []string{oldToken, newToken} {
		if _, err := both.Authenticate(ctx, ingress.Credentials{BearerToken: token}); err != nil {
			t.Fatalf("新旧 kid 并存时都要收：%v", err)
		}
	}
	rotated, err := jwtingress.New(jwtingress.Options{Config: config(map[string]ed25519.PrivateKey{"k2": current}), Clock: clk})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := rotated.Authenticate(ctx, ingress.Credentials{BearerToken: oldToken}); providererr.CategoryOf(err) != providererr.Denied {
		t.Fatalf("去掉旧 kid 后旧令牌不收：%v", err)
	}
}

func TestFallbackOnlyTakesTokensThatAreNotJWTs(t *testing.T) {
	ctx := context.Background()
	clk := testkit.NewFakeClock()
	key := newKey(t)
	dev := localingress.New(localingress.Options{SigningKey: []byte("dev-key"), Clock: clk})
	p, err := jwtingress.New(jwtingress.Options{Config: config(map[string]ed25519.PrivateKey{kid: key}), Clock: clk, Fallback: dev})
	if err != nil {
		t.Fatal(err)
	}
	legacy := dev.IssueToken(conformance.TenantAID, "u-alice", time.Hour)
	if id, err := p.Authenticate(ctx, ingress.Credentials{BearerToken: legacy}); err != nil || id.TenantID != conformance.TenantAID {
		t.Fatalf("迁移期间开发令牌要收：%+v %v", id, err)
	}
	jwt, _ := jwtingress.NewSigner(iss, kid, key, clk).Issue(jwtingress.Claims{Tenant: conformance.TenantBID, User: "u-bob", TTL: time.Hour})
	if id, err := p.Authenticate(ctx, ingress.Credentials{BearerToken: jwt}); err != nil || id.TenantID != conformance.TenantBID {
		t.Fatalf("JWT 照收：%+v %v", id, err)
	}
	forged, _ := jwtingress.NewSigner(iss, kid, newKey(t), clk).Issue(jwtingress.Claims{Tenant: conformance.TenantBID, User: "u-bob", TTL: time.Hour})
	if _, err := p.Authenticate(ctx, ingress.Credentials{BearerToken: forged}); providererr.CategoryOf(err) != providererr.Denied {
		t.Fatalf("验不过的 JWT 不交给开发实现：%v", err)
	}
}

func TestCacheLimiterIsSharedAcrossReplicas(t *testing.T) {
	ctx := context.Background()
	clk := testkit.NewFakeClock()
	key := newKey(t)
	shared := sharedCache(t, clk)
	var replicas []*jwtingress.Provider
	for range 2 {
		p, err := jwtingress.New(jwtingress.Options{Config: config(map[string]ed25519.PrivateKey{kid: key}), Clock: clk, Limits: shared, Limit: limit, Window: window})
		if err != nil {
			t.Fatal(err)
		}
		replicas = append(replicas, p)
	}
	id := ingress.Identity{TenantID: conformance.TenantAID, UserID: "u-alice"}
	for i := range limit {
		if err := replicas[i%2].Admit(ctx, id, "submit_task"); err != nil {
			t.Fatalf("第 %d 次：%v", i+1, err)
		}
	}
	err := replicas[1].Admit(ctx, id, "submit_task")
	if providererr.CategoryOf(err) != providererr.Transient {
		t.Fatalf("两个副本合起来超过上限要拒：%v", err)
	}
}

func TestConfigFileIsValidated(t *testing.T) {
	dir := t.TempDir()
	good := pub(newKey(t))
	write := func(body string) string {
		path := filepath.Join(dir, "issuers.json")
		if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
		return path
	}
	cfg, err := jwtingress.LoadConfig(write(`{"issuers": [{"iss": "coding-agent", "keys": {"k1": "` + good + `"}, "tenants": ["t-alpha", "eval-*"]}]}`))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Audience != jwtingress.DefaultAudience || len(cfg.Issuers) != 1 {
		t.Fatalf("cfg = %+v", cfg)
	}
	bad := map[string]string{
		"不是 JSON":    `{`,
		"没有签发方":      `{"issuers": []}`,
		"iss 为空":     `{"issuers": [{"iss": "", "keys": {"k1": "` + good + `"}}]}`,
		"重复的 iss":    `{"issuers": [{"iss": "a", "keys": {"k1": "` + good + `"}}, {"iss": "a", "keys": {"k2": "` + good + `"}}]}`,
		"没有公钥":       `{"issuers": [{"iss": "a", "keys": {}}]}`,
		"坏的公钥":       `{"issuers": [{"iss": "a", "keys": {"k1": "not-a-key"}}]}`,
		"坏的租户范围":     `{"issuers": [{"iss": "a", "keys": {"k1": "` + good + `"}, "tenants": ["../x"]}]}`,
		"不认识的字段":     `{"issuers": [{"iss": "a", "keys": {"k1": "` + good + `"}, "tenant": ["t"]}]}`,
		"私钥写进了公钥的位置": `{"issuers": [{"iss": "a", "keys": {"k1": "` + base64.RawURLEncoding.EncodeToString(newKey(t)) + `"}}]}`,
	}
	for name, body := range bad {
		if _, err := jwtingress.LoadConfig(write(body)); err == nil {
			t.Errorf("%s：应当报错", name)
		}
	}
	if _, err := jwtingress.New(jwtingress.Options{}); err == nil {
		t.Error("没有签发方时 New 应当报错")
	}
}

func TestGeneratedKeysSignAndVerify(t *testing.T) {
	publicKey, privatePEM, err := jwtingress.GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(privatePEM), "BEGIN PRIVATE KEY") || strings.Contains(publicKey, "PRIVATE") {
		t.Fatalf("私钥是 PKCS#8 PEM、公钥是 base64url：%q", publicKey)
	}
	key, err := jwtingress.ParsePrivateKey(privatePEM)
	if err != nil {
		t.Fatal(err)
	}
	pemPub, err := jwtingress.PublicKeyPEM(key.Public().(ed25519.PublicKey))
	if err != nil {
		t.Fatal(err)
	}
	for _, form := range []string{publicKey, pemPub} {
		parsed, err := jwtingress.ParsePublicKey(form)
		if err != nil || !parsed.Equal(key.Public()) {
			t.Fatalf("公钥 %q 解析不对：%v", form, err)
		}
	}
	clk := testkit.NewFakeClock()
	p, err := jwtingress.New(jwtingress.Options{Config: jwtingress.Config{Issuers: []jwtingress.Issuer{{Issuer: iss, Keys: map[string]string{kid: publicKey}}}}, Clock: clk})
	if err != nil {
		t.Fatal(err)
	}
	token, _ := jwtingress.NewSigner(iss, kid, key, clk).Issue(jwtingress.Claims{Tenant: conformance.TenantAID, User: "u", TTL: time.Minute})
	if _, err := p.Authenticate(context.Background(), ingress.Credentials{BearerToken: token}); err != nil {
		t.Fatal(err)
	}
}

func TestLeewayToleratesSmallClockSkew(t *testing.T) {
	clk := testkit.NewFakeClock()
	key := newKey(t)
	p, err := jwtingress.New(jwtingress.Options{Config: config(map[string]ed25519.PrivateKey{kid: key}), Clock: clk, Leeway: 5 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	token, _ := jwtingress.NewSigner(iss, kid, key, clk).Issue(jwtingress.Claims{Tenant: conformance.TenantAID, User: "u", TTL: time.Minute})
	clk.Advance(time.Minute + 3*time.Second)
	if _, err := p.Authenticate(context.Background(), ingress.Credentials{BearerToken: token}); err != nil {
		t.Fatalf("在容差之内：%v", err)
	}
	clk.Advance(5 * time.Second)
	if _, err := p.Authenticate(context.Background(), ingress.Credentials{BearerToken: token}); providererr.CategoryOf(err) != providererr.Denied {
		t.Fatalf("超过容差：%v", err)
	}
}
