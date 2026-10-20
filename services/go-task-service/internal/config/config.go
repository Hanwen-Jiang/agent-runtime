// Package config 从环境变量读取 task-service 的配置。
package config

import (
	"fmt"
	"net/url"
	"path/filepath"
	"strconv"

	"github.com/agentruntime/agent-runtime/libs/go-core/secret"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/session"
)

// 环境变量名。
const (
	EnvTemporalAddress     = "AR_TEMPORAL_ADDRESS"
	EnvTemporalNamespace   = "AR_TEMPORAL_NAMESPACE"
	EnvHTTPAddr            = "AR_HTTP_ADDR"
	EnvPublicBaseURL       = "AR_PUBLIC_BASE_URL"
	EnvDevSigningKey       = "AR_DEV_SIGNING_KEY"
	EnvDataDir             = "AR_DATA_DIR"
	EnvPinnedVersionsFile  = "AR_PINNED_VERSIONS_FILE"
	EnvTenantPoliciesFile  = "AR_DEV_TENANT_POLICIES_FILE"
	EnvObjectStoreDir      = "AR_OBJECT_STORE_DIR"
	EnvSecretsFile         = "AR_SECRETS_FILE"
	EnvClaimCheckThreshold = "AR_CLAIM_CHECK_THRESHOLD_BYTES"
	EnvRateLimitPerMinute  = "AR_DEV_RATE_LIMIT_PER_MINUTE"
	EnvReadOnlyLimit       = "AR_SESSION_READ_ONLY_LIMIT"
	EnvContinueAsNewTasks  = "AR_SESSION_CAN_TASKS"
	EnvContinueAsNewEvents = "AR_SESSION_CAN_EVENTS"
)

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
	// DevSigningKey 是开发用内置 Ingress 的签名密钥，打印与日志里只显示掩码。
	DevSigningKey      secret.Value
	DataDir            string
	PinnedVersionsFile string
	// TenantPoliciesFile 是开发用的租户策略文件（各租户的 effect 上限），见 taskapi.FilePolicyResolver；空表示都用默认值。
	TenantPoliciesFile string
	// ObjectStoreDir、SecretsFile 是 ObjectStore 与 SecretStore 开发实现的位置（Claim-Check 用，S-06）；
	// 与 Python Worker 指向同一处，两边才能互相解开对方卸载的载荷。默认放在 DataDir 下。
	ObjectStoreDir string
	SecretsFile    string
	// ClaimCheckThreshold 是 Claim-Check 的阈值（字节），默认 64 KiB。
	ClaimCheckThreshold int
	// RateLimitPerMinute 是内置 Ingress 每个租户每个路由每分钟的请求上限。
	RateLimitPerMinute int
	Session            session.Config
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
		Session:            session.DefaultConfig(),
	}
	cfg.PublicBaseURL = or(EnvPublicBaseURL, "http://"+cfg.HTTPAddr)
	cfg.ObjectStoreDir = or(EnvObjectStoreDir, filepath.Join(cfg.DataDir, "objects"))
	cfg.SecretsFile = or(EnvSecretsFile, filepath.Join(cfg.DataDir, "secrets.json"))
	if cfg.TemporalAddress == "" {
		return Config{}, fmt.Errorf("缺少 %s", EnvTemporalAddress)
	}
	key := getenv(EnvDevSigningKey)
	if key == "" {
		return Config{}, fmt.Errorf("缺少 %s", EnvDevSigningKey)
	}
	cfg.DevSigningKey = secret.New(key)
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
	if err := cfg.Session.Validate(); err != nil {
		return Config{}, fmt.Errorf("Session 参数不合法：%w", err)
	}
	return cfg, nil
}
