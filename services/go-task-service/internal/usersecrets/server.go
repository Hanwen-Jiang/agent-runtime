// Package usersecrets 是 UserSecretService 的 connect-go 实现（SEC-API；契约 proto/agentruntime/workspace/v1/user_secrets.proto）：
// 与 Task API 同一个端口、同样认证。
//
// 用户来自认证身份，只能管自己的密钥；请求里的 tenant_id 只校验一致。值写进 SecretStore 租户作用域的 user-secrets/<用户>/<name>
// （出网网关按这个名字现读，ENV-02），业务库的 user_secrets 表只记名字与时间（列举用）。值只进不出：响应、错误、日志与审计里都
// 没有值。部署的 SecretStore 不能写（绑定的 Store 没实现 secretstore.Writer）时三个方法都报 Unimplemented，code 为
// user_secrets_unavailable。
//
// 写入的顺序：先核对上限，再写值，再在同一个事务里重新核对并记下这一行；并发的写入抢到了第 101 个时把刚写的值删掉再报上限。
// 删除先删值（立即生效），再删行。
package usersecrets

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/connecterr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/secretstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	workspacev1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1/workspacev1connect"
)

const (
	// MaxPerUser 是每个用户的密钥个数上限。
	MaxPerUser = 100
	// MaxValueBytes 是一个值按 UTF-8 计的字节上限。
	MaxValueBytes = 8 << 10

	routePut    = "put_user_secret"
	routeList   = "list_user_secrets"
	routeDelete = "delete_user_secret"

	timeFormat = "2006-01-02T15:04:05.000000Z"
)

// name 的规则同 SecretHeader.secret_name。
var nameRule = regexp.MustCompile(`^[A-Za-z0-9._-]{1,128}$`)

// SecretName 是值在 SecretStore 租户作用域里的名字，与出网网关读的一致。
func SecretName(user, name string) string { return "user-secrets/" + user + "/" + name }

// Migrations 建 user_secrets 表（只有名字与时间，没有值）。版本 401：task-service 自己的表，不与 Worker 的 2xx、预算的 3xx 冲突。
func Migrations() []relational.Migration {
	return []relational.Migration{{Version: 401, Name: "user_secrets", Statements: []string{
		`CREATE TABLE user_secrets (user_id TEXT NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
		 PRIMARY KEY (user_id, name))`,
	}}}
}

// Deps 是构造参数。Secrets 为 nil 或绑定的 Store 不能写时服务报不可用；Logger 记审计（租户、用户、名字、动作），不记值。
type Deps struct {
	Ingress ingress.Ingress
	Store   relational.Provider
	Secrets secretstore.Provider
	Clock   clock.Clock
	Logger  *slog.Logger
}

// Server 实现 UserSecretService。
type Server struct{ d Deps }

var _ workspacev1connect.UserSecretServiceHandler = (*Server)(nil)

// New 构造；Ingress 与 Store 必填。
func New(d Deps) (*Server, error) {
	if d.Ingress == nil || d.Store == nil {
		return nil, errors.New("usersecrets: Ingress 与 Store 必填")
	}
	if d.Clock == nil {
		d.Clock = clock.System{}
	}
	if d.Logger == nil {
		d.Logger = slog.Default()
	}
	return &Server{d: d}, nil
}

// Mount 把服务挂到 mux 上。
func (s *Server) Mount(mux *http.ServeMux, opts ...connect.HandlerOption) {
	path, handler := workspacev1connect.NewUserSecretServiceHandler(s, opts...)
	mux.Handle(path, handler)
}

// caller 认证、限流，给出租户上下文与调用者的用户 ID。
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
		return nil, "", providererr.New(providererr.Denied, "no_user", "认证身份里没有用户，不能管用户密钥")
	}
	return tenant, user, nil
}

// writer 给出本租户可写的 SecretStore；不能写时报不可用。
func (s *Server) writer(tenant *commonv1.TenantContext) (secretstore.Writer, error) {
	if s.d.Secrets == nil {
		return nil, errUnavailable
	}
	store, err := s.d.Secrets.Bind(tenant)
	if err != nil {
		return nil, err
	}
	w, ok := store.(secretstore.Writer)
	if !ok {
		return nil, errUnavailable
	}
	return w, nil
}

// errUnavailable 与 errLimit 要的 Connect 码不在统一错误模型的类别里，单独构造（trailer 照样带 common.v1.Error）。
var (
	errUnavailable = errors.New("user_secrets_unavailable")
	errLimit       = errors.New("user_secret_limit")
)

func reply(err error) error {
	switch {
	case errors.Is(err, errUnavailable):
		return withDetail(connect.CodeUnimplemented, providererr.Invalid, "user_secrets_unavailable", "这个部署的 SecretStore 不能写用户密钥")
	case errors.Is(err, errLimit):
		return withDetail(connect.CodeResourceExhausted, providererr.Denied, "user_secret_limit", "每个用户最多 100 个密钥")
	}
	return connecterr.Error(err)
}

func withDetail(code connect.Code, category providererr.Category, errCode, message string) error {
	ce := connect.NewError(code, errors.New(message))
	if raw, err := proto.Marshal(&commonv1.Error{Category: category, Code: errCode, Message: message}); err == nil {
		ce.Meta().Set(connecterr.Trailer, connect.EncodeBinaryHeader(raw))
	}
	return ce
}

func invalid(message string) error {
	return providererr.New(providererr.Invalid, "invalid_argument", message)
}

func checkName(name string) error {
	if !nameRule.MatchString(name) {
		return invalid("name 只能用字母、数字、.、_ 与 -，最多 128 个字符")
	}
	return nil
}

// checkValue 的错误只说哪条规则，不带值。
func checkValue(v string) error {
	switch {
	case v == "":
		return invalid("值不能为空")
	case len(v) > MaxValueBytes:
		return invalid("值最多 8 KiB")
	case !utf8.ValidString(v):
		return invalid("值要是 UTF-8")
	}
	for _, r := range v {
		if r != '\t' && (r < 0x20 || r == 0x7f || (r >= 0x80 && r < 0xa0)) {
			return invalid("值不能有控制字符（水平制表符除外）")
		}
	}
	return nil
}

func (s *Server) now() string { return s.d.Clock.Now().UTC().Format(timeFormat) }

func parseTime(v any) *timestamppb.Timestamp {
	text, _ := v.(string)
	t, err := time.Parse(timeFormat, text)
	if err != nil {
		return nil
	}
	return timestamppb.New(t)
}

func project(row relational.Row) *workspacev1.UserSecret {
	name, _ := row["name"].(string)
	return &workspacev1.UserSecret{Name: name, CreatedAt: parseTime(row["created_at"]), UpdatedAt: parseTime(row["updated_at"])}
}

func (s *Server) db(tenant *commonv1.TenantContext) (relational.Store, error) {
	return s.d.Store.Bind(&commonv1.TenantContext{TenantId: tenant.GetTenantId()})
}

// room 核对能不能再加一个（已有同名的是替换，不受限）；返回已有的那一行。
func room(ctx context.Context, tx relational.Tx, user, name string) (relational.Row, error) {
	row, found, err := tx.QueryOne(ctx, `SELECT name, created_at, updated_at FROM user_secrets WHERE user_id = $1 AND name = $2`, user, name)
	if err != nil || found {
		return row, err
	}
	count, _, err := tx.QueryOne(ctx, `SELECT COUNT(*) AS n FROM user_secrets WHERE user_id = $1`, user)
	if err != nil {
		return nil, err
	}
	if n, _ := count["n"].(int64); n >= MaxPerUser {
		return nil, errLimit
	}
	return nil, nil
}

func (s *Server) PutUserSecret(ctx context.Context, req *connect.Request[workspacev1.PutUserSecretRequest]) (*connect.Response[workspacev1.PutUserSecretResponse], error) {
	tenant, user, err := s.caller(ctx, req.Header(), routePut, req.Msg.GetTenantId())
	if err != nil {
		return nil, reply(err)
	}
	name, value := req.Msg.GetName(), req.Msg.GetValue()
	if err := checkName(name); err != nil {
		return nil, reply(err)
	}
	if err := checkValue(value); err != nil {
		return nil, reply(err)
	}
	w, err := s.writer(tenant)
	if err != nil {
		return nil, reply(err)
	}
	db, err := s.db(tenant)
	if err != nil {
		return nil, reply(err)
	}
	if err := db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		_, err := room(ctx, tx, user, name)
		return err
	}); err != nil {
		return nil, reply(err)
	}
	if err := w.Write(ctx, SecretName(user, name), secret.New(value)); err != nil {
		return nil, reply(err)
	}
	now := s.now()
	out := &workspacev1.PutUserSecretResponse{}
	err = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		existing, err := room(ctx, tx, user, name)
		if err != nil {
			return err
		}
		if existing == nil {
			out.Created = true
			out.Secret = project(relational.Row{"name": name, "created_at": now, "updated_at": now})
			_, err = tx.Execute(ctx, `INSERT INTO user_secrets (user_id, name, created_at, updated_at) VALUES ($1, $2, $3, $4)`, user, name, now, now)
			return err
		}
		existing["updated_at"] = now
		out.Secret = project(existing)
		_, err = tx.Execute(ctx, `UPDATE user_secrets SET updated_at = $1 WHERE user_id = $2 AND name = $3`, now, user, name)
		return err
	})
	if errors.Is(err, errLimit) {
		// 并发的写入抢到了最后一个位置：刚写的值没有对应的一行，删掉，不留可用却列不出的密钥。
		_, _ = w.Delete(ctx, SecretName(user, name))
	}
	if err != nil {
		return nil, reply(err)
	}
	action := "replace"
	if out.Created {
		action = "create"
	}
	s.d.Logger.Info("用户密钥", "action", action, "tenant_id", tenant.GetTenantId(), "user_id", user, "name", name)
	return connect.NewResponse(out), nil
}

func (s *Server) ListUserSecrets(ctx context.Context, req *connect.Request[workspacev1.ListUserSecretsRequest]) (*connect.Response[workspacev1.ListUserSecretsResponse], error) {
	tenant, user, err := s.caller(ctx, req.Header(), routeList, req.Msg.GetTenantId())
	if err != nil {
		return nil, reply(err)
	}
	if _, err := s.writer(tenant); err != nil {
		return nil, reply(err)
	}
	db, err := s.db(tenant)
	if err != nil {
		return nil, reply(err)
	}
	out := &workspacev1.ListUserSecretsResponse{}
	err = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		rows, err := tx.Query(ctx, `SELECT name, created_at, updated_at FROM user_secrets WHERE user_id = $1 ORDER BY name`, user)
		for _, row := range rows {
			out.Secrets = append(out.Secrets, project(row))
		}
		return err
	})
	if err != nil {
		return nil, reply(err)
	}
	return connect.NewResponse(out), nil
}

func (s *Server) DeleteUserSecret(ctx context.Context, req *connect.Request[workspacev1.DeleteUserSecretRequest]) (*connect.Response[workspacev1.DeleteUserSecretResponse], error) {
	tenant, user, err := s.caller(ctx, req.Header(), routeDelete, req.Msg.GetTenantId())
	if err != nil {
		return nil, reply(err)
	}
	name := req.Msg.GetName()
	if err := checkName(name); err != nil {
		return nil, reply(err)
	}
	w, err := s.writer(tenant)
	if err != nil {
		return nil, reply(err)
	}
	db, err := s.db(tenant)
	if err != nil {
		return nil, reply(err)
	}
	removed, err := w.Delete(ctx, SecretName(user, name))
	if err != nil {
		return nil, reply(err)
	}
	var rows int64
	err = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		rows, err = tx.Execute(ctx, `DELETE FROM user_secrets WHERE user_id = $1 AND name = $2`, user, name)
		return err
	})
	if err != nil {
		return nil, reply(err)
	}
	deleted := removed || rows > 0
	if deleted {
		s.d.Logger.Info("用户密钥", "action", "delete", "tenant_id", tenant.GetTenantId(), "user_id", user, "name", name)
	}
	return connect.NewResponse(&workspacev1.DeleteUserSecretResponse{Deleted: deleted}), nil
}
