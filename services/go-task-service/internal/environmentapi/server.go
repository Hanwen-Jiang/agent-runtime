// Package environmentapi exposes the existing Runtime sandbox ledger without managing its lifecycle.
package environmentapi

import (
	"context"
	"errors"
	"net/http"
	"regexp"
	"strings"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/agentruntime/agent-runtime/libs/go-core/connecterr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	envv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/environment/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/environment/v1/environmentv1connect"
)

// Deps uses the same authenticated ingress and business store as WorkspaceService.
// Clock only controls expiry projection; queries never change expiry or probe the backend.
type Deps struct {
	Ingress ingress.Ingress
	Store   relational.Provider
	Clock   func() time.Time
}

type Server struct{ d Deps }

var _ environmentv1connect.EnvironmentServiceHandler = (*Server)(nil)
var envRef = regexp.MustCompile(`^sbe-[a-z0-9-]{1,120}$`)

func New(d Deps) (*Server, error) {
	if d.Ingress == nil || d.Store == nil {
		return nil, errors.New("environmentapi: Ingress and Store are required")
	}
	if d.Clock == nil {
		d.Clock = time.Now
	}
	return &Server{d: d}, nil
}

func (s *Server) Mount(mux *http.ServeMux, opts ...connect.HandlerOption) {
	path, handler := environmentv1connect.NewEnvironmentServiceHandler(s, opts...)
	mux.Handle(path, handler)
}

func (s *Server) caller(ctx context.Context, h http.Header, route, tenantID, workspaceID, sessionID string) (*commonv1.TenantContext, error) {
	var token string
	if parts := strings.Fields(h.Get("Authorization")); len(parts) == 2 && strings.EqualFold(parts[0], "Bearer") {
		token = parts[1]
	}
	id, err := s.d.Ingress.Authenticate(ctx, ingress.Credentials{BearerToken: token})
	if err != nil {
		return nil, err
	}
	if err = s.d.Ingress.Admit(ctx, id, route); err != nil {
		return nil, err
	}
	return ingress.TenantContextFor(id, tenantID, workspaceID, sessionID, nil)
}

const selectColumns = `env_ref, backend, kind, isolation, backend_env_id, workspace_id, session_id, task_id, writable,
 runtime, image, state, created_at, ready_at, last_used_at, expires_at, max_until, destroyed_at, health_confirmed_at, reused_at`

// setupColumns 是 ENV-02 在同一行上记的 setup、start 事实（Worker 的迁移 207）；还没迁移时按旧的列读，setup 按 NONE。
const setupColumns = `, environment_id, setup_state, setup_key, setup_finished_at, setup_error, start_state`

// missingColumn：SQLite 报 no such column，Postgres 报 column … does not exist（在包着的错误里）。
func missingColumn(err error) bool {
	for e := err; e != nil; e = errors.Unwrap(e) {
		text := strings.ToLower(e.Error())
		if strings.Contains(text, "no such column") || (strings.Contains(text, "column") && strings.Contains(text, "does not exist")) {
			return true
		}
	}
	return false
}

// readOnly 跑一次只读查询；缺 setup 的列时另开事务按旧的列再跑（Postgres 上出错的事务不能再用）。
func readOnly(ctx context.Context, db relational.Store, query func(ctx context.Context, tx relational.Tx, columns string) error) error {
	run := func(columns string) error {
		return db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error { return query(ctx, tx, columns) })
	}
	err := run(selectColumns + setupColumns)
	if missingColumn(err) {
		err = run(selectColumns)
	}
	return err
}

func unavailable(err error) error {
	return providererr.Wrap(providererr.Transient, "environment_status_unavailable", "Runtime 环境状态暂时不可读，不能确认 ready 或 reused", err)
}

func (s *Server) GetEnvironment(ctx context.Context, req *connect.Request[envv1.GetEnvironmentRequest]) (*connect.Response[envv1.GetEnvironmentResponse], error) {
	tenant, err := s.caller(ctx, req.Header(), "environment_get", req.Msg.GetTenantId(), "", "")
	if err != nil {
		return nil, connecterr.Error(err)
	}
	if !envRef.MatchString(req.Msg.GetEnvRef()) {
		return nil, connecterr.Error(providererr.New(providererr.Invalid, "invalid_env_ref", "要给有效的 Runtime env_ref"))
	}
	db, err := s.d.Store.Bind(tenant)
	if err != nil {
		return nil, connecterr.Error(err)
	}
	var row relational.Row
	var found bool
	err = readOnly(ctx, db, func(ctx context.Context, tx relational.Tx, columns string) error {
		var e error
		row, found, e = tx.QueryOne(ctx, "SELECT "+columns+" FROM sandbox_envs WHERE env_ref = $1", req.Msg.GetEnvRef())
		return e
	})
	if err != nil {
		return nil, connecterr.Error(unavailable(err))
	}
	if !found {
		return nil, connecterr.Error(providererr.New(providererr.NotFound, "environment_not_found", "环境不存在"))
	}
	record, err := project(row, tenant.GetTenantId(), s.d.Clock())
	if err != nil {
		return nil, connecterr.Error(unavailable(err))
	}
	return connect.NewResponse(&envv1.GetEnvironmentResponse{Environment: record}), nil
}

func (s *Server) ListEnvironments(ctx context.Context, req *connect.Request[envv1.ListEnvironmentsRequest]) (*connect.Response[envv1.ListEnvironmentsResponse], error) {
	m := req.Msg
	tenant, err := s.caller(ctx, req.Header(), "environment_list", m.GetTenantId(), m.GetWorkspaceId(), m.GetSessionId())
	if err != nil {
		return nil, connecterr.Error(err)
	}
	if m.GetWorkspaceId() == "" || m.GetSessionId() == "" || m.GetLimit() < 0 || m.GetLimit() > 100 {
		return nil, connecterr.Error(providererr.New(providererr.Invalid, "invalid_environment_query", "workspace_id、session_id 必填；limit 在 0 到 100 之间"))
	}
	limit := m.GetLimit()
	if limit == 0 {
		limit = 50
	}
	db, err := s.d.Store.Bind(tenant)
	if err != nil {
		return nil, connecterr.Error(err)
	}
	var rows []relational.Row
	err = readOnly(ctx, db, func(ctx context.Context, tx relational.Tx, columns string) error {
		var e error
		rows, e = tx.Query(ctx, "SELECT "+columns+" FROM sandbox_envs WHERE workspace_id = $1 AND session_id = $2 ORDER BY created_at DESC, env_ref ASC LIMIT $3", m.GetWorkspaceId(), m.GetSessionId(), int64(limit+1))
		return e
	})
	if err != nil {
		return nil, connecterr.Error(unavailable(err))
	}
	out := &envv1.ListEnvironmentsResponse{Truncated: len(rows) > int(limit)}
	if out.Truncated {
		rows = rows[:limit]
	}
	now := s.d.Clock()
	for _, row := range rows {
		record, err := project(row, tenant.GetTenantId(), now)
		if err != nil {
			return nil, connecterr.Error(unavailable(err))
		}
		out.Environments = append(out.Environments, record)
	}
	return connect.NewResponse(out), nil
}

func value(row relational.Row, key string) string { s, _ := row[key].(string); return s }

func project(row relational.Row, tenant string, now time.Time) (*envv1.EnvRecord, error) {
	writable, _ := row["writable"].(int64)
	r := &envv1.EnvRecord{
		EnvRef: value(row, "env_ref"), TenantId: tenant, WorkspaceId: value(row, "workspace_id"), SessionId: value(row, "session_id"),
		Backend: value(row, "backend"), BackendEnvId: value(row, "backend_env_id"), Kind: value(row, "kind"), Isolation: value(row, "isolation"),
		TaskId: value(row, "task_id"), Writable: writable == 1, Runtime: value(row, "runtime"), Image: value(row, "image"),
		EnvironmentId: value(row, "environment_id"), SetupKey: value(row, "setup_key"), SetupError: value(row, "setup_error"),
	}
	// 没有这一列（还没迁移 207）时还没有 setup：NONE。认不出的值是 UNSPECIFIED，不当作成功。
	setup, start := "none", "none"
	if _, ok := row["setup_state"]; ok {
		setup, start = value(row, "setup_state"), value(row, "start_state")
	}
	r.SetupState = envv1.SetupState(envv1.SetupState_value["SETUP_STATE_"+strings.ToUpper(setup)])
	r.StartState = envv1.StartState(envv1.StartState_value["START_STATE_"+strings.ToUpper(start)])
	r.State = envv1.EnvironmentState(envv1.EnvironmentState_value["ENVIRONMENT_STATE_"+strings.ToUpper(value(row, "state"))])
	for _, field := range []struct {
		name   string
		target **timestamppb.Timestamp
	}{
		{"created_at", &r.CreatedAt}, {"ready_at", &r.ReadyAt}, {"last_used_at", &r.LastUsedAt},
		{"expires_at", &r.ExpiresAt}, {"max_until", &r.MaxUntil}, {"destroyed_at", &r.DestroyedAt},
		{"health_confirmed_at", &r.HealthConfirmedAt}, {"reused_at", &r.ReusedAt}, {"setup_finished_at", &r.SetupFinishedAt},
	} {
		text := value(row, field.name)
		if text == "" {
			continue
		}
		parsed, err := time.Parse(time.RFC3339Nano, text)
		if err != nil {
			return nil, errors.New("invalid environment timestamp")
		}
		ts := timestamppb.New(parsed)
		if err := ts.CheckValid(); err != nil {
			return nil, err
		}
		*field.target = ts
	}
	prepared := r.SetupState == envv1.SetupState_SETUP_STATE_NONE || r.SetupState == envv1.SetupState_SETUP_STATE_SUCCEEDED || r.SetupState == envv1.SetupState_SETUP_STATE_CACHED
	r.Ready = prepared && r.State == envv1.EnvironmentState_ENVIRONMENT_STATE_READY && r.HealthConfirmedAt != nil && r.ExpiresAt != nil && r.MaxUntil != nil && now.Before(r.ExpiresAt.AsTime()) && now.Before(r.MaxUntil.AsTime())
	r.Reused = r.ReusedAt != nil && r.WorkspaceId != "" && r.SessionId != ""
	return r, nil
}
