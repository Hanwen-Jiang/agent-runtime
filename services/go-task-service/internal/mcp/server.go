// Package mcp 是 McpConnectionService 的 connect-go 实现与 Task 的 MCP 租约（N-15；契约 proto/agentruntime/mcp/v1/mcp.proto，
// docs/plan/n15-mcp-contract.md）：与 Task API 同一个端口、同样认证。
//
//   - 连接属于（租户，用户）：用户取自认证身份，别人的连接与不存在的一样 NotFound（mcp_connection_not_found）。
//   - 三层授权：Server（https、域名、落在租户策略 mcp.hosts 里，按 netguard 拨号）、Tool（逐个打开）、Effect（登记者定：
//     read 要远端自己说只读；不超过 mcp.max_effect）。
//   - 凭据只给引用：UserSecretService 里本人的 secret_name 与请求头名；值只在连远端时从 SecretStore 现读，加在这个连接的
//     主机的 HTTPS 请求上，不跟重定向；响应、错误、日志里都没有值。
//   - 租约（mcp_leases）：Task 提交时按当时打开的工具发（IssueLeases），Task 结束（RevokeTask）与连接删除时收回；Worker 的 MCP
//     Gateway 每次调用读它（agentruntime_worker.tools.mcp_http）。
//
// 表都在业务库里，task-service 迁移（版本 402）。
package mcp

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/netguard"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/secretstore"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	mcpv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/mcp/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/mcp/v1/mcpv1connect"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskapi"
)

const (
	routeCreate    = "create_mcp_connection"
	routeList      = "list_mcp_connections"
	routeGet       = "get_mcp_connection"
	routeRefresh   = "refresh_mcp_tools"
	routeConfigure = "configure_mcp_tools"
	routeDelete    = "delete_mcp_connection"

	timeFormat = "2006-01-02T15:04:05.000000Z"

	defaultMaxConnections = 10
	defaultTimeoutS       = 30
	maxToolNameLen        = 48
)

var (
	connNameRule   = regexp.MustCompile(`^[a-z][a-z0-9_]{0,31}$`)
	secretNameRule = regexp.MustCompile(`^[A-Za-z0-9._-]{1,128}$`)
	headerRule     = regexp.MustCompile(`^[A-Za-z0-9-]{1,64}$`)
	toolNonWord    = regexp.MustCompile(`[^a-z0-9_]+`)
)

// Migrations 建连接、工具与租约三张表。版本 402（task-service 自己的 4xx）。
func Migrations() []relational.Migration {
	return []relational.Migration{{Version: 402, Name: "mcp_connections", Statements: []string{
		`CREATE TABLE mcp_connections (connection_id TEXT NOT NULL PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, url TEXT NOT NULL,
		 auth_secret TEXT NOT NULL, auth_header TEXT NOT NULL, state TEXT NOT NULL, error_code TEXT NOT NULL, server_name TEXT NOT NULL,
		 server_version TEXT NOT NULL, created_at TEXT NOT NULL, refreshed_at TEXT NOT NULL, UNIQUE (user_id, name))`,
		`CREATE TABLE mcp_tools (connection_id TEXT NOT NULL, tool TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL,
		 input_schema TEXT NOT NULL, schema_digest TEXT NOT NULL, annotations TEXT NOT NULL, enabled INTEGER NOT NULL, effect TEXT NOT NULL,
		 timeout_s INTEGER NOT NULL, unavailable_reason TEXT NOT NULL, confirmed_at TEXT NOT NULL, PRIMARY KEY (connection_id, tool))`,
		`CREATE TABLE mcp_leases (task_id TEXT NOT NULL, connection_id TEXT NOT NULL, user_id TEXT NOT NULL, connection_name TEXT NOT NULL,
		 url TEXT NOT NULL, auth_secret TEXT NOT NULL, auth_header TEXT NOT NULL, tools TEXT NOT NULL, state TEXT NOT NULL,
		 created_at TEXT NOT NULL, revoked_at TEXT NOT NULL, PRIMARY KEY (task_id, connection_id))`,
	}}}
}

// Policy 是租户策略的 mcp 一节。
type Policy struct {
	Allowed        bool     `json:"allowed"`
	Hosts          []string `json:"hosts"`
	MaxConnections int      `json:"max_connections"`
	MaxEffect      string   `json:"max_effect"`
}

func (p Policy) maxConnections() int {
	if p.MaxConnections <= 0 {
		return defaultMaxConnections
	}
	return p.MaxConnections
}

func (p Policy) maxEffect() commonv1.Effect {
	if p.MaxEffect == "read" {
		return commonv1.Effect_EFFECT_READ
	}
	return commonv1.Effect_EFFECT_WRITE_IRREVERSIBLE
}

// coversHost 判断已经核对过是公网域名的主机（netguard.NormalizeHost）落不落在 mcp.hosts 里。单独一个 "*" 表示任意公网域名
// （平台默认策略用）：IP 字面量、内网与云元数据地址在这之前已经拒绝了。
func (p Policy) coversHost(host string) bool {
	for _, pattern := range p.Hosts {
		pattern = strings.ToLower(strings.TrimSuffix(pattern, "."))
		if pattern == "*" || pattern == host {
			return true
		}
		if suffix, ok := strings.CutPrefix(pattern, "*"); ok && strings.HasPrefix(suffix, ".") && strings.HasSuffix(host, suffix) && host != suffix[1:] {
			return true
		}
	}
	return false
}

// PolicySource 给出租户策略的正文（Registry 的当前租户策略）；没有时 found 为 false。
type PolicySource func(ctx context.Context, tenant *commonv1.TenantContext) ([]byte, bool, error)

// PolicyFromDocument 取租户策略正文里的 mcp 一节；没有时不允许。
func PolicyFromDocument(doc []byte) (Policy, error) {
	var wrapper struct {
		MCP *Policy `json:"mcp"`
	}
	if err := json.Unmarshal(doc, &wrapper); err != nil {
		return Policy{}, providererr.Wrap(providererr.Invalid, "tenant_policy_malformed", "租户策略解析不了", err)
	}
	if wrapper.MCP == nil {
		return Policy{}, nil
	}
	return *wrapper.MCP, nil
}

// Discoverer 连一次远端（*Client 实现它）。
type Discoverer interface {
	Discover(ctx context.Context, url, header, value string) (Discovery, error)
}

// Deps 是构造参数。
type Deps struct {
	Ingress  ingress.Ingress
	Store    relational.Provider
	Secrets  secretstore.Provider
	Policies PolicySource
	Remote   Discoverer
	Clock    clock.Clock
	Logger   *slog.Logger
}

// Server 实现 McpConnectionService，并发放、收回 Task 的租约。
type Server struct{ d Deps }

var _ mcpv1connect.McpConnectionServiceHandler = (*Server)(nil)

// New 构造；Ingress 与 Store 必填。
func New(d Deps) (*Server, error) {
	if d.Ingress == nil || d.Store == nil {
		return nil, errors.New("mcp: Ingress 与 Store 必填")
	}
	if d.Clock == nil {
		d.Clock = clock.System{}
	}
	if d.Logger == nil {
		d.Logger = slog.Default()
	}
	if d.Remote == nil {
		d.Remote = NewClient(Dialer{})
	}
	return &Server{d: d}, nil
}

// Mount 把服务挂到 mux 上。
func (s *Server) Mount(mux *http.ServeMux, opts ...connect.HandlerOption) {
	path, handler := mcpv1connect.NewMcpConnectionServiceHandler(s, opts...)
	mux.Handle(path, handler)
}

// --- 错误 ---

type reasoned struct {
	code    connect.Code
	reason  string
	message string
}

func (r *reasoned) Error() string { return r.reason + ": " + r.message }

func reason(code connect.Code, why, message string) error { return &reasoned{code, why, message} }

func reply(err error) error {
	var r *reasoned
	if errors.As(err, &r) {
		ce := connect.NewError(r.code, errors.New(r.message))
		if detail, derr := connect.NewErrorDetail(&errdetails.ErrorInfo{Reason: r.reason, Domain: taskapi.ErrorDomain}); derr == nil {
			ce.AddDetail(detail)
		}
		return ce
	}
	return taskapi.ConnectError(err)
}

func invalid(code, message string) error { return providererr.New(providererr.Invalid, code, message) }

var errNotFound = reason(connect.CodeNotFound, "mcp_connection_not_found", "MCP 连接不存在")

// --- 认证与策略 ---

func (s *Server) caller(ctx context.Context, h http.Header, route, tenantID string) (*commonv1.TenantContext, string, error) {
	var token string
	if parts := strings.Fields(h.Get("Authorization")); len(parts) == 2 && strings.EqualFold(parts[0], "Bearer") {
		token = parts[1]
	}
	id, err := s.d.Ingress.Authenticate(ctx, ingress.Credentials{BearerToken: token})
	if err != nil {
		return nil, "", err
	}
	if err := s.d.Ingress.Admit(ctx, id, route); err != nil {
		return nil, "", err
	}
	tenant, err := ingress.TenantContextFor(id, tenantID, "", "", nil)
	if err != nil {
		return nil, "", err
	}
	user := tenant.GetUserId()
	if user == "" || strings.Contains(user, "/") {
		return nil, "", providererr.New(providererr.Denied, "no_user", "认证身份里没有用户，不能管 MCP 连接")
	}
	return tenant, user, nil
}

func (s *Server) policy(ctx context.Context, tenant *commonv1.TenantContext) (Policy, error) {
	if s.d.Policies == nil {
		return Policy{}, nil
	}
	doc, found, err := s.d.Policies(ctx, tenant)
	if err != nil {
		return Policy{}, providererr.Wrap(providererr.Transient, "tenant_policy_unavailable", "读不到租户策略", err)
	}
	if !found {
		return Policy{}, nil
	}
	return PolicyFromDocument(doc)
}

func (s *Server) allowedPolicy(ctx context.Context, tenant *commonv1.TenantContext) (Policy, error) {
	p, err := s.policy(ctx, tenant)
	if err != nil {
		return Policy{}, err
	}
	if !p.Allowed {
		return Policy{}, reason(connect.CodePermissionDenied, "mcp_not_allowed", "租户策略没有允许 MCP 连接")
	}
	return p, nil
}

// checkURL 规范化并核对地址；返回规范后的地址与主机。
func checkURL(raw string, p Policy) (string, string, error) {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || u.Scheme != "https" || u.Host == "" || u.Opaque != "" {
		return "", "", invalid("invalid_mcp_url", "地址要是 https://<域名>/...")
	}
	if u.User != nil || u.Fragment != "" || u.RawFragment != "" {
		return "", "", invalid("invalid_mcp_url", "地址不能带凭据或片段")
	}
	host, reasonText := netguard.NormalizeHost(u.Hostname())
	if reasonText != "" {
		return "", "", reason(connect.CodePermissionDenied, "mcp_host_not_public", "地址的主机要是公网域名（"+reasonText+"）")
	}
	if port := u.Port(); port != "" {
		n, err := strconv.Atoi(port)
		if err != nil || n < 1 || n > 65535 {
			return "", "", invalid("invalid_mcp_url", "端口不对")
		}
		if n != 443 {
			host2 := host + ":" + port
			u.Host = host2
		} else {
			u.Host = host
		}
	} else {
		u.Host = host
	}
	if !p.coversHost(host) {
		return "", "", reason(connect.CodePermissionDenied, "mcp_host_not_allowed", host+" 不在租户策略的 mcp.hosts 里")
	}
	if u.Path == "" {
		u.Path = "/"
	}
	return u.String(), host, nil
}

func checkAuth(a *mcpv1.McpAuth) (string, string, error) {
	if a == nil || a.GetSecretName() == "" {
		if a != nil && a.GetHeader() != "" {
			return "", "", invalid("invalid_mcp_auth", "给了请求头就要给 secret_name")
		}
		return "", "", nil
	}
	if !secretNameRule.MatchString(a.GetSecretName()) {
		return "", "", invalid("invalid_mcp_auth", "secret_name 只能用字母、数字、.、_ 与 -，最多 128 个字符")
	}
	header := a.GetHeader()
	if header != "" {
		if !headerRule.MatchString(header) {
			return "", "", invalid("invalid_mcp_auth", "请求头的名字只能用字母、数字与 -")
		}
		switch strings.ToLower(header) {
		case "host", "content-type", "content-length", "accept", "mcp-session-id", "mcp-protocol-version", "transfer-encoding", "connection", "cookie":
			return "", "", invalid("invalid_mcp_auth", header+" 不能用作鉴权头")
		}
		header = http.CanonicalHeaderKey(header)
	}
	return a.GetSecretName(), header, nil
}

// --- 行与投影 ---

type connRow struct {
	ID, User, Name, URL, Secret, Header, State, ErrorCode, ServerName, ServerVersion, CreatedAt, RefreshedAt string
}

type toolRow struct {
	ConnectionID, Tool, Name, Description, InputSchema, Digest, Annotations string
	Enabled                                                                 bool
	Effect                                                                  string
	TimeoutS                                                                int64
	Unavailable, ConfirmedAt                                                string
}

func str(r relational.Row, k string) string {
	v, _ := r[k].(string)
	return v
}

func num(r relational.Row, k string) int64 {
	switch v := r[k].(type) {
	case int64:
		return v
	case int32:
		return int64(v)
	case int:
		return int64(v)
	case float64:
		return int64(v)
	case bool:
		if v {
			return 1
		}
	}
	return 0
}

func scanConn(r relational.Row) connRow {
	return connRow{ID: str(r, "connection_id"), User: str(r, "user_id"), Name: str(r, "name"), URL: str(r, "url"), Secret: str(r, "auth_secret"),
		Header: str(r, "auth_header"), State: str(r, "state"), ErrorCode: str(r, "error_code"), ServerName: str(r, "server_name"),
		ServerVersion: str(r, "server_version"), CreatedAt: str(r, "created_at"), RefreshedAt: str(r, "refreshed_at")}
}

func scanTool(r relational.Row) toolRow {
	return toolRow{ConnectionID: str(r, "connection_id"), Tool: str(r, "tool"), Name: str(r, "name"), Description: str(r, "description"),
		InputSchema: str(r, "input_schema"), Digest: str(r, "schema_digest"), Annotations: str(r, "annotations"), Enabled: num(r, "enabled") != 0,
		Effect: str(r, "effect"), TimeoutS: num(r, "timeout_s"), Unavailable: str(r, "unavailable_reason"), ConfirmedAt: str(r, "confirmed_at")}
}

const connColumns = `connection_id, user_id, name, url, auth_secret, auth_header, state, error_code, server_name, server_version, created_at, refreshed_at`
const toolColumns = `connection_id, tool, name, description, input_schema, schema_digest, annotations, enabled, effect, timeout_s, unavailable_reason, confirmed_at`

func parseTime(text string) *timestamppb.Timestamp {
	t, err := time.Parse(timeFormat, text)
	if err != nil {
		return nil
	}
	return timestamppb.New(t)
}

func effectName(e commonv1.Effect) string {
	switch e {
	case commonv1.Effect_EFFECT_READ:
		return "read"
	case commonv1.Effect_EFFECT_WRITE_IRREVERSIBLE:
		return "write_irreversible"
	}
	return ""
}

func effectValue(name string) commonv1.Effect {
	switch name {
	case "read":
		return commonv1.Effect_EFFECT_READ
	case "write_irreversible":
		return commonv1.Effect_EFFECT_WRITE_IRREVERSIBLE
	}
	return commonv1.Effect_EFFECT_UNSPECIFIED
}

type annotations struct {
	ReadOnly    *bool `json:"read_only_hint,omitempty"`
	Destructive *bool `json:"destructive_hint,omitempty"`
	Idempotent  *bool `json:"idempotent_hint,omitempty"`
	OpenWorld   *bool `json:"open_world_hint,omitempty"`
}

func (t toolRow) annotations() annotations {
	var a annotations
	_ = json.Unmarshal([]byte(t.Annotations), &a)
	return a
}

func projectTool(t toolRow, withSchema bool) *mcpv1.McpTool {
	a := t.annotations()
	out := &mcpv1.McpTool{
		Tool: t.Tool, Name: t.Name, Description: t.Description, SchemaDigest: t.Digest, Enabled: t.Enabled,
		Effect: effectValue(t.Effect), TimeoutS: int32(t.TimeoutS), UnavailableReason: t.Unavailable, ConfirmedAt: parseTime(t.ConfirmedAt),
		Annotations: &mcpv1.McpToolAnnotations{ReadOnlyHint: a.ReadOnly, DestructiveHint: a.Destructive, IdempotentHint: a.Idempotent, OpenWorldHint: a.OpenWorld},
	}
	if withSchema && t.InputSchema != "" {
		value := &structpb.Value{}
		if err := protojson.Unmarshal([]byte(t.InputSchema), value); err == nil {
			out.InputSchema = &commonv1.JsonPayload{Value: value}
		}
	}
	return out
}

func projectConn(c connRow, tools []toolRow, withSchema bool) *mcpv1.McpConnection {
	out := &mcpv1.McpConnection{
		ConnectionId: c.ID, Name: c.Name, Url: c.URL, ErrorCode: c.ErrorCode, ServerName: c.ServerName, ServerVersion: c.ServerVersion,
		CreatedAt: parseTime(c.CreatedAt), RefreshedAt: parseTime(c.RefreshedAt), State: mcpv1.McpConnectionState_MCP_CONNECTION_STATE_ERROR,
	}
	if c.State == "ready" {
		out.State = mcpv1.McpConnectionState_MCP_CONNECTION_STATE_READY
	}
	if c.Secret != "" {
		out.Auth = &mcpv1.McpAuth{SecretName: c.Secret, Header: c.Header}
	}
	for _, t := range tools {
		out.Tools = append(out.Tools, projectTool(t, withSchema))
	}
	return out
}

func (s *Server) now() string { return s.d.Clock.Now().UTC().Format(timeFormat) }

func (s *Server) db(tenant *commonv1.TenantContext) (relational.Store, error) {
	return s.d.Store.Bind(&commonv1.TenantContext{TenantId: tenant.GetTenantId()})
}

func loadConn(ctx context.Context, tx relational.Tx, user, id string) (connRow, bool, error) {
	row, found, err := tx.QueryOne(ctx, `SELECT `+connColumns+` FROM mcp_connections WHERE connection_id = $1 AND user_id = $2`, id, user)
	if err != nil || !found {
		return connRow{}, false, err
	}
	return scanConn(row), true, nil
}

func loadTools(ctx context.Context, tx relational.Tx, id string) ([]toolRow, error) {
	rows, err := tx.Query(ctx, `SELECT `+toolColumns+` FROM mcp_tools WHERE connection_id = $1 ORDER BY tool`, id)
	if err != nil {
		return nil, err
	}
	out := make([]toolRow, 0, len(rows))
	for _, r := range rows {
		out = append(out, scanTool(r))
	}
	return out, nil
}

// --- 连远端 ---

// KernelName 是内核里的工具名：mcp.<连接名>.<工具名规整成小写、字母数字与 _>。
func KernelName(conn, tool string) string {
	n := toolNonWord.ReplaceAllString(strings.ToLower(tool), "_")
	n = strings.Trim(n, "_")
	if len(n) > maxToolNameLen {
		n = n[:maxToolNameLen]
	}
	if n == "" {
		n = "tool"
	}
	return "mcp." + conn + "." + n
}

func digest(schema json.RawMessage) (string, string) {
	var v any
	if len(schema) == 0 || json.Unmarshal(schema, &v) != nil {
		v = map[string]any{"type": "object"}
	}
	if m, ok := v.(map[string]any); !ok || m == nil {
		v = map[string]any{"type": "object"}
	}
	canonical, _ := json.Marshal(v) // map 的键按字典序
	sum := sha256.Sum256(canonical)
	return string(canonical), hex.EncodeToString(sum[:])
}

// secretValue 现读本人的网络密钥；没有时报 mcp_auth_missing。
func (s *Server) secretValue(ctx context.Context, tenant *commonv1.TenantContext, user, name string) (string, error) {
	if name == "" {
		return "", nil
	}
	if s.d.Secrets == nil {
		return "", fail("mcp_auth_missing", "这个部署读不了用户密钥")
	}
	store, err := s.d.Secrets.Bind(&commonv1.TenantContext{TenantId: tenant.GetTenantId()})
	if err != nil {
		return "", fail("mcp_auth_missing", "读不了用户密钥")
	}
	v, err := store.Read(ctx, "user-secrets/"+user+"/"+name)
	if err != nil || v.IsZero() {
		return "", fail("mcp_auth_missing", "网络密钥 "+name+" 不存在")
	}
	return v.RevealString(), nil
}

func headerValue(header, value string) (string, string) {
	if value == "" {
		return "", ""
	}
	if header == "" {
		return "Authorization", "Bearer " + value
	}
	return header, value
}

// discover 连一次远端；失败时给出 error_code（不报 RPC 错误：连不上照样建好）。
func (s *Server) discover(ctx context.Context, tenant *commonv1.TenantContext, c connRow) (Discovery, string) {
	value, err := s.secretValue(ctx, tenant, c.User, c.Secret)
	if err != nil {
		return Discovery{}, errorCode(err)
	}
	header, v := headerValue(c.Header, value)
	found, err := s.d.Remote.Discover(ctx, c.URL, header, v)
	if err != nil {
		s.d.Logger.Warn("MCP 连不上", "tenant_id", tenant.GetTenantId(), "connection_id", c.ID, "error", err.Error())
		return Discovery{}, errorCode(err)
	}
	return found, ""
}

func errorCode(err error) string {
	var de *DiscoverError
	if errors.As(err, &de) {
		return de.Code
	}
	return "mcp_unreachable"
}

// merge 把远端的工具并进已有的：新出现的先关着；schema 变了的关掉；远端说的不再只读而定了 read 的关掉；远端没有了的去掉。
// 返回新的工具行与 added、disabled、removed（远端的工具名）。
func merge(connName string, existing []toolRow, remote []RemoteTool, connID string) ([]toolRow, []string, []string, []string) {
	old := map[string]toolRow{}
	for _, t := range existing {
		old[t.Tool] = t
	}
	var out []toolRow
	var added, disabled, removed []string
	seen := map[string]bool{}
	for _, r := range remote {
		if r.Name == "" || seen[r.Name] || len(r.Name) > 128 {
			continue
		}
		seen[r.Name] = true
		schema, sum := digest(r.InputSchema)
		var a annotations
		if r.Annotations != nil {
			a = annotations{ReadOnly: r.Annotations.ReadOnlyHint, Destructive: r.Annotations.DestructiveHint, Idempotent: r.Annotations.IdempotentHint, OpenWorld: r.Annotations.OpenWorldHint}
		}
		ann, _ := json.Marshal(a)
		t := toolRow{ConnectionID: connID, Tool: r.Name, Name: KernelName(connName, r.Name), Description: clip(r.Description, 4000),
			InputSchema: schema, Digest: sum, Annotations: string(ann)}
		if prev, ok := old[r.Name]; ok {
			t.Enabled, t.Effect, t.TimeoutS, t.ConfirmedAt = prev.Enabled, prev.Effect, prev.TimeoutS, prev.ConfirmedAt
			readOnly := a.ReadOnly != nil && *a.ReadOnly
			if prev.Digest != sum || (prev.Effect == "read" && !readOnly) {
				if prev.Enabled {
					disabled = append(disabled, r.Name)
				}
				t.Enabled, t.Effect, t.ConfirmedAt = false, "", ""
			}
		} else {
			added = append(added, r.Name)
		}
		out = append(out, t)
	}
	for _, t := range existing {
		if !seen[t.Tool] {
			removed = append(removed, t.Tool)
		}
	}
	// 规整后撞名的都打不开。
	count := map[string]int{}
	for _, t := range out {
		count[t.Name]++
	}
	for i := range out {
		out[i].Unavailable = ""
		if count[out[i].Name] > 1 {
			out[i].Unavailable = "tool_name_conflict"
			if out[i].Enabled {
				disabled = append(disabled, out[i].Tool)
			}
			out[i].Enabled, out[i].Effect, out[i].ConfirmedAt = false, "", ""
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Tool < out[j].Tool })
	return out, added, disabled, removed
}

func writeTools(ctx context.Context, tx relational.Tx, connID string, tools []toolRow) error {
	if _, err := tx.Execute(ctx, `DELETE FROM mcp_tools WHERE connection_id = $1`, connID); err != nil {
		return err
	}
	for _, t := range tools {
		enabled := 0
		if t.Enabled {
			enabled = 1
		}
		if _, err := tx.Execute(ctx, `INSERT INTO mcp_tools (`+toolColumns+`) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)`,
			connID, t.Tool, t.Name, t.Description, t.InputSchema, t.Digest, t.Annotations, enabled, t.Effect, t.TimeoutS, t.Unavailable, t.ConfirmedAt); err != nil {
			return err
		}
	}
	return nil
}

func newID() string {
	var b [10]byte
	_, _ = rand.Read(b[:])
	return "mcp-" + hex.EncodeToString(b[:])
}

// --- RPC ---

func (s *Server) CreateMcpConnection(ctx context.Context, req *connect.Request[mcpv1.CreateMcpConnectionRequest]) (*connect.Response[mcpv1.CreateMcpConnectionResponse], error) {
	tenant, user, err := s.caller(ctx, req.Header(), routeCreate, req.Msg.GetTenantId())
	if err != nil {
		return nil, reply(err)
	}
	policy, err := s.allowedPolicy(ctx, tenant)
	if err != nil {
		return nil, reply(err)
	}
	name := req.Msg.GetName()
	if !connNameRule.MatchString(name) {
		return nil, reply(invalid("invalid_mcp_name", "名字要小写字母开头，只用小写字母、数字与 _，最多 32 个字符"))
	}
	target, _, err := checkURL(req.Msg.GetUrl(), policy)
	if err != nil {
		return nil, reply(err)
	}
	secretName, header, err := checkAuth(req.Msg.GetAuth())
	if err != nil {
		return nil, reply(err)
	}
	db, err := s.db(tenant)
	if err != nil {
		return nil, reply(err)
	}
	// 同名的已有连接：设置相同就返回它，不同就 AlreadyExists；个数上限。
	var existing *connRow
	err = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		row, found, err := tx.QueryOne(ctx, `SELECT `+connColumns+` FROM mcp_connections WHERE user_id = $1 AND name = $2`, user, name)
		if err != nil {
			return err
		}
		if found {
			c := scanConn(row)
			existing = &c
			return nil
		}
		count, _, err := tx.QueryOne(ctx, `SELECT COUNT(*) AS n FROM mcp_connections WHERE user_id = $1`, user)
		if err != nil {
			return err
		}
		if int(num(count, "n")) >= policy.maxConnections() {
			return reason(connect.CodeResourceExhausted, "mcp_connection_limit", fmt.Sprintf("每个用户最多 %d 个 MCP 连接", policy.maxConnections()))
		}
		return nil
	})
	if err != nil {
		return nil, reply(err)
	}
	if existing != nil {
		if existing.URL != target || existing.Secret != secretName || existing.Header != header {
			return nil, reply(reason(connect.CodeAlreadyExists, "mcp_connection_exists", "已经有同名、设置不同的连接"))
		}
		out, err := s.read(ctx, tenant, user, existing.ID, true)
		if err != nil {
			return nil, reply(err)
		}
		return connect.NewResponse(&mcpv1.CreateMcpConnectionResponse{Connection: out, Created: false}), nil
	}
	now := s.now()
	c := connRow{ID: newID(), User: user, Name: name, URL: target, Secret: secretName, Header: header, CreatedAt: now, RefreshedAt: ""}
	found, code := s.discover(ctx, tenant, c)
	c.State, c.ErrorCode = "ready", code
	if code != "" {
		c.State = "error"
	} else {
		c.ServerName, c.ServerVersion, c.RefreshedAt = found.ServerName, found.ServerVersion, now
	}
	tools, _, _, _ := merge(name, nil, found.Tools, c.ID)
	err = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		count, _, err := tx.QueryOne(ctx, `SELECT COUNT(*) AS n FROM mcp_connections WHERE user_id = $1`, user)
		if err != nil {
			return err
		}
		if int(num(count, "n")) >= policy.maxConnections() {
			return reason(connect.CodeResourceExhausted, "mcp_connection_limit", fmt.Sprintf("每个用户最多 %d 个 MCP 连接", policy.maxConnections()))
		}
		if _, err := tx.Execute(ctx, `INSERT INTO mcp_connections (`+connColumns+`) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)`,
			c.ID, c.User, c.Name, c.URL, c.Secret, c.Header, c.State, c.ErrorCode, c.ServerName, c.ServerVersion, c.CreatedAt, c.RefreshedAt); err != nil {
			return err
		}
		return writeTools(ctx, tx, c.ID, tools)
	})
	if err != nil {
		if providererr.CategoryOf(err) == providererr.Conflict {
			return nil, reply(reason(connect.CodeAlreadyExists, "mcp_connection_exists", "已经有同名的连接"))
		}
		return nil, reply(err)
	}
	s.d.Logger.Info("MCP 连接", "action", "create", "tenant_id", tenant.GetTenantId(), "user_id", user, "connection_id", c.ID, "name", name,
		"state", c.State, "error_code", c.ErrorCode, "tools", len(tools))
	return connect.NewResponse(&mcpv1.CreateMcpConnectionResponse{Connection: projectConn(c, tools, true), Created: true}), nil
}

func (s *Server) read(ctx context.Context, tenant *commonv1.TenantContext, user, id string, withSchema bool) (*mcpv1.McpConnection, error) {
	db, err := s.db(tenant)
	if err != nil {
		return nil, err
	}
	var out *mcpv1.McpConnection
	err = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		c, found, err := loadConn(ctx, tx, user, id)
		if err != nil {
			return err
		}
		if !found {
			return errNotFound
		}
		tools, err := loadTools(ctx, tx, id)
		if err != nil {
			return err
		}
		out = projectConn(c, tools, withSchema)
		return nil
	})
	return out, err
}

func (s *Server) ListMcpConnections(ctx context.Context, req *connect.Request[mcpv1.ListMcpConnectionsRequest]) (*connect.Response[mcpv1.ListMcpConnectionsResponse], error) {
	tenant, user, err := s.caller(ctx, req.Header(), routeList, req.Msg.GetTenantId())
	if err != nil {
		return nil, reply(err)
	}
	db, err := s.db(tenant)
	if err != nil {
		return nil, reply(err)
	}
	out := &mcpv1.ListMcpConnectionsResponse{}
	err = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		rows, err := tx.Query(ctx, `SELECT `+connColumns+` FROM mcp_connections WHERE user_id = $1 ORDER BY name`, user)
		if err != nil {
			return err
		}
		for _, r := range rows {
			c := scanConn(r)
			tools, err := loadTools(ctx, tx, c.ID)
			if err != nil {
				return err
			}
			out.Connections = append(out.Connections, projectConn(c, tools, false))
		}
		return nil
	})
	if err != nil {
		return nil, reply(err)
	}
	return connect.NewResponse(out), nil
}

func (s *Server) GetMcpConnection(ctx context.Context, req *connect.Request[mcpv1.GetMcpConnectionRequest]) (*connect.Response[mcpv1.GetMcpConnectionResponse], error) {
	tenant, user, err := s.caller(ctx, req.Header(), routeGet, req.Msg.GetTenantId())
	if err != nil {
		return nil, reply(err)
	}
	out, err := s.read(ctx, tenant, user, req.Msg.GetConnectionId(), true)
	if err != nil {
		return nil, reply(err)
	}
	return connect.NewResponse(&mcpv1.GetMcpConnectionResponse{Connection: out}), nil
}

func (s *Server) RefreshMcpTools(ctx context.Context, req *connect.Request[mcpv1.RefreshMcpToolsRequest]) (*connect.Response[mcpv1.RefreshMcpToolsResponse], error) {
	tenant, user, err := s.caller(ctx, req.Header(), routeRefresh, req.Msg.GetTenantId())
	if err != nil {
		return nil, reply(err)
	}
	policy, err := s.allowedPolicy(ctx, tenant)
	if err != nil {
		return nil, reply(err)
	}
	db, err := s.db(tenant)
	if err != nil {
		return nil, reply(err)
	}
	id := req.Msg.GetConnectionId()
	var c connRow
	err = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		var found bool
		var err error
		c, found, err = loadConn(ctx, tx, user, id)
		if err == nil && !found {
			err = errNotFound
		}
		return err
	})
	if err != nil {
		return nil, reply(err)
	}
	// 策略收紧之后（主机不再在名单里）不再连它。
	if _, _, err := checkURL(c.URL, policy); err != nil {
		return nil, reply(err)
	}
	found, code := s.discover(ctx, tenant, c)
	out := &mcpv1.RefreshMcpToolsResponse{}
	err = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		current, ok, err := loadConn(ctx, tx, user, id)
		if err != nil {
			return err
		}
		if !ok {
			return errNotFound
		}
		existing, err := loadTools(ctx, tx, id)
		if err != nil {
			return err
		}
		if code != "" {
			// 连不上：已打开的工具照旧记着。
			current.State, current.ErrorCode = "error", code
			if _, err := tx.Execute(ctx, `UPDATE mcp_connections SET state = 'error', error_code = $1 WHERE connection_id = $2`, code, id); err != nil {
				return err
			}
			out.Connection = projectConn(current, existing, true)
			return nil
		}
		tools, added, disabled, removed := merge(current.Name, existing, found.Tools, id)
		now := s.now()
		if _, err := tx.Execute(ctx, `UPDATE mcp_connections SET state = 'ready', error_code = '', server_name = $1, server_version = $2, refreshed_at = $3
			WHERE connection_id = $4`, found.ServerName, found.ServerVersion, now, id); err != nil {
			return err
		}
		if err := writeTools(ctx, tx, id, tools); err != nil {
			return err
		}
		current.State, current.ErrorCode, current.ServerName, current.ServerVersion, current.RefreshedAt = "ready", "", found.ServerName, found.ServerVersion, now
		out.Connection = projectConn(current, tools, true)
		out.Added, out.Disabled, out.Removed = added, disabled, removed
		return nil
	})
	if err != nil {
		return nil, reply(err)
	}
	s.d.Logger.Info("MCP 连接", "action", "refresh", "tenant_id", tenant.GetTenantId(), "user_id", user, "connection_id", id, "error_code", code,
		"added", len(out.Added), "disabled", len(out.Disabled), "removed", len(out.Removed))
	return connect.NewResponse(out), nil
}

func (s *Server) ConfigureMcpTools(ctx context.Context, req *connect.Request[mcpv1.ConfigureMcpToolsRequest]) (*connect.Response[mcpv1.ConfigureMcpToolsResponse], error) {
	tenant, user, err := s.caller(ctx, req.Header(), routeConfigure, req.Msg.GetTenantId())
	if err != nil {
		return nil, reply(err)
	}
	policy, err := s.allowedPolicy(ctx, tenant)
	if err != nil {
		return nil, reply(err)
	}
	if len(req.Msg.GetTools()) == 0 {
		return nil, reply(invalid("invalid_argument", "tools 不能为空"))
	}
	db, err := s.db(tenant)
	if err != nil {
		return nil, reply(err)
	}
	id := req.Msg.GetConnectionId()
	var out *mcpv1.McpConnection
	err = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		c, found, err := loadConn(ctx, tx, user, id)
		if err != nil {
			return err
		}
		if !found {
			return errNotFound
		}
		tools, err := loadTools(ctx, tx, id)
		if err != nil {
			return err
		}
		index := map[string]int{}
		for i, t := range tools {
			index[t.Tool] = i
		}
		now := s.now()
		seen := map[string]bool{}
		for _, setting := range req.Msg.GetTools() {
			i, ok := index[setting.GetTool()]
			if !ok {
				return invalid("mcp_tool_not_found", "连接上没有工具 "+setting.GetTool())
			}
			if seen[setting.GetTool()] {
				return invalid("invalid_argument", "工具 "+setting.GetTool()+" 写了两次")
			}
			seen[setting.GetTool()] = true
			t := &tools[i]
			if !setting.GetEnabled() {
				t.Enabled, t.Effect, t.ConfirmedAt = false, "", ""
				continue
			}
			if t.Unavailable != "" {
				return reason(connect.CodeFailedPrecondition, t.Unavailable, "工具 "+t.Tool+" 打不开（"+t.Unavailable+"）")
			}
			if setting.GetSchemaDigest() != t.Digest {
				return reason(connect.CodeFailedPrecondition, "mcp_schema_changed", "工具 "+t.Tool+" 的 schema 变了，先刷新再确认")
			}
			effect := setting.GetEffect()
			switch effect {
			case commonv1.Effect_EFFECT_READ:
				if a := t.annotations(); a.ReadOnly == nil || !*a.ReadOnly {
					return invalid("invalid_mcp_effect", "工具 "+t.Tool+" 远端没说只读，只能定为 write_irreversible")
				}
			case commonv1.Effect_EFFECT_WRITE_IRREVERSIBLE:
			default:
				return invalid("invalid_mcp_effect", "MCP 工具的 effect 只能是 read 或 write_irreversible")
			}
			if effect > policy.maxEffect() {
				return reason(connect.CodePermissionDenied, "effect_over_limit", "工具 "+t.Tool+" 的 effect 超过租户策略的 mcp.max_effect")
			}
			timeout := setting.GetTimeoutS()
			if timeout == 0 {
				timeout = defaultTimeoutS
			}
			if timeout < 1 || timeout > 300 {
				return invalid("invalid_argument", "timeout_s 要在 1 到 300 之间")
			}
			if !t.Enabled || t.Effect != effectName(effect) || t.TimeoutS != int64(timeout) {
				t.ConfirmedAt = now
			}
			t.Enabled, t.Effect, t.TimeoutS = true, effectName(effect), int64(timeout)
		}
		if err := writeTools(ctx, tx, id, tools); err != nil {
			return err
		}
		out = projectConn(c, tools, true)
		return nil
	})
	if err != nil {
		return nil, reply(err)
	}
	s.d.Logger.Info("MCP 连接", "action", "configure", "tenant_id", tenant.GetTenantId(), "user_id", user, "connection_id", id, "tools", len(req.Msg.GetTools()))
	return connect.NewResponse(&mcpv1.ConfigureMcpToolsResponse{Connection: out}), nil
}

func (s *Server) DeleteMcpConnection(ctx context.Context, req *connect.Request[mcpv1.DeleteMcpConnectionRequest]) (*connect.Response[mcpv1.DeleteMcpConnectionResponse], error) {
	tenant, user, err := s.caller(ctx, req.Header(), routeDelete, req.Msg.GetTenantId())
	if err != nil {
		return nil, reply(err)
	}
	db, err := s.db(tenant)
	if err != nil {
		return nil, reply(err)
	}
	id := req.Msg.GetConnectionId()
	deleted := false
	err = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		n, err := tx.Execute(ctx, `DELETE FROM mcp_connections WHERE connection_id = $1 AND user_id = $2`, id, user)
		if err != nil || n == 0 {
			return err
		}
		deleted = true
		if _, err := tx.Execute(ctx, `DELETE FROM mcp_tools WHERE connection_id = $1`, id); err != nil {
			return err
		}
		// 撤销：已发出的租约立即失效（Worker 每次调用、调用进行中都查它）。
		_, err = tx.Execute(ctx, `UPDATE mcp_leases SET state = 'revoked', revoked_at = $1 WHERE connection_id = $2 AND state = 'active'`, s.now(), id)
		return err
	})
	if err != nil {
		return nil, reply(err)
	}
	if deleted {
		s.d.Logger.Info("MCP 连接", "action", "delete", "tenant_id", tenant.GetTenantId(), "user_id", user, "connection_id", id)
	}
	return connect.NewResponse(&mcpv1.DeleteMcpConnectionResponse{Deleted: deleted}), nil
}

// --- 租约 ---

// LeaseTool 是租约里的一个工具（Worker 按它给模型工具定义、核对 effect 与超时）。
type LeaseTool struct {
	Tool        string          `json:"tool"`
	Name        string          `json:"name"`
	Description string          `json:"description"`
	InputSchema json.RawMessage `json:"input_schema"`
	Effect      string          `json:"effect"`
	TimeoutS    int64           `json:"timeout_s"`
}

// IssueLeases 按提交时的设置给 Task 发租约：提交者的每个连接上打开了的工具（主机仍在策略的名单里）。租户策略不允许 MCP 时
// 不发。同一个 Task 再发（重投递）不变。
func (s *Server) IssueLeases(ctx context.Context, tenant *commonv1.TenantContext, taskID, user string) error {
	return s.IssueSelectedLeases(ctx, tenant, taskID, user, nil)
}

// IssueSelectedLeases intersects caller selections with the existing enabled/authorized tools.
func (s *Server) IssueSelectedLeases(ctx context.Context, tenant *commonv1.TenantContext, taskID, user string, selected []string) error {
	if user == "" || taskID == "" {
		return nil
	}
	policy, err := s.policy(ctx, tenant)
	if err != nil || !policy.Allowed {
		if err == nil && len(selected) > 0 {
			return providererr.New(providererr.Denied, "personal_mcp_unavailable", "租户没有启用引用的 MCP 连接")
		}
		return err
	}
	db, err := s.db(tenant)
	if err != nil {
		return err
	}
	now := s.now()
	issued := 0
	err = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		rows, err := tx.Query(ctx, `SELECT `+connColumns+` FROM mcp_connections WHERE user_id = $1 ORDER BY name`, user)
		if err != nil {
			return err
		}
		wanted := map[string]bool{}
		for _, id := range selected {
			wanted[id] = true
		}
		available := map[string]bool{}
		for _, r := range rows {
			c := scanConn(r)
			if selected != nil && !wanted[c.ID] {
				continue
			}
			if _, _, err := checkURL(c.URL, policy); err != nil {
				continue
			}
			tools, err := loadTools(ctx, tx, c.ID)
			if err != nil {
				return err
			}
			var leased []LeaseTool
			for _, t := range tools {
				if !t.Enabled || t.Unavailable != "" || effectValue(t.Effect) == commonv1.Effect_EFFECT_UNSPECIFIED || effectValue(t.Effect) > policy.maxEffect() {
					continue
				}
				leased = append(leased, LeaseTool{Tool: t.Tool, Name: t.Name, Description: t.Description, InputSchema: json.RawMessage(t.InputSchema),
					Effect: t.Effect, TimeoutS: t.TimeoutS})
			}
			if len(leased) == 0 {
				continue
			}
			available[c.ID] = true
			data, _ := json.Marshal(leased)
			if _, err := tx.Execute(ctx, `INSERT INTO mcp_leases (task_id, connection_id, user_id, connection_name, url, auth_secret, auth_header, tools, state,
				created_at, revoked_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'active', $9, '') ON CONFLICT (task_id, connection_id) DO NOTHING`,
				taskID, c.ID, user, c.Name, c.URL, c.Secret, c.Header, string(data), now); err != nil {
				return err
			}
			issued++
		}
		for id := range wanted {
			if !available[id] {
				return providererr.New(providererr.Invalid, "personal_mcp_unavailable", "个人配置引用的 MCP 连接不存在、未启用或不可用："+id)
			}
		}
		return nil
	})
	if err != nil {
		return err
	}
	if issued > 0 {
		s.d.Logger.Info("MCP 租约", "action", "issue", "tenant_id", tenant.GetTenantId(), "task_id", taskID, "connections", issued)
	}
	return nil
}

// RevokeTask 收回一个 Task 的全部租约（Task 结束时）；表还没有时什么也不做。
func RevokeTask(ctx context.Context, store relational.Provider, tenant *commonv1.TenantContext, taskID string, now time.Time) error {
	db, err := store.Bind(&commonv1.TenantContext{TenantId: tenant.GetTenantId()})
	if err != nil {
		return err
	}
	err = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, `UPDATE mcp_leases SET state = 'revoked', revoked_at = $1 WHERE task_id = $2 AND state = 'active'`,
			now.UTC().Format(timeFormat), taskID)
		return err
	})
	if err != nil && missingTable(err) {
		return nil
	}
	return err
}

func missingTable(err error) bool {
	for e := err; e != nil; e = errors.Unwrap(e) {
		text := strings.ToLower(e.Error())
		if strings.Contains(text, "no such table") || (strings.Contains(text, "relation") && strings.Contains(text, "does not exist")) {
			return true
		}
	}
	return false
}
