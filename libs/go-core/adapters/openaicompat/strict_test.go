package openaicompat_test

// T-13 的真模型端到端发现（docs/plan/live-e2e.md）：Reviewer 避开 anthropic 之后落到 gpt-5.5，每次调用都是 bad_response。
// 原因有两层：
//   - json_schema 的 strict 模式要求每个 object 都写 additionalProperties: false、required 列出全部属性；Reviewer 的输出 schema
//     不满足，OpenAI 拒绝这个请求。只有满足这些要求的 schema 才发 strict: true，其余发 strict: false（照样按 schema 输出，结果由调用方校验）。
//   - 非流式请求的 Accept 带着 text/event-stream，聚合网关把拒绝包成 HTTP 200 的 SSE 流返回，适配层解析不了。非流式只接受 application/json。

import (
	"encoding/json"
	"testing"
)

var looseNested = json.RawMessage(`{"type":"object","required":["verdict","state_update"],"additionalProperties":false,` +
	`"properties":{"verdict":{"type":"string"},"state_update":{"type":"object","properties":{"facts":{"type":"array","items":{"type":"string"}}}}}}`)

var optionalProperty = json.RawMessage(`{"type":"object","required":["answer"],"additionalProperties":false,` +
	`"properties":{"answer":{"type":"string"},"confidence":{"type":"number"}}}`)

var strictNested = json.RawMessage(`{"type":"object","required":["verdict","items"],"additionalProperties":false,"properties":{` +
	`"verdict":{"type":"string","enum":["done","continue"]},` +
	`"items":{"type":"array","items":{"type":"object","required":["id"],"additionalProperties":false,"properties":{"id":{"type":"string"}}}}}}`)

func TestStrictModeOnlyForStrictCompatibleSchemas(t *testing.T) {
	for _, tc := range []struct {
		name   string
		schema json.RawMessage
		strict bool
	}{
		{"顶层与嵌套都写了 additionalProperties: false、required 列全", strictNested, true},
		{"简单的严格 schema", answerSchema, true},
		{"嵌套的 object 没写 additionalProperties: false", looseNested, false},
		{"required 没有列出全部属性", optionalProperty, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			e := newEnv(t, envConfig{})
			req := askFake("按 schema 回答")
			req.OutputSchema = tc.schema
			generate(t, e.tenantA(t), req)
			rf := e.lastBody(t, chatPath)["response_format"]
			if at(rf, "type") != "json_schema" || at(rf, "json_schema", "strict") != tc.strict {
				t.Fatalf("strict 应为 %v：%v", tc.strict, rf)
			}
			if !jsonEqual(t, at(rf, "json_schema", "schema"), tc.schema) {
				t.Fatalf("schema 应原样传入：%v", at(rf, "json_schema", "schema"))
			}
		})
	}
}

func TestNonStreamingRequestsAcceptOnlyJSON(t *testing.T) {
	e := newEnv(t, envConfig{})
	generate(t, e.tenantA(t), askFake("x"))
	reqs := e.fake.RequestsTo(chatPath)
	if got := reqs[len(reqs)-1].Header.Get("Accept"); got != "application/json" {
		t.Fatalf("非流式请求的 Accept 应只有 application/json，got %q", got)
	}
	d := stream(t, e.tenantA(t), askFake("y"))
	if d.err != nil {
		t.Fatalf("Recv: %v", d.err)
	}
	reqs = e.fake.RequestsTo(chatPath)
	if got := reqs[len(reqs)-1].Header.Get("Accept"); got != "text/event-stream" {
		t.Fatalf("流式请求的 Accept 应是 text/event-stream，got %q", got)
	}
}
