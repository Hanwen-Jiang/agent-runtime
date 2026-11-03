package config

// S-03 验收：task-service 的配置。Session 工作流的上限经这里设置，必须过 session.Config.Validate
// （计划 S-03 难点“上限”）；签名密钥不得出现在日志与错误信息里（CLAUDE.md“其他”）。

import (
	"fmt"
	"path/filepath"
	"strings"
	"testing"
)

const key = "dev-signing-key-should-never-be-printed"

func env(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

func base() map[string]string {
	return map[string]string{
		EnvTemporalAddress: "127.0.0.1:7233",
		EnvHTTPAddr:        "127.0.0.1:8080",
		EnvPublicBaseURL:   "http://127.0.0.1:8080",
		EnvDevSigningKey:   key,
		EnvDataDir:         "data",
	}
}

func TestFromEnvDefaults(t *testing.T) {
	cfg, err := FromEnv(env(base()))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.TemporalNamespace != "default" {
		t.Errorf("默认 namespace 为 default，实际 %q", cfg.TemporalNamespace)
	}
	if cfg.DevSigningKey.RevealString() != key {
		t.Error("签名密钥没有读进来")
	}
	if err := cfg.Session.Validate(); err != nil {
		t.Errorf("默认的 Session 参数应当合法：%v", err)
	}
}

func TestFromEnvSessionLimits(t *testing.T) {
	m := base()
	m[EnvReadOnlyLimit] = "2"
	m[EnvContinueAsNewTasks] = "3"
	m[EnvContinueAsNewEvents] = "4000"
	cfg, err := FromEnv(env(m))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Session.ReadOnlyLimit != 2 || cfg.Session.ContinueAsNewTasks != 3 || cfg.Session.ContinueAsNewEvents != 4000 {
		t.Errorf("Session 参数没有读进来：%+v", cfg.Session)
	}
}

// S-06：Claim-Check 用的 ObjectStore、SecretStore 与阈值；变量名与 Python Worker 相同。
func TestFromEnvClaimCheck(t *testing.T) {
	cfg, err := FromEnv(env(base()))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ClaimCheckThreshold != 64<<10 || cfg.ObjectStoreDir != filepath.Join("data", "objects") || cfg.SecretsFile != filepath.Join("data", "secrets.json") {
		t.Errorf("默认值：阈值 64 KiB，ObjectStore 与密钥文件放在数据目录下，实际 %d / %q / %q", cfg.ClaimCheckThreshold, cfg.ObjectStoreDir, cfg.SecretsFile)
	}
	m := base()
	m[EnvObjectStoreDir] = "/shared/objects"
	m[EnvSecretsFile] = "/shared/secrets.json"
	m[EnvClaimCheckThreshold] = "1024"
	cfg, err = FromEnv(env(m))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ObjectStoreDir != "/shared/objects" || cfg.SecretsFile != "/shared/secrets.json" || cfg.ClaimCheckThreshold != 1024 {
		t.Errorf("环境变量没有读进来：%q / %q / %d", cfg.ObjectStoreDir, cfg.SecretsFile, cfg.ClaimCheckThreshold)
	}
	for _, bad := range []string{"0", "-1", "big"} {
		m[EnvClaimCheckThreshold] = bad
		if _, err := FromEnv(env(m)); err == nil {
			t.Errorf("阈值 %q 应当报错", bad)
		}
	}
}

// S-12a：配置了 AR_RELATIONAL_DSN 时业务库用 Postgres（行级安全）；连接串可能带口令，按密钥对待。
func TestFromEnvRelationalDSN(t *testing.T) {
	cfg, err := FromEnv(env(base()))
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.RelationalDSN.IsZero() {
		t.Error("没配置时业务库用 SQLite（开发实现）")
	}
	m := base()
	dsn := "postgres://app:s3cret-password@db.internal:5432/agentruntime"
	m[EnvRelationalDSN] = dsn
	cfg, err = FromEnv(env(m))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.RelationalDSN.RevealString() != dsn {
		t.Error("连接串没有读进来")
	}
	if strings.Contains(fmt.Sprintf("%v %+v", cfg, cfg), "s3cret-password") {
		t.Error("打印配置时不能带出数据库口令")
	}
}

// TSP-1 验收（TS-PROD-01 ①，docs/plan/workspace-read-latency-and-disk-2026-11-03.md 第 2 节）：AR_PROFILE=production 时
// 业务库必须是 Postgres，没配 AR_RELATIONAL_DSN 就拒绝启动，不静默回落到 AR_DATA_DIR 下的 SQLite；
// 不设或设 development 时行为不变（开发与端到端测试不设它）。
func TestFromEnvProfile(t *testing.T) {
	for _, profile := range []string{"", ProfileDevelopment} {
		m := base()
		m[EnvProfile] = profile
		cfg, err := FromEnv(env(m))
		if err != nil {
			t.Fatalf("AR_PROFILE=%q 时没有连接串也能起来（SQLite 开发实现）：%v", profile, err)
		}
		if cfg.Production {
			t.Errorf("AR_PROFILE=%q 不是生产模式", profile)
		}
	}

	m := base()
	m[EnvProfile] = ProfileProduction
	_, err := FromEnv(env(m))
	if err == nil {
		t.Fatal("生产模式没有 AR_RELATIONAL_DSN 应当拒绝启动")
	}
	for _, name := range []string{EnvRelationalDSN, EnvProfile} {
		if !strings.Contains(err.Error(), name) {
			t.Errorf("错误信息指出变量名 %s：%v", name, err)
		}
	}
	if strings.Contains(err.Error(), key) {
		t.Error("错误信息不能带出签名密钥")
	}

	dsn := "postgres://app:s3cret-password@db.internal:5432/agentruntime"
	m[EnvRelationalDSN] = dsn
	// TSP-2（TS-PROD-01 ②）：生产模式的 Cache 也必须是 Valkey，进程内的 Cache 只能单副本、重启丢流。
	_, err = FromEnv(env(m))
	if err == nil || !strings.Contains(err.Error(), EnvValkeyURL) {
		t.Fatalf("生产模式没有 %s 应当拒绝启动并指出变量名，实际 %v", EnvValkeyURL, err)
	}
	m[EnvValkeyURL] = "valkey://:valkey-s3cret@valkey-agentruntime.data.svc:6379/0"
	cfg, err := FromEnv(env(m))
	if err != nil {
		t.Fatalf("生产模式配了连接串与 Valkey 应当能起来：%v", err)
	}
	if !cfg.Production || cfg.RelationalDSN.RevealString() != dsn {
		t.Errorf("生产模式与连接串没有读进来：production=%v", cfg.Production)
	}

	for _, bad := range []string{"prod", "Production", " production", "staging"} {
		m := base()
		m[EnvRelationalDSN] = dsn
		m[EnvProfile] = bad
		_, err := FromEnv(env(m))
		if err == nil || !strings.Contains(err.Error(), EnvProfile) {
			t.Errorf("AR_PROFILE=%q 应当报错并指出变量名，实际 %v", bad, err)
		}
	}
}

// registry-seed 写的是 serve 用的同一个业务库：生产模式下同样不回落 SQLite（否则种子写进了没人读的文件）。
func TestStoreFromEnvProfile(t *testing.T) {
	s, err := StoreFromEnv(env(map[string]string{}))
	if err != nil || s.Production || s.DataDir != "data" || s.SchemasDir != "schemas" {
		t.Errorf("不设时是开发模式、默认目录：%+v %v", s, err)
	}
	_, err = StoreFromEnv(env(map[string]string{EnvProfile: ProfileProduction}))
	if err == nil || !strings.Contains(err.Error(), EnvRelationalDSN) {
		t.Errorf("生产模式没有连接串应当报错并指出 %s，实际 %v", EnvRelationalDSN, err)
	}
	s, err = StoreFromEnv(env(map[string]string{EnvProfile: ProfileProduction, EnvRelationalDSN: "postgres://db/agentruntime"}))
	if err != nil || !s.Production || s.RelationalDSN.IsZero() {
		t.Errorf("生产模式配了连接串：%+v %v", s, err)
	}
	if _, err := StoreFromEnv(env(map[string]string{EnvProfile: "prod"})); err == nil {
		t.Error("AR_PROFILE 取值不对应当报错")
	}
}

// CACHE-01：设了 AR_VALKEY_URL 时 Cache 用 Valkey（事件流、限流、锁跨副本共享），否则用进程内的开发实现。
// 地址里有口令，按密钥对待；格式不对在启动时就报错（错误信息不带原文）。
func TestFromEnvValkeyURL(t *testing.T) {
	cfg, err := FromEnv(env(base()))
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.ValkeyURL.IsZero() {
		t.Error("没配置时 Cache 用进程内的开发实现")
	}
	m := base()
	u := "valkey://:valkey-s3cret@valkey-agentruntime.data.svc:6379/0"
	m[EnvValkeyURL] = u
	cfg, err = FromEnv(env(m))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ValkeyURL.RevealString() != u {
		t.Error("Valkey 地址没有读进来")
	}
	if strings.Contains(fmt.Sprintf("%v %+v %#v", cfg, cfg, cfg), "valkey-s3cret") {
		t.Error("打印配置时不能带出 Valkey 的口令")
	}
	for _, bad := range []string{"http://:valkey-s3cret@cache:6379", "valkey://:valkey-s3cret@", "not a url valkey-s3cret"} {
		m[EnvValkeyURL] = bad
		_, err := FromEnv(env(m))
		if err == nil || !strings.Contains(err.Error(), EnvValkeyURL) {
			t.Errorf("%q 应当报错并指出变量名，实际 %v", bad, err)
		} else if strings.Contains(err.Error(), "valkey-s3cret") {
			t.Errorf("错误信息带出了口令：%v", err)
		}
	}
}

// TSP-2（TS-PROD-01 ①）：业务库连接池的上限可配（AR_RELATIONAL_MAX_CONNS），副本多了以后按 Postgres 的
// max_connections 配比（Chart 的 taskService.relationalMaxConns）；不设时用适配器的默认值。
func TestFromEnvRelationalMaxConns(t *testing.T) {
	cfg, err := FromEnv(env(base()))
	if err != nil || cfg.RelationalMaxConns != 0 || cfg.Store().MaxConns != 0 {
		t.Fatalf("不设时用适配器的默认值：%d %v", cfg.RelationalMaxConns, err)
	}
	m := base()
	m[EnvRelationalMaxConns] = "12"
	cfg, err = FromEnv(env(m))
	if err != nil || cfg.RelationalMaxConns != 12 || cfg.Store().MaxConns != 12 {
		t.Fatalf("连接池上限没有读进来：%d %v", cfg.RelationalMaxConns, err)
	}
	for _, bad := range []string{"0", "-1", "many", "100000"} {
		m[EnvRelationalMaxConns] = bad
		if _, err := FromEnv(env(m)); err == nil || !strings.Contains(err.Error(), EnvRelationalMaxConns) {
			t.Errorf("%q 应当报错并指出变量名，实际 %v", bad, err)
		}
	}
}

func TestFromEnvRejectsBadValues(t *testing.T) {
	cases := map[string]map[string]string{
		"缺少 Temporal 地址": {EnvTemporalAddress: ""},
		"缺少签名密钥":         {EnvDevSigningKey: ""},
		"只读上限不是数字":       {EnvReadOnlyLimit: "many"},
		"只读上限超出范围":       {EnvReadOnlyLimit: "5000"},
		"续接事件数超出范围":      {EnvContinueAsNewEvents: "50000"},
		"订阅地址不是 URL":     {EnvPublicBaseURL: "not a url"},
	}
	for name, override := range cases {
		t.Run(name, func(t *testing.T) {
			m := base()
			for k, v := range override {
				m[k] = v
			}
			_, err := FromEnv(env(m))
			if err == nil {
				t.Fatal("应当报错")
			}
			if strings.Contains(err.Error(), key) {
				t.Error("错误信息不能带出签名密钥")
			}
		})
	}
}

func TestConfigNeverPrintsTheSigningKey(t *testing.T) {
	cfg, err := FromEnv(env(base()))
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range []string{fmt.Sprint(cfg), fmt.Sprintf("%+v", cfg), fmt.Sprintf("%#v", cfg)} {
		if strings.Contains(s, key) {
			t.Fatalf("打印配置时泄露了签名密钥：%s", s)
		}
	}
}

// T-08 验收：Registry 的配置（docs/plan/registry.md 第 2 节第 5、7 条）。设了 AR_REGISTRY_ADDR 才启用 Registry：
// 它在单独的内部端口上提供 RegistryService，新 Task 的固定版本改由它给出；这时不能再配固定版本文件（两处来源会互相顶替）。
// AR_SCHEMAS_DIR 是 Registry 校验 document 用的 /schemas 目录，默认 schemas（相对工作目录）。
func TestFromEnvRegistry(t *testing.T) {
	cfg, err := FromEnv(env(base()))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.RegistryAddr != "" {
		t.Errorf("没配置时不启用 Registry，实际 %q", cfg.RegistryAddr)
	}
	if cfg.SchemasDir != "schemas" {
		t.Errorf("schema 目录默认 schemas，实际 %q", cfg.SchemasDir)
	}

	m := base()
	m[EnvRegistryAddr] = "127.0.0.1:8092"
	m[EnvSchemasDir] = filepath.Join("repo", "schemas")
	cfg, err = FromEnv(env(m))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.RegistryAddr != "127.0.0.1:8092" || cfg.SchemasDir != filepath.Join("repo", "schemas") {
		t.Errorf("Registry 的地址与 schema 目录没有读进来：%q %q", cfg.RegistryAddr, cfg.SchemasDir)
	}

	for name, override := range map[string]map[string]string{
		"同时配了固定版本文件":      {EnvRegistryAddr: "127.0.0.1:8092", EnvPinnedVersionsFile: "pins.json"},
		"与 Task API 共用端口": {EnvRegistryAddr: "127.0.0.1:8080"},
	} {
		t.Run(name, func(t *testing.T) {
			m := base()
			for k, v := range override {
				m[k] = v
			}
			_, err := FromEnv(env(m))
			if err == nil {
				t.Fatal("应当报错")
			}
			if !strings.Contains(err.Error(), EnvRegistryAddr) {
				t.Errorf("错误信息指出变量名 %s：%v", EnvRegistryAddr, err)
			}
		})
	}
}

// 按租户给准入时“未终态 Task 至多几个”（没列出的租户不设上限）。
func TestFromEnvActiveTaskLimits(t *testing.T) {
	m := base()
	m[EnvTenantActiveTasks] = " swebench=8, evals=0 ,gh-hanwen-jiang=6"
	cfg, err := FromEnv(env(m))
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]int{"swebench": 8, "evals": 0, "gh-hanwen-jiang": 6}
	if fmt.Sprint(cfg.ActiveTaskLimits) != fmt.Sprint(want) {
		t.Errorf("读到 %v，要 %v", cfg.ActiveTaskLimits, want)
	}
	cfg, err = FromEnv(env(base()))
	if err != nil || len(cfg.ActiveTaskLimits) != 0 {
		t.Errorf("没设时不覆盖：%v %v", cfg.ActiveTaskLimits, err)
	}
	for _, bad := range []string{"swebench", "swebench=-1", "swebench=many", "=3", "a=1,a=2"} {
		m[EnvTenantActiveTasks] = bad
		if _, err := FromEnv(env(m)); err == nil {
			t.Errorf("%q 应当报错", bad)
		}
	}
}
