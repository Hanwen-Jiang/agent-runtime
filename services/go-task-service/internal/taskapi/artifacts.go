package taskapi

// N-18 文件交付的公开入口（docs/plan/n18-file-delivery-f3.md，F3）：ListTaskArtifacts 与下载路由
// GET /v1/tasks/{task_id}/artifacts/{artifact_id}。验收测试在 artifacts_test.go；实现等测试人审过再写，在那之前返回未实现。

import (
	"context"
	"errors"

	"connectrpc.com/connect"

	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
)

// ListTaskArtifacts 列出 Task 交付的文件（只给元数据）。
func (s *Server) ListTaskArtifacts(context.Context, *connect.Request[taskapiv1.ListTaskArtifactsRequest]) (*connect.Response[taskapiv1.ListTaskArtifactsResponse], error) {
	return nil, connect.NewError(connect.CodeUnimplemented, errors.New("ListTaskArtifacts 还没实现（F3，等验收测试人审）"))
}
