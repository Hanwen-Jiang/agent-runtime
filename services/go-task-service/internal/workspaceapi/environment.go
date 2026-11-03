package workspaceapi

// ENV-02（人工验收 Q16–37；docs/plan/env-02-implementation-2026-11-03.md）：建工作区时带的环境配置快照与删除环境配置。
// 校验与 Worker（agentruntime_worker.sandbox.env_config）同一套规则：这里先拒掉明显不对的，Worker 存之前再核一遍。

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/structpb"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	workspacev1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/sandboxgrants"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskapi"
)

const (
	maxEnvScriptBytes    = 64 << 10
	maxEnvVars           = 64
	maxEnvBytes          = 16 << 10
	maxEnvHosts          = 50
	maxEnvSecretHeaders  = 20
	envSecretNamePattern = `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`
)

var (
	environmentID = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)
	envVarName    = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
	secretName    = regexp.MustCompile(envSecretNamePattern)
	headerName    = regexp.MustCompile(`^[A-Za-z0-9-]{1,64}$`)
	// 与 tenant_policy.schema.json 的 host_pattern 相同（去掉 *. 前缀：子域名由 include_subdomains 表示）。
	domainName = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*\.[a-z]([a-z0-9-]{0,61}[a-z0-9])?$`)
	// 平台注入的变量（沙箱的代理、包镜像与 CA 证书包），.env 不能盖掉；与 Worker 的 sandbox.env_config 一致。
	reservedEnv = map[string]bool{
		"PIP_INDEX_URL": true, "UV_DEFAULT_INDEX": true, "PIP_TRUSTED_HOST": true, "NPM_CONFIG_REGISTRY": true, "GOPROXY": true, "HTTP_PROXY": true,
		"HTTPS_PROXY": true, "NO_PROXY": true, "ALL_PROXY": true, "NPM_CONFIG_PROXY": true, "NPM_CONFIG_HTTPS_PROXY": true, "NPM_CONFIG_AUDIT": true,
		"NPM_CONFIG_FUND": true, "GONOSUMDB": true, "GOFLAGS": true, "SSL_CERT_FILE": true, "REQUESTS_CA_BUNDLE": true, "CURL_CA_BUNDLE": true,
		"NODE_EXTRA_CA_CERTS": true, "GIT_SSL_CAINFO": true,
	}
	secretWords   = map[string]bool{"SECRET": true, "TOKEN": true, "PASSWORD": true, "PASSWD": true, "CREDENTIAL": true, "CREDENTIALS": true, "APIKEY": true, "PRIVATEKEY": true}
	internalHosts = []string{".local", ".localhost", ".internal", ".svc", ".cluster.local", ".home.arpa", ".lan", ".intranet", ".corp"}
	// 网关自己管、或会改变请求去向与分帧的头，不能配成网络密钥的头。
	forbiddenHeaders = map[string]bool{
		"host": true, "connection": true, "content-length": true, "transfer-encoding": true, "te": true, "trailer": true, "upgrade": true,
		"keep-alive": true, "proxy-authorization": true, "proxy-authenticate": true, "proxy-connection": true, "cookie": true,
	}
)

// secretLike：名字按 _ 分段后有一段是机密的说法（API_KEY、PRIVATE_KEY 两段合起来也算）。
func secretLike(name string) bool {
	parts := strings.Split(strings.ToUpper(name), "_")
	for i, p := range parts {
		if secretWords[p] {
			return true
		}
		if i > 0 && (parts[i-1]+p == "APIKEY" || parts[i-1]+p == "PRIVATEKEY") {
			return true
		}
	}
	return false
}

func checkDotEnv(text string) error {
	if len(text) > maxEnvBytes {
		return invalid("env_too_large", fmt.Sprintf(".env 合计最多 %d 字节", maxEnvBytes))
	}
	seen := map[string]bool{}
	for i, raw := range strings.Split(text, "\n") {
		line := strings.TrimSpace(raw)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		name, value, ok := strings.Cut(line, "=")
		if !ok || !envVarName.MatchString(name) || seen[name] || strings.ContainsRune(value, 0) {
			return invalid("invalid_env_line", fmt.Sprintf(".env 第 %d 行要是 KEY=value（名字不重复）", i+1))
		}
		seen[name] = true
		upper := strings.ToUpper(name)
		if reservedEnv[upper] || strings.HasPrefix(upper, "AGENT_") {
			return invalid("env_reserved_name", fmt.Sprintf(".env 第 %d 行的名字是平台保留的", i+1))
		}
		if secretLike(name) {
			return invalid("env_secret_name", fmt.Sprintf(".env 第 %d 行的名字像机密：机密请用网络密钥，不要写进 .env", i+1))
		}
	}
	if len(seen) > maxEnvVars {
		return invalid("env_too_large", fmt.Sprintf(".env 最多 %d 个变量", maxEnvVars))
	}
	return nil
}

func checkDomain(host string) bool {
	if !domainName.MatchString(host) || len(host) > 253 || host == "localhost" {
		return false
	}
	for _, s := range internalHosts {
		if strings.HasSuffix(host, s) {
			return false
		}
	}
	return true
}

// checkEnvironment 校验快照；只说哪一处不对，不带值。
func checkEnvironment(e *workspacev1.WorkspaceEnvironment) error {
	if !environmentID.MatchString(e.GetEnvironmentId()) {
		return invalid("invalid_environment", "environment_id 只能是字母、数字、_ 与 -，最多 128 个字符")
	}
	for _, s := range []string{e.GetSetupScript(), e.GetStartCommand()} {
		if len(s) > maxEnvScriptBytes || !utf8.ValidString(s) || strings.ContainsRune(s, 0) {
			return invalid("environment_script_too_large", fmt.Sprintf("安装脚本与启动命令各自最多 %d 字节的 UTF-8 文本", maxEnvScriptBytes))
		}
	}
	if err := checkDotEnv(e.GetEnv()); err != nil {
		return err
	}
	n := e.GetNetwork()
	if len(n.GetHosts()) > maxEnvHosts {
		return invalid("invalid_network_host", fmt.Sprintf("联网名单最多 %d 个主机", maxEnvHosts))
	}
	for _, h := range n.GetHosts() {
		if !checkDomain(h.GetHost()) {
			return invalid("invalid_network_host", "联网名单里只能是公网域名（小写，不能是 IP、* 或内网名字）")
		}
	}
	if len(e.GetSecretHeaders()) > maxEnvSecretHeaders {
		return invalid("invalid_secret_header", fmt.Sprintf("网络密钥最多 %d 个", maxEnvSecretHeaders))
	}
	pairs := map[string]bool{}
	for _, s := range e.GetSecretHeaders() {
		header := strings.ToLower(s.GetHeader())
		if header == "" {
			header = "authorization"
		}
		switch {
		case !checkDomain(s.GetHost()):
			return invalid("invalid_secret_header", "网络密钥的主机要是公网域名")
		case !secretName.MatchString(s.GetSecretName()) || strings.Contains(s.GetSecretName(), ".."):
			return invalid("invalid_secret_header", "网络密钥的名字只能是字母、数字、.、_ 与 -")
		case !headerName.MatchString(header) || forbiddenHeaders[header] || strings.HasPrefix(header, "proxy-"):
			return invalid("invalid_secret_header", "这个请求头不能用作网络密钥")
		case pairs[s.GetHost()+"\x00"+header]:
			return invalid("invalid_secret_header", "同一台主机的同一个请求头只能配一个网络密钥")
		}
		pairs[s.GetHost()+"\x00"+header] = true
	}
	return nil
}

// environmentArgs 是交给 Worker 的快照（protojson 的字段名同 proto）。
func environmentArgs(e *workspacev1.WorkspaceEnvironment) (map[string]any, error) {
	raw, err := protojson.MarshalOptions{UseProtoNames: true}.Marshal(e)
	if err != nil {
		return nil, err
	}
	st := &structpb.Struct{}
	if err := protojson.Unmarshal(raw, st); err != nil {
		return nil, err
	}
	return st.AsMap(), nil
}

// missingColumn：Worker 还没迁移 207（SQLite 报 no such column，Postgres 报 column … does not exist）。
func missingColumn(err error) bool {
	for e := err; e != nil; e = errors.Unwrap(e) {
		text := strings.ToLower(e.Error())
		if strings.Contains(text, "no such column") || (strings.Contains(text, "column") && strings.Contains(text, "does not exist")) {
			return true
		}
	}
	return false
}

// DeleteEnvironmentConfig 只标记（Q35）：调用者用这个环境建的工作区不再准备新的执行环境，已有的由 Worker 的对账在闲着时销毁。
func (s *Server) DeleteEnvironmentConfig(ctx context.Context, req *connect.Request[workspacev1.DeleteEnvironmentConfigRequest]) (*connect.Response[workspacev1.DeleteEnvironmentConfigResponse], error) {
	m := req.Msg
	id, tenant, err := s.caller(ctx, req.Header(), "workspace_environment_delete", m.GetTenantId(), "")
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	if !environmentID.MatchString(m.GetEnvironmentId()) {
		return nil, taskapi.ConnectError(invalid("invalid_environment", "environment_id 的格式不对"))
	}
	db, err := s.d.Store.Bind(tenant)
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	var n int64
	now := time.Now().UTC().Format("2006-01-02T15:04:05.000000Z")
	err = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		var e error
		n, e = tx.Execute(ctx, `UPDATE workspaces SET environment_deleted_at = $1, updated_at = $1
			WHERE environment_id = $2 AND environment_owner = $3 AND environment_deleted_at = ''`, now, m.GetEnvironmentId(), id.UserID)
		return e
	})
	if err != nil && (sandboxgrants.MissingTable(err) || missingColumn(err)) {
		n, err = 0, nil // Worker 还没迁移：没有工作区选用过环境
	}
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	return connect.NewResponse(&workspacev1.DeleteEnvironmentConfigResponse{Workspaces: int32(n)}), nil
}
