// Package modelprovider 定义 ModelProvider 接口（设计书 4.8）：生成（含流式与结构化输出）、打分、嵌入，并回传计量。
//
// 只在 Go 侧（Model Gateway 的供应商适配层）使用；Python 经 gRPC 调用 Model Gateway。适配层只做协议转换，
// 不放业务逻辑：选档、限流、切换供应商、按 Model Profile 计价都在 Model Gateway 里。
//
// 语义：
//   - 绑定租户的客户端按租户选择凭据（租户自带的 Key 或平台 Key）并按租户计量；调用方无法换成别的租户。
//   - 每个结果都带 Usage（token 数）；成本可以为 0，由 Model Gateway 按 Model Profile 计价。
//   - 上游已经回了结果并计费、但结果不合格时（结构化输出不是合法 JSON、工具参数不是 JSON、Score 的个数不对），返回错误的同时
//     在结果里带回 Usage，供 Model Gateway 计量；上游没有回结果的失败（HTTP 错误、超时、断开）结果里没有 Usage。
//   - OutputSchema 非空时，Structured 必须是符合该 schema 的 JSON。
//   - StreamGenerate：各分片的 TextDelta 依次拼接等于最后一个分片 Final 的 Text；Final 带 Usage；之后 Recv 返回 io.EOF。
//   - Score、Embed 的结果与输入一一对应、顺序相同；同一模型的向量维度一致。
//   - 错误分类：限流、超时、供应商暂时不可用 Transient（限流带 RetryAfter）；请求不合法（例如消息为空、上下文过长、
//     schema 不合法）Invalid；凭据无效 Denied；模型不存在 NotFound。内容被过滤不是错误，见 FinishReason。
package modelprovider

import (
	"context"
	"encoding/json"

	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Provider 是长期存在的工厂。
type Provider interface {
	Bind(tenant *commonv1.TenantContext) (Client, error)
	BindPlatform(purpose string) (Client, error)
	Close() error
}

// Role 是对话消息的角色。
type Role string

const (
	RoleSystem    Role = "system"
	RoleUser      Role = "user"
	RoleAssistant Role = "assistant"
	RoleTool      Role = "tool"
)

// Message 是一条对话消息。
type Message struct {
	Role Role
	Text string
	// Parts is mutually exclusive with Text. Image parts are user-role only.
	Parts     []ContentPart `json:",omitempty"`
	ToolCalls []ToolCall
	// ToolCallID 是 Role 为 tool 时对应的调用。
	ToolCallID string
	// CacheBreakpoint 在这条消息之后设提示缓存断点（附录 G.1）。
	CacheBreakpoint bool
}

// ContentPart carries text or a gateway-validated image, never both.
type ContentPart struct {
	Text  string
	Image *ImagePart
}

// ImagePart holds a tenant-bound reference until the gateway validates it.
// Data is deliberately excluded from JSON estimates, traces and stored state;
// only the provider's wire adapter may encode it for transmission.
type ImagePart struct {
	ObjectURI string
	MIMEType  string
	SHA256    string
	Detail    string
	Data      []byte `json:"-"`
	Width     int
	Height    int
}

// ToolSpec 是提供给模型的工具。
type ToolSpec struct {
	Name         string
	Description  string
	ParamsSchema json.RawMessage
}

// ToolCall 是模型发起的工具调用。
type ToolCall struct {
	ID        string
	Name      string
	Arguments json.RawMessage
}

// FinishReason 是生成结束的原因。
type FinishReason string

const (
	FinishStop          FinishReason = "stop"
	FinishMaxTokens     FinishReason = "max_tokens"
	FinishToolCalls     FinishReason = "tool_calls"
	FinishContentFilter FinishReason = "content_filter"
)

// GenerateRequest 是生成请求。Model 是供应商的模型名。
type GenerateRequest struct {
	Model                string
	Messages             []Message
	Tools                []ToolSpec
	OutputSchema         json.RawMessage
	MaxOutputTokens      int
	ThinkingBudgetTokens int
}

// GenerateResult 是生成结果。Thinking 是思考摘要，只写入 trace。
type GenerateResult struct {
	Text         string
	Structured   json.RawMessage
	ToolCalls    []ToolCall
	Thinking     string
	FinishReason FinishReason
	Usage        *commonv1.Usage
}

// Chunk 是流式生成的一个分片；最后一个分片 Final 非空。
type Chunk struct {
	TextDelta     string
	ThinkingDelta string
	ToolCall      *ToolCall
	Final         *GenerateResult
}

// Stream 依次返回分片；结束后 Recv 返回 io.EOF。
type Stream interface {
	Recv(ctx context.Context) (Chunk, error)
	Close() error
}

// ScoreRequest 是打分请求。
type ScoreRequest struct {
	MaxOutputTokens int
	Model           string
	Query           string
	Candidates      []string
	Rubric          string
}

// ScoreResult 与 Candidates 一一对应。
type ScoreResult struct {
	Scores []float64
	Usage  *commonv1.Usage
}

// EmbedRequest 是嵌入请求。
type EmbedRequest struct {
	Model string
	Texts []string
}

// EmbedResult 与 Texts 一一对应。
type EmbedResult struct {
	Vectors [][]float32
	Usage   *commonv1.Usage
}

// Client 是绑定了作用域的客户端。
type Client interface {
	Generate(ctx context.Context, req GenerateRequest) (GenerateResult, error)
	StreamGenerate(ctx context.Context, req GenerateRequest) (Stream, error)
	Score(ctx context.Context, req ScoreRequest) (ScoreResult, error)
	Embed(ctx context.Context, req EmbedRequest) (EmbedResult, error)
}
