package broker

import (
	"context"
	"encoding/json"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/envcred"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/registry"
)

// 环境的短期凭据与注入的环境变量（docs/plan/sandbox-platform.md 第 3 节；D-15、D-16）。SBX-01 起只在服务里：
// 每次 Exec 由服务按环境表补上，不回传给调用方。每个环境两个凭据（用途 mirror 与 egress），到期时间等于环境的活满时间。
//
//	PIP_INDEX_URL、UV_DEFAULT_INDEX    <镜像>/t/<凭据>/pypi/simple/
//	PIP_TRUSTED_HOST                   镜像是 http 时
//	npm_config_registry                <镜像>/t/<凭据>/npm/（另关掉 audit 与 fund）
//	GOPROXY                            <镜像>/t/<凭据>/go
//	HTTP_PROXY、HTTPS_PROXY（与小写）   http://ar:<凭据>@<出网网关>
//	NO_PROXY（与小写）                  包镜像的主机、localhost、127.0.0.1
//	npm_config_proxy、npm_config_https_proxy
//
// 哪些后端注入：登记里写了 pkg_mirror_url、egress_gateway_url 的用它；没写的 K8s 与本地后端用服务的 AR_PKG_MIRROR_URL、
// AR_EGRESS_GATEWAY_URL；托管后端默认不注入。登记里 egress_gateway: false 的不经网关。
// 测试用的密钥（W8）：租户 SecretStore 的 sandbox/test-env（JSON 对象），每条命令都带上；名字的规则同 exec 的 env，不能盖掉上面这些。

// TestEnvSecret 是租户的测试用密钥。
const TestEnvSecret = "sandbox/test-env"

// Credentials 签发环境的凭据。
type Credentials struct {
	MirrorURL string
	EgressURL string
	reg       *registry.Registry
	mu        sync.Mutex
	key       []byte
	loaded    bool
}

// NewCredentials 构造。
func NewCredentials(reg *registry.Registry, mirror, egress string) *Credentials {
	return &Credentials{MirrorURL: strings.TrimRight(mirror, "/"), EgressURL: strings.TrimRight(egress, "/"), reg: reg}
}

func (c *Credentials) loadKey(ctx context.Context) ([]byte, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.loaded {
		key, err := c.reg.ReadSecret(ctx, envcred.SecretName)
		if err != nil {
			return nil, err
		}
		c.key, c.loaded = key, true
	}
	return c.key, nil
}

// MirrorFor 是这个后端用的包镜像；空表示不注入。
func (c *Credentials) MirrorFor(spec registry.Spec) string {
	if v := spec.Str("pkg_mirror_url"); v != "" {
		return strings.TrimRight(v, "/")
	}
	if k := spec.Kind(); k == "k8s" || k == "local" {
		return c.MirrorURL
	}
	return ""
}

// EgressFor 是这个后端用的出网网关；空表示没有网关。
func (c *Credentials) EgressFor(spec registry.Spec) string {
	if v, ok := spec["egress_gateway"].(bool); ok && !v {
		return ""
	}
	if v := spec.Str("egress_gateway_url"); v != "" {
		return strings.TrimRight(v, "/")
	}
	if k := spec.Kind(); k == "k8s" || k == "local" {
		return c.EgressURL
	}
	return ""
}

func (c *Credentials) token(key []byte, rec ledger.Record, purpose string, limits map[string]int64) (string, error) {
	expires := ledger.ParseISO(rec.MaxUntil)
	if expires.IsZero() {
		expires = time.Now().Add(6 * time.Hour)
	}
	return envcred.Issue(key, envcred.Claims{EnvRef: rec.EnvRef, TenantID: rec.Owner, WorkspaceID: rec.WorkspaceID, SessionID: rec.SessionID,
		ExpiresAt: expires.Unix(), Purpose: purpose, Limits: limits})
}

// ExecEnv 是这个环境的命令要的包镜像与出网代理变量。
func (c *Credentials) ExecEnv(ctx context.Context, rec ledger.Record, spec registry.Spec, quota registry.Quota) (map[string]string, error) {
	out := map[string]string{}
	mirror, egress := c.MirrorFor(spec), c.EgressFor(spec)
	if mirror == "" && egress == "" {
		return out, nil
	}
	key, err := c.loadKey(ctx)
	if err != nil || len(key) == 0 {
		return out, err
	}
	if mirror != "" {
		var limits map[string]int64
		if quota.DailyMirrorMissBytes > 0 {
			limits = map[string]int64{"mirror_miss_bytes": quota.DailyMirrorMissBytes}
		}
		tok, err := c.token(key, rec, "mirror", limits)
		if err != nil {
			return nil, err
		}
		for k, v := range MirrorEnv(mirror, tok) {
			out[k] = v
		}
	}
	if egress != "" {
		var limits map[string]int64
		if quota.DailyEgressBytes > 0 {
			limits = map[string]int64{"egress_bytes": quota.DailyEgressBytes}
		}
		tok, err := c.token(key, rec, "egress", limits)
		if err != nil {
			return nil, err
		}
		for k, v := range ProxyEnv(egress, tok, mirror) {
			out[k] = v
		}
	}
	return out, nil
}

// TestEnv 是租户的测试用密钥；没有时空。格式不对 invalid（说明哪一处不对，不带值）。
func (c *Credentials) TestEnv(ctx context.Context, tenantID string) (map[string]string, error) {
	if tenantID == "" || tenantID == ledger.PlatformID {
		return map[string]string{}, nil
	}
	raw, err := c.reg.ReadTenantSecret(ctx, tenantID, TestEnvSecret)
	if err != nil || len(raw) == 0 {
		return map[string]string{}, err
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil, providererr.New(providererr.Invalid, "invalid_test_env", TestEnvSecret+" 要是 JSON 对象")
	}
	env := map[string]string{}
	for k, v := range doc {
		s, ok := v.(string)
		if !ok {
			return nil, providererr.New(providererr.Invalid, "invalid_test_env", TestEnvSecret+" 要是名字到字符串的 JSON 对象")
		}
		env[k] = s
	}
	if err := sandbox.CheckExecEnv(env); err != nil {
		return nil, err
	}
	var clash []string
	for k := range env {
		if Reserved[strings.ToUpper(k)] {
			clash = append(clash, k)
		}
	}
	if len(clash) > 0 {
		sort.Strings(clash)
		return nil, providererr.New(providererr.Invalid, "invalid_test_env", TestEnvSecret+" 不能设这些变量："+strings.Join(clash, "、"))
	}
	return env, nil
}

// MirrorEnv 是经包镜像的变量。
func MirrorEnv(mirror, token string) map[string]string {
	base := mirror + "/t/" + token
	env := map[string]string{
		"PIP_INDEX_URL": base + "/pypi/simple/", "UV_DEFAULT_INDEX": base + "/pypi/simple/",
		"npm_config_registry": base + "/npm/", "npm_config_audit": "false", "npm_config_fund": "false", "GOPROXY": base + "/go",
	}
	if u, err := url.Parse(mirror); err == nil && u.Scheme == "http" && u.Hostname() != "" {
		env["PIP_TRUSTED_HOST"] = u.Hostname()
	}
	return env
}

// ProxyEnv 是经出网网关的代理变量；凭据是代理认证的口令，各家客户端都认 URL 里的 userinfo。
func ProxyEnv(gateway, token, mirror string) map[string]string {
	u, _ := url.Parse(gateway)
	scheme, host := "http", gateway
	if u != nil {
		if u.Scheme != "" {
			scheme = u.Scheme
		}
		host = u.Host
	}
	proxy := scheme + "://ar:" + token + "@" + host
	bypass := []string{"localhost", "127.0.0.1"}
	if mirror != "" {
		if m, err := url.Parse(mirror); err == nil && m.Hostname() != "" {
			bypass = append([]string{m.Hostname()}, bypass...)
		}
	}
	noProxy := strings.Join(bypass, ",")
	return map[string]string{
		"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy, "https_proxy": proxy,
		"NO_PROXY": noProxy, "no_proxy": noProxy, "npm_config_proxy": proxy, "npm_config_https_proxy": proxy,
	}
}
