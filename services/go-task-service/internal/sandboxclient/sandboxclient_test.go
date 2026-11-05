package sandboxclient

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"

	"connectrpc.com/connect"

	sandboxv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1/sandboxv1connect"
)

type recorder struct {
	sandboxv1connect.UnimplementedSandboxServiceHandler
	mu      sync.Mutex
	headers []http.Header
}

func (r *recorder) ReadFile(_ context.Context, req *connect.Request[sandboxv1.ReadFileRequest]) (*connect.Response[sandboxv1.ReadFileResponse], error) {
	r.mu.Lock()
	r.headers = append(r.headers, req.Header().Clone())
	r.mu.Unlock()
	return connect.NewResponse(&sandboxv1.ReadFileResponse{}), nil
}

// 每次请求带上调用方与当时的令牌（轮换后不用重启）；还没有令牌时不发请求、报 unavailable。
func TestRequestsCarryTheCallerAndTheCurrentToken(t *testing.T) {
	rec := &recorder{}
	path, h := sandboxv1connect.NewSandboxServiceHandler(rec)
	mux := http.NewServeMux()
	mux.Handle(path, h)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	token := ""
	client := New(srv.Listener.Addr().String(), func(context.Context) ([]byte, error) { return []byte(token), nil })
	ctx := context.Background()
	if _, err := client.ReadFile(ctx, connect.NewRequest(&sandboxv1.ReadFileRequest{})); connect.CodeOf(err) != connect.CodeUnavailable {
		t.Fatalf("没有令牌：%v", err)
	}
	for _, tok := range []string{"first-token-0123456789abcdef0123456789", "rotated-token-0123456789abcdef012345678\n"} {
		token = tok
		if _, err := client.ReadFile(ctx, connect.NewRequest(&sandboxv1.ReadFileRequest{})); err != nil {
			t.Fatal(err)
		}
	}
	rec.mu.Lock()
	defer rec.mu.Unlock()
	if len(rec.headers) != 2 {
		t.Fatalf("没有令牌时不发请求：%d", len(rec.headers))
	}
	if rec.headers[0].Get(CallerHeader) != "task-service" || rec.headers[0].Get("Authorization") != "Bearer first-token-0123456789abcdef0123456789" {
		t.Fatalf("%v", rec.headers[0])
	}
	if rec.headers[1].Get("Authorization") != "Bearer rotated-token-0123456789abcdef012345678" {
		t.Fatalf("轮换后用新的令牌（去掉换行）：%v", rec.headers[1])
	}
}
