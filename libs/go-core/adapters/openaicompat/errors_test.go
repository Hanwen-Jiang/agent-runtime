package openaicompat_test

// 难点“错误分类一致”：上游 429、5xx、超时为 transient（429 带 RetryAfter）；400 为 invalid；401、403 为 denied；
// 模型不存在为 not_found。只有 transient 可重试。适配层只做协议转换，不自己重试：每次调用恰好向上游发一个请求。
//
// “模型不存在”与“5xx 为 transient”在 503 + model_not_found 上相交：one-api、new-api 一类聚合网关用它表示“该模型此刻没有
// 可用渠道”，是暂时的。这里按状态码决定可否重试：404，或 4xx 且 error.code 为 model_not_found 为 NotFound；5xx 一律
// Transient（含 model_not_found），让 Model Gateway 能重试并切换同档的下一个 Model Profile（附录 H.1 reliability.fallbacks）。
// 这一条待人确认；真实端点对未知模型实际返回什么，由 live_test.go 的 TestLiveErrorsAreClassifiedWithoutLeakingKey 记录。

import (
	"context"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/openaicompat"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/openaicompat/fakeopenai"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
)

type errorCase struct {
	name       string
	fault      fakeopenai.Fault // Path 由用例按调用的方法补上
	want       providererr.Category
	retryAfter time.Duration // 非 0 时检查 RetryAfter
}

var httpErrorCases = []errorCase{
	{name: "400", fault: fakeopenai.Fault{Status: 400}, want: providererr.Invalid},
	{name: "400 context_length_exceeded", fault: fakeopenai.Fault{Status: 400, Code: "context_length_exceeded"}, want: providererr.Invalid},
	{name: "401", fault: fakeopenai.Fault{Status: 401, Code: "invalid_api_key"}, want: providererr.Denied},
	{name: "403", fault: fakeopenai.Fault{Status: 403}, want: providererr.Denied},
	{name: "404", fault: fakeopenai.Fault{Status: 404}, want: providererr.NotFound},
	{name: "400 model_not_found", fault: fakeopenai.Fault{Status: 400, Code: "model_not_found"}, want: providererr.NotFound},
	// 聚合网关的“该模型暂时没有可用渠道”：按 5xx 处理。
	{name: "503 model_not_found", fault: fakeopenai.Fault{Status: 503, Code: "model_not_found"}, want: providererr.Transient},
	{name: "500 model_not_found", fault: fakeopenai.Fault{Status: 500, Code: "model_not_found"}, want: providererr.Transient},
	{name: "429 Retry-After", fault: fakeopenai.Fault{Status: 429, Code: "rate_limit_exceeded", Header: map[string]string{"Retry-After": "7"}}, want: providererr.Transient, retryAfter: 7 * time.Second},
	{name: "429", fault: fakeopenai.Fault{Status: 429, Code: "rate_limit_exceeded"}, want: providererr.Transient},
	{name: "500", fault: fakeopenai.Fault{Status: 500}, want: providererr.Transient},
	{name: "502", fault: fakeopenai.Fault{Status: 502, Body: "<html>502 Bad Gateway</html>"}, want: providererr.Transient},
	{name: "503", fault: fakeopenai.Fault{Status: 503}, want: providererr.Transient},
	{name: "200 非法 JSON", fault: fakeopenai.Fault{MalformedJSON: true}, want: providererr.Transient},
	{name: "断开连接", fault: fakeopenai.Fault{Drop: true}, want: providererr.Transient},
}

// call 是一种调用方式：用哪个方法、发往哪个路径。
type call struct {
	name string
	path string
	do   func(ctx context.Context, c modelprovider.Client) error
}

var calls = []call{
	{"Generate", chatPath, func(ctx context.Context, c modelprovider.Client) error {
		_, err := c.Generate(ctx, askFake("x"))
		return err
	}},
	{"StreamGenerate", chatPath, func(ctx context.Context, c modelprovider.Client) error {
		_, err := streamErr(ctx, c, askFake("x"))
		return err
	}},
	{"Score", chatPath, func(ctx context.Context, c modelprovider.Client) error {
		_, err := c.Score(ctx, modelprovider.ScoreRequest{Model: fakeopenai.ChatModel, Query: "q", Candidates: []string{"a", "b"}})
		return err
	}},
	{"Embed", embedPath, func(ctx context.Context, c modelprovider.Client) error {
		_, err := c.Embed(ctx, modelprovider.EmbedRequest{Model: fakeopenai.EmbeddingModel, Texts: []string{"a"}})
		return err
	}},
}

// TestHTTPErrorsAreClassified：每种上游错误在四个方法上都映射到同一类别（Retryable 与类别一致），429 的 RetryAfter 取自
// Retry-After 头；适配层不自己重试（每次调用恰好一个上游请求）；故障过去后客户端照常可用。
func TestHTTPErrorsAreClassified(t *testing.T) {
	for _, m := range calls {
		for _, tc := range httpErrorCases {
			t.Run(m.name+"/"+tc.name, func(t *testing.T) {
				e := newEnv(t, envConfig{})
				c := e.tenantA(t)
				f := tc.fault
				f.Path = m.path
				e.fake.FailNext(f)
				err := m.do(t.Context(), c)
				conformance.ExpectError(t, err, tc.want, "")
				if tc.retryAfter != 0 {
					if pe := expectProviderError(t, err); pe.RetryAfter != tc.retryAfter {
						t.Fatalf("RetryAfter = %v, want %v", pe.RetryAfter, tc.retryAfter)
					}
				}
				if n := e.posts(m.path); n != 1 {
					t.Fatalf("适配层不应自己重试：上游收到 %d 个请求", n)
				}
				if err := m.do(t.Context(), c); err != nil {
					t.Fatalf("故障过去后应照常可用：%v", err)
				}
			})
		}
	}
}

// TestTimeoutIsTransient：超过 Options.Timeout 为 Transient，且及时返回。
func TestTimeoutIsTransient(t *testing.T) {
	for _, m := range calls {
		t.Run(m.name, func(t *testing.T) {
			e := newEnv(t, envConfig{adapter: func(o *openaicompat.Options) { o.Timeout = 300 * time.Millisecond }})
			e.fake.FailNext(fakeopenai.Fault{Path: m.path, Delay: 5 * time.Second})
			start := time.Now()
			err := m.do(t.Context(), e.tenantA(t))
			conformance.ExpectError(t, err, providererr.Transient, "")
			if elapsed := time.Since(start); elapsed > 3*time.Second {
				t.Fatalf("超时没有生效：%v", elapsed)
			}
		})
	}
}

// TestCallerDeadlineIsTransient：调用方 ctx 的截止时间先到时同样为 Transient（Options.Timeout 用默认值）。
func TestCallerDeadlineIsTransient(t *testing.T) {
	e := newEnv(t, envConfig{adapter: func(o *openaicompat.Options) { o.Timeout = 0 }})
	e.fake.FailNext(fakeopenai.Fault{Path: chatPath, Delay: 5 * time.Second})
	ctx, cancel := context.WithTimeout(t.Context(), 300*time.Millisecond)
	defer cancel()
	start := time.Now()
	_, err := e.tenantA(t).Generate(ctx, askFake("x"))
	conformance.ExpectError(t, err, providererr.Transient, "")
	if elapsed := time.Since(start); elapsed > 3*time.Second {
		t.Fatalf("截止时间没有生效：%v", elapsed)
	}
}

// TestNetworkErrorIsTransient：连不上上游（端口没有监听）为 Transient。
func TestNetworkErrorIsTransient(t *testing.T) {
	e := newEnv(t, envConfig{noAdapter: true})
	p := newProvider(t, openaicompat.Options{BaseURL: "http://" + closedAddr(t) + "/v1", Secrets: e.secrets, KeyName: keyName, Timeout: 10 * time.Second})
	c, err := p.Bind(conformance.TenantA())
	if err != nil {
		t.Fatalf("Bind: %v", err)
	}
	for _, m := range calls {
		conformance.ExpectError(t, m.do(t.Context(), c), providererr.Transient, "")
	}
}

// TestEmptyMessagesIsInvalid：消息为空是不合法的请求（本地拦下或上游 400 均可），Invalid、不可重试。
func TestEmptyMessagesIsInvalid(t *testing.T) {
	e := newEnv(t, envConfig{})
	_, err := e.tenantA(t).Generate(t.Context(), modelprovider.GenerateRequest{Model: fakeopenai.ChatModel})
	conformance.ExpectError(t, err, providererr.Invalid, "")
}
