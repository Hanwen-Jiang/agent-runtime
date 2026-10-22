// Package config 从环境变量读取 model-gateway 的配置。错误信息只提变量名，不带取值。
package config

import (
	"errors"
	"net/url"
	"strings"
)

// 环境变量名。
const (
	// EnvAddr 是监听地址：同一端口提供 gRPC（h2c）与 GET /healthz。
	EnvAddr = "AR_MODEL_GATEWAY_ADDR"
	// EnvProfilesFile 是 Model Profile 文件（必填）。
	EnvProfilesFile = "AR_MODEL_PROFILES_FILE"
	// EnvProvider 选择供应商适配层：openai_compat（默认）或 mock（libs/go-core/dev/mockmodel）。
	EnvProvider = "AR_MODEL_PROVIDER"
	// EnvProviderBaseURL 是 OpenAI 兼容端点，形如 https://host/v1；openai_compat 时必填。
	EnvProviderBaseURL = "AR_MODEL_PROVIDER_BASE_URL"
	// EnvProviderKeyName 是密钥在 SecretStore 里的名字，默认 model-provider/api-key。
	EnvProviderKeyName = "AR_MODEL_PROVIDER_KEY_NAME"
	// EnvSecretsFile 是 SecretStore 开发实现（filesecrets）的文件；openai_compat 与 dev-secret 时必填。
	EnvSecretsFile = "AR_SECRETS_FILE"
	// EnvTenantPoliciesFile 是开发用的租户策略文件（可选的模型白名单），与 task-service 同一个文件。
	EnvTenantPoliciesFile = "AR_DEV_TENANT_POLICIES_FILE"
)

// 默认值与取值。
const (
	DefaultAddr          = "127.0.0.1:8091"
	DefaultKeyName       = "model-provider/api-key"
	ProviderOpenAICompat = "openai_compat"
	ProviderMock         = "mock"
)

// Config 是 serve 子命令的配置。
type Config struct {
	Addr               string
	ProfilesFile       string
	Provider           string
	ProviderBaseURL    string
	ProviderKeyName    string
	SecretsFile        string
	TenantPoliciesFile string
}

// FromEnv 读取并校验 serve 的配置：
//   - AR_MODEL_PROFILES_FILE 必填。
//   - AR_MODEL_PROVIDER 只能是 openai_compat 或 mock，默认 openai_compat。
//   - openai_compat 时 AR_MODEL_PROVIDER_BASE_URL 必填，且必须是带主机名的 http 或 https 地址；AR_SECRETS_FILE 必填。
//   - mock 时不要求端点与密钥文件。
func FromEnv(getenv func(string) string) (Config, error) {
	get := func(name string) string { return strings.TrimSpace(getenv(name)) }
	cfg := Config{
		Addr:               or(get(EnvAddr), DefaultAddr),
		ProfilesFile:       get(EnvProfilesFile),
		Provider:           or(get(EnvProvider), ProviderOpenAICompat),
		ProviderBaseURL:    get(EnvProviderBaseURL),
		ProviderKeyName:    or(get(EnvProviderKeyName), DefaultKeyName),
		SecretsFile:        get(EnvSecretsFile),
		TenantPoliciesFile: get(EnvTenantPoliciesFile),
	}
	if cfg.ProfilesFile == "" {
		return Config{}, errors.New("缺少 " + EnvProfilesFile)
	}
	switch cfg.Provider {
	case ProviderMock:
	case ProviderOpenAICompat:
		if cfg.ProviderBaseURL == "" {
			return Config{}, errors.New("缺少 " + EnvProviderBaseURL)
		}
		u, err := url.Parse(cfg.ProviderBaseURL)
		if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" {
			return Config{}, errors.New(EnvProviderBaseURL + " 必须是带主机名的 http 或 https 地址")
		}
		if cfg.SecretsFile == "" {
			return Config{}, errors.New("缺少 " + EnvSecretsFile)
		}
	default:
		return Config{}, errors.New(EnvProvider + " 只能是 " + ProviderOpenAICompat + " 或 " + ProviderMock)
	}
	return cfg, nil
}

// SecretsFile 返回 dev-secret 子命令要写的密钥文件（AR_SECRETS_FILE，必填）。
func SecretsFile(getenv func(string) string) (string, error) {
	path := strings.TrimSpace(getenv(EnvSecretsFile))
	if path == "" {
		return "", errors.New("缺少 " + EnvSecretsFile)
	}
	return path, nil
}

func or(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}
