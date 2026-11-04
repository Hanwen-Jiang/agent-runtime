package gateway

// 工具与输出 schema 同时给时的收尾工具（TOOLS-KIMI，docs/plan/all-plans-dag-2026-11-03.md）。2026-11-04 起经中转站的
// kimi-k3.1-pro 在 response_format 是 json_schema 时不再调工具，直接按 schema 编一个结果，tool_loop 一个命令都没执行就交了
// “做完了”。这时不把 schema 交给上游的 response_format，改为多给模型一个收尾工具，参数就是那份 schema：模型干完活调它交结果，
// 网关把参数还原成结构化结果，调用方看到的与原来一样。所有供应商都这样做，不按模型区分。

import (
	"encoding/json"
	"slices"
	"strings"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
)

const (
	finishToolName        = "submit_result"
	finishToolDescription = "Call this exactly once, after you have finished the work with the other tools, to hand in your final answer. " +
		"Its arguments are the final answer and must follow its schema. Do not call it to report work you have not done."
)

// withFinishTool 在请求同时带工具与 object 型的输出 schema、且没有同名工具时，去掉 OutputSchema，把它作为收尾工具的参数加在
// 工具的最后；第二个返回值说明改没改。
func withFinishTool(gen modelprovider.GenerateRequest) (modelprovider.GenerateRequest, bool) {
	if len(gen.Tools) == 0 || len(gen.OutputSchema) == 0 {
		return gen, false
	}
	var schema struct {
		Type any `json:"type"`
	}
	if json.Unmarshal(gen.OutputSchema, &schema) != nil || schema.Type != "object" {
		return gen, false
	}
	if slices.ContainsFunc(gen.Tools, func(t modelprovider.ToolSpec) bool { return t.Name == finishToolName }) {
		return gen, false
	}
	out := gen
	out.Tools = append(slices.Clone(gen.Tools), modelprovider.ToolSpec{Name: finishToolName, Description: finishToolDescription, ParamsSchema: gen.OutputSchema})
	out.OutputSchema = nil
	return out, true
}

// fromFinishTool 把收尾工具的调用还原成结构化结果。同时还调了真工具时只交真工具（活还没干完）；没调任何工具时，文字是 JSON
// 对象（可以包在 ``` 里）就当结果，否则 bad_structured_output，由调用方带着改正说明重问。
func fromFinishTool(r modelprovider.GenerateResult) (modelprovider.GenerateResult, error) {
	var real []modelprovider.ToolCall
	var finish *modelprovider.ToolCall
	for i := range r.ToolCalls {
		if r.ToolCalls[i].Name != finishToolName {
			real = append(real, r.ToolCalls[i])
		} else if finish == nil {
			finish = &r.ToolCalls[i]
		}
	}
	switch {
	case len(real) > 0:
		r.ToolCalls = real
	case finish != nil:
		args := finish.Arguments
		if len(args) == 0 {
			args = json.RawMessage("{}")
		}
		r.Structured, r.ToolCalls, r.FinishReason = args, nil, modelprovider.FinishStop
	case len(r.Structured) > 0:
	default:
		text, ok := jsonObjectText(r.Text)
		if !ok {
			return r, invalid("bad_structured_output", "要结构化结果：干完活调 "+finishToolName+" 交结果，模型既没调它也没给出 JSON 对象")
		}
		r.Structured = json.RawMessage(text)
	}
	return r, nil
}

func jsonObjectText(text string) (string, bool) {
	s := strings.TrimSpace(text)
	if rest, ok := strings.CutPrefix(s, "```"); ok {
		rest = strings.TrimPrefix(strings.TrimPrefix(rest, "json"), "JSON")
		if i := strings.LastIndex(rest, "```"); i >= 0 {
			rest = rest[:i]
		}
		s = strings.TrimSpace(rest)
	}
	var v map[string]any
	if json.Unmarshal([]byte(s), &v) != nil {
		return "", false
	}
	return s, true
}
