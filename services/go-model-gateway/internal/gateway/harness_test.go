package gateway_test

// 测试支撑：把 gateway.Server 挂在明文 HTTP/2 的 httptest 服务上，用 connect 的 gRPC 客户端调用
// （与 Python grpcio 走同一种协议，trailer 也经线上传输）。等待只记录、不真睡。

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"

	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	mg "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1/modelgatewayv1connect"

	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/gateway"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/profiles"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/router"
)

// testProfiles：mid 档两家（mid-a 便宜，mid-b 贵），small、strong、embed 各有。模型名即 id 中间那段。
const testProfiles = `{"version":1,"profiles":[
 {"id":"test/mid-a@1","vendor":"deepseek","model":"mid-a","class":"mid","tiers":[{"name":"low","effort":"","thinking_budget":0}],
  "capabilities":["generate","structured","tools","stream"],"cost":{"in_per_m":0.15,"out_per_m":0.6,"cache_read_per_m":0.075},
  "constraints":{"context_window":128000,"max_output":16384},"freshness":{"price_at":"2026-10-21"}},
 {"id":"test/mid-b@1","vendor":"google","model":"mid-b","class":"mid","tiers":[{"name":"low","effort":"","thinking_budget":0}],
  "capabilities":["generate","structured","tools","stream"],"cost":{"in_per_m":0.3,"out_per_m":2.5,"cache_read_per_m":0.03},
  "constraints":{"context_window":1000000,"max_output":65536},"freshness":{"price_at":"2026-10-28"}},
 {"id":"test/small-a@1","vendor":"openai","model":"small-a","class":"small","tiers":[{"name":"low","effort":"","thinking_budget":0}],
  "capabilities":["generate","structured","tools","stream"],"cost":{"in_per_m":0.1,"out_per_m":0.4,"cache_read_per_m":0.025},
  "constraints":{"context_window":128000,"max_output":16384},"freshness":{"price_at":"2026-10-21"}},
 {"id":"test/strong-a@1","vendor":"anthropic","model":"strong-a","class":"strong",
  "tiers":[{"name":"medium","effort":"medium","thinking_budget":8192},{"name":"high","effort":"high","thinking_budget":32768}],
  "capabilities":["generate","structured","tools","stream"],"cost":{"in_per_m":3,"out_per_m":15,"cache_read_per_m":0.3},
  "constraints":{"context_window":200000,"max_output":64000},"freshness":{"price_at":"2026-10-21"}},
 {"id":"test/strong-b@1","vendor":"openai","model":"strong-b","class":"strong",
  "tiers":[{"name":"medium","effort":"medium","thinking_budget":8192},{"name":"high","effort":"high","thinking_budget":32768}],
  "capabilities":["generate","structured","tools","stream"],"cost":{"in_per_m":1.75,"out_per_m":14,"cache_read_per_m":0.175},
  "constraints":{"context_window":400000,"max_output":128000},"freshness":{"price_at":"2026-10-21"}},
 {"id":"test/embed-a@1","vendor":"openai","model":"embed-a","class":"embed","tiers":[{"name":"default"}],
  "capabilities":["embed"],"cost":{"in_per_m":0.02,"out_per_m":0,"cache_read_per_m":0},
  "constraints":{"context_window":8191,"max_output":0},"freshness":{"price_at":"2026-10-30"}}
]}`

// leakyKey 是测试用的假密钥，用来断言它不出现在错误、trailer、日志与计量里。
const leakyKey = "sk-test-DO-NOT-LEAK-0123"

// testRetry：每个 Profile 最多 3 次，退避 100ms 起翻倍，单次等待上限 5s。
var testRetry = gateway.RetryPolicy{MaxAttempts: 3, BaseDelay: 100 * time.Millisecond, MaxDelay: 5 * time.Second}

// syncBuffer 是并发安全的缓冲区。
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// sleepRecorder 记录等待时长，立即返回。
type sleepRecorder struct {
	mu sync.Mutex
	d  []time.Duration
}

func (s *sleepRecorder) sleep(ctx context.Context, d time.Duration) error {
	s.mu.Lock()
	s.d = append(s.d, d)
	s.mu.Unlock()
	return ctx.Err()
}

func (s *sleepRecorder) all() []time.Duration {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]time.Duration(nil), s.d...)
}

type harness struct {
	fake   *fakeProvider
	client modelgatewayv1connect.ModelGatewayServiceClient
	meter  *syncBuffer
	logs   *syncBuffer
	sleeps *sleepRecorder
}

func newHarness(t *testing.T) *harness { return newHarnessWith(t, nil) }

// newHarnessWith 用给定的租户白名单构造（nil 表示不限制）。
func newHarnessWith(t *testing.T, allow router.Allowlist) *harness {
	t.Helper()
	return newHarnessOpts(t, allow, nil)
}

// newHarnessOpts 另可调整 gateway.Options（例如每次尝试的超时）。
func newHarnessOpts(t *testing.T, allow router.Allowlist, adjust func(*gateway.Options)) *harness {
	t.Helper()
	catalog, err := profiles.Parse([]byte(testProfiles))
	if err != nil {
		t.Fatalf("测试用 Profile 应能解析：%v", err)
	}
	h := &harness{fake: newFakeProvider(), meter: &syncBuffer{}, logs: &syncBuffer{}, sleeps: &sleepRecorder{}}
	opts := gateway.Options{
		Provider: h.fake,
		Router:   router.New(catalog, allow),
		Meter:    h.meter,
		Logger:   slog.New(slog.NewTextHandler(h.logs, &slog.HandlerOptions{Level: slog.LevelDebug})),
		Retry:    testRetry,
		Sleep:    h.sleeps.sleep,
		Jitter:   func(d time.Duration) time.Duration { return d },
	}
	if adjust != nil {
		adjust(&opts)
	}
	srv, err := gateway.New(opts)
	if err != nil {
		t.Fatalf("gateway.New：%v", err)
	}
	path, handler := modelgatewayv1connect.NewModelGatewayServiceHandler(srv)
	mux := http.NewServeMux()
	mux.Handle(path, handler)
	ts := httptest.NewUnstartedServer(mux)
	serverProtocols := new(http.Protocols)
	serverProtocols.SetHTTP1(true)
	serverProtocols.SetUnencryptedHTTP2(true)
	ts.Config.Protocols = serverProtocols
	ts.Start()
	t.Cleanup(ts.Close)
	clientProtocols := new(http.Protocols)
	clientProtocols.SetUnencryptedHTTP2(true)
	httpClient := &http.Client{Transport: &http.Transport{Protocols: clientProtocols}}
	t.Cleanup(httpClient.CloseIdleConnections)
	h.client = modelgatewayv1connect.NewModelGatewayServiceClient(httpClient, ts.URL, connect.WithGRPC())
	return h
}

func tenant(id string) *commonv1.TenantContext {
	return &commonv1.TenantContext{TenantId: id, UserId: "u1"}
}

func call(role commonv1.Role) *mg.CallContext {
	return &mg.CallContext{TaskId: "task-1", NodeId: "node-1", Role: role}
}

func say(text string) *mg.GenerateParams {
	return &mg.GenerateParams{Messages: []*mg.ChatMessage{{Role: mg.ChatRole_CHAT_ROLE_USER, Text: text}}}
}

func (h *harness) generate(t *testing.T, tenantID string, c *mg.CallContext, g *mg.GenerateParams) (*mg.GenerateResponse, error) {
	t.Helper()
	resp, err := h.client.Generate(context.Background(), connect.NewRequest(&mg.GenerateRequest{Tenant: tenant(tenantID), Call: c, Generation: g}))
	if err != nil {
		return nil, err
	}
	return resp.Msg, nil
}

func (h *harness) mustGenerate(t *testing.T, tenantID string, c *mg.CallContext, g *mg.GenerateParams) *mg.GenerateResponse {
	t.Helper()
	resp, err := h.generate(t, tenantID, c, g)
	if err != nil {
		t.Fatalf("Generate：%v", err)
	}
	return resp
}

// streamAll 收完一个流；返回全部分片与流的错误。
func (h *harness) streamAll(t *testing.T, req *mg.StreamGenerateRequest) ([]*mg.StreamGenerateResponse, error) {
	t.Helper()
	stream, err := h.client.StreamGenerate(context.Background(), connect.NewRequest(req))
	if err != nil {
		return nil, err
	}
	defer stream.Close()
	var out []*mg.StreamGenerateResponse
	for stream.Receive() {
		out = append(out, proto.Clone(stream.Msg()).(*mg.StreamGenerateResponse))
	}
	return out, stream.Err()
}

// meterLines 解析计量输出：每行一个 JSON 对象。
func (h *harness) meterLines(t *testing.T) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, line := range strings.Split(h.meter.String(), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		var m map[string]any
		if err := json.Unmarshal([]byte(line), &m); err != nil {
			t.Fatalf("计量记录的每一行都应是 JSON：%q（%v）", line, err)
		}
		out = append(out, m)
	}
	return out
}

// errorDetail 取出 Connect 错误码与 ar-error-bin trailer 里的 common.v1.Error。
func errorDetail(t *testing.T, err error) (connect.Code, *commonv1.Error) {
	t.Helper()
	if err == nil {
		t.Fatal("应返回错误")
	}
	var ce *connect.Error
	if !errors.As(err, &ce) {
		t.Fatalf("应是 *connect.Error，实际 %T：%v", err, err)
	}
	values := ce.Meta().Values(gateway.ErrorTrailer)
	if len(values) == 0 {
		t.Fatalf("错误应带 %s trailer：%v", gateway.ErrorTrailer, err)
	}
	raw, decodeErr := connect.DecodeBinaryHeader(values[0])
	if decodeErr != nil {
		t.Fatalf("%s 不是合法的二进制 trailer：%v", gateway.ErrorTrailer, decodeErr)
	}
	var pe commonv1.Error
	if err := proto.Unmarshal(raw, &pe); err != nil {
		t.Fatalf("%s 不是序列化的 common.v1.Error：%v", gateway.ErrorTrailer, err)
	}
	return ce.Code(), &pe
}

// retryAfterMS 取 details 里的 retry_after_ms；没有时 ok 为 false。
func retryAfterMS(pe *commonv1.Error) (int64, bool) {
	v, ok := pe.GetDetails().GetFields()[gateway.RetryAfterDetail]
	if !ok {
		return 0, false
	}
	return int64(v.GetNumberValue()), true
}

// jsonValue 把 JSON 文本转成 structpb.Value。
func jsonValue(t *testing.T, text string) *structpb.Value {
	t.Helper()
	var v any
	if err := json.Unmarshal([]byte(text), &v); err != nil {
		t.Fatal(err)
	}
	out, err := structpb.NewValue(v)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// writePolicies 写一份租户策略文件（与 task-service 同一格式），返回路径。
func writePolicies(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "tenant-policies.json")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

// sameJSON 比较两段 JSON 的语义是否相同。
func sameJSON(t *testing.T, a, b []byte) bool {
	t.Helper()
	var x, y any
	if err := json.Unmarshal(a, &x); err != nil {
		return false
	}
	if err := json.Unmarshal(b, &y); err != nil {
		return false
	}
	ja, _ := json.Marshal(x)
	jb, _ := json.Marshal(y)
	return bytes.Equal(ja, jb)
}
