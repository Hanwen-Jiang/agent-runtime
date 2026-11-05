// Package sandboxclient 是 task-service 调 Sandbox Service（SBX-01）的客户端：Workspace API 的只读方法（WS-READ-01）。
// task-service 是只读的调用方（服务端只放它调列目录、读文件、提交记录、diff 与 DescribeTenant）。令牌取 SecretStore 平台作用域的
// sandbox-service/tokens/task-service（服务启动时生成，共用的 SecretStore 文件），每次请求时读，轮换后不用重启；令牌不进日志。
package sandboxclient

import (
	"context"
	"errors"
	"net/http"
	"strings"
	"time"

	"connectrpc.com/connect"

	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1/sandboxv1connect"
)

// Caller 是 task-service 的调用方名；TokenSecret 是它的令牌在 SecretStore 平台作用域里的名字（与服务的 TokenSecretPrefix 一致）。
const (
	Caller       = "task-service"
	TokenSecret  = "sandbox-service/tokens/" + Caller
	CallerHeader = "X-Agentruntime-Caller"
)

// New 构造客户端。address 是主机:端口（集群内的 Service）；token 读令牌（没有时返回空）。
func New(address string, token func(ctx context.Context) ([]byte, error)) sandboxv1connect.SandboxServiceClient {
	base := address
	if !strings.HasPrefix(base, "http://") && !strings.HasPrefix(base, "https://") {
		base = "http://" + base
	}
	client := &http.Client{Timeout: 2 * time.Minute, Transport: &http.Transport{Proxy: nil, MaxIdleConnsPerHost: 16}}
	return sandboxv1connect.NewSandboxServiceClient(client, base, connect.WithInterceptors(auth{token: token}))
}

type auth struct {
	token func(ctx context.Context) ([]byte, error)
}

func (a auth) WrapUnary(next connect.UnaryFunc) connect.UnaryFunc {
	return func(ctx context.Context, req connect.AnyRequest) (connect.AnyResponse, error) {
		tok, err := a.token(ctx)
		if err != nil {
			return nil, err
		}
		if len(tok) == 0 {
			return nil, connect.NewError(connect.CodeUnavailable, errors.New("还没有 Sandbox Service 的调用方令牌（"+TokenSecret+"）"))
		}
		req.Header().Set(CallerHeader, Caller)
		req.Header().Set("Authorization", "Bearer "+strings.TrimSpace(string(tok)))
		return next(ctx, req)
	}
}

func (a auth) WrapStreamingClient(next connect.StreamingClientFunc) connect.StreamingClientFunc {
	return next
}

func (a auth) WrapStreamingHandler(next connect.StreamingHandlerFunc) connect.StreamingHandlerFunc {
	return next
}
