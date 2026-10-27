// Package envcred 是沙箱环境的短期凭据（docs/plan/sandbox-platform.md 第 3 节；D-15 包镜像、D-16 出网网关）。
// Worker（Python 的 agentruntime_core.envcred）签发，这里校验，不查表就能知道是哪个租户的哪个环境：
//
//	ar1.<base64url(JSON)>.<base64url(HMAC-SHA256(key, "ar1." + base64url(JSON)))>
//	JSON：{"e": env_ref, "t": tenant_id, "w": workspace_id, "s": session_id, "x": 到期的 Unix 秒, "k": 用途, "q": {限额}}
//
// 用途只能是 mirror、egress 或 preview（出网网关的预览入口，q.port 是环境的端口），一个凭据只能用在一处；
// q 是签发方填的这个环境的限额。任何一项不对都是同一个 denied
// （invalid_credential），不说明是哪一项。两种语言对着同一组向量测试（tests/contract/sandbox_credential_vectors.json）。
// 凭据不进日志、trace 与错误信息。
package envcred

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"regexp"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
)

// Prefix 是凭据的版本前缀。
const Prefix = "ar1."

// SecretName 是签名密钥在 SecretStore 平台作用域里的名字。
const SecretName = "sandbox/env-credential-key"

// 用途。
const (
	PurposeMirror  = "mirror"
	PurposeEgress  = "egress"
	PurposePreview = "preview"
)

var (
	tenantPattern = regexp.MustCompile(`^(_platform|[a-z0-9][a-z0-9_-]{0,62})$`)
	envRefPattern = regexp.MustCompile(`^sbe-[0-9a-f]{16}$`)
)

// Claims 是凭据里的内容。
type Claims struct {
	EnvRef      string           `json:"e"`
	TenantID    string           `json:"t"`
	WorkspaceID string           `json:"w,omitempty"`
	SessionID   string           `json:"s,omitempty"`
	ExpiresAt   int64            `json:"x"`
	Purpose     string           `json:"k"`
	Limits      map[string]int64 `json:"q,omitempty"`
}

func invalid() error {
	return providererr.New(providererr.Denied, "invalid_credential", "环境凭据无效或已过期")
}

func signature(key []byte, body string) string {
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(Prefix + body))
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

// Issue 签发一个凭据（Go 一侧只在测试里用；生产由 Worker 签发）。
func Issue(key []byte, c Claims) (string, error) {
	if len(key) < 32 {
		return "", providererr.New(providererr.Invalid, "weak_credential_key", "环境凭据的密钥至少 32 字节")
	}
	if c.Purpose != PurposeMirror && c.Purpose != PurposeEgress && c.Purpose != PurposePreview {
		return "", providererr.New(providererr.Invalid, "invalid_purpose", "环境凭据的用途只能是 mirror、egress 或 preview")
	}
	if !tenantPattern.MatchString(c.TenantID) || !envRefPattern.MatchString(c.EnvRef) {
		return "", providererr.New(providererr.Invalid, "invalid_claims", "环境凭据的租户或环境号格式不对")
	}
	data, err := json.Marshal(c)
	if err != nil {
		return "", err
	}
	body := base64.RawURLEncoding.EncodeToString(data)
	return Prefix + body + "." + signature(key, body), nil
}

// Verify 校验凭据，返回其中的内容。
func Verify(key []byte, token, purpose string, now time.Time) (Claims, error) {
	if !strings.HasPrefix(token, Prefix) || strings.Count(token, ".") != 2 || len(key) == 0 {
		return Claims{}, invalid()
	}
	body, sig, _ := strings.Cut(strings.TrimPrefix(token, Prefix), ".")
	if !hmac.Equal([]byte(signature(key, body)), []byte(sig)) {
		return Claims{}, invalid()
	}
	raw, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(body, "="))
	if err != nil {
		return Claims{}, invalid()
	}
	var c Claims
	if err := json.Unmarshal(raw, &c); err != nil {
		return Claims{}, invalid()
	}
	if c.Purpose != purpose || c.ExpiresAt <= now.Unix() || !tenantPattern.MatchString(c.TenantID) || c.EnvRef == "" {
		return Claims{}, invalid()
	}
	return c, nil
}
