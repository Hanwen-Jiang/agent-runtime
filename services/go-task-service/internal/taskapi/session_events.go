package taskapi

import (
	"context"
	"errors"

	"connectrpc.com/connect"

	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
)

// ListSessionEvents 一次取回一个 Session 里若干 Task 的事件（HIST-01）。契约还在 S-01 人审，实现等审过再写；
// 在那之前返回 Unimplemented，客户端照旧用会话快照与 SSE。
func (s *Server) ListSessionEvents(context.Context, *connect.Request[taskapiv1.ListSessionEventsRequest]) (*connect.Response[taskapiv1.ListSessionEventsResponse], error) {
	return nil, connect.NewError(connect.CodeUnimplemented, errors.New("ListSessionEvents 的契约还在评审（HIST-01）"))
}
