package main

// S-03 验收：命令行入口。dev-token 签发的令牌能通过内置 Ingress 的认证（端到端测试用它）；
// replay 子命令就是重放测试工具（计划 S-03 交付“重放测试工具”），回放成功返回 0，失败返回 1。

import (
	"bytes"
	"context"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/jwtingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/localingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/memcache"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/config"
)

const signingKey = "cmd-test-signing-key"

func getenv(m map[string]string) func(string) string { return func(k string) string { return m[k] } }

func TestDevTokenIsAcceptedByTheIngress(t *testing.T) {
	var out, errOut bytes.Buffer
	code := run([]string{"dev-token", "--tenant", "t1", "--user", "u1", "--ttl", "1h"}, &out, &errOut,
		getenv(map[string]string{"AR_DEV_SIGNING_KEY": signingKey}))
	if code != 0 {
		t.Fatalf("dev-token 应成功，退出码 %d：%s", code, errOut.String())
	}
	token := strings.TrimSpace(out.String())
	id, err := localingress.New(localingress.Options{SigningKey: []byte(signingKey)}).
		Authenticate(context.Background(), ingress.Credentials{BearerToken: token})
	if err != nil || id.TenantID != "t1" || id.UserID != "u1" {
		t.Errorf("令牌应能通过认证并带出 t1/u1，实际 %+v（%v）", id, err)
	}
}

func TestDevTokenCarriesScopes(t *testing.T) {
	var out, errOut bytes.Buffer
	code := run([]string{"dev-token", "--tenant", "t1", "--user", "u3", "--scope", "task.approve"}, &out, &errOut,
		getenv(map[string]string{"AR_DEV_SIGNING_KEY": signingKey}))
	if code != 0 {
		t.Fatalf("dev-token 应成功，退出码 %d：%s", code, errOut.String())
	}
	id, err := localingress.New(localingress.Options{SigningKey: []byte(signingKey)}).
		Authenticate(context.Background(), ingress.Credentials{BearerToken: strings.TrimSpace(out.String())})
	if err != nil || len(id.Scopes) != 1 || id.Scopes[0] != "task.approve" {
		t.Errorf("令牌应带出 scope task.approve（R-05 评审 D9），实际 %+v（%v）", id, err)
	}
}

func TestDevTokenNeedsTheSigningKey(t *testing.T) {
	var out, errOut bytes.Buffer
	if code := run([]string{"dev-token", "--tenant", "t1", "--user", "u1"}, &out, &errOut, getenv(nil)); code == 0 {
		t.Error("没有 AR_DEV_SIGNING_KEY 时应失败")
	}
}

func TestUnknownSubcommandIsAUsageError(t *testing.T) {
	var out, errOut bytes.Buffer
	if code := run([]string{"frobnicate"}, &out, &errOut, getenv(nil)); code != 2 {
		t.Errorf("未知子命令应返回 2，实际 %d", code)
	}
	if code := run(nil, &out, &errOut, getenv(nil)); code != 2 {
		t.Errorf("没有子命令应返回 2，实际 %d", code)
	}
}

// TSP-1 验收（TS-PROD-01 ①）：生产模式下 serve 没有 AR_RELATIONAL_DSN 时在连 Temporal 之前就以配置错误退出（2），
// 不在 AR_DATA_DIR 下建 SQLite。
func TestServeRefusesSQLiteInProduction(t *testing.T) {
	dataDir := t.TempDir()
	var out, errOut bytes.Buffer
	code := run([]string{"serve"}, &out, &errOut, getenv(map[string]string{
		"AR_PROFILE":          "production",
		"AR_TEMPORAL_ADDRESS": "127.0.0.1:1",
		"AR_DEV_SIGNING_KEY":  signingKey,
		"AR_DATA_DIR":         dataDir,
	}))
	if code != 2 {
		t.Fatalf("应以配置错误退出（2），实际 %d：%s", code, errOut.String())
	}
	if !strings.Contains(errOut.String(), "AR_RELATIONAL_DSN") {
		t.Errorf("错误信息指出缺的是 AR_RELATIONAL_DSN：%s", errOut.String())
	}
	if strings.Contains(errOut.String(), signingKey) {
		t.Error("错误信息不能带出签名密钥")
	}
	if _, err := os.Stat(filepath.Join(dataDir, "relational")); !os.IsNotExist(err) {
		t.Errorf("不应建 SQLite 的目录：%v", err)
	}
}

// registry-seed 在生产模式下同样不回落 SQLite。
func TestRegistrySeedRefusesSQLiteInProduction(t *testing.T) {
	dataDir := t.TempDir()
	seed := filepath.Join(t.TempDir(), "seed.json")
	if err := os.WriteFile(seed, []byte(`{"entries":[]}`), 0o644); err != nil {
		t.Fatal(err)
	}
	var out, errOut bytes.Buffer
	code := run([]string{"registry-seed", seed}, &out, &errOut, getenv(map[string]string{
		"AR_PROFILE":  "production",
		"AR_DATA_DIR": dataDir,
	}))
	if code == 0 {
		t.Fatal("生产模式没有连接串时 registry-seed 应失败")
	}
	if !strings.Contains(errOut.String(), "AR_RELATIONAL_DSN") {
		t.Errorf("错误信息指出缺的是 AR_RELATIONAL_DSN：%s", errOut.String())
	}
	if _, err := os.Stat(filepath.Join(dataDir, "relational")); !os.IsNotExist(err) {
		t.Errorf("不应建 SQLite 的目录：%v", err)
	}
}

// 构造业务库的函数本身也守住这一条：生产模式下没有连接串直接报错，不建目录（防止以后新的调用方绕过配置校验）。
func TestNewRelationalStoreNeverFallsBackInProduction(t *testing.T) {
	dataDir := t.TempDir()
	if _, err := newRelationalStore(context.Background(), config.Store{DataDir: dataDir, Production: true}); err == nil {
		t.Fatal("生产模式没有连接串应当报错")
	}
	if _, err := os.Stat(filepath.Join(dataDir, "relational")); !os.IsNotExist(err) {
		t.Errorf("不应建 SQLite 的目录：%v", err)
	}
	store, err := newRelationalStore(context.Background(), config.Store{DataDir: dataDir})
	if err != nil {
		t.Fatalf("开发模式回落 SQLite：%v", err)
	}
	_ = store.Close()
}

// CACHE-01：配了 AR_VALKEY_URL 而连不上时 serve 起不来（不悄悄退回进程内的 Cache），错误信息不带口令；没配时是开发实现。
func TestNewCacheFailsFastWhenValkeyIsUnreachable(t *testing.T) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := l.Addr().String()
	_ = l.Close()
	cfg := config.Config{ValkeyURL: secret.New("valkey://:cmd-valkey-s3cret@" + addr)}
	_, err = newCache(context.Background(), cfg, clock.System{})
	if err == nil {
		t.Fatal("连不上 Valkey 时应当报错")
	}
	if !strings.Contains(err.Error(), "AR_VALKEY_URL") || strings.Contains(err.Error(), "cmd-valkey-s3cret") {
		t.Errorf("错误信息要指出变量名、不带口令：%v", err)
	}
	p, err := newCache(context.Background(), config.Config{}, clock.System{})
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := p.(*memcache.Provider); !ok {
		t.Errorf("没配 AR_VALKEY_URL 时用进程内的开发实现，实际 %T", p)
	}
	_ = p.Close()
}

// AUTH-01（docs/plan/auth-tokens-2026-11-03.md）：serve 按配置构造 Ingress。配了签发方登记时验 Ed25519 的 JWT，
// 收开发令牌时两种都收（迁移期间），不收时开发令牌被拒；生产模式下仍收开发令牌要记一条警告；登记文件坏了起不来。
func TestNewIngressFollowsTheConfig(t *testing.T) {
	ctx := context.Background()
	dir := t.TempDir()
	publicKey, privatePEM, err := jwtingress.GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	issuers := filepath.Join(dir, "issuers.json")
	if err := os.WriteFile(issuers, []byte(`{"issuers": [{"iss": "coding-agent", "keys": {"k1": "`+publicKey+`"}}]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	key, err := jwtingress.ParsePrivateKey(privatePEM)
	if err != nil {
		t.Fatal(err)
	}
	jwt, _ := jwtingress.NewSigner("coding-agent", "k1", key, nil).Issue(jwtingress.Claims{Tenant: "t1", User: "u1", TTL: time.Hour})
	dev := localingress.New(localingress.Options{SigningKey: []byte(signingKey)}).IssueToken("t1", "u1", time.Hour)
	caches := memcache.New(memcache.Options{})
	defer caches.Close()
	base := config.Config{DevSigningKey: secret.New(signingKey), RateLimitPerMinute: 60}
	cases := []struct {
		name         string
		edit         func(*config.Config)
		jwtOK, devOK bool
		warnsInLog   bool
	}{
		{"只收开发令牌（开发模式的默认）", func(c *config.Config) { c.DevTokens = true }, false, true, false},
		{"迁移期间两种都收", func(c *config.Config) { c.DevTokens, c.IngressIssuersFile = true, issuers }, true, true, false},
		{"只收 JWT", func(c *config.Config) { c.IngressIssuersFile, c.DevSigningKey = issuers, secret.Value{} }, true, false, false},
		{"生产模式迁移期间", func(c *config.Config) { c.Production, c.DevTokens, c.IngressIssuersFile = true, true, issuers }, true, true, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := base
			tc.edit(&cfg)
			var logs bytes.Buffer
			ing, err := newIngress(cfg, caches, clock.System{}, slog.New(slog.NewTextHandler(&logs, nil)))
			if err != nil {
				t.Fatal(err)
			}
			defer ing.Close()
			for token, want := range map[string]bool{jwt: tc.jwtOK, dev: tc.devOK} {
				_, err := ing.Authenticate(ctx, ingress.Credentials{BearerToken: token})
				if (err == nil) != want {
					t.Errorf("令牌 %.12s…：应当%v，实际 %v", token, map[bool]string{true: "收", false: "拒"}[want], err)
				}
			}
			if warned := strings.Contains(logs.String(), config.EnvIngressDevTokens); warned != tc.warnsInLog {
				t.Errorf("生产模式收开发令牌时记警告（且只在那时）：%s", logs.String())
			}
			if strings.Contains(logs.String(), signingKey) {
				t.Error("日志不能带出签名密钥")
			}
		})
	}
	broken := filepath.Join(dir, "broken.json")
	_ = os.WriteFile(broken, []byte(`{"issuers": []}`), 0o600)
	for _, path := range []string{broken, filepath.Join(dir, "missing.json")} {
		if _, err := newIngress(config.Config{IngressIssuersFile: path}, caches, clock.System{}, slog.Default()); err == nil {
			t.Errorf("登记文件 %s 不合格时 serve 起不来", filepath.Base(path))
		}
	}
}

// AUTH-01：keygen 生成一对密钥，私钥写进 --out（不覆盖已有的文件），标准输出是能直接用的签发方登记（只有公钥）；
// dev-token --jwt-key 用这把私钥签 JWT，按那份登记验得过。
func TestKeygenAndJWTDevToken(t *testing.T) {
	dir := t.TempDir()
	keyFile := filepath.Join(dir, "coding-agent.pem")
	var out, errOut bytes.Buffer
	if code := run([]string{"keygen", "--iss", "coding-agent", "--kid", "k2026", "--out", keyFile}, &out, &errOut, getenv(nil)); code != 0 {
		t.Fatalf("keygen 应成功，退出码 %d：%s", code, errOut.String())
	}
	private, err := os.ReadFile(keyFile)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := jwtingress.ParsePrivateKey(private); err != nil {
		t.Fatalf("--out 里是私钥：%v", err)
	}
	if strings.Contains(out.String(), "PRIVATE") || strings.Contains(out.String(), strings.TrimSpace(string(private))) {
		t.Fatal("标准输出不能带出私钥")
	}
	issuers := filepath.Join(dir, "issuers.json")
	if err := os.WriteFile(issuers, out.Bytes(), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := jwtingress.LoadConfig(issuers)
	if err != nil || len(cfg.Issuers) != 1 || cfg.Issuers[0].Issuer != "coding-agent" || cfg.Issuers[0].Keys["k2026"] == "" {
		t.Fatalf("标准输出是签发方登记：%+v %v", cfg, err)
	}
	if code := run([]string{"keygen", "--iss", "coding-agent", "--kid", "k2", "--out", keyFile}, &out, &errOut, getenv(nil)); code == 0 {
		t.Error("--out 已经有文件时不覆盖")
	}

	out.Reset()
	code := run([]string{"dev-token", "--tenant", "t1", "--user", "u1", "--scope", "task.approve", "--jwt-key", keyFile, "--iss", "coding-agent", "--kid", "k2026"},
		&out, &errOut, getenv(nil))
	if code != 0 {
		t.Fatalf("dev-token --jwt-key 不要 AR_DEV_SIGNING_KEY：退出码 %d：%s", code, errOut.String())
	}
	p, err := jwtingress.New(jwtingress.Options{Config: cfg})
	if err != nil {
		t.Fatal(err)
	}
	id, err := p.Authenticate(context.Background(), ingress.Credentials{BearerToken: strings.TrimSpace(out.String())})
	if err != nil || id.TenantID != "t1" || id.UserID != "u1" || len(id.Scopes) != 1 || id.Scopes[0] != "task.approve" {
		t.Fatalf("JWT 验得过并带出 t1/u1/task.approve：%+v %v", id, err)
	}
}

func TestReplaySubcommand(t *testing.T) {
	golden := filepath.Join("..", "..", "internal", "session", "testdata", "histories")
	var out, errOut bytes.Buffer
	if code := run([]string{"replay", "--dir", golden}, &out, &errOut, getenv(nil)); code != 0 {
		t.Fatalf("当前代码应能回放 %s，退出码 %d：%s", golden, code, errOut.String())
	}

	empty := t.TempDir()
	if code := run([]string{"replay", "--dir", empty}, &out, &errOut, getenv(nil)); code == 0 {
		t.Error("目录里没有事件历史时应失败，免得误以为回放过了")
	}

	broken := t.TempDir()
	if err := os.WriteFile(filepath.Join(broken, "bad.json"), []byte(`{"events":[{"eventId":"1"}]}`), 0o644); err != nil {
		t.Fatal(err)
	}
	errOut.Reset()
	if code := run([]string{"replay", "--dir", broken}, &out, &errOut, getenv(nil)); code != 1 {
		t.Errorf("回放失败应返回 1，实际 %d", code)
	}
	if !strings.Contains(errOut.String(), "bad.json") {
		t.Errorf("失败信息应指出哪份历史回放失败：%s", errOut.String())
	}
}
