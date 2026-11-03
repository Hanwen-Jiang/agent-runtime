// Package config 从环境变量读取 task-service 的配置。
package config

import (
	"fmt"
	"net/url"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/s3objects"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/valkey"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/session"
)

// 环境变量名。
const (
	EnvTemporalAddress    = "AR_TEMPORAL_ADDRESS"
	EnvTemporalNamespace  = "AR_TEMPORAL_NAMESPACE"
	EnvHTTPAddr           = "AR_HTTP_ADDR"
	EnvPublicBaseURL      = "AR_PUBLIC_BASE_URL"
	EnvDevSigningKey      = "AR_DEV_SIGNING_KEY"
	EnvDataDir            = "AR_DATA_DIR"
	EnvPinnedVersionsFile = "AR_PINNED_VERSIONS_FILE"
	EnvTenantPoliciesFile = "AR_DEV_TENANT_POLICIES_FILE"
	EnvObjectStoreDir     = "AR_OBJECT_STORE_DIR"
	EnvSecretsFile        = "AR_SECRETS_FILE"
	// EnvSecretsSeedFile：集群里密钥文件放在共用的可写卷上（kek 要写回），静态密钥来自只读挂载的 K8s Secret，
	// serve 启动时把它并进 AR_SECRETS_FILE（filesecrets.MergeSeed；docs/plan/cloud-deploy.md D-04）。Worker 与 Memory Service 相同。
	EnvSecretsSeedFile     = "AR_SECRETS_SEED_FILE"
	EnvClaimCheckThreshold = "AR_CLAIM_CHECK_THRESHOLD_BYTES"
	EnvRelationalDSN       = "AR_RELATIONAL_DSN"
	EnvRateLimitPerMinute  = "AR_DEV_RATE_LIMIT_PER_MINUTE"
	EnvReadOnlyLimit       = "AR_SESSION_READ_ONLY_LIMIT"
	EnvContinueAsNewTasks  = "AR_SESSION_CAN_TASKS"
	EnvContinueAsNewEvents = "AR_SESSION_CAN_EVENTS"
	// EnvRegistryAddr 设了才启用 Registry（T-08）：在这个单独的内部端口上提供 RegistryService，新 Task 的固定版本改由它给出；
	// 不能与 Task API 共用端口，也不能再配 AR_PINNED_VERSIONS_FILE。EnvSchemasDir 是 Registry 校验 document 用的 /schemas，默认 schemas。
	EnvRegistryAddr = "AR_REGISTRY_ADDR"
	EnvSchemasDir   = "AR_SCHEMAS_DIR"
	// EnvEgressAdminURL 是出网网关的管理地址（D-16），例如 http://ar-agent-runtime-egress-gateway:8096：Task 结束、收回联网授权
	// 之后通知它立即断开；管理令牌取 AR_SECRETS_FILE 平台作用域的 egress-gateway/admin-token。没设时只靠网关几秒的缓存。
	EnvEgressAdminURL = "AR_EGRESS_GATEWAY_ADMIN_URL"
	// Workspace API（D-06）：等一次工作区操作的最长时间（秒，默认 600）；收不收 file:// 的仓库地址（只给测试，0 或 1）。
	EnvWorkspaceOperationTimeout  = "AR_WORKSPACE_OPERATION_TIMEOUT_S"
	EnvWorkspaceAllowLocalRemotes = "AR_WORKSPACE_ALLOW_LOCAL_REMOTES"
	// EnvTenantActiveTasks 按租户给准入时未终态 Task 的上限（没列出的租户不设上限，2026-11-03 人定，docs/plan/open-signup.md）：
	// 逗号分隔的“租户=个数”，0 也表示不设上限，例如 t1=2,t2=4。按用户类型的策略进租户策略的契约之前（S-01）放在部署配置里。
	EnvTenantActiveTasks = "AR_TENANT_ACTIVE_TASKS"
	// EnvProfile 是运行模式（TS-PROD-01，docs/plan/workspace-read-latency-and-disk-2026-11-03.md）：不设或 development 时
	// 开发实现可以兜底；production 时业务库必须是 Postgres，没有 AR_RELATIONAL_DSN 拒绝启动，不回落 AR_DATA_DIR 下的 SQLite。
	EnvProfile = "AR_PROFILE"
	// EnvValkeyURL 设了时 Cache 用 Valkey（CACHE-01，libs/go-core/adapters/valkey）：形如 valkey://:口令@主机:6379/0，
	// TLS 用 valkeys://。事件流、限流计数与锁放在那里，几个副本共享、重启不丢；没设时用进程内的开发实现。带口令，按密钥对待。
	EnvValkeyURL = "AR_VALKEY_URL"
)

// AR_PROFILE 的取值。
const (
	ProfileDevelopment = "development"
	ProfileProduction  = "production"
)

// production 读 AR_PROFILE：只认空、development 与 production，别的取值报错（拼错了不能悄悄当成开发模式）。
func production(getenv func(string) string) (bool, error) {
	switch getenv(EnvProfile) {
	case "", ProfileDevelopment:
		return false, nil
	case ProfileProduction:
		return true, nil
	default:
		return false, fmt.Errorf("%s 只能是 %s 或 %s", EnvProfile, ProfileDevelopment, ProfileProduction)
	}
}

// errNoDSNInProduction 是生产模式没配业务库时的错误。
var errNoDSNInProduction = fmt.Errorf("%s=%s 时要配 %s：生产模式不回落到 %s 下的 SQLite", EnvProfile, ProfileProduction, EnvRelationalDSN, EnvDataDir)

// DefaultClaimCheckThreshold 是 Claim-Check 的默认阈值（字节）。
const DefaultClaimCheckThreshold = 64 << 10

// StoreLocations 返回 ObjectStore、SecretStore 开发实现的位置与 Claim-Check 的阈值：replay 子命令只需要这几项，
// 不要求 Temporal 地址与签名密钥。
func StoreLocations(getenv func(string) string) (objectStoreDir, secretsFile string, threshold int, err error) {
	dataDir := getenv(EnvDataDir)
	if dataDir == "" {
		dataDir = "data"
	}
	objectStoreDir, secretsFile = getenv(EnvObjectStoreDir), getenv(EnvSecretsFile)
	if objectStoreDir == "" {
		objectStoreDir = filepath.Join(dataDir, "objects")
	}
	if secretsFile == "" {
		secretsFile = filepath.Join(dataDir, "secrets.json")
	}
	threshold = DefaultClaimCheckThreshold
	if raw := getenv(EnvClaimCheckThreshold); raw != "" {
		if threshold, err = strconv.Atoi(raw); err != nil || threshold < 1 {
			return "", "", 0, fmt.Errorf("%s 必须是正整数", EnvClaimCheckThreshold)
		}
	}
	return objectStoreDir, secretsFile, threshold, nil
}

// Config 是 serve 子命令的配置。
type Config struct {
	TemporalAddress   string
	TemporalNamespace string
	HTTPAddr          string
	PublicBaseURL     string
	// Production 是 AR_PROFILE=production：业务库必须是 Postgres（RelationalDSN 非空）。
	Production bool
	// DevSigningKey 是开发用内置 Ingress 的签名密钥，打印与日志里只显示掩码。
	DevSigningKey      secret.Value
	DataDir            string
	PinnedVersionsFile string
	// TenantPoliciesFile 是开发用的租户策略文件（各租户的 effect 上限），见 taskapi.FilePolicyResolver；空表示都用默认值。
	TenantPoliciesFile string
	// ActiveTaskLimits 是按租户给的未终态 Task 上限（AR_TENANT_ACTIVE_TASKS）；没列出的租户与 0 都不设上限。
	ActiveTaskLimits map[string]int
	// ObjectStoreDir、SecretsFile 是 ObjectStore 与 SecretStore 开发实现的位置（Claim-Check 用，S-06）；
	// 与 Python Worker 指向同一处，两边才能互相解开对方卸载的载荷。默认放在 DataDir 下。
	ObjectStoreDir string
	SecretsFile    string
	// SecretsSeedFile 非空时 serve 启动时并进 SecretsFile（EnvSecretsSeedFile）。
	SecretsSeedFile string
	// ObjectStoreS3 非空时 ObjectStore 用 S3 兼容实现（R2、S3、MinIO；AR_S3_*，见 s3objects.OptionsFromEnv；D-09），
	// ObjectStoreDir 不再用。Worker 读同一组变量。
	ObjectStoreS3 *s3objects.Options
	// ClaimCheckThreshold 是 Claim-Check 的阈值（字节），默认 64 KiB。
	ClaimCheckThreshold int
	// RelationalDSN 是业务库（Postgres，行级安全）的连接串；为空时用 SQLite 开发实现（S-12a）。可能带口令，打印只显示掩码。
	RelationalDSN secret.Value
	// ValkeyURL 见 EnvValkeyURL；为空时 Cache 用进程内的开发实现。打印只显示掩码。
	ValkeyURL secret.Value
	// RateLimitPerMinute 是内置 Ingress 每个租户每个路由每分钟的请求上限。
	RateLimitPerMinute int
	Session            session.Config
	// RegistryAddr 为空表示不启用 Registry；SchemasDir 见 EnvSchemasDir。
	RegistryAddr string
	SchemasDir   string
	// EgressAdminURL 见 EnvEgressAdminURL。
	EgressAdminURL string
	// WorkspaceOperationTimeout 是 WorkspaceService 等一次工作区操作的最长时间（EnvWorkspaceOperationTimeout，默认 10 分钟）；
	// WorkspaceAllowLocalRemotes 让它收 file:// 的仓库地址（EnvWorkspaceAllowLocalRemotes，只给测试；Worker 读同一个变量）。
	WorkspaceOperationTimeout  time.Duration
	WorkspaceAllowLocalRemotes bool
}

// FromEnv 读取并校验配置。错误信息只提变量名，不带取值。
func FromEnv(getenv func(string) string) (Config, error) {
	or := func(key, fallback string) string {
		if v := getenv(key); v != "" {
			return v
		}
		return fallback
	}
	cfg := Config{
		TemporalAddress:    getenv(EnvTemporalAddress),
		TemporalNamespace:  or(EnvTemporalNamespace, "default"),
		HTTPAddr:           or(EnvHTTPAddr, "127.0.0.1:8080"),
		DataDir:            or(EnvDataDir, "data"),
		PinnedVersionsFile: getenv(EnvPinnedVersionsFile),
		TenantPoliciesFile: getenv(EnvTenantPoliciesFile),
		RegistryAddr:       getenv(EnvRegistryAddr),
		SchemasDir:         or(EnvSchemasDir, "schemas"),
		Session:            session.DefaultConfig(),
		EgressAdminURL:     getenv(EnvEgressAdminURL),
	}
	cfg.PublicBaseURL = or(EnvPublicBaseURL, "http://"+cfg.HTTPAddr)
	cfg.ObjectStoreDir = or(EnvObjectStoreDir, filepath.Join(cfg.DataDir, "objects"))
	cfg.SecretsFile = or(EnvSecretsFile, filepath.Join(cfg.DataDir, "secrets.json"))
	cfg.SecretsSeedFile = getenv(EnvSecretsSeedFile)
	if cfg.TemporalAddress == "" {
		return Config{}, fmt.Errorf("缺少 %s", EnvTemporalAddress)
	}
	key := getenv(EnvDevSigningKey)
	if key == "" {
		return Config{}, fmt.Errorf("缺少 %s", EnvDevSigningKey)
	}
	cfg.DevSigningKey = secret.New(key)
	if dsn := getenv(EnvRelationalDSN); dsn != "" {
		cfg.RelationalDSN = secret.New(dsn)
	}
	prod, err := production(getenv)
	if err != nil {
		return Config{}, err
	}
	if prod && cfg.RelationalDSN.IsZero() {
		return Config{}, errNoDSNInProduction
	}
	cfg.Production = prod
	if raw := getenv(EnvValkeyURL); raw != "" {
		cfg.ValkeyURL = secret.New(raw)
		// 只校验格式，不连接（serve 启动时再 Ping）；错误信息不带原文。
		if _, err := valkey.New(valkey.Options{URL: cfg.ValkeyURL}); err != nil {
			return Config{}, fmt.Errorf("%s 不合法：%w", EnvValkeyURL, err)
		}
	}
	if s3, ok, err := s3objects.OptionsFromEnv(getenv); err != nil {
		return Config{}, err
	} else if ok {
		cfg.ObjectStoreS3 = &s3
	}
	if u, err := url.Parse(cfg.PublicBaseURL); err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return Config{}, fmt.Errorf("%s 必须是 http 或 https 地址", EnvPublicBaseURL)
	}
	ints := []struct {
		key    string
		target *int
	}{
		{EnvRateLimitPerMinute, &cfg.RateLimitPerMinute},
		{EnvReadOnlyLimit, &cfg.Session.ReadOnlyLimit},
		{EnvContinueAsNewTasks, &cfg.Session.ContinueAsNewTasks},
		{EnvContinueAsNewEvents, &cfg.Session.ContinueAsNewEvents},
		{EnvClaimCheckThreshold, &cfg.ClaimCheckThreshold},
	}
	cfg.RateLimitPerMinute = 1200
	cfg.ClaimCheckThreshold = DefaultClaimCheckThreshold
	for _, f := range ints {
		raw := getenv(f.key)
		if raw == "" {
			continue
		}
		n, err := strconv.Atoi(raw)
		if err != nil {
			return Config{}, fmt.Errorf("%s 必须是整数", f.key)
		}
		*f.target = n
	}
	if cfg.RateLimitPerMinute < 1 {
		return Config{}, fmt.Errorf("%s 必须大于 0", EnvRateLimitPerMinute)
	}
	if cfg.ClaimCheckThreshold < 1 {
		return Config{}, fmt.Errorf("%s 必须大于 0", EnvClaimCheckThreshold)
	}
	if cfg.RegistryAddr != "" && cfg.PinnedVersionsFile != "" {
		return Config{}, fmt.Errorf("%s 与 %s 不能同时配置：固定版本由 Registry 给出", EnvRegistryAddr, EnvPinnedVersionsFile)
	}
	if cfg.RegistryAddr != "" && cfg.RegistryAddr == cfg.HTTPAddr {
		return Config{}, fmt.Errorf("%s 要用单独的内部端口，不能与 %s 相同", EnvRegistryAddr, EnvHTTPAddr)
	}
	if err := cfg.Session.Validate(); err != nil {
		return Config{}, fmt.Errorf("Session 参数不合法：%w", err)
	}
	limits, err := parseActiveTaskLimits(getenv(EnvTenantActiveTasks))
	if err != nil {
		return Config{}, err
	}
	cfg.ActiveTaskLimits = limits
	cfg.WorkspaceOperationTimeout = 10 * time.Minute
	if raw := getenv(EnvWorkspaceOperationTimeout); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil || n < 1 {
			return Config{}, fmt.Errorf("%s 必须是正整数（秒）", EnvWorkspaceOperationTimeout)
		}
		cfg.WorkspaceOperationTimeout = time.Duration(n) * time.Second
	}
	switch getenv(EnvWorkspaceAllowLocalRemotes) {
	case "", "0":
	case "1":
		cfg.WorkspaceAllowLocalRemotes = true
	default:
		return Config{}, fmt.Errorf("%s 只能是 0 或 1", EnvWorkspaceAllowLocalRemotes)
	}
	return cfg, nil
}

// Store 是业务库与 schema 目录的位置：registry-seed 子命令只需要这几项，不要求 Temporal 地址与签名密钥。
type Store struct {
	DataDir string
	// RelationalDSN 为空时用 DataDir 下的 SQLite（生产模式下不许）；打印只显示掩码。
	RelationalDSN secret.Value
	SchemasDir    string
	// Production 见 Config.Production。
	Production bool
}

// Store 返回 serve 用的业务库位置。
func (c Config) Store() Store {
	return Store{DataDir: c.DataDir, RelationalDSN: c.RelationalDSN, SchemasDir: c.SchemasDir, Production: c.Production}
}

// StoreFromEnv 读取业务库与 schema 目录的位置（AR_DATA_DIR、AR_RELATIONAL_DSN、AR_SCHEMAS_DIR、AR_PROFILE）。
func StoreFromEnv(getenv func(string) string) (Store, error) {
	s := Store{DataDir: getenv(EnvDataDir), SchemasDir: getenv(EnvSchemasDir)}
	if s.DataDir == "" {
		s.DataDir = "data"
	}
	if s.SchemasDir == "" {
		s.SchemasDir = "schemas"
	}
	if dsn := getenv(EnvRelationalDSN); dsn != "" {
		s.RelationalDSN = secret.New(dsn)
	}
	prod, err := production(getenv)
	if err != nil {
		return Store{}, err
	}
	if prod && s.RelationalDSN.IsZero() {
		return Store{}, errNoDSNInProduction
	}
	s.Production = prod
	return s, nil
}

// parseActiveTaskLimits 解析 AR_TENANT_ACTIVE_TASKS：“租户=个数”，逗号分隔，个数是非负整数（0 表示不设上限）。
func parseActiveTaskLimits(raw string) (map[string]int, error) {
	out := map[string]int{}
	if strings.TrimSpace(raw) == "" {
		return out, nil
	}
	for _, part := range strings.Split(raw, ",") {
		tenant, count, ok := strings.Cut(strings.TrimSpace(part), "=")
		tenant, count = strings.TrimSpace(tenant), strings.TrimSpace(count)
		n, err := strconv.Atoi(count)
		if !ok || tenant == "" || err != nil || n < 0 {
			return nil, fmt.Errorf("%s 的每一项要写成 租户=个数（非负整数），得到 %q", EnvTenantActiveTasks, strings.TrimSpace(part))
		}
		if _, dup := out[tenant]; dup {
			return nil, fmt.Errorf("%s 里租户 %s 写了两次", EnvTenantActiveTasks, tenant)
		}
		out[tenant] = n
	}
	return out, nil
}
