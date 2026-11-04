package broker

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
)

// 环境配置（ENV-02；docs/plan/env-02-implementation-2026-11-03.md）：工作区选用的配置存在 workspaces 的行上（Worker 的
// Workspace 操作写，task-service 校验过）。服务只读它：setup、start 在每个新的执行环境上先后跑；.env 的变量进这条命令的 env；
// 有网络密钥时沙箱信任出网网关的 CA。

// CABundle 是沙箱里信任出网网关 CA 的证书包；CAEnv 是指向它的变量。
const CABundle = "/tmp/agentruntime-ca.pem"

// CAEnv 是指向证书包的变量。
var CAEnv = []string{"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "GIT_SSL_CAINFO"}

// Reserved 是平台注入的变量：.env 与测试用密钥都不能盖掉。
var Reserved = map[string]bool{}

func init() {
	for _, n := range []string{"PIP_INDEX_URL", "UV_DEFAULT_INDEX", "PIP_TRUSTED_HOST", "NPM_CONFIG_REGISTRY", "GOPROXY", "HTTP_PROXY", "HTTPS_PROXY",
		"NO_PROXY", "ALL_PROXY", "NPM_CONFIG_PROXY", "NPM_CONFIG_HTTPS_PROXY", "NPM_CONFIG_AUDIT", "NPM_CONFIG_FUND", "GONOSUMDB", "GOFLAGS"} {
		Reserved[n] = true
	}
	for _, n := range CAEnv {
		Reserved[n] = true
	}
}

// EnvironmentConfig 是服务要用到的部分。
type EnvironmentConfig struct {
	EnvironmentID string
	SetupScript   string
	StartCommand  string
	Variables     map[string]string
	SecretHeaders bool
}

var (
	dotenvName  = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
	secretWords = map[string]bool{"SECRET": true, "TOKEN": true, "PASSWORD": true, "PASSWD": true, "CREDENTIAL": true, "CREDENTIALS": true,
		"APIKEY": true, "PRIVATEKEY": true}
)

func secretLike(name string) bool {
	parts := strings.Split(strings.ToUpper(name), "_")
	for i, p := range parts {
		if secretWords[p] {
			return true
		}
		if i+1 < len(parts) && (p+parts[i+1] == "APIKEY" || p+parts[i+1] == "PRIVATEKEY") {
			return true
		}
	}
	return false
}

// ParseDotenv 解析 .env 文本（规则同 Worker 的 env_config.parse_dotenv：存之前已核过，这里再核一遍）。
func ParseDotenv(text string) (map[string]string, error) {
	if len(text) > 16*1024 {
		return nil, providererr.New(providererr.Invalid, "env_too_large", ".env 合计最多 16384 字节")
	}
	out := map[string]string{}
	for i, raw := range strings.Split(text, "\n") {
		line := strings.TrimSpace(raw)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		name, value, ok := strings.Cut(line, "=")
		if _, dup := out[name]; !ok || !dotenvName.MatchString(name) || dup || strings.ContainsRune(value, 0) {
			return nil, providererr.New(providererr.Invalid, "invalid_env_line", fmt.Sprintf(".env 第 %d 行要是 KEY=value（名字不重复）", i+1))
		}
		upper := strings.ToUpper(name)
		if Reserved[upper] || strings.HasPrefix(upper, "AGENT_") {
			return nil, providererr.New(providererr.Invalid, "env_reserved_name", fmt.Sprintf(".env 第 %d 行的名字是平台保留的", i+1))
		}
		if secretLike(name) {
			return nil, providererr.New(providererr.Invalid, "env_secret_name", fmt.Sprintf(".env 第 %d 行的名字像机密：机密请用网络密钥，不要写进 .env", i+1))
		}
		out[name] = value
	}
	if len(out) > 64 {
		return nil, providererr.New(providererr.Invalid, "env_too_large", ".env 最多 64 个变量")
	}
	return out, nil
}

// SetupKey 是 setup 的缓存键：sha256("agentruntime-setup-v1\0" ‖ sha256(脚本) ‖ "\0" ‖ 基础镜像 digest)（Q21）。
func SetupKey(script, baseDigest string) string {
	inner := sha256.Sum256([]byte(script))
	h := sha256.New()
	h.Write([]byte("agentruntime-setup-v1\x00"))
	h.Write(inner[:])
	h.Write([]byte("\x00" + baseDigest))
	return hex.EncodeToString(h.Sum(nil))
}

// Environment 是工作区选用的环境配置；没有选用（或工作区不在）时 nil。配置删除之后只剩非机密的部分。
func (b *Broker) Environment(ctx context.Context, owner, workspaceID string) (*EnvironmentConfig, error) {
	if workspaceID == "" || owner == ledger.PlatformID {
		return nil, nil
	}
	if err := b.archiver.Migrate(ctx); err != nil {
		return nil, err
	}
	store, err := b.store.Bind(&commonv1.TenantContext{TenantId: owner})
	if err != nil {
		return nil, err
	}
	var public, secrets string
	err = store.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		row, ok, err := tx.QueryOne(ctx, "SELECT environment, environment_secrets FROM workspaces WHERE workspace_id = $1 AND state <> 'deleted'", workspaceID)
		if ok {
			public, secrets = ledger.Str(row, "environment"), ledger.Str(row, "environment_secrets")
		}
		return err
	})
	if err != nil || public == "" {
		return nil, err
	}
	var doc struct {
		EnvironmentID string `json:"environment_id"`
		SetupScript   string `json:"setup_script"`
		StartCommand  string `json:"start_command"`
		Env           string `json:"env"`
		SecretHeaders []any  `json:"secret_headers"`
	}
	if err := json.Unmarshal([]byte(public), &doc); err != nil {
		return nil, providererr.New(providererr.Invalid, "invalid_environment", "工作区存的环境配置格式不对")
	}
	if secrets != "" {
		if err := json.Unmarshal([]byte(secrets), &doc); err != nil {
			return nil, providererr.New(providererr.Invalid, "invalid_environment", "工作区存的环境配置格式不对")
		}
	}
	vars, err := ParseDotenv(doc.Env)
	if err != nil {
		return nil, err
	}
	return &EnvironmentConfig{EnvironmentID: doc.EnvironmentID, SetupScript: doc.SetupScript, StartCommand: doc.StartCommand, Variables: vars,
		SecretHeaders: len(doc.SecretHeaders) > 0}, nil
}
