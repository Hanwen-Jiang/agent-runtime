package personalconfigapi

import (
	"connectrpc.com/connect"
	"context"
	"errors"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	pb "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/personalconfig/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/personalconfig/v1/personalconfigv1connect"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskapi"
	"log/slog"
	"net/http"
	"strings"
)

type Registry interface {
	GetPersonalConfig(context.Context, *commonv1.TenantContext, string) (*pb.PersonalConfig, error)
	PutPersonalConfig(context.Context, *commonv1.TenantContext, *pb.PersonalConfigDocument, string) (*pb.PersonalConfig, bool, error)
	RetirePersonalConfig(context.Context, *commonv1.TenantContext) (bool, error)
}
type Server struct {
	Ingress  ingress.Ingress
	Registry Registry
	Logger   *slog.Logger
}

func (s *Server) Mount(mux *http.ServeMux, opts ...connect.HandlerOption) {
	path, h := personalconfigv1connect.NewPersonalConfigServiceHandler(s, opts...)
	mux.Handle(path, h)
}
func (s *Server) caller(ctx context.Context, h http.Header, route, tenantID string) (*commonv1.TenantContext, error) {
	token := ""
	parts := strings.Fields(h.Get("Authorization"))
	if len(parts) == 2 && strings.EqualFold(parts[0], "Bearer") {
		token = parts[1]
	}
	id, err := s.Ingress.Authenticate(ctx, ingress.Credentials{BearerToken: token})
	if err != nil {
		return nil, err
	}
	if err = s.Ingress.Admit(ctx, id, route); err != nil {
		return nil, err
	}
	t, err := ingress.TenantContextFor(id, tenantID, "", "", nil)
	if err != nil {
		return nil, err
	}
	if t.UserId == "" {
		return nil, providererr.New(providererr.Denied, "no_user", "没有用户身份")
	}
	if s.Registry == nil {
		return nil, connect.NewError(connect.CodeUnimplemented, errors.New("personal_config_unavailable"))
	}
	return t, nil
}
func (s *Server) PutPersonalConfig(ctx context.Context, req *connect.Request[pb.PutPersonalConfigRequest]) (*connect.Response[pb.PutPersonalConfigResponse], error) {
	t, err := s.caller(ctx, req.Header(), "personal_config_put", req.Msg.TenantId)
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	config, changed, err := s.Registry.PutPersonalConfig(ctx, t, req.Msg.Document, req.Msg.ExpectedVersion)
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	if s.Logger != nil && changed {
		s.Logger.Info("personal config published", "tenant", t.TenantId, "user", t.UserId, "version", config.Version, "digest", config.Digest)
	}
	return connect.NewResponse(&pb.PutPersonalConfigResponse{Config: config, NewVersion: changed}), nil
}
func (s *Server) GetPersonalConfig(ctx context.Context, req *connect.Request[pb.GetPersonalConfigRequest]) (*connect.Response[pb.GetPersonalConfigResponse], error) {
	t, err := s.caller(ctx, req.Header(), "personal_config_get", req.Msg.TenantId)
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	config, err := s.Registry.GetPersonalConfig(ctx, t, req.Msg.Version)
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	return connect.NewResponse(&pb.GetPersonalConfigResponse{Config: config}), nil
}
func (s *Server) RetirePersonalConfig(ctx context.Context, req *connect.Request[pb.RetirePersonalConfigRequest]) (*connect.Response[pb.RetirePersonalConfigResponse], error) {
	t, err := s.caller(ctx, req.Header(), "personal_config_retire", req.Msg.TenantId)
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	retired, err := s.Registry.RetirePersonalConfig(ctx, t)
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	if s.Logger != nil && retired {
		s.Logger.Info("personal config retired", "tenant", t.TenantId, "user", t.UserId)
	}
	return connect.NewResponse(&pb.RetirePersonalConfigResponse{Retired: retired}), nil
}
