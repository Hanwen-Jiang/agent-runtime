package openaicompat_test

// 测试共用的环境：模拟的 OpenAI 兼容服务（fakeopenai）+ 开发 SecretStore（filesecrets）+ 适配层。
//
// 测试用的 Key 都带 DO-NOT-LEAK 字样，便于断言它们不出现在错误、打印形式里。

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/openaicompat"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/openaicompat/fakeopenai"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

const (
	keyName = "model-provider/api-key"
	// platformKey 是平台作用域的 Key。
	platformKey = "sk-test-DO-NOT-LEAK-0123"
	// alphaKey、betaKey 是租户 A、B 自带的 Key（BYOK）。
	alphaKey = "sk-test-DO-NOT-LEAK-byok-alpha-4567"
	betaKey  = "sk-test-DO-NOT-LEAK-byok-beta-8901"
	// leakMarker 是所有测试 Key 共有的片段：错误里出现它，说明 Key 至少部分外泄了。
	leakMarker = "DO-NOT-LEAK"

	chatPath  = "/chat/completions"
	embedPath = "/embeddings"
)

var allKeys = []string{platformKey, alphaKey, betaKey}

// env 是一个测试用例的环境。
type env struct {
	fake         *fakeopenai.Server
	secrets      *filesecrets.Provider
	secretFaults *testkit.Faults
	provider     *openaicompat.Provider
}

// envConfig 调整环境。
type envConfig struct {
	fake      fakeopenai.Options
	seed      map[string]string // 作用域（租户 ID，平台为空串）→ Key；nil 时为平台 Key 加租户 A 的 BYOK
	adapter   func(*openaicompat.Options)
	noAdapter bool
}

func defaultSeed() map[string]string {
	return map[string]string{"": platformKey, conformance.TenantAID: alphaKey}
}

func newEnv(t *testing.T, cfg envConfig) *env {
	t.Helper()
	if len(cfg.fake.APIKeys) == 0 {
		cfg.fake.APIKeys = allKeys
	}
	e := &env{fake: fakeopenai.Start(t, cfg.fake), secretFaults: testkit.NewFaults()}
	// 每个用到本环境的测试结束时都检查：上游收到的每个请求里，Key 只出现在 Authorization 头里。
	t.Cleanup(func() { assertKeyOnlyInAuthorization(t, e.fake.Requests()) })
	secrets, err := filesecrets.New(filesecrets.Options{Path: filepath.Join(t.TempDir(), "secrets.json"), Faults: e.secretFaults})
	if err != nil {
		t.Fatalf("filesecrets.New: %v", err)
	}
	e.secrets = secrets
	seed := cfg.seed
	if seed == nil {
		seed = defaultSeed()
	}
	for scope, key := range seed {
		secrets.SeedSecret(scope, keyName, key)
	}
	if cfg.noAdapter {
		return e
	}
	opts := openaicompat.Options{BaseURL: e.fake.URL, Secrets: secrets, KeyName: keyName, Timeout: 10 * time.Second}
	if cfg.adapter != nil {
		cfg.adapter(&opts)
	}
	e.provider = newProvider(t, opts)
	return e
}

func newProvider(t *testing.T, opts openaicompat.Options) *openaicompat.Provider {
	t.Helper()
	p, err := openaicompat.New(opts)
	if err != nil {
		t.Fatalf("openaicompat.New: %v", err)
	}
	t.Cleanup(func() { _ = p.Close() })
	return p
}

func (e *env) bind(t *testing.T, tenant *commonv1.TenantContext) modelprovider.Client {
	t.Helper()
	c, err := e.provider.Bind(tenant)
	if err != nil {
		t.Fatalf("Bind: %v", err)
	}
	return c
}

func (e *env) tenantA(t *testing.T) modelprovider.Client { return e.bind(t, conformance.TenantA()) }

func (e *env) platform(t *testing.T) modelprovider.Client {
	t.Helper()
	c, err := e.provider.BindPlatform("openaicompat-test")
	if err != nil {
		t.Fatalf("BindPlatform: %v", err)
	}
	return c
}

// lastBody 返回发往 path 的最后一个请求的 JSON 体。
func (e *env) lastBody(t *testing.T, path string) map[string]any {
	t.Helper()
	reqs := e.fake.RequestsTo(path)
	if len(reqs) == 0 {
		t.Fatalf("上游没有收到 %s 请求", path)
	}
	body := reqs[len(reqs)-1].JSON()
	if body == nil {
		t.Fatalf("%s 的请求体不是 JSON 对象：%s", path, reqs[len(reqs)-1].Body)
	}
	return body
}

// posts 返回发往 path 的 POST 请求数（适配层不自己重试：每次调用恰好一个）。
func (e *env) posts(path string) int {
	n := 0
	for _, r := range e.fake.RequestsTo(path) {
		if r.Method == "POST" {
			n++
		}
	}
	return n
}

func generate(t testing.TB, c modelprovider.Client, req modelprovider.GenerateRequest) modelprovider.GenerateResult {
	t.Helper()
	r, err := c.Generate(context.Background(), req)
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	return r
}

func score(t testing.TB, c modelprovider.Client, req modelprovider.ScoreRequest) modelprovider.ScoreResult {
	t.Helper()
	r, err := c.Score(context.Background(), req)
	if err != nil {
		t.Fatalf("Score: %v", err)
	}
	return r
}

func embed(t testing.TB, c modelprovider.Client, req modelprovider.EmbedRequest) modelprovider.EmbedResult {
	t.Helper()
	r, err := c.Embed(context.Background(), req)
	if err != nil {
		t.Fatalf("Embed: %v", err)
	}
	return r
}

// stream 发起流式调用并读完；任何错误都让用例失败。
func stream(t testing.TB, c modelprovider.Client, req modelprovider.GenerateRequest) *drained {
	t.Helper()
	d, err := streamErr(context.Background(), c, req)
	if err != nil {
		t.Fatalf("StreamGenerate: %v", err)
	}
	return d
}

func ask(model, text string) modelprovider.GenerateRequest {
	return modelprovider.GenerateRequest{
		Model:           model,
		Messages:        []modelprovider.Message{{Role: modelprovider.RoleUser, Text: text}},
		MaxOutputTokens: 64,
	}
}

func askFake(text string) modelprovider.GenerateRequest { return ask(fakeopenai.ChatModel, text) }

// drain 读完一个流：返回拼接的文本与思考分片、各分片、最后的 Final 与遇到的错误。
type drained struct {
	text, thinking strings.Builder
	chunks         []modelprovider.Chunk
	final          *modelprovider.GenerateResult
	err            error
}

func drain(ctx context.Context, s modelprovider.Stream) *drained {
	d := &drained{}
	for range 10000 {
		chunk, err := s.Recv(ctx)
		var pe *providererr.Error
		if errors.Is(err, io.EOF) && !errors.As(err, &pe) { // 包着 io.EOF 的 Provider 错误不算正常结束
			if d.final == nil {
				d.err = errors.New("流在交出 Final 之前就返回了 io.EOF")
			}
			return d
		}
		if err != nil {
			d.err = err
			return d
		}
		d.chunks = append(d.chunks, chunk)
		d.text.WriteString(chunk.TextDelta)
		d.thinking.WriteString(chunk.ThinkingDelta)
		if chunk.Final != nil {
			d.final = chunk.Final
		}
	}
	d.err = errors.New("流没有结束")
	return d
}

// streamErr 发起流式调用并读完，返回第一个错误（StreamGenerate 本身或 Recv 的）。
func streamErr(ctx context.Context, c modelprovider.Client, req modelprovider.GenerateRequest) (*drained, error) {
	s, err := c.StreamGenerate(ctx, req)
	if err != nil {
		return nil, err
	}
	defer s.Close()
	d := drain(ctx, s)
	return d, d.err
}

// bindAndGenerate 绑定并生成一次，返回第一个错误（凭据可能在 Bind 时读取，也可能在调用时读取）。
func bindAndGenerate(p *openaicompat.Provider, tenant *commonv1.TenantContext, platform bool) error {
	var c modelprovider.Client
	var err error
	if platform {
		c, err = p.BindPlatform("openaicompat-test")
	} else {
		c, err = p.Bind(tenant)
	}
	if err != nil {
		return err
	}
	_, err = c.Generate(context.Background(), askFake("x"))
	return err
}

// jsonEqual 判断两段 JSON 语义相同。
func jsonEqual(t testing.TB, got, want any) bool {
	t.Helper()
	norm := func(v any) any {
		var raw []byte
		switch x := v.(type) {
		case json.RawMessage:
			raw = x
		case []byte:
			raw = x
		case string:
			raw = []byte(x)
		default:
			b, err := json.Marshal(x)
			if err != nil {
				t.Fatalf("marshal: %v", err)
			}
			raw = b
		}
		var out any
		if err := json.Unmarshal(raw, &out); err != nil {
			t.Fatalf("不是合法的 JSON：%q（%v）", raw, err)
		}
		return out
	}
	return reflect.DeepEqual(norm(got), norm(want))
}

// at 按路径取 JSON 里的值：字符串取对象的字段，整数取数组的元素；取不到时为 nil。
func at(v any, path ...any) any {
	for _, p := range path {
		switch k := p.(type) {
		case string:
			m, ok := v.(map[string]any)
			if !ok {
				return nil
			}
			v = m[k]
		case int:
			s, ok := v.([]any)
			if !ok || k >= len(s) {
				return nil
			}
			v = s[k]
		}
	}
	return v
}

// expectProviderError 断言 err 是 *providererr.Error（不限类别）。
func expectProviderError(t testing.TB, err error) *providererr.Error {
	t.Helper()
	var e *providererr.Error
	if !errors.As(err, &e) {
		t.Fatalf("want *providererr.Error, got %v", err)
	}
	return e
}

// assertNoSecret 断言错误的文本、各种打印形式与整条错误链里都不含任何测试 Key。
func assertNoSecret(t testing.TB, err error, what string) {
	t.Helper()
	if err == nil {
		return
	}
	var texts []string
	var walk func(e error, depth int)
	walk = func(e error, depth int) {
		if e == nil || depth > 32 {
			return
		}
		texts = append(texts, e.Error(), fmt.Sprintf("%v", e), fmt.Sprintf("%+v", e), fmt.Sprintf("%#v", e))
		switch u := e.(type) {
		case interface{ Unwrap() []error }:
			for _, inner := range u.Unwrap() {
				walk(inner, depth+1)
			}
		case interface{ Unwrap() error }:
			walk(u.Unwrap(), depth+1)
		}
	}
	walk(err, 0)
	for _, text := range texts {
		if containsSecret(text) {
			t.Fatalf("%s：错误里出现了密钥：%q", what, text)
		}
	}
}

// credentialHeaderHints 是请求头名里提示“放了凭据”的片段（不分大小写）；除 Authorization 外，名字含这些片段的头都不应出现
// （例如 api-key、x-api-key、Proxy-Authorization），即使值经过编码、看不出 Key 的原文。
var credentialHeaderHints = []string{"key", "auth", "token", "secret", "credential"}

// containsSecret 判断文本里是否有任何测试 Key 或 DO-NOT-LEAK 片段。
func containsSecret(s string) bool {
	if strings.Contains(s, leakMarker) {
		return true
	}
	for _, k := range allKeys {
		if strings.Contains(s, k) {
			return true
		}
	}
	return false
}

// assertKeyOnlyInAuthorization：难点“密钥只在适配层发请求时出现在 Authorization 头里”。对每个记录下的请求断言：
// 原始 URI（含查询串）、请求体、Authorization 以外的每个请求头都不含任何测试 Key 或 DO-NOT-LEAK 片段；
// 没有别的放凭据的头；Authorization 头只有一个值。失败时只报位置，不打印 Key。
func assertKeyOnlyInAuthorization(t testing.TB, reqs []fakeopenai.Request) {
	t.Helper()
	for i, r := range reqs {
		where := fmt.Sprintf("第 %d 个请求（%s %s）", i+1, r.Method, r.Path)
		if containsSecret(r.RawURI) {
			t.Errorf("%s：URL（路径或查询串）里出现了密钥", where)
		}
		if containsSecret(string(r.Body)) {
			t.Errorf("%s：请求体里出现了密钥", where)
		}
		for name, values := range r.Header {
			if http.CanonicalHeaderKey(name) == "Authorization" {
				if len(values) != 1 {
					t.Errorf("%s：Authorization 头应恰好一个值，got %d 个", where, len(values))
				}
				continue
			}
			lower := strings.ToLower(name)
			for _, hint := range credentialHeaderHints {
				if strings.Contains(lower, hint) {
					t.Errorf("%s：除 Authorization 外不应有放凭据的请求头：%s", where, name)
					break
				}
			}
			for _, v := range values {
				if containsSecret(v) {
					t.Errorf("%s：请求头 %s 里出现了密钥", where, name)
				}
			}
		}
	}
}

// assertNoSecretInValue 断言一个值的各种打印形式里都不含任何测试 Key。
func assertNoSecretInValue(t testing.TB, v any, what string) {
	t.Helper()
	for _, format := range []string{"%v", "%+v", "%#v", "%s"} {
		text := fmt.Sprintf(format, v)
		if strings.Contains(text, leakMarker) {
			t.Fatalf("%s 的 %s 打印出了密钥：%q", what, format, text)
		}
	}
}
