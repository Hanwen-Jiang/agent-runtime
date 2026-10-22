package openaicompat_test

// 难点“密钥不外泄”：密钥只在适配层发请求时出现在 Authorization 头里；错误信息（含整条错误链与各种打印形式）不出现密钥，
// 即使上游把它回显在错误正文里；客户端与 Provider 的打印形式也不出现密钥。

import (
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/openaicompat"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/openaicompat/fakeopenai"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
)

// TestEchoedKeyNeverReachesErrors：上游在错误正文里回显 Authorization 头（JSON 的 error.message、自定义 JSON、纯文本），
// 四个方法、租户 BYOK 与平台 Key 两种作用域下，错误里都不出现密钥。
func TestEchoedKeyNeverReachesErrors(t *testing.T) {
	faults := map[string]fakeopenai.Fault{
		"400":                 {Status: 400},
		"401":                 {Status: 401, Code: "invalid_api_key"},
		"403":                 {Status: 403},
		"404 model_not_found": {Status: 404, Code: "model_not_found"},
		"429":                 {Status: 429, Code: "rate_limit_exceeded", Header: map[string]string{"Retry-After": "1"}},
		"500":                 {Status: 500},
		"503":                 {Status: 503},
		"JSON 正文里的 message":   {Status: 401, Body: `{"error":{"message":"bad key: ` + fakeopenai.AuthorizationPlaceholder + `","type":"invalid_request_error","code":"invalid_api_key"}}`},
		"JSON 正文里的其他字段":       {Status: 400, Body: `{"error":{"message":"bad request","type":"invalid_request_error","param":"` + fakeopenai.AuthorizationPlaceholder + `","code":null},"debug":"` + fakeopenai.AuthorizationPlaceholder + `"}`},
		"纯文本正文":               {Status: 502, Body: "upstream proxy error, request headers: " + fakeopenai.AuthorizationPlaceholder},
	}
	for _, m := range calls {
		for name, f := range faults {
			t.Run(m.name+"/"+name, func(t *testing.T) {
				e := newEnv(t, envConfig{fake: fakeopenai.Options{EchoAuthorization: true}})
				for scope, c := range map[string]modelprovider.Client{"租户": e.tenantA(t), "平台": e.platform(t)} {
					fault := f
					fault.Path = m.path
					e.fake.FailNext(fault)
					err := m.do(t.Context(), c)
					if err == nil {
						t.Fatalf("%s：注入的故障应让调用失败", scope)
					}
					expectProviderError(t, err)
					assertNoSecret(t, err, scope)
				}
			})
		}
	}
}

// TestUnknownModelErrorWithEchoNeverLeaksKey：上游本身的 404 model_not_found 与 401 回显 Authorization 时，错误里不出现密钥。
func TestUnknownModelErrorWithEchoNeverLeaksKey(t *testing.T) {
	e := newEnv(t, envConfig{fake: fakeopenai.Options{EchoAuthorization: true}})
	c := e.tenantA(t)
	_, err := c.Generate(t.Context(), ask("no-such-model", "x"))
	assertNoSecret(t, err, "未知模型")
	_, err = streamErr(t.Context(), c, ask("no-such-model", "x"))
	assertNoSecret(t, err, "未知模型（流式）")
	_, err = c.Embed(t.Context(), modelprovider.EmbedRequest{Model: "no-such-embedding", Texts: []string{"a"}})
	assertNoSecret(t, err, "未知嵌入模型")

	// 上游不认这个 Key（例如 Key 已被吊销），401 正文回显 Key。
	other := newEnv(t, envConfig{fake: fakeopenai.Options{APIKeys: []string{"sk-some-other-key"}, EchoAuthorization: true}})
	_, err = other.tenantA(t).Generate(t.Context(), askFake("x"))
	if err == nil {
		t.Fatal("上游不认 Key 时应报错")
	}
	assertNoSecret(t, err, "上游不认 Key")
}

// TestTransportFailuresNeverLeakKey：断开连接、超时、连不上、非法 JSON、流式中途断开时，错误里不出现密钥。
func TestTransportFailuresNeverLeakKey(t *testing.T) {
	for name, f := range map[string]fakeopenai.Fault{
		"断开连接":    {Drop: true},
		"超时":      {Delay: 5 * time.Second},
		"非法 JSON": {MalformedJSON: true},
	} {
		for _, m := range calls {
			t.Run(name+"/"+m.name, func(t *testing.T) {
				e := newEnv(t, envConfig{adapter: func(o *openaicompat.Options) { o.Timeout = 300 * time.Millisecond }})
				fault := f
				fault.Path = m.path
				e.fake.FailNext(fault)
				err := m.do(t.Context(), e.tenantA(t))
				if err == nil {
					t.Fatal("注入的故障应让调用失败")
				}
				assertNoSecret(t, err, name)
			})
		}
	}
	t.Run("流式中途断开", func(t *testing.T) {
		e := newEnv(t, envConfig{})
		e.fake.ReplyNext(fakeopenai.Reply{Content: "这段回复会在中途断开"})
		e.fake.FailNext(fakeopenai.Fault{Path: chatPath, CutStreamAfter: 2})
		_, err := streamErr(t.Context(), e.tenantA(t), askFake("x"))
		if err == nil {
			t.Fatal("断开的流应报错")
		}
		assertNoSecret(t, err, "流式中途断开")
	})
	t.Run("连不上", func(t *testing.T) {
		e := newEnv(t, envConfig{noAdapter: true})
		p := newProvider(t, openaicompat.Options{BaseURL: "http://" + closedAddr(t) + "/v1", Secrets: e.secrets, KeyName: keyName, Timeout: 10 * time.Second})
		c, err := p.Bind(conformance.TenantA())
		if err != nil {
			t.Fatalf("Bind: %v", err)
		}
		for _, m := range calls {
			err := m.do(t.Context(), c)
			if err == nil {
				t.Fatalf("%s：连不上时应报错", m.name)
			}
			assertNoSecret(t, err, "连不上："+m.name)
		}
	})
}

// TestClientAndProviderNeverPrintKey：调用成功（密钥已读出）之后，客户端与 Provider 的各种打印形式里都不出现密钥。
func TestClientAndProviderNeverPrintKey(t *testing.T) {
	e := newEnv(t, envConfig{})
	a, p := e.tenantA(t), e.platform(t)
	generate(t, a, askFake("x"))
	generate(t, p, askFake("x"))
	assertNoSecretInValue(t, a, "租户客户端")
	assertNoSecretInValue(t, p, "平台客户端")
	assertNoSecretInValue(t, e.provider, "Provider")
	s, err := a.StreamGenerate(t.Context(), askFake("x"))
	if err != nil {
		t.Fatalf("StreamGenerate: %v", err)
	}
	defer s.Close()
	assertNoSecretInValue(t, s, "Stream")
}
