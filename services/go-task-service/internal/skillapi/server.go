// Package skillapi 是 SkillService 的 connect-go 实现（N-14；契约 proto/agentruntime/skill/v1/skill.proto）：与 Task API
// 同一个端口、同样认证。
//
// 用户来自认证身份，只能发布、收回自己的用户层 Skill；请求里的 tenant_id 只校验一致。规则（三层、同名上层优先、版本、
// 每人 100 个）都在 Registry（internal/registry 的 skills.go），这里只做认证、限流、错误映射与审计。审计只记（租户，用户，
// name，版本，动作），不记正文。没接 Registry（没配 AR_REGISTRY_ADDR）时四个方法都报 Unimplemented，code 为 skills_unavailable。
package skillapi

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"strings"

	"connectrpc.com/connect"
	"google.golang.org/genproto/googleapis/rpc/errdetails"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
	skillv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/skill/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/skill/v1/skillv1connect"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/registry"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskapi"
)

const (
	routePut    = "put_skill"
	routeList   = "list_skills"
	routeGet    = "get_skill"
	routeRetire = "retire_skill"
)

// Skills 是 Registry 里用户层 Skill 的那几个方法（*registry.Registry 实现它）。
type Skills interface {
	PutUserSkill(ctx context.Context, tenant *commonv1.TenantContext, name string, document *commonv1.JsonPayload) (registry.UserSkillResult, error)
	RetireUserSkill(ctx context.Context, tenant *commonv1.TenantContext, name string) (string, bool, error)
	ReadSkill(ctx context.Context, tenant *commonv1.TenantContext, name, version string) (*registryv1.Skill, error)
	OwnerSkills(ctx context.Context, tenant *commonv1.TenantContext, pageSize int32, pageToken string) (registry.SkillPage, error)
}

// Deps 是构造参数。Skills 为 nil 时服务报不可用；Logger 记审计。
type Deps struct {
	Ingress ingress.Ingress
	Skills  Skills
	Logger  *slog.Logger
}

// Server 实现 SkillService。
type Server struct{ d Deps }

var _ skillv1connect.SkillServiceHandler = (*Server)(nil)

// New 构造；Ingress 必填。
func New(d Deps) (*Server, error) {
	if d.Ingress == nil {
		return nil, errors.New("skillapi: Ingress 必填")
	}
	if d.Logger == nil {
		d.Logger = slog.Default()
	}
	return &Server{d: d}, nil
}

// Mount 把服务挂到 mux 上。
func (s *Server) Mount(mux *http.ServeMux, opts ...connect.HandlerOption) {
	path, handler := skillv1connect.NewSkillServiceHandler(s, opts...)
	mux.Handle(path, handler)
}

var errUnavailable = errors.New("skills_unavailable")

func reply(err error) error {
	switch {
	case errors.Is(err, errUnavailable):
		return withReason(connect.CodeUnimplemented, "skills_unavailable", "这个部署没有接 Registry，不能管 Skill")
	case errors.Is(err, registry.ErrSkillLimit):
		return withReason(connect.CodeResourceExhausted, "skill_limit", "每个用户最多 100 个 Skill")
	}
	return taskapi.ConnectError(err)
}

func withReason(code connect.Code, reason, message string) error {
	ce := connect.NewError(code, errors.New(message))
	if detail, err := connect.NewErrorDetail(&errdetails.ErrorInfo{Reason: reason, Domain: taskapi.ErrorDomain}); err == nil {
		ce.AddDetail(detail)
	}
	return ce
}

// caller 认证、限流，给出带用户的租户上下文；没接 Registry 时不可用。
func (s *Server) caller(ctx context.Context, h http.Header, route, tenantID string) (*commonv1.TenantContext, error) {
	var token string
	if parts := strings.Fields(h.Get("Authorization")); len(parts) == 2 && strings.EqualFold(parts[0], "Bearer") {
		token = parts[1]
	}
	id, err := s.d.Ingress.Authenticate(ctx, ingress.Credentials{BearerToken: token})
	if err != nil {
		return nil, err
	}
	if err := s.d.Ingress.Admit(ctx, id, route); err != nil {
		return nil, err
	}
	tenant, err := ingress.TenantContextFor(id, tenantID, "", "", nil)
	if err != nil {
		return nil, err
	}
	if tenant.GetUserId() == "" {
		return nil, providererr.New(providererr.Denied, "no_user", "认证身份里没有用户，不能管 Skill")
	}
	if s.d.Skills == nil {
		return nil, errUnavailable
	}
	return tenant, nil
}

func (s *Server) audit(tenant *commonv1.TenantContext, action, name, version string) {
	s.d.Logger.Info("Skill", "action", action, "tenant", tenant.GetTenantId(), "user", tenant.GetUserId(), "name", name, "version", version)
}

func (s *Server) PutSkill(ctx context.Context, req *connect.Request[skillv1.PutSkillRequest]) (*connect.Response[skillv1.PutSkillResponse], error) {
	tenant, err := s.caller(ctx, req.Header(), routePut, req.Msg.GetTenantId())
	if err != nil {
		return nil, reply(err)
	}
	got, err := s.d.Skills.PutUserSkill(ctx, tenant, req.Msg.GetName(), req.Msg.GetDocument())
	if err != nil {
		return nil, reply(err)
	}
	if got.NewVersion {
		s.audit(tenant, "put", req.Msg.GetName(), got.Skill.GetVersion())
	}
	return connect.NewResponse(&skillv1.PutSkillResponse{Skill: got.Skill, Created: got.Created, NewVersion: got.NewVersion}), nil
}

func (s *Server) ListSkills(ctx context.Context, req *connect.Request[skillv1.ListSkillsRequest]) (*connect.Response[skillv1.ListSkillsResponse], error) {
	tenant, err := s.caller(ctx, req.Header(), routeList, req.Msg.GetTenantId())
	if err != nil {
		return nil, reply(err)
	}
	page, err := s.d.Skills.OwnerSkills(ctx, tenant, req.Msg.GetPageSize(), req.Msg.GetPageToken())
	if err != nil {
		return nil, reply(err)
	}
	return connect.NewResponse(&skillv1.ListSkillsResponse{Skills: page.Skills, NextPageToken: page.NextPageToken}), nil
}

func (s *Server) GetSkill(ctx context.Context, req *connect.Request[skillv1.GetSkillRequest]) (*connect.Response[skillv1.GetSkillResponse], error) {
	tenant, err := s.caller(ctx, req.Header(), routeGet, req.Msg.GetTenantId())
	if err != nil {
		return nil, reply(err)
	}
	got, err := s.d.Skills.ReadSkill(ctx, tenant, req.Msg.GetName(), req.Msg.GetVersion())
	if err != nil {
		return nil, reply(err)
	}
	return connect.NewResponse(&skillv1.GetSkillResponse{Skill: got}), nil
}

func (s *Server) RetireSkill(ctx context.Context, req *connect.Request[skillv1.RetireSkillRequest]) (*connect.Response[skillv1.RetireSkillResponse], error) {
	tenant, err := s.caller(ctx, req.Header(), routeRetire, req.Msg.GetTenantId())
	if err != nil {
		return nil, reply(err)
	}
	version, retired, err := s.d.Skills.RetireUserSkill(ctx, tenant, req.Msg.GetName())
	if err != nil {
		return nil, reply(err)
	}
	if retired {
		s.audit(tenant, "retire", req.Msg.GetName(), version)
	}
	return connect.NewResponse(&skillv1.RetireSkillResponse{Retired: retired, Version: version}), nil
}
