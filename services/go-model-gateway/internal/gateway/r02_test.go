package gateway_test

// R-02 评审（docs/plan/review-R-02-model-gateway.md）第 4 节“本期修”的第 2 到 5 条：
//
//  2. pinned_model_profile 没用上、avoid_vendor 没能避开时，计量记录与日志里看得见（网关按实际服务的 Profile 判断，
//     含切换之后的情况）；
//  3. 上游已经计费、但网关判为失败的调用也写计量（status failed、error_code），按租户的成本不再少算；
//  4. 每次尝试有自己的超时，落在调用方剩余的 deadline 之内，重试与切换在 deadline 之内发生；剩下的时间不够等下一次时
//     不再等待，直接返回 transient；
//  5. 只针对某个 Profile 的 model_not_found 切换同档的下一个 Profile，不等待、不在同一个 Profile 上重试。

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/proto"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	mg "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1"

	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/gateway"
)

// ---- 2. pin 与 avoid_vendor 看得见 ----

func TestMeterAndLogShowWhetherThePinWasHonored(t *testing.T) {
	h := newHarness(t)
	h.mustGenerate(t, "t1", call(transform), say("没有 pin"))
	honored := call(transform)
	honored.PinnedModelProfile = "test/mid-b@1"
	h.mustGenerate(t, "t1", honored, say("pin 用上了"))
	ignored := call(transform)
	ignored.PinnedModelProfile = "test/strong-a@1" // 不在 mid@low 里：选档忽略它
	h.mustGenerate(t, "t1", ignored, say("pin 没用上"))

	lines := h.meterLines(t)
	if len(lines) != 3 {
		t.Fatalf("应有 3 行计量：%s", h.meter.String())
	}
	if lines[0]["pin_ignored"] != false || lines[0]["pinned_model_profile"] != nil && lines[0]["pinned_model_profile"] != "" {
		t.Errorf("没有 pin 时 pin_ignored 为 false、不记 pinned_model_profile：%v", lines[0])
	}
	if lines[1]["pinned_model_profile"] != "test/mid-b@1" || lines[1]["pin_ignored"] != false || lines[1]["model_profile_id"] != "test/mid-b@1" {
		t.Errorf("pin 用上了：%v", lines[1])
	}
	if lines[2]["pinned_model_profile"] != "test/strong-a@1" || lines[2]["pin_ignored"] != true || lines[2]["model_profile_id"] != "test/mid-a@1" {
		t.Errorf("pin 没用上时 pin_ignored 为 true：%v", lines[2])
	}
	logs := h.logs.String()
	if !strings.Contains(logs, "test/strong-a@1") || !strings.Contains(logs, "pinned_model_profile") {
		t.Errorf("pin 没用上时日志里要写出请求的 Profile：%s", logs)
	}
	if strings.Count(logs, "pinned_model_profile") != 1 {
		t.Errorf("只有 pin 没用上的那一次记日志：%s", logs)
	}
}

func TestPinIsReportedIgnoredWhenTheCallSwitchesAway(t *testing.T) {
	h := newHarness(t)
	h.fake.failNext("mid-b", transient("timeout", 0), transient("timeout", 0), transient("timeout", 0))
	c := call(transform)
	c.PinnedModelProfile = "test/mid-b@1"
	resp := h.mustGenerate(t, "t1", c, say("hi"))
	if resp.GetModel().GetModelProfileId() != "test/mid-a@1" {
		t.Fatalf("pin 的 Profile 用尽重试后切到同档的下一个：%v", resp.GetModel())
	}
	lines := h.meterLines(t)
	if len(lines) != 1 || lines[0]["pin_ignored"] != true || lines[0]["model_profile_id"] != "test/mid-a@1" {
		t.Errorf("按实际服务的 Profile 判断 pin 有没有用上：%v", lines)
	}
}

func TestMeterAndLogShowWhenAvoidVendorCouldNotBeMet(t *testing.T) {
	h := newHarness(t)
	// small@low 只有 small-a（openai）：避不开。
	unmet := call(commonv1.Role_ROLE_ROUTE)
	unmet.AvoidVendor = "openai"
	resp := h.mustGenerate(t, "t1", unmet, say("SECRET-PROMPT-NOT-FOR-LOGS"))
	if resp.GetModel().GetVendor() != "openai" {
		t.Fatalf("只剩同一家时仍可用：%v", resp.GetModel())
	}
	// mid@low 有别家：避开 deepseek。
	met := call(transform)
	met.AvoidVendor = "deepseek"
	if resp := h.mustGenerate(t, "t1", met, say("hi")); resp.GetModel().GetVendor() != "google" {
		t.Fatalf("有别家时避开：%v", resp.GetModel())
	}
	lines := h.meterLines(t)
	if len(lines) != 2 {
		t.Fatalf("应有 2 行计量：%s", h.meter.String())
	}
	if lines[0]["avoid_vendor"] != "openai" || lines[0]["avoid_vendor_unmet"] != true {
		t.Errorf("避不开时 avoid_vendor_unmet 为 true：%v", lines[0])
	}
	if lines[1]["avoid_vendor"] != "deepseek" || lines[1]["avoid_vendor_unmet"] != false {
		t.Errorf("避开了：%v", lines[1])
	}
	logs := h.logs.String()
	if !strings.Contains(logs, "avoid_vendor") || !strings.Contains(logs, "test/small-a@1") {
		t.Errorf("避不开时日志里要写出 vendor 与实际的 Profile：%s", logs)
	}
	if strings.Contains(logs, "SECRET-PROMPT-NOT-FOR-LOGS") {
		t.Error("日志里不得出现消息内容")
	}
}

// ---- 3. 已计费但判为失败的调用也写计量 ----

const answerSchema = `{"type":"object","required":["answer"],"properties":{"answer":{"type":"string"}}}`

func TestSuccessfulCallsAreMeteredAsOK(t *testing.T) {
	h := newHarness(t)
	h.mustGenerate(t, "t1", call(transform), say("hi"))
	lines := h.meterLines(t)
	if len(lines) != 1 || lines[0]["status"] != "ok" {
		t.Fatalf("成功的调用 status 为 ok：%v", lines)
	}
	if code, ok := lines[0]["error_code"]; ok && code != "" {
		t.Errorf("成功的调用没有 error_code：%v", lines[0])
	}
}

func TestBilledButInvalidStructuredOutputIsMetered(t *testing.T) {
	h := newHarness(t)
	// 适配层：上游回了 200 并计费，但结构化输出不是合法 JSON；错误之外仍带回 Usage。
	h.fake.onGenerate("mid-a", func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		usage := &commonv1.Usage{InputTokens: 1000, OutputTokens: 500, CacheReadTokens: 200}
		return modelprovider.GenerateResult{Text: "我觉得答案是 42", Usage: usage}, providererr.New(providererr.Invalid, "bad_structured_output", "结构化输出不是合法的 JSON")
	})
	g := say("1+1?")
	g.OutputSchema = &commonv1.JsonPayload{Value: jsonValue(t, answerSchema)}
	_, err := h.generate(t, "t-billed", &mg.CallContext{TaskId: "task-7", NodeId: "node-3", Role: transform}, g)
	if code, pe := errorDetail(t, err); code != connect.CodeInvalidArgument || pe.GetCode() != "bad_structured_output" {
		t.Fatalf("错误照样返回：%v %s", code, pe.GetCode())
	}
	lines := h.meterLines(t)
	if len(lines) != 1 {
		t.Fatalf("已计费的失败调用要写一行计量：%s", h.meter.String())
	}
	line := lines[0]
	want := map[string]any{
		"tenant_id": "t-billed", "task_id": "task-7", "node_id": "node-3", "method": "generate", "status": "failed",
		"error_code": "bad_structured_output", "model_profile_id": "test/mid-a@1", "input_tokens": float64(1000), "cost_usd_micros": float64(435),
	}
	for key, value := range want {
		if line[key] != value {
			t.Errorf("计量字段 %s = %v, want %v", key, line[key], value)
		}
	}
	if strings.Contains(h.meter.String(), "42") {
		t.Error("计量记录不含模型的输出")
	}
}

func TestOutputsTheGatewayRejectsAreMetered(t *testing.T) {
	// 适配层成功返回，但网关自己的检查不过：结构化结果转不成 Value、Classify 给了标签外的值。
	h := newHarness(t)
	h.fake.onGenerate("mid-a", func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		return modelprovider.GenerateResult{Text: "{", Structured: json.RawMessage(`{`)}, nil
	})
	if _, err := h.generate(t, "t1", call(transform), say("hi")); err == nil {
		t.Fatal("结构化结果不是合法 JSON 应报错")
	}
	h.fake.onGenerate("small-a", func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		out := json.RawMessage(`{"results":[{"label":"weather","confidence":0.5}]}`)
		return modelprovider.GenerateResult{Text: string(out), Structured: out}, nil
	})
	_, err := h.client.Classify(context.Background(), connect.NewRequest(&mg.ClassifyRequest{
		Tenant: tenant("t1"), Call: call(commonv1.Role_ROLE_ROUTE), Texts: []string{"发票开错了"}, Labels: []string{"billing", "tech"},
	}))
	if code, _ := errorDetail(t, err); code != connect.CodeInvalidArgument {
		t.Fatalf("标签外的值：%v", code)
	}
	lines := h.meterLines(t)
	if len(lines) != 2 {
		t.Fatalf("两次已计费的失败调用各写一行：%s", h.meter.String())
	}
	if lines[0]["status"] != "failed" || lines[0]["error_code"] != "bad_structured_output" || lines[0]["cost_usd_micros"] != float64(435) {
		t.Errorf("结构化结果转换失败：%v", lines[0])
	}
	// (1000-200)*0.1 + 200*0.025 + 500*0.4 = 285。
	if lines[1]["status"] != "failed" || lines[1]["error_code"] != "bad_classification" || lines[1]["method"] != "classify" || lines[1]["cost_usd_micros"] != float64(285) {
		t.Errorf("Classify 标签外的值：%v", lines[1])
	}
}

func TestFailuresWithoutUsageAreNotMetered(t *testing.T) {
	h := newHarness(t)
	h.fake.failNext("mid-a", providererr.New(providererr.Denied, "invalid_api_key", "凭据无效"))
	if _, err := h.generate(t, "t1", call(transform), say("hi")); err == nil {
		t.Fatal("应报错")
	}
	if lines := h.meterLines(t); len(lines) != 0 {
		t.Errorf("没有 Usage 的失败（上游没计费）不写计量：%v", lines)
	}
}

// ---- 4. 每次尝试的超时落在调用方的 deadline 之内 ----

func TestEachAttemptTimesOutWithinTheCallerDeadline(t *testing.T) {
	h := newHarnessOpts(t, nil, func(o *gateway.Options) { o.AttemptTimeout = 300 * time.Millisecond })
	h.fake.hangNext("mid-a", 1) // 第一次尝试上游一直没有响应
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	started := time.Now()
	resp, err := h.client.Generate(ctx, connect.NewRequest(&mg.GenerateRequest{Tenant: tenant("t1"), Call: call(transform), Generation: say("hi")}))
	if err != nil {
		t.Fatalf("第一次尝试超时后应在 deadline 之内重试成功：%v", err)
	}
	if elapsed := time.Since(started); elapsed > 3*time.Second {
		t.Errorf("每次尝试的超时没生效：用了 %v", elapsed)
	}
	if resp.Msg.GetModel().GetModelProfileId() != "test/mid-a@1" || h.fake.callsTo("mid-a") != 2 {
		t.Errorf("在同一个 Profile 上重试：%v，mid-a %d 次", resp.Msg.GetModel(), h.fake.callsTo("mid-a"))
	}
}

func TestAttemptTimeoutNeverExceedsTheCallerDeadline(t *testing.T) {
	// 每次尝试的超时比调用方的 deadline 长：以调用方的为准，并且及时返回 transient。
	h := newHarnessOpts(t, nil, func(o *gateway.Options) { o.AttemptTimeout = time.Minute })
	h.fake.hangNext("mid-a", 5)
	h.fake.hangNext("mid-b", 5)
	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()
	started := time.Now()
	_, err := h.client.Generate(ctx, connect.NewRequest(&mg.GenerateRequest{Tenant: tenant("t1"), Call: call(transform), Generation: say("hi")}))
	if connect.CodeOf(err) != connect.CodeUnavailable && connect.CodeOf(err) != connect.CodeDeadlineExceeded {
		t.Fatalf("超过调用方的 deadline 应是 transient：%v", err)
	}
	if elapsed := time.Since(started); elapsed > 3*time.Second {
		t.Errorf("没有按调用方的 deadline 返回：用了 %v", elapsed)
	}
}

func TestNoWaitThatWouldOutliveTheCallerDeadline(t *testing.T) {
	h := newHarness(t)
	// 上游要求 3 秒后再试（MaxDelay 是 5 秒），调用方只剩不到 1 秒：不等，直接返回 transient，带上建议的等待时间。
	h.fake.failNext("mid-a", transient("rate_limit", 3*time.Second))
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	_, err := h.client.Generate(ctx, connect.NewRequest(&mg.GenerateRequest{Tenant: tenant("t1"), Call: call(transform), Generation: say("hi")}))
	code, pe := errorDetail(t, err)
	if code != connect.CodeUnavailable || pe.GetCategory() != providererr.Transient {
		t.Fatalf("剩下的时间不够再试一次：%v %v/%s", code, pe.GetCategory(), pe.GetCode())
	}
	if ms, ok := retryAfterMS(pe); !ok || ms < 3000 {
		t.Errorf("带回建议的等待时间：%v（%v）", ms, ok)
	}
	if s := h.sleeps.all(); len(s) != 0 {
		t.Errorf("不应等待到 deadline 之后：%v", s)
	}
	if n := h.fake.callsTo("mid-a"); n != 1 {
		t.Errorf("只调用一次：%d", n)
	}
}

func TestStreamThatNeverStartsIsRetriedWithinTheAttemptTimeout(t *testing.T) {
	h := newHarnessOpts(t, nil, func(o *gateway.Options) { o.AttemptTimeout = 300 * time.Millisecond })
	h.fake.hangNext("mid-a", 1) // 建流时上游一直没有响应
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	stream, err := h.client.StreamGenerate(ctx, connect.NewRequest(&mg.StreamGenerateRequest{Tenant: tenant("t1"), Call: call(transform), Generation: say("说点什么")}))
	if err != nil {
		t.Fatal(err)
	}
	defer stream.Close()
	var final *mg.GenerateResponse
	for stream.Receive() {
		if f := stream.Msg().GetFinal(); f != nil {
			final = proto.Clone(f).(*mg.GenerateResponse)
		}
	}
	if err := stream.Err(); err != nil || final == nil {
		t.Fatalf("第一个分片之前超时可以重试：%v，final %v", err, final)
	}
	if n := h.fake.callsTo("mid-a"); n != 2 {
		t.Errorf("建流重试一次：%d", n)
	}
}

// ---- 5. model_not_found 切换同档的下一个 Profile ----

func TestModelNotFoundSwitchesToTheNextProfile(t *testing.T) {
	h := newHarness(t)
	h.fake.failNext("mid-a", providererr.New(providererr.NotFound, "model_not_found", "模型供应商没有这个模型：mid-a"))
	resp := h.mustGenerate(t, "t1", call(transform), say("hi"))
	if resp.GetModel().GetModelProfileId() != "test/mid-b@1" {
		t.Fatalf("model_not_found 切换同档的下一个 Profile：%v", resp.GetModel())
	}
	if a, b := h.fake.callsTo("mid-a"), h.fake.callsTo("mid-b"); a != 1 || b != 1 {
		t.Errorf("不在下架的 Profile 上重试：mid-a %d 次、mid-b %d 次", a, b)
	}
	if s := h.sleeps.all(); len(s) != 0 {
		t.Errorf("切换不等待：%v", s)
	}
	if !strings.Contains(h.logs.String(), "test/mid-a@1") {
		t.Errorf("日志里写出下架的 Profile，便于修 Profile 文件：%s", h.logs.String())
	}
}

func TestModelNotFoundOnEveryProfileIsNotFound(t *testing.T) {
	h := newHarness(t)
	gone := providererr.New(providererr.NotFound, "model_not_found", "模型供应商没有这个模型")
	h.fake.failNext("mid-a", gone)
	h.fake.failNext("mid-b", gone)
	_, err := h.generate(t, "t1", call(transform), say("hi"))
	code, pe := errorDetail(t, err)
	if code != connect.CodeNotFound || pe.GetCode() != "model_not_found" {
		t.Errorf("同档全都下架：not_found，不是 transient：%v %s", code, pe.GetCode())
	}
}

func TestOtherNotFoundErrorsStillDoNotSwitch(t *testing.T) {
	h := newHarness(t)
	h.fake.failNext("mid-a", providererr.New(providererr.NotFound, "file_not_found", "引用的文件不存在"))
	if _, err := h.generate(t, "t1", call(transform), say("hi")); connect.CodeOf(err) != connect.CodeNotFound {
		t.Fatalf("其他 not_found 照旧直接返回：%v", err)
	}
	if n := h.fake.callsTo("mid-b"); n != 0 {
		t.Errorf("只有 model_not_found 才切换：mid-b 被调用 %d 次", n)
	}
}

func TestPinnedEmbeddingVersionDoesNotSwitchOnModelNotFound(t *testing.T) {
	// 指定了 embedding_version：向量不能混用，只有这一个 Profile，下架就是 not_found。
	h := newHarness(t)
	h.fake.failNext("embed-a", providererr.New(providererr.NotFound, "model_not_found", "模型供应商没有这个模型"))
	_, err := h.client.Embed(context.Background(), connect.NewRequest(&mg.EmbedRequest{
		Tenant: tenant("t1"), Call: call(commonv1.Role_ROLE_FETCH), Texts: []string{"a"}, EmbeddingVersion: "test/embed-a@1",
	}))
	if code, pe := errorDetail(t, err); code != connect.CodeNotFound || pe.GetCode() != "model_not_found" {
		t.Errorf("指定版本的嵌入模型下架：%v %s", code, pe.GetCode())
	}
}
