// 工作项“Model Gateway 单供应商直连”（docs/plan/model-gateway-direct.md）的适配层验收：
//
//   - 在本地模拟的 OpenAI 兼容服务（fakeopenai）上跑通 modelprovidertest 一致性测试，含越权用例（S-12a 的隔离覆盖检查）。
//   - 请求与响应映射（mapping_test.go）、流式（stream_test.go）、错误分类（errors_test.go）、密钥不外泄（leak_test.go）。
//   - 真实端点（live_test.go）：设了 AR_LIVE_MODEL_PROVIDER=1 才跑。
//
// 本文件：一致性测试、构造参数、思考预算的映射、凭据按绑定的作用域选择（交付 1“凭据经 SecretStore 读取”）。
package openaicompat_test

import (
	"bytes"
	"context"
	"net"
	"net/http"
	"strings"
	"sync"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/openaicompat"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/openaicompat/fakeopenai"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance/modelprovidertest"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
)

// harness 让一致性测试跑在 fakeopenai 上：注入故障映射到 fakeopenai 的按次故障，调用记录由上游收到的 Key 反推作用域。
type harness struct{ e *env }

func (h *harness) Provider() modelprovider.Provider { return h.e.provider }
func (h *harness) Model() string                    { return fakeopenai.ChatModel }
func (h *harness) EmbeddingModel() string           { return fakeopenai.EmbeddingModel }

func pathOf(operation string) string {
	if operation == "Embed" {
		return embedPath
	}
	return chatPath // Generate、StreamGenerate、Score 都走 chat/completions
}

func (h *harness) InjectFault(operation string, category providererr.Category) error {
	path := pathOf(operation)
	switch category {
	case providererr.Transient:
		h.e.fake.FailNext(fakeopenai.RateLimit(path, "1"))
	case providererr.Invalid:
		h.e.fake.FailNext(fakeopenai.Fault{Path: path, Status: 400})
	case providererr.Denied:
		h.e.fake.FailNext(fakeopenai.Fault{Path: path, Status: 401, Code: "invalid_api_key"})
	case providererr.NotFound:
		h.e.fake.FailNext(fakeopenai.Fault{Path: path, Status: 404, Code: "model_not_found"})
	default:
		return conformance.ErrUnsupported // OpenAI 兼容协议没有对应 Conflict 的错误
	}
	return nil
}

// scopeOfKey 由上游收到的 Key 反推作用域：平台 Key 为空串，BYOK 为对应租户。
func scopeOfKey(key string) string {
	switch key {
	case platformKey:
		return ""
	case alphaKey:
		return conformance.TenantAID
	case betaKey:
		return conformance.TenantBID
	default:
		return "unknown-key"
	}
}

func (h *harness) Calls() ([]modelprovidertest.RecordedCall, error) {
	var calls []modelprovidertest.RecordedCall
	for _, r := range h.e.fake.Requests() {
		if r.Method == "POST" && (r.Path == chatPath || r.Path == embedPath) {
			calls = append(calls, modelprovidertest.RecordedCall{TenantID: scopeOfKey(r.APIKey), Method: r.Path})
		}
	}
	return calls, nil
}

// TestConformance：验收“适配层在本地模拟的 OpenAI 兼容服务上跑通 modelprovidertest 一致性测试（含越权用例）”。
// 租户 A 有自带的 Key（BYOK），平台作用域用平台 Key，CallsUseTheBoundScope 由此区分两者。
func TestConformance(t *testing.T) {
	modelprovidertest.Run(t, func(t *testing.T) modelprovidertest.Harness {
		return &harness{e: newEnv(t, envConfig{})}
	})
}

// TestNewValidatesOptions：合法的参数能构造；缺 BaseURL、BaseURL 不是 http(s) 绝对地址、缺 Secrets 时报错。
func TestNewValidatesOptions(t *testing.T) {
	e := newEnv(t, envConfig{noAdapter: true})
	valid := openaicompat.Options{BaseURL: e.fake.URL, Secrets: e.secrets, KeyName: keyName}
	p, err := openaicompat.New(valid)
	if err != nil {
		t.Fatalf("合法的参数应能构造：%v", err)
	}
	_ = p.Close()
	for name, mutate := range map[string]func(*openaicompat.Options){
		"缺 BaseURL":      func(o *openaicompat.Options) { o.BaseURL = "" },
		"BaseURL 不是绝对地址": func(o *openaicompat.Options) { o.BaseURL = "api.example.invalid/v1" },
		"BaseURL 协议不对":   func(o *openaicompat.Options) { o.BaseURL = "ftp://api.example.invalid/v1" },
		"缺 Secrets":      func(o *openaicompat.Options) { o.Secrets = nil },
	} {
		opts := valid
		mutate(&opts)
		if p, err := openaicompat.New(opts); err == nil {
			_ = p.Close()
			t.Errorf("%s：应当报错", name)
		}
	}
}

// TestBaseURLTrailingSlashIsAccepted：BaseURL 末尾带 / 时路径不重复（请求仍发到 /v1/chat/completions）。
func TestBaseURLTrailingSlashIsAccepted(t *testing.T) {
	e := newEnv(t, envConfig{noAdapter: true})
	p := newProvider(t, openaicompat.Options{BaseURL: e.fake.URL + "/", Secrets: e.secrets, KeyName: keyName})
	c, err := p.Bind(conformance.TenantA())
	if err != nil {
		t.Fatalf("Bind: %v", err)
	}
	generate(t, c, askFake("你好"))
	if e.posts(chatPath) != 1 {
		t.Fatalf("requests = %+v", e.fake.Requests())
	}
}

// effortOf 调用 EffortForBudget；骨架阶段它会 panic，这里转成用例失败，不让整个测试进程退出。
func effortOf(t *testing.T, budget int) (effort string) {
	t.Helper()
	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("EffortForBudget(%d) panic: %v", budget, r)
		}
	}()
	return openaicompat.EffortForBudget(budget)
}

// TestEffortForBudget：交付 1“思考预算按区间映射到 reasoning_effort（0 不发送）”，含区间边界。
func TestEffortForBudget(t *testing.T) {
	for _, tc := range []struct {
		budget int
		want   string
	}{
		{0, ""}, {1, "low"}, {1024, "low"}, {4096, "low"},
		{4097, "medium"}, {8192, "medium"}, {16384, "medium"},
		{16385, "high"}, {32768, "high"}, {1 << 20, "high"},
	} {
		if got := effortOf(t, tc.budget); got != tc.want {
			t.Errorf("EffortForBudget(%d) = %q, want %q", tc.budget, got, tc.want)
		}
	}
}

// keysSeen 返回上游收到的 chat 与 embeddings 请求所带的 Key，按顺序。
func keysSeen(e *env) []string {
	var keys []string
	for _, r := range e.fake.Requests() {
		if r.Path == chatPath || r.Path == embedPath {
			keys = append(keys, r.APIKey)
		}
	}
	return keys
}

// TestAuthorizationUsesTheBoundScopeKey：难点“密钥只在适配层发请求时出现在 Authorization 头里”与交付 1 的凭据选择：
// 租户有 BYOK 用 BYOK（四个方法都一样）；租户没有 BYOK 用平台 Key；BindPlatform 只用平台 Key；头的格式为 Bearer；
// 每个请求的 URL（含查询串）、请求体与其他请求头里都没有 Key，也没有别的放凭据的头（例如 api-key、x-api-key）。
// （newEnv 在每个测试结束时也做同样的检查，这里显式断言，覆盖四个方法与两种作用域。）
func TestAuthorizationUsesTheBoundScopeKey(t *testing.T) {
	e := newEnv(t, envConfig{})
	a := e.tenantA(t)
	structured := askFake("a")
	structured.OutputSchema = answerSchema
	structured.Tools = []modelprovider.ToolSpec{{Name: "lookup", Description: "查天气", ParamsSchema: lookupParams}}
	generate(t, a, structured)
	stream(t, a, askFake("a"))
	score(t, a, modelprovider.ScoreRequest{Model: fakeopenai.ChatModel, Query: "q", Candidates: []string{"x", "y"}})
	embed(t, a, modelprovider.EmbedRequest{Model: fakeopenai.EmbeddingModel, Texts: []string{"a"}})
	if got := keysSeen(e); len(got) != 4 || got[0] != alphaKey || got[1] != alphaKey || got[2] != alphaKey || got[3] != alphaKey {
		t.Fatalf("租户 A 的四次调用应都用 BYOK，scopes = %v", scopes(got))
	}
	assertBearerOnly(t, e.fake.Requests())
	e.fake.Reset()

	b := e.bind(t, conformance.TenantB())
	generate(t, b, askFake("b"))
	generate(t, e.platform(t), askFake("p"))
	if got := keysSeen(e); len(got) != 2 || got[0] != platformKey || got[1] != platformKey {
		t.Fatalf("没有 BYOK 的租户与平台作用域应用平台 Key，scopes = %v", scopes(got))
	}
	assertBearerOnly(t, e.fake.Requests())
}

// assertBearerOnly：每个请求都带 Bearer 格式的 Authorization 头，且 Key 只出现在这个头里。
func assertBearerOnly(t *testing.T, reqs []fakeopenai.Request) {
	t.Helper()
	if len(reqs) == 0 {
		t.Fatal("上游没有收到请求")
	}
	for _, r := range reqs {
		if auth := r.Header.Get("Authorization"); !strings.HasPrefix(auth, "Bearer ") {
			t.Fatalf("Authorization 头应为 Bearer 格式（这里不打印头的内容）")
		}
	}
	assertKeyOnlyInAuthorization(t, reqs)
}

func scopes(keys []string) []string {
	out := make([]string, len(keys))
	for i, k := range keys {
		out[i] = "[" + scopeOfKey(k) + "]"
	}
	return out
}

// TestEachTenantUsesItsOwnBYOK：租户 A、B 都有 BYOK 时各用各的，平台作用域不用任何租户的 Key（越权）。
func TestEachTenantUsesItsOwnBYOK(t *testing.T) {
	e := newEnv(t, envConfig{seed: map[string]string{"": platformKey, conformance.TenantAID: alphaKey, conformance.TenantBID: betaKey}})
	generate(t, e.tenantA(t), askFake("a"))
	generate(t, e.bind(t, conformance.TenantB()), askFake("b"))
	generate(t, e.platform(t), askFake("p"))
	got := keysSeen(e)
	if len(got) != 3 || got[0] != alphaKey || got[1] != betaKey || got[2] != platformKey {
		t.Fatalf("scopes = %v", scopes(got))
	}
}

// TestBindCopiesTenantContext：绑定之后修改原 TenantContext，不改变客户端用哪个作用域的 Key（越权）。
func TestBindCopiesTenantContext(t *testing.T) {
	e := newEnv(t, envConfig{seed: map[string]string{"": platformKey, conformance.TenantAID: alphaKey, conformance.TenantBID: betaKey}})
	tenant := conformance.TenantA()
	c := e.bind(t, tenant)
	tenant.TenantId = conformance.TenantBID
	generate(t, c, askFake("a"))
	if got := keysSeen(e); len(got) != 1 || got[0] != alphaKey {
		t.Fatalf("scopes = %v", scopes(got))
	}
}

// TestMissingKeyIsDeniedWithoutCallingUpstream：两处都没有密钥时 Denied，且不调用上游。
func TestMissingKeyIsDeniedWithoutCallingUpstream(t *testing.T) {
	e := newEnv(t, envConfig{seed: map[string]string{}})
	err := bindAndGenerate(e.provider, conformance.TenantA(), false)
	conformance.ExpectError(t, err, providererr.Denied, "")
	err = bindAndGenerate(e.provider, nil, true)
	conformance.ExpectError(t, err, providererr.Denied, "")
	if n := len(e.fake.Requests()); n != 0 {
		t.Fatalf("没有密钥时不应调用上游，收到 %d 个请求", n)
	}
}

// TestPlatformKeyIsNotFallbackForSecretStoreFailure：读租户的 BYOK 遇到 Transient 时照原类别返回，
// 不退回平台 Key（否则租户的调用记到平台的账上），也不调用上游。
func TestPlatformKeyIsNotFallbackForSecretStoreFailure(t *testing.T) {
	e := newEnv(t, envConfig{})
	e.secretFaults.FailNext("Read", providererr.Transient)
	err := bindAndGenerate(e.provider, conformance.TenantA(), false)
	conformance.ExpectError(t, err, providererr.Transient, "")
	if n := len(e.fake.Requests()); n != 0 {
		t.Fatalf("读密钥失败时不应调用上游，收到 %d 个请求", n)
	}
}

// TestUsesProvidedHTTPClient：Options.HTTPClient 非空时用它发请求。
func TestUsesProvidedHTTPClient(t *testing.T) {
	var mu sync.Mutex
	trips := 0
	e := newEnv(t, envConfig{adapter: func(o *openaicompat.Options) {
		o.HTTPClient = newCountingClient(func() { mu.Lock(); trips++; mu.Unlock() })
	}})
	generate(t, e.tenantA(t), askFake("x"))
	mu.Lock()
	defer mu.Unlock()
	if trips == 0 {
		t.Fatal("没有用 Options.HTTPClient")
	}
}

// TestConcurrentCallsKeepTheirScopes：并发调用互不串作用域。
func TestConcurrentCallsKeepTheirScopes(t *testing.T) {
	e := newEnv(t, envConfig{})
	a, p := e.tenantA(t), e.platform(t)
	var wg sync.WaitGroup
	errs := make(chan error, 16)
	for i := range 16 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			c, text := a, "tenant"
			if i%2 == 1 {
				c, text = p, "platform"
			}
			if _, err := c.Generate(context.Background(), askFake(text)); err != nil {
				errs <- err
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("Generate: %v", err)
	}
	for _, r := range e.fake.RequestsTo(chatPath) {
		want, text := platformKey, "platform"
		if bytes.Contains(r.Body, []byte(`"tenant"`)) {
			want, text = alphaKey, "tenant"
		}
		if r.APIKey != want {
			t.Fatalf("%s 的请求用了 [%s] 的 Key", text, scopeOfKey(r.APIKey))
		}
	}
	if n := e.posts(chatPath); n != 16 {
		t.Fatalf("posts = %d", n)
	}
}

// 以下是只在本文件用到的小工具。

// closedAddr 返回一个刚关闭的本地端口，连上去会被拒绝。
func closedAddr(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := l.Addr().String()
	_ = l.Close()
	return addr
}

// countingTransport 在每次往返时回调 hit。
type countingTransport struct {
	inner http.RoundTripper
	hit   func()
}

func (c countingTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	c.hit()
	return c.inner.RoundTrip(r)
}

func newCountingClient(hit func()) *http.Client {
	return &http.Client{Transport: countingTransport{inner: http.DefaultTransport, hit: hit}}
}
