package gateway_test

// 验收（docs/plan/model-gateway-direct.md“验收·服务”）：缺租户拒绝、成本计算精确到 micros、transient 重试与同档切换、
// 非 transient 不重试、流式最后分片带计量、Classify 只出给定标签、计量记录不含内容与密钥，各有测试。
// 难点与防错：密钥不外泄、租户上下文（按请求租户 Bind）、错误分类一致（Connect 码与 ar-error-bin）、结构化输出、流式、
// rationale 只进 trace。

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	mg "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1"

	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/gateway"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/router"
)

var (
	transform = commonv1.Role_ROLE_TRANSFORM
	ctx       = context.Background()
)

func transient(code string, retryAfter time.Duration) error {
	e := providererr.New(providererr.Transient, code, "上游暂时不可用")
	e.RetryAfter = retryAfter
	return e
}

func TestNewRequiresProviderAndRouter(t *testing.T) {
	r := router.New(nil, nil)
	for name, c := range map[string]struct {
		opts    gateway.Options
		mention string
	}{
		"缺 Provider": {gateway.Options{Router: r}, "Provider"},
		"缺 Router":   {gateway.Options{Provider: newFakeProvider()}, "Router"},
	} {
		if _, err := gateway.New(c.opts); err == nil || !strings.Contains(err.Error(), c.mention) {
			t.Errorf("%s：应报错并指出 %s，实际 %v", name, c.mention, err)
		}
	}
}

// 难点“租户上下文”：缺少租户的请求直接拒绝，不调用供应商。约定 3：invalid，code missing_tenant。
func TestMissingTenantIsRejectedWithoutCallingProvider(t *testing.T) {
	h := newHarness(t)
	for name, tc := range map[string]*commonv1.TenantContext{
		"TenantContext 为空": nil,
		"tenant_id 为空":     {UserId: "u1", WorkspaceId: "w1"},
	} {
		calls := map[string]func() error{
			"Generate": func() error {
				_, err := h.client.Generate(ctx, connect.NewRequest(&mg.GenerateRequest{Tenant: tc, Call: call(transform), Generation: say("hi")}))
				return err
			},
			"StreamGenerate": func() error {
				_, err := h.streamAll(t, &mg.StreamGenerateRequest{Tenant: tc, Call: call(transform), Generation: say("hi")})
				return err
			},
			"Score": func() error {
				_, err := h.client.Score(ctx, connect.NewRequest(&mg.ScoreRequest{Tenant: tc, Call: call(commonv1.Role_ROLE_JUDGE), Query: "q", Candidates: []string{"a"}}))
				return err
			},
			"Embed": func() error {
				_, err := h.client.Embed(ctx, connect.NewRequest(&mg.EmbedRequest{Tenant: tc, Call: call(commonv1.Role_ROLE_FETCH), Texts: []string{"a"}}))
				return err
			},
			"Classify": func() error {
				_, err := h.client.Classify(ctx, connect.NewRequest(&mg.ClassifyRequest{Tenant: tc, Call: call(commonv1.Role_ROLE_ROUTE), Texts: []string{"a"}, Labels: []string{"x", "y"}}))
				return err
			},
		}
		for method, do := range calls {
			code, pe := errorDetail(t, do())
			if code != connect.CodeInvalidArgument || pe.GetCategory() != providererr.Invalid || pe.GetCode() != "missing_tenant" {
				t.Errorf("%s / %s：应是 InvalidArgument + invalid/missing_tenant，实际 %v %v/%s", name, method, code, pe.GetCategory(), pe.GetCode())
			}
		}
	}
	if tenants, platform := h.fake.boundTenants(); len(tenants)+len(platform) != 0 {
		t.Errorf("缺租户时不应绑定供应商客户端：%v %v", tenants, platform)
	}
	if calls := h.fake.allCalls(); len(calls) != 0 {
		t.Errorf("缺租户时不应调用供应商：%+v", calls)
	}
	if lines := h.meterLines(t); len(lines) != 0 {
		t.Errorf("被拒绝的请求不写计量：%v", lines)
	}
}

func TestMalformedTenantIsInvalid(t *testing.T) {
	h := newHarness(t)
	_, err := h.generate(t, "T 1", call(transform), say("hi"))
	code, pe := errorDetail(t, err)
	if code != connect.CodeInvalidArgument || pe.GetCategory() != providererr.Invalid {
		t.Errorf("tenant_id 格式不对：%v %v/%s", code, pe.GetCategory(), pe.GetCode())
	}
	if len(h.fake.allCalls()) != 0 {
		t.Error("tenant_id 格式不对时不应调用供应商")
	}
}

// 难点“租户上下文”：供应商客户端按请求的租户绑定，计量记在该租户名下；不用平台作用域。
func TestProviderIsBoundToTheRequestTenant(t *testing.T) {
	h := newHarness(t)
	for _, id := range []string{"t-alpha", "t-beta", "t-alpha"} {
		h.mustGenerate(t, id, call(transform), say("hi"))
	}
	tenants, platform := h.fake.boundTenants()
	if !slices.Equal(tenants, []string{"t-alpha", "t-beta", "t-alpha"}) || len(platform) != 0 {
		t.Errorf("每次 RPC 按请求租户 Bind 一次：%v，平台作用域 %v", tenants, platform)
	}
	var called []string
	for _, c := range h.fake.allCalls() {
		called = append(called, c.TenantID)
	}
	if !slices.Equal(called, []string{"t-alpha", "t-beta", "t-alpha"}) {
		t.Errorf("供应商调用的租户：%v", called)
	}
	var metered []string
	for _, line := range h.meterLines(t) {
		metered = append(metered, fmt.Sprint(line["tenant_id"]))
	}
	if !slices.Equal(metered, []string{"t-alpha", "t-beta", "t-alpha"}) {
		t.Errorf("计量记在请求的租户名下：%v", metered)
	}
}

func TestGenerateFillsModelChoiceUsageAndCost(t *testing.T) {
	h := newHarness(t)
	resp := h.mustGenerate(t, "t1", call(transform), say("hello"))
	want := &mg.ModelChoice{ModelProfileId: "test/mid-a@1", Tier: "mid@low", Vendor: "deepseek"}
	if !proto.Equal(resp.GetModel(), want) {
		t.Errorf("ModelChoice = %v, want %v", resp.GetModel(), want)
	}
	if calls := h.fake.allCalls(); len(calls) != 1 || calls[0].Model != "mid-a" {
		t.Errorf("供应商的模型名取 Profile.model：%+v", calls)
	}
	if resp.GetText() != "reply from mid-a" || resp.GetFinishReason() != mg.FinishReason_FINISH_REASON_STOP {
		t.Errorf("text/finish_reason：%q %v", resp.GetText(), resp.GetFinishReason())
	}
	// Usage 原样带回；(1000-200)*0.15 + 200*0.075 + 500*0.6 = 435。
	wantUsage := &commonv1.Usage{InputTokens: 1000, OutputTokens: 500, CacheReadTokens: 200, ReasoningTokens: 50, CostUsdMicros: 435}
	if !proto.Equal(resp.GetUsage(), wantUsage) {
		t.Errorf("Usage = %v, want %v", resp.GetUsage(), wantUsage)
	}

	// 显式 Tier strong@high：strong-b（1.75/14）比 strong-a 便宜。2000*1.75 + 1000*14 = 17500。
	h.fake.setUsage(&commonv1.Usage{InputTokens: 2000, OutputTokens: 1000})
	c := call(transform)
	c.Tier = "strong@high"
	resp = h.mustGenerate(t, "t1", c, say("hello"))
	want = &mg.ModelChoice{ModelProfileId: "test/strong-b@1", Tier: "strong@high", Vendor: "openai"}
	if !proto.Equal(resp.GetModel(), want) || resp.GetUsage().GetCostUsdMicros() != 17500 {
		t.Errorf("strong@high：%v，成本 %d（want 17500）", resp.GetModel(), resp.GetUsage().GetCostUsdMicros())
	}
}

// 思考预算：请求为 0 时用 Tier 的 thinking_budget，否则用请求的；max_output_tokens 原样传。
func TestThinkingBudgetAndMaxOutput(t *testing.T) {
	h := newHarness(t)
	c := call(commonv1.Role_ROLE_PLAN) // strong@medium
	g := say("plan it")
	g.MaxOutputTokens = 256
	h.mustGenerate(t, "t1", c, g)
	g.ThinkingBudgetTokens = 1000
	h.mustGenerate(t, "t1", c, g)
	calls := h.fake.allCalls()
	if len(calls) != 2 {
		t.Fatalf("应有 2 次调用：%+v", calls)
	}
	if calls[0].Generate.ThinkingBudgetTokens != 8192 || calls[1].Generate.ThinkingBudgetTokens != 1000 {
		t.Errorf("思考预算：%d、%d，want 8192（Tier 默认）、1000（请求指定）", calls[0].Generate.ThinkingBudgetTokens, calls[1].Generate.ThinkingBudgetTokens)
	}
	if calls[0].Generate.MaxOutputTokens != 256 {
		t.Errorf("max_output_tokens：%d", calls[0].Generate.MaxOutputTokens)
	}
}

// 难点“rationale 只进 trace”：思考内容放进 rationale，不混进 text 或 result。
func TestRationaleOnlyInRationaleField(t *testing.T) {
	h := newHarness(t)
	const thinking = "THINKING-SHOULD-STAY-IN-RATIONALE"
	h.fake.onGenerate("mid-a", func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		return modelprovider.GenerateResult{Text: "final answer", Thinking: thinking}, nil
	})
	resp := h.mustGenerate(t, "t1", call(transform), say("hi"))
	if resp.GetRationale() != thinking {
		t.Errorf("rationale = %q", resp.GetRationale())
	}
	if strings.Contains(resp.GetText(), thinking) || resp.GetResult() != nil {
		t.Errorf("思考内容不得进 text 或 result：text=%q result=%v", resp.GetText(), resp.GetResult())
	}
	if strings.Contains(h.meter.String(), thinking) || strings.Contains(h.logs.String(), thinking) {
		t.Error("思考内容不得进计量记录或日志")
	}
}

// 难点“结构化输出”：output_schema 原样传给适配层，结构化结果放在 result。
func TestStructuredOutputGoesToResult(t *testing.T) {
	h := newHarness(t)
	const schema = `{"type":"object","required":["answer"],"properties":{"answer":{"type":"string"}},"additionalProperties":false}`
	var got modelprovider.GenerateRequest
	h.fake.onGenerate("mid-a", func(req modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		got = req
		return modelprovider.GenerateResult{Text: `{"answer":"42"}`, Structured: json.RawMessage(`{"answer":"42"}`), Thinking: "because"}, nil
	})
	g := say("1+1?")
	g.OutputSchema = &commonv1.JsonPayload{SchemaId: "test://answer", Value: jsonValue(t, schema)}
	resp := h.mustGenerate(t, "t1", call(transform), g)
	if !sameJSON(t, got.OutputSchema, []byte(schema)) {
		t.Errorf("传给适配层的 OutputSchema = %s", got.OutputSchema)
	}
	result, err := protojson.Marshal(resp.GetResult())
	if err != nil || !sameJSON(t, result, []byte(`{"answer":"42"}`)) {
		t.Errorf("result = %s（%v）", result, err)
	}
	if resp.GetRationale() != "because" || strings.Contains(string(result), "because") {
		t.Errorf("rationale 与 result 分开：rationale=%q result=%s", resp.GetRationale(), result)
	}
}

func TestToolCallsRoundTrip(t *testing.T) {
	h := newHarness(t)
	const params = `{"type":"object","properties":{"q":{"type":"string"}},"required":["q"]}`
	var got modelprovider.GenerateRequest
	h.fake.onGenerate("mid-a", func(req modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		got = req
		return modelprovider.GenerateResult{
			ToolCalls:    []modelprovider.ToolCall{{ID: "call-2", Name: "search", Arguments: json.RawMessage(`{"q":"next"}`)}},
			FinishReason: modelprovider.FinishToolCalls,
		}, nil
	})
	args := jsonValue(t, `{"q":"first"}`).GetStructValue()
	g := &mg.GenerateParams{
		Messages: []*mg.ChatMessage{
			{Role: mg.ChatRole_CHAT_ROLE_SYSTEM, Text: "你是助手"},
			{Role: mg.ChatRole_CHAT_ROLE_USER, Text: "查一下", CacheBreakpoint: true},
			{Role: mg.ChatRole_CHAT_ROLE_ASSISTANT, ToolCalls: []*mg.ToolCall{{Id: "call-1", Name: "search", Arguments: args}}},
			{Role: mg.ChatRole_CHAT_ROLE_TOOL, ToolCallId: "call-1", Text: `{"hits":3}`},
		},
		Tools: []*mg.ToolSpec{{Name: "search", Description: "搜索", ParamsSchema: &commonv1.JsonPayload{Value: jsonValue(t, params)}}},
	}
	resp := h.mustGenerate(t, "t1", call(commonv1.Role_ROLE_ACT), g)

	wantRoles := []modelprovider.Role{modelprovider.RoleSystem, modelprovider.RoleUser, modelprovider.RoleAssistant, modelprovider.RoleTool}
	var roles []modelprovider.Role
	for _, m := range got.Messages {
		roles = append(roles, m.Role)
	}
	if !slices.Equal(roles, wantRoles) {
		t.Fatalf("消息角色：%v", roles)
	}
	if !got.Messages[1].CacheBreakpoint {
		t.Error("cache_breakpoint 应原样传给适配层")
	}
	if tc := got.Messages[2].ToolCalls; len(tc) != 1 || tc[0].ID != "call-1" || tc[0].Name != "search" || !sameJSON(t, tc[0].Arguments, []byte(`{"q":"first"}`)) {
		t.Errorf("assistant 的工具调用：%+v", tc)
	}
	if got.Messages[3].ToolCallID != "call-1" || got.Messages[3].Text != `{"hits":3}` {
		t.Errorf("tool 消息：%+v", got.Messages[3])
	}
	if len(got.Tools) != 1 || got.Tools[0].Name != "search" || got.Tools[0].Description != "搜索" || !sameJSON(t, got.Tools[0].ParamsSchema, []byte(params)) {
		t.Errorf("工具：%+v", got.Tools)
	}
	if len(resp.GetToolCalls()) != 1 || resp.GetToolCalls()[0].GetId() != "call-2" || resp.GetToolCalls()[0].GetName() != "search" ||
		resp.GetToolCalls()[0].GetArguments().GetFields()["q"].GetStringValue() != "next" {
		t.Errorf("返回的工具调用：%v", resp.GetToolCalls())
	}
	if resp.GetFinishReason() != mg.FinishReason_FINISH_REASON_TOOL_CALLS {
		t.Errorf("finish_reason = %v", resp.GetFinishReason())
	}
}

func TestUnspecifiedChatRoleIsInvalid(t *testing.T) {
	h := newHarness(t)
	_, err := h.generate(t, "t1", call(transform), &mg.GenerateParams{Messages: []*mg.ChatMessage{{Text: "no role"}}})
	if code, pe := errorDetail(t, err); code != connect.CodeInvalidArgument || pe.GetCategory() != providererr.Invalid {
		t.Errorf("消息角色未指定：%v %v", code, pe.GetCategory())
	}
	if len(h.fake.allCalls()) != 0 {
		t.Error("请求不合法时不应调用供应商")
	}
}

// 交付“调用”：transient 失败带抖动重试，退避翻倍（测试里抖动为恒等）。
func TestTransientIsRetriedThenSucceeds(t *testing.T) {
	h := newHarness(t)
	h.fake.failNext("mid-a", transient("timeout", 0), transient("upstream_5xx", 0))
	resp := h.mustGenerate(t, "t1", call(transform), say("hi"))
	if resp.GetModel().GetModelProfileId() != "test/mid-a@1" {
		t.Errorf("重试后在同一个 Profile 上成功：%v", resp.GetModel())
	}
	if n := h.fake.callsTo("mid-a"); n != 3 {
		t.Errorf("mid-a 应尝试 3 次，实际 %d", n)
	}
	if n := h.fake.callsTo("mid-b"); n != 0 {
		t.Errorf("没用尽重试前不切换：mid-b 被调用 %d 次", n)
	}
	if got := h.sleeps.all(); !slices.Equal(got, []time.Duration{100 * time.Millisecond, 200 * time.Millisecond}) {
		t.Errorf("退避：%v，want [100ms 200ms]", got)
	}
	if lines := h.meterLines(t); len(lines) != 1 {
		t.Errorf("一次 RPC 一条计量记录（不按尝试次数）：%d 条", len(lines))
	}
}

// 交付“调用”：遵守 RetryAfter，有上限（单次等待不超过 MaxDelay）。
func TestRetryAfterIsHonoredUpToTheCap(t *testing.T) {
	for name, c := range map[string]struct {
		retryAfter time.Duration
		want       time.Duration
	}{
		"RetryAfter 长于退避":   {3 * time.Second, 3 * time.Second},
		"RetryAfter 短于退避":   {10 * time.Millisecond, 100 * time.Millisecond},
		"RetryAfter 超过上限":   {time.Hour, 5 * time.Second},
		"没有 RetryAfter 用退避": {0, 100 * time.Millisecond},
	} {
		h := newHarness(t)
		h.fake.failNext("mid-a", transient("rate_limit", c.retryAfter))
		h.mustGenerate(t, "t1", call(transform), say("hi"))
		if got := h.sleeps.all(); !slices.Equal(got, []time.Duration{c.want}) {
			t.Errorf("%s：等待 %v，want [%v]", name, got, c.want)
		}
	}
}

// 交付“调用”与难点“错误分类一致”：非 transient 不重试、不切换，类别经 Connect 码与 ar-error-bin 带回。
func TestNonTransientIsNotRetried(t *testing.T) {
	for _, c := range []struct {
		category providererr.Category
		code     connect.Code
	}{
		{providererr.Invalid, connect.CodeInvalidArgument},
		{providererr.Denied, connect.CodePermissionDenied},
		{providererr.NotFound, connect.CodeNotFound},
		{providererr.Conflict, connect.CodeAborted},
	} {
		h := newHarness(t)
		h.fake.failNext("mid-a", providererr.New(c.category, "upstream_says_no", "上游拒绝"))
		_, err := h.generate(t, "t1", call(transform), say("hi"))
		code, pe := errorDetail(t, err)
		if code != c.code || pe.GetCategory() != c.category || pe.GetCode() != "upstream_says_no" || pe.GetMessage() != "上游拒绝" {
			t.Errorf("%v：Connect %v，trailer %v/%s/%q", c.category, code, pe.GetCategory(), pe.GetCode(), pe.GetMessage())
		}
		if a, b := h.fake.callsTo("mid-a"), h.fake.callsTo("mid-b"); a != 1 || b != 0 {
			t.Errorf("%v：不重试、不切换，实际 mid-a %d 次、mid-b %d 次", c.category, a, b)
		}
		if s := h.sleeps.all(); len(s) != 0 {
			t.Errorf("%v：不应等待：%v", c.category, s)
		}
		if lines := h.meterLines(t); len(lines) != 0 {
			t.Errorf("%v：失败的调用不写计量：%v", c.category, lines)
		}
	}
}

// 交付“调用”：重试用尽后切换同档位的下一个 Model Profile，ModelChoice 与成本反映实际服务的模型。
func TestSwitchesToTheNextProfileOfTheSameTier(t *testing.T) {
	h := newHarness(t)
	h.fake.failNext("mid-a", transient("timeout", 0), transient("timeout", 0), transient("timeout", 0))
	resp := h.mustGenerate(t, "t1", call(transform), say("hi"))
	want := &mg.ModelChoice{ModelProfileId: "test/mid-b@1", Tier: "mid@low", Vendor: "google"}
	if !proto.Equal(resp.GetModel(), want) {
		t.Errorf("ModelChoice = %v, want %v", resp.GetModel(), want)
	}
	if a, b := h.fake.callsTo("mid-a"), h.fake.callsTo("mid-b"); a != 3 || b != 1 {
		t.Errorf("mid-a 3 次后切到 mid-b 1 次，实际 %d、%d", a, b)
	}
	if got := h.sleeps.all(); !slices.Equal(got, []time.Duration{100 * time.Millisecond, 200 * time.Millisecond}) {
		t.Errorf("只在同一个 Profile 的重试之间等待，切换时不等：%v", got)
	}
	// 按 mid-b 的单价：(1000-200)*0.3 + 200*0.03 + 500*2.5 = 1496。
	if got := resp.GetUsage().GetCostUsdMicros(); got != 1496 {
		t.Errorf("成本按实际模型计算：%d，want 1496", got)
	}
	lines := h.meterLines(t)
	if len(lines) != 1 || lines[0]["model_profile_id"] != "test/mid-b@1" || lines[0]["vendor"] != "google" || lines[0]["cost_usd_micros"] != float64(1496) {
		t.Errorf("计量记录实际模型：%v", lines)
	}
}

// 交付“调用”：全部失败返回 transient（Unavailable + ar-error-bin），RetryAfter 经 details 带回。
func TestAllProfilesFailingIsTransient(t *testing.T) {
	h := newHarness(t)
	h.fake.failNext("mid-a", transient("timeout", 0), transient("timeout", 0), transient("rate_limit", 2*time.Second))
	h.fake.failNext("mid-b", transient("timeout", 0), transient("timeout", 0), transient("rate_limit", 2*time.Second))
	_, err := h.generate(t, "t1", call(transform), say("hi"))
	code, pe := errorDetail(t, err)
	if code != connect.CodeUnavailable || pe.GetCategory() != providererr.Transient || pe.GetCode() != "models_exhausted" {
		t.Errorf("全部失败：%v %v/%s", code, pe.GetCategory(), pe.GetCode())
	}
	if ms, ok := retryAfterMS(pe); !ok || ms != 2000 {
		t.Errorf("details.retry_after_ms = %v（%v），want 2000", ms, ok)
	}
	if a, b := h.fake.callsTo("mid-a"), h.fake.callsTo("mid-b"); a != 3 || b != 3 {
		t.Errorf("每个 Profile 各 3 次：%d、%d", a, b)
	}
	if lines := h.meterLines(t); len(lines) != 0 {
		t.Errorf("失败的调用不写计量：%v", lines)
	}
}

// 约定 3：错误类别到 Connect 码与 ar-error-bin 的映射。
func TestConnectErrorMapping(t *testing.T) {
	if gateway.ConnectError(nil) != nil {
		t.Error("nil 应映射为 nil")
	}
	for _, c := range []struct {
		category providererr.Category
		code     connect.Code
	}{
		{providererr.Transient, connect.CodeUnavailable},
		{providererr.Invalid, connect.CodeInvalidArgument},
		{providererr.Denied, connect.CodePermissionDenied},
		{providererr.NotFound, connect.CodeNotFound},
		{providererr.Conflict, connect.CodeAborted},
	} {
		ce := gateway.ConnectError(providererr.New(c.category, "some_code", "给开发者看的说明"))
		code, pe := errorDetail(t, ce)
		if code != c.code || pe.GetCategory() != c.category || pe.GetCode() != "some_code" || pe.GetMessage() != "给开发者看的说明" {
			t.Errorf("%v → %v，trailer %v/%s/%q", c.category, code, pe.GetCategory(), pe.GetCode(), pe.GetMessage())
		}
		if !strings.Contains(ce.Message(), "给开发者看的说明") {
			t.Errorf("%v：Connect 错误文本应带说明：%q", c.category, ce.Message())
		}
		if _, ok := retryAfterMS(pe); ok {
			t.Errorf("%v：没有 RetryAfter 时 details 不带 retry_after_ms", c.category)
		}
	}
	// transient 带 RetryAfter：details.retry_after_ms 为整数毫秒。
	_, pe := errorDetail(t, gateway.ConnectError(transient("rate_limit", 1500*time.Millisecond)))
	if ms, ok := retryAfterMS(pe); !ok || ms != 1500 {
		t.Errorf("retry_after_ms = %v（%v），want 1500", ms, ok)
	}
	// 包了一层的 providererr 也按类别映射。
	code, pe := errorDetail(t, gateway.ConnectError(fmt.Errorf("调用失败：%w", providererr.New(providererr.Denied, "bad_key", "凭据无效"))))
	if code != connect.CodePermissionDenied || pe.GetCode() != "bad_key" {
		t.Errorf("包装过的错误：%v %s", code, pe.GetCode())
	}
	// 超时与取消：transient。
	for name, err := range map[string]error{"超时": context.DeadlineExceeded, "包装的超时": fmt.Errorf("x: %w", context.DeadlineExceeded), "取消": context.Canceled} {
		code, pe := errorDetail(t, gateway.ConnectError(err))
		if code != connect.CodeUnavailable || pe.GetCategory() != providererr.Transient {
			t.Errorf("%s：%v %v", name, code, pe.GetCategory())
		}
	}
	// 其他错误：Internal，trailer 为 transient/internal，文本不带原始错误。
	ce := gateway.ConnectError(errors.New("dial tcp: Authorization: Bearer " + leakyKey))
	code, pe = errorDetail(t, ce)
	if code != connect.CodeInternal || pe.GetCategory() != providererr.Transient || pe.GetCode() != "internal" {
		t.Errorf("其他错误：%v %v/%s", code, pe.GetCategory(), pe.GetCode())
	}
	if strings.Contains(ce.Error(), leakyKey) || strings.Contains(pe.GetMessage(), leakyKey) {
		t.Errorf("原始错误文本可能带密钥，不得外传：%q / %q", ce.Error(), pe.GetMessage())
	}
}

// 难点“密钥不外泄”：底层原因里的密钥（例如上游回显）不进 Connect 错误、trailer、日志与计量。
func TestSecretsInUpstreamErrorsDoNotLeak(t *testing.T) {
	h := newHarness(t)
	echoed := errors.New("401 Unauthorized: invalid api key " + leakyKey)
	h.fake.failNext("mid-a", providererr.Wrap(providererr.Denied, "bad_credentials", "供应商拒绝了凭据", echoed))
	_, err := h.generate(t, "t1", call(transform), say("hi"))
	code, pe := errorDetail(t, err)
	if code != connect.CodePermissionDenied || pe.GetCode() != "bad_credentials" {
		t.Errorf("denied：%v %s", code, pe.GetCode())
	}
	raw, _ := proto.Marshal(pe)
	for where, text := range map[string]string{"Connect 错误": err.Error(), "trailer": string(raw), "日志": h.logs.String(), "计量": h.meter.String()} {
		if strings.Contains(text, leakyKey) {
			t.Errorf("密钥出现在%s里：%q", where, text)
		}
	}

	// 供应商违反约定返回了不是 *providererr.Error 的错误：不重试，按“其他错误”返回，文本不外传。
	h = newHarness(t)
	h.fake.failNext("mid-a", errors.New("upstream said: key="+leakyKey))
	_, err = h.generate(t, "t1", call(transform), say("hi"))
	code, pe = errorDetail(t, err)
	if code != connect.CodeInternal || pe.GetCode() != "internal" {
		t.Errorf("非 providererr：%v %s", code, pe.GetCode())
	}
	if n := h.fake.callsTo("mid-a"); n != 1 {
		t.Errorf("非 providererr 不重试：%d 次", n)
	}
	raw, _ = proto.Marshal(pe)
	for where, text := range map[string]string{"Connect 错误": err.Error(), "trailer": string(raw), "日志": h.logs.String()} {
		if strings.Contains(text, leakyKey) {
			t.Errorf("密钥出现在%s里：%q", where, text)
		}
	}
}

// 难点“流式”：各分片文本依次拼接等于最后一个分片的文本；最后一个分片带 Usage 与成本。
func TestStreamGenerate(t *testing.T) {
	h := newHarness(t)
	const text, thinking = "第一段，第二段，第三段。", "先想一想"
	h.fake.onGenerate("mid-a", func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		return modelprovider.GenerateResult{Text: text, Thinking: thinking}, nil
	})
	// 第一次建流时 transient 失败：发出第一个分片之前可以重试。
	h.fake.failNext("mid-a", transient("timeout", 0))
	chunks, err := h.streamAll(t, &mg.StreamGenerateRequest{Tenant: tenant("t1"), Call: call(commonv1.Role_ROLE_DELIVER), Generation: say("说三段")})
	if err != nil {
		t.Fatalf("StreamGenerate：%v", err)
	}
	if len(chunks) < 2 {
		t.Fatalf("应有若干分片与最后的完整结果：%v", chunks)
	}
	var deltas, thoughts strings.Builder
	for i, c := range chunks {
		last := i == len(chunks)-1
		if c.GetFinal() != nil != last {
			t.Errorf("第 %d 个分片：只有最后一个分片是 final", i)
		}
		if c.GetUsage() != nil != last {
			t.Errorf("第 %d 个分片：只有最后一个分片带 usage", i)
		}
		deltas.WriteString(c.GetTextDelta())
		thoughts.WriteString(c.GetThinkingDelta())
	}
	final := chunks[len(chunks)-1]
	if deltas.String() != text || final.GetFinal().GetText() != text {
		t.Errorf("分片拼接 %q，final %q，want %q", deltas.String(), final.GetFinal().GetText(), text)
	}
	if thoughts.String() != thinking || final.GetFinal().GetRationale() != thinking || strings.Contains(final.GetFinal().GetText(), thinking) {
		t.Errorf("思考分片 %q，rationale %q", thoughts.String(), final.GetFinal().GetRationale())
	}
	wantUsage := &commonv1.Usage{InputTokens: 1000, OutputTokens: 500, CacheReadTokens: 200, ReasoningTokens: 50, CostUsdMicros: 435}
	if !proto.Equal(final.GetUsage(), wantUsage) || !proto.Equal(final.GetFinal().GetUsage(), wantUsage) {
		t.Errorf("最后分片的 usage %v，final.usage %v，want %v", final.GetUsage(), final.GetFinal().GetUsage(), wantUsage)
	}
	if !proto.Equal(final.GetFinal().GetModel(), &mg.ModelChoice{ModelProfileId: "test/mid-a@1", Tier: "mid@low", Vendor: "deepseek"}) {
		t.Errorf("final.model = %v", final.GetFinal().GetModel())
	}
	if n := h.fake.callsTo("mid-a"); n != 2 {
		t.Errorf("建流失败后重试一次：%d 次", n)
	}
	lines := h.meterLines(t)
	if len(lines) != 1 || lines[0]["method"] != "stream_generate" || lines[0]["cost_usd_micros"] != float64(435) {
		t.Errorf("流式一次调用一条计量：%v", lines)
	}
}

// 交付“Classify”：转成结构化输出的生成，标签只能取请求给的值。
func TestClassifyOnlyReturnsGivenLabels(t *testing.T) {
	labels := []string{"billing", "tech", "other"}
	texts := []string{"发票开错了", "登录报 500"}
	classify := func(h *harness, ls, ts []string) (*mg.ClassifyResponse, error) {
		resp, err := h.client.Classify(ctx, connect.NewRequest(&mg.ClassifyRequest{Tenant: tenant("t1"), Call: call(commonv1.Role_ROLE_ROUTE), Texts: ts, Labels: ls}))
		if err != nil {
			return nil, err
		}
		return resp.Msg, nil
	}

	h := newHarness(t)
	var got modelprovider.GenerateRequest
	h.fake.onGenerate("small-a", func(req modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		got = req
		out := json.RawMessage(`{"results":[{"label":"tech","confidence":0.9},{"label":"billing","confidence":1.7}]}`)
		return modelprovider.GenerateResult{Text: string(out), Structured: out}, nil
	})
	resp, err := classify(h, labels, texts)
	if err != nil {
		t.Fatalf("Classify：%v", err)
	}
	if len(resp.GetResults()) != 2 || resp.GetResults()[0].GetLabel() != "tech" || resp.GetResults()[1].GetLabel() != "billing" {
		t.Errorf("results 与 texts 一一对应：%v", resp.GetResults())
	}
	if c := resp.GetResults()[0].GetConfidence(); c != 0.9 {
		t.Errorf("confidence = %v", c)
	}
	if c := resp.GetResults()[1].GetConfidence(); c != 1 {
		t.Errorf("confidence 截到 [0, 1]：%v", c)
	}
	if !proto.Equal(resp.GetModel(), &mg.ModelChoice{ModelProfileId: "test/small-a@1", Tier: "small@low", Vendor: "openai"}) {
		t.Errorf("Classify 默认 small@low：%v", resp.GetModel())
	}
	// (1000-200)*0.1 + 200*0.025 + 500*0.4 = 285。
	if resp.GetUsage().GetCostUsdMicros() != 285 || resp.GetUsage().GetInputTokens() != 1000 {
		t.Errorf("usage = %v", resp.GetUsage())
	}
	// 上游拿到的 schema 里，label 的 enum 正好是请求的 labels；每条文本都在消息里。
	enums := findEnums(t, got.OutputSchema)
	if len(enums) != 1 || !slices.Equal(enums[0], labels) {
		t.Errorf("schema 里的 enum：%v，want %v（schema %s）", enums, labels, got.OutputSchema)
	}
	var all strings.Builder
	for _, m := range got.Messages {
		all.WriteString(m.Text)
	}
	for _, text := range texts {
		if !strings.Contains(all.String(), text) {
			t.Errorf("文本 %q 应传给模型", text)
		}
	}

	// 上游给出标签外的值、条数不对、不是合法 JSON：invalid，不重试。
	for name, out := range map[string]string{
		"标签外的值":     `{"results":[{"label":"weather","confidence":0.5},{"label":"tech","confidence":0.5}]}`,
		"条数不对":      `{"results":[{"label":"tech","confidence":0.5}]}`,
		"不是合法 JSON": `{"results":`,
	} {
		h := newHarness(t)
		h.fake.onGenerate("small-a", func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
			return modelprovider.GenerateResult{Text: out, Structured: json.RawMessage(out)}, nil
		})
		_, err := classify(h, labels, texts)
		code, pe := errorDetail(t, err)
		if code != connect.CodeInvalidArgument || pe.GetCategory() != providererr.Invalid {
			t.Errorf("%s：%v %v", name, code, pe.GetCategory())
		}
		if n := h.fake.callsTo("small-a"); n != 1 {
			t.Errorf("%s：invalid 不重试，实际 %d 次", name, n)
		}
	}

	// labels 或 texts 为空：invalid，不调用供应商。
	h = newHarness(t)
	for name, args := range map[string][2][]string{"labels 为空": {nil, texts}, "texts 为空": {labels, nil}} {
		_, err := classify(h, args[0], args[1])
		if code, _ := errorDetail(t, err); code != connect.CodeInvalidArgument {
			t.Errorf("%s：%v", name, code)
		}
	}
	if len(h.fake.allCalls()) != 0 {
		t.Error("请求不合法时不应调用供应商")
	}
}

// findEnums 找出 JSON Schema 里所有的 enum 数组。
func findEnums(t *testing.T, schema json.RawMessage) [][]string {
	t.Helper()
	var root any
	if err := json.Unmarshal(schema, &root); err != nil {
		t.Fatalf("schema 不是 JSON：%s", schema)
	}
	var out [][]string
	var walk func(v any)
	walk = func(v any) {
		switch x := v.(type) {
		case map[string]any:
			if e, ok := x["enum"].([]any); ok {
				var labels []string
				for _, item := range e {
					labels = append(labels, fmt.Sprint(item))
				}
				out = append(out, labels)
			}
			for _, child := range x {
				walk(child)
			}
		case []any:
			for _, child := range x {
				walk(child)
			}
		}
	}
	walk(root)
	return out
}

// 约定 2：嵌入用 embed 档，embedding_version 等于 Profile id；请求指定了不一致的 embedding_version 时 invalid。
func TestEmbed(t *testing.T) {
	h := newHarness(t)
	h.fake.setUsage(&commonv1.Usage{InputTokens: 1000})
	embed := func(version string) (*mg.EmbedResponse, error) {
		resp, err := h.client.Embed(ctx, connect.NewRequest(&mg.EmbedRequest{
			Tenant: tenant("t1"), Call: call(commonv1.Role_ROLE_FETCH), Texts: []string{"alpha", "beta"}, EmbeddingVersion: version,
		}))
		if err != nil {
			return nil, err
		}
		return resp.Msg, nil
	}
	for _, version := range []string{"", "test/embed-a@1"} {
		resp, err := embed(version)
		if err != nil {
			t.Fatalf("Embed（embedding_version %q）：%v", version, err)
		}
		if resp.GetEmbeddingVersion() != "test/embed-a@1" {
			t.Errorf("embedding_version = %q", resp.GetEmbeddingVersion())
		}
		if !proto.Equal(resp.GetModel(), &mg.ModelChoice{ModelProfileId: "test/embed-a@1", Tier: "embed@default", Vendor: "openai"}) {
			t.Errorf("model = %v", resp.GetModel())
		}
		if len(resp.GetEmbeddings()) != 2 || !slices.Equal(resp.GetEmbeddings()[0].GetValues(), []float32{5, 1}) || !slices.Equal(resp.GetEmbeddings()[1].GetValues(), []float32{4, 1}) {
			t.Errorf("向量与 texts 一一对应：%v", resp.GetEmbeddings())
		}
		// 1000 * 0.02 = 20。
		if resp.GetUsage().GetCostUsdMicros() != 20 {
			t.Errorf("嵌入成本 = %d, want 20", resp.GetUsage().GetCostUsdMicros())
		}
	}
	for _, c := range h.fake.allCalls() {
		if c.Method != "Embed" || c.Model != "embed-a" || !slices.Equal(c.Embed.Texts, []string{"alpha", "beta"}) {
			t.Errorf("供应商调用：%+v", c)
		}
	}
	before := len(h.fake.allCalls())
	for _, version := range []string{"other/embed@9", "test/mid-a@1"} {
		_, err := embed(version)
		code, pe := errorDetail(t, err)
		if code != connect.CodeInvalidArgument || pe.GetCode() != "embedding_version_mismatch" {
			t.Errorf("embedding_version %q 不一致：%v %s", version, code, pe.GetCode())
		}
	}
	if len(h.fake.allCalls()) != before {
		t.Error("embedding_version 不一致时不应调用供应商")
	}
}

func TestScore(t *testing.T) {
	h := newHarness(t)
	resp, err := h.client.Score(ctx, connect.NewRequest(&mg.ScoreRequest{
		Tenant: tenant("t1"), Call: call(commonv1.Role_ROLE_JUDGE), Query: "哪个回答更好", Candidates: []string{"甲", "乙", "丙"}, Rubric: "准确性",
	}))
	if err != nil {
		t.Fatalf("Score：%v", err)
	}
	if !slices.Equal(resp.Msg.GetScores(), []float64{0.1, 0.2, 0.3}) {
		t.Errorf("scores 与 candidates 一一对应：%v", resp.Msg.GetScores())
	}
	// judge 默认 strong@medium，strong-b 更便宜。(1000-200)*1.75 + 200*0.175 + 500*14 = 8435。
	if !proto.Equal(resp.Msg.GetModel(), &mg.ModelChoice{ModelProfileId: "test/strong-b@1", Tier: "strong@medium", Vendor: "openai"}) ||
		resp.Msg.GetUsage().GetCostUsdMicros() != 8435 {
		t.Errorf("model %v，成本 %d", resp.Msg.GetModel(), resp.Msg.GetUsage().GetCostUsdMicros())
	}
	calls := h.fake.allCalls()
	if len(calls) != 1 || calls[0].Score.Query != "哪个回答更好" || calls[0].Score.Rubric != "准确性" || len(calls[0].Score.Candidates) != 3 {
		t.Errorf("供应商调用：%+v", calls)
	}
}

// 选档的输入经 CallContext 传进来：avoid_vendor、pinned_model_profile、租户白名单。
func TestCallContextDrivesSelection(t *testing.T) {
	h := newHarness(t)
	c := call(transform)
	c.AvoidVendor = "deepseek"
	if resp := h.mustGenerate(t, "t1", c, say("hi")); resp.GetModel().GetVendor() != "google" {
		t.Errorf("avoid_vendor deepseek：%v", resp.GetModel())
	}
	c = call(transform)
	c.PinnedModelProfile = "test/mid-b@1"
	if resp := h.mustGenerate(t, "t1", c, say("hi")); resp.GetModel().GetModelProfileId() != "test/mid-b@1" {
		t.Errorf("pinned mid-b：%v", resp.GetModel())
	}

	h = newHarnessWith(t, router.FileAllowlist{Path: writePolicies(t, `{"tenants":{"t-limited":{"effect_ceiling":"read","model_allowlist":["test/mid-b@1"]}}}`)})
	if resp := h.mustGenerate(t, "t-limited", call(transform), say("hi")); resp.GetModel().GetModelProfileId() != "test/mid-b@1" {
		t.Errorf("白名单只允许 mid-b：%v", resp.GetModel())
	}
	if resp := h.mustGenerate(t, "t-free", call(transform), say("hi")); resp.GetModel().GetModelProfileId() != "test/mid-a@1" {
		t.Errorf("没列出的租户不限制：%v", resp.GetModel())
	}
	// 白名单把同档都排除了：not_found，不调用供应商。
	c = call(transform)
	c.Tier = "strong@medium"
	before := len(h.fake.allCalls())
	_, err := h.generate(t, "t-limited", c, say("hi"))
	if code, pe := errorDetail(t, err); code != connect.CodeNotFound || pe.GetCategory() != providererr.NotFound {
		t.Errorf("白名单排除了整档：%v %v", code, pe.GetCategory())
	}
	if len(h.fake.allCalls()) != before {
		t.Error("没有可选模型时不应调用供应商")
	}
}

func TestBadOrUnknownTier(t *testing.T) {
	h := newHarness(t)
	for tier, want := range map[string]connect.Code{"huge@low": connect.CodeNotFound, "strong@ultra": connect.CodeNotFound, "strong": connect.CodeInvalidArgument} {
		c := call(transform)
		c.Tier = tier
		_, err := h.generate(t, "t1", c, say("hi"))
		if code, _ := errorDetail(t, err); code != want {
			t.Errorf("Tier %q：%v，want %v", tier, code, want)
		}
	}
	if len(h.fake.allCalls()) != 0 {
		t.Error("选不出模型时不应调用供应商")
	}
}

// 交付“计量”与约定 4：每次调用一行 JSON，字段齐全，不含消息内容与密钥。
func TestMeterRecordPerCallWithoutContent(t *testing.T) {
	h := newHarness(t)
	const prompt, output, thinking = "PROMPT-TEXT-MUST-NOT-BE-METERED", "OUTPUT-TEXT-MUST-NOT-BE-METERED", "THINKING-MUST-NOT-BE-METERED"
	h.fake.onGenerate("mid-a", func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		return modelprovider.GenerateResult{Text: output, Thinking: thinking}, nil
	})
	h.mustGenerate(t, "t-alpha", &mg.CallContext{TaskId: "task-9", NodeId: "node-7", Role: transform}, say(prompt+" "+leakyKey))
	if _, err := h.client.Embed(ctx, connect.NewRequest(&mg.EmbedRequest{Tenant: tenant("t-alpha"), Call: call(commonv1.Role_ROLE_FETCH), Texts: []string{prompt}})); err != nil {
		t.Fatalf("Embed：%v", err)
	}
	if _, err := h.streamAll(t, &mg.StreamGenerateRequest{Tenant: tenant("t-alpha"), Call: call(commonv1.Role_ROLE_DELIVER), Generation: say(prompt)}); err != nil {
		t.Fatalf("StreamGenerate：%v", err)
	}
	lines := h.meterLines(t)
	if len(lines) != 3 {
		t.Fatalf("3 次调用应有 3 行计量，实际 %d：%s", len(lines), h.meter.String())
	}
	required := []string{"tenant_id", "task_id", "node_id", "role", "model_profile_id", "tier", "vendor", "input_tokens", "output_tokens", "cost_usd_micros"}
	for i, line := range lines {
		for _, key := range required {
			if _, ok := line[key]; !ok {
				t.Errorf("第 %d 行缺字段 %s：%v", i, key, line)
			}
		}
	}
	first := lines[0]
	want := map[string]any{
		"tenant_id": "t-alpha", "task_id": "task-9", "node_id": "node-7", "role": "transform", "method": "generate",
		"model_profile_id": "test/mid-a@1", "tier": "mid@low", "vendor": "deepseek",
		"input_tokens": float64(1000), "output_tokens": float64(500), "cost_usd_micros": float64(435),
	}
	for key, value := range want {
		if first[key] != value {
			t.Errorf("计量字段 %s = %v, want %v", key, first[key], value)
		}
	}
	if lines[1]["method"] != "embed" || lines[1]["tier"] != "embed@default" || lines[1]["role"] != "fetch" {
		t.Errorf("嵌入的计量：%v", lines[1])
	}
	for _, marker := range []string{prompt, output, thinking, leakyKey} {
		if strings.Contains(h.meter.String(), marker) {
			t.Errorf("计量记录不得包含 %q", marker)
		}
		if strings.Contains(h.logs.String(), marker) {
			t.Errorf("日志不得包含 %q", marker)
		}
	}
}

func TestMeterLinesStayIntactUnderConcurrency(t *testing.T) {
	h := newHarness(t)
	const n = 20
	var wg sync.WaitGroup
	errs := make(chan error, n)
	for i := range n {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := h.client.Generate(ctx, connect.NewRequest(&mg.GenerateRequest{
				Tenant: tenant(fmt.Sprintf("t-%02d", i)), Call: call(transform), Generation: say("hi"),
			}))
			errs <- err
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("Generate：%v", err)
		}
	}
	lines := h.meterLines(t)
	if len(lines) != n {
		t.Fatalf("并发 %d 次调用应有 %d 行完整的计量，实际 %d", n, n, len(lines))
	}
	seen := map[any]bool{}
	for _, line := range lines {
		seen[line["tenant_id"]] = true
	}
	if len(seen) != n {
		t.Errorf("每个租户一行：%d 个不同的租户", len(seen))
	}
}
