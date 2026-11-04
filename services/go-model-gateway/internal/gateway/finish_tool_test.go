package gateway_test

// TOOLS-KIMI（docs/plan/all-plans-dag-2026-11-03.md）：2026-11-04 起经中转站的 kimi-k3.1-pro 在请求同时带工具与 json_schema 的
// response_format 时不再调工具，直接按 schema 编一个结果（线上的 tool_loop 一个命令都没执行就报告“做完了”）。
// 网关的做法：工具与输出 schema 同时给时，不把 schema 交给上游的 response_format，改为多给一个收尾工具 submit_result（参数就是
// 那份 schema）。调用方看到的与原来一样：结果在 result 里，真工具的调用在 tool_calls 里。
//   - 模型调了收尾工具：参数还原成 result，不出现在 tool_calls 里；同时还调了真工具时只交真工具（活还没干完）。
//   - 模型只回了文字：是 JSON 对象（可以包在 ``` 里）就当结果，否则 bad_structured_output（Worker 的 tool_loop 会带着改正说明重问）。
//   - 只有 schema 没有工具、schema 不是 object、已经有同名工具时照旧走 response_format。

import (
	"encoding/json"
	"testing"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/encoding/protojson"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	mg "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1"
)

const answerSchemaJSON = `{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"],"additionalProperties":false}`

func withToolsAndSchema(t *testing.T, schema string, tools ...string) *mg.GenerateParams {
	t.Helper()
	g := say("做完再交结果")
	for _, name := range tools {
		g.Tools = append(g.Tools, &mg.ToolSpec{Name: name, ParamsSchema: &commonv1.JsonPayload{Value: jsonValue(t, `{"type":"object"}`)}})
	}
	g.OutputSchema = &commonv1.JsonPayload{Value: jsonValue(t, schema)}
	return g
}

// replies 让 mid-a 回 r，并记下它收到的请求。
func replies(h *harness, r modelprovider.GenerateResult) *modelprovider.GenerateRequest {
	seen := &modelprovider.GenerateRequest{}
	h.fake.onGenerate("mid-a", func(req modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		*seen = req
		return r, nil
	})
	return seen
}

func finishCall(args string) modelprovider.ToolCall {
	return modelprovider.ToolCall{ID: "call-f", Name: "submit_result", Arguments: json.RawMessage(args)}
}

func resultJSON(t *testing.T, resp *mg.GenerateResponse) string {
	t.Helper()
	if resp.GetResult() == nil {
		t.Fatalf("没有 result：%v", resp)
	}
	raw, err := protojson.Marshal(resp.GetResult())
	if err != nil {
		t.Fatal(err)
	}
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	out, _ := json.Marshal(v)
	return string(out)
}

func toolNames(req *modelprovider.GenerateRequest) []string {
	var out []string
	for _, tool := range req.Tools {
		out = append(out, tool.Name)
	}
	return out
}

func TestToolsWithASchemaHandTheSchemaToAFinishTool(t *testing.T) {
	h := newHarness(t)
	seen := replies(h, modelprovider.GenerateResult{ToolCalls: []modelprovider.ToolCall{finishCall(`{"answer":"42"}`)}, FinishReason: modelprovider.FinishToolCalls})
	resp := h.mustGenerate(t, "t1", call(commonv1.Role_ROLE_ACT), withToolsAndSchema(t, answerSchemaJSON, "shell_run"))

	if len(seen.OutputSchema) != 0 {
		t.Errorf("有工具时 schema 不交给上游的 response_format：%s", seen.OutputSchema)
	}
	if names := toolNames(seen); len(names) != 2 || names[0] != "shell_run" || names[1] != "submit_result" {
		t.Fatalf("真工具在前、收尾工具在后：%v", names)
	}
	if p := mustCompact(t, string(seen.Tools[1].ParamsSchema)); p != mustCompact(t, answerSchemaJSON) {
		t.Errorf("收尾工具的参数就是那份 schema：%s", p)
	}
	if seen.Tools[1].Description == "" {
		t.Error("收尾工具要说明什么时候调")
	}

	if got := resultJSON(t, resp); got != `{"answer":"42"}` {
		t.Errorf("收尾工具的参数还原成 result：%s", got)
	}
	if len(resp.GetToolCalls()) != 0 {
		t.Errorf("收尾工具不出现在 tool_calls 里：%v", resp.GetToolCalls())
	}
	if resp.GetFinishReason() != mg.FinishReason_FINISH_REASON_STOP {
		t.Errorf("交了结果就是 stop：%v", resp.GetFinishReason())
	}
}

func mustCompact(t *testing.T, s string) string {
	t.Helper()
	var v any
	if err := json.Unmarshal([]byte(s), &v); err != nil {
		t.Fatal(err)
	}
	out, _ := json.Marshal(v)
	return string(out)
}

func TestARealToolCallWinsOverTheFinishTool(t *testing.T) {
	h := newHarness(t)
	replies(h, modelprovider.GenerateResult{
		ToolCalls:    []modelprovider.ToolCall{{ID: "call-1", Name: "shell_run", Arguments: json.RawMessage(`{"command":"ls"}`)}, finishCall(`{"answer":"made up"}`)},
		FinishReason: modelprovider.FinishToolCalls,
	})
	resp := h.mustGenerate(t, "t1", call(commonv1.Role_ROLE_ACT), withToolsAndSchema(t, answerSchemaJSON, "shell_run"))
	if len(resp.GetToolCalls()) != 1 || resp.GetToolCalls()[0].GetName() != "shell_run" {
		t.Fatalf("同时调了真工具：只交真工具，活还没干完：%v", resp.GetToolCalls())
	}
	if resp.GetResult() != nil {
		t.Errorf("这时还没有结果：%v", resp.GetResult())
	}
}

func TestAJSONAnswerInTextIsAcceptedAsTheResult(t *testing.T) {
	for name, text := range map[string]string{
		"plain":  `{"answer":"7"}`,
		"fenced": "```json\n{\"answer\":\"7\"}\n```",
	} {
		t.Run(name, func(t *testing.T) {
			h := newHarness(t)
			replies(h, modelprovider.GenerateResult{Text: text})
			resp := h.mustGenerate(t, "t1", call(commonv1.Role_ROLE_ACT), withToolsAndSchema(t, answerSchemaJSON, "shell_run"))
			if got := resultJSON(t, resp); got != `{"answer":"7"}` {
				t.Errorf("文字里的 JSON 对象当结果：%s", got)
			}
		})
	}
}

func TestAPlainTextAnswerIsABadStructuredOutput(t *testing.T) {
	h := newHarness(t)
	replies(h, modelprovider.GenerateResult{Text: "都做完了"})
	_, err := h.generate(t, "t1", call(commonv1.Role_ROLE_ACT), withToolsAndSchema(t, answerSchemaJSON, "shell_run"))
	code, detail := errorDetail(t, err)
	if code != connect.CodeInvalidArgument || detail.GetCode() != "bad_structured_output" {
		t.Fatalf("要结构化结果却只回了文字：%v %v", code, detail)
	}
	if billed, ok := billedMicros(detail); !ok || billed != midACost {
		t.Errorf("已计费的花费回给调用方：%v %v", billed, ok)
	}
	if n := h.fake.callsTo("mid-a"); n != 1 {
		t.Errorf("交给调用方改正，网关不重试：mid-a 调了 %d 次", n)
	}
}

func TestTheResponseFormatStaysWhenTheFinishToolDoesNotApply(t *testing.T) {
	cases := map[string]*mg.GenerateParams{}
	schemaOnly := say("只要结构化结果")
	schemaOnly.OutputSchema = &commonv1.JsonPayload{Value: jsonValue(t, answerSchemaJSON)}
	cases["没有工具"] = schemaOnly
	cases["schema 不是 object"] = withToolsAndSchema(t, `{"type":"array","items":{"type":"string"}}`, "shell_run")
	cases["已经有同名工具"] = withToolsAndSchema(t, answerSchemaJSON, "shell_run", "submit_result")
	for name, g := range cases {
		t.Run(name, func(t *testing.T) {
			h := newHarness(t)
			seen := replies(h, modelprovider.GenerateResult{Structured: json.RawMessage(`{"answer":"x"}`)})
			if g.GetOutputSchema().GetValue().GetStructValue() == nil {
				seen = replies(h, modelprovider.GenerateResult{Structured: json.RawMessage(`["x"]`)})
			}
			h.mustGenerate(t, "t1", call(commonv1.Role_ROLE_ACT), g)
			if len(seen.OutputSchema) == 0 {
				t.Error("照旧把 schema 交给 response_format")
			}
			for _, tool := range seen.Tools {
				if tool.Name == "submit_result" && len(g.GetTools()) < 2 {
					t.Errorf("不加收尾工具：%v", toolNames(seen))
				}
			}
			if len(seen.Tools) != len(g.GetTools()) {
				t.Errorf("工具原样：%v", toolNames(seen))
			}
		})
	}
}
