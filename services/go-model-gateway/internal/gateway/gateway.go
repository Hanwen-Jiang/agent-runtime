// Package gateway 实现 ModelGatewayService（设计书 3.3.1 核心逻辑 3、4；附录 H）：所有模型调用的唯一出口。
//
// 每次调用的流程：
//  1. 校验租户：TenantContext 为空或 tenant_id 为空 → Invalid（code missing_tenant），不选档、不调用供应商。
//  2. 选档：交给 router（role 默认 Tier、显式 Tier、pinned、avoid_vendor、租户白名单、能力）。
//  3. 按请求的租户 Bind 供应商客户端（每次 RPC 绑定一次，不用平台作用域）。
//  4. 调用：每次尝试有自己的超时（AttemptTimeout，受调用方剩余的 deadline 限制；流式只限到第一个分片）。
//     transient 失败按 RetryPolicy 带抖动重试，等待时间不短于上游给的 RetryAfter，单次等待不超过 MaxDelay；调用方剩下的时间
//     不够等这一次时不再等待，返回 Transient（code deadline，RetryAfter 为该等的时间）。同一个 Profile 尝试 MaxAttempts 次
//     仍失败，切换同档的下一个 Profile（不再等待）；全部失败返回 Transient（code models_exhausted，RetryAfter 取最后一次失败的）。
//     not_found 且 code 为 model_not_found（模型在上游不存在）只说明这一个 Profile 坏了：不重试，直接切换下一个；同档都是这样时
//     返回这个 not_found。其余非 transient 失败立即返回，不重试、不切换。
//     StreamGenerate 只在发出第一个分片之前重试与切换；之后出错直接结束流。
//  5. 计价：响应的 Usage 原样带回上游的 token 数，cost_usd_micros 按实际服务的 Profile 单价计算（profiles.Cost.USDMicros）。
//  6. 计量：向 Meter 写 JSON 行（MeterRecord），不含消息内容、输出内容与密钥。每次成功的 RPC 一行（status ok）；上游已经计费、
//     但网关判为失败的尝试也各写一行（status failed，带 error_code），例如结构化输出不是合法 JSON、Classify 给了标签外的值。
//     请求的 pinned_model_profile 没用上、avoid_vendor 没能避开（按实际服务的 Profile 判断，含切换之后）时，计量记录里标明，
//     并记一条日志。
//  7. trace（附录 J.1；T-15）：配了 Options.Telemetry 时每次 RPC 一个 span，按请求的租户绑定。名字按 GenAI 语义约定是
//     `{chat|embeddings} {请求的模型}`，属性有请求与实际服务的模型、供应商、token、成本、Tier、尝试次数、task_id、node_id；
//     失败时 span 为 error、error.type 是错误的 code。不记消息内容、输出内容与密钥。
//
// 供应商返回的错误不是 *providererr.Error 时（违反 ModelProvider 约定）：不重试、不切换，按 ConnectError 的“其他错误”返回。
//
// 请求与结果的转换：
//   - 消息角色、工具、工具调用、cache_breakpoint 一一转换；消息角色未指定 → Invalid，不调用供应商。
//   - 供应商的模型名取 Profile.model；思考预算取请求的 thinking_budget_tokens，为 0 时取 Tier 的 thinking_budget；
//     max_output_tokens 原样传。
//   - 结构化输出放在 GenerateResponse.result（google.protobuf.Value）；思考内容只放在 rationale，不混进 text 或 result。
//   - Classify 转成一次结构化输出的生成：output_schema 为
//     {"type":"object","properties":{"results":{"type":"array","items":{"type":"object",
//     "properties":{"label":{"type":"string","enum":[<labels>]},"confidence":{"type":"number"}},
//     "required":["label","confidence"],"additionalProperties":false}}},"required":["results"],"additionalProperties":false}，
//     results 与 texts 一一对应。上游给出标签外的值、条数不对或不是合法 JSON → Invalid（不重试）；confidence 截到 [0, 1]。
//     labels 或 texts 为空 → Invalid，不调用供应商。
//   - Embed 用 embed 档（默认 embed@default）；响应的 embedding_version 等于实际 Profile 的 id。
//     请求指定了 embedding_version 时只用这一个 Profile（向量不能混用，不切换）；它不是可选的嵌入 Profile → Invalid
//     （code embedding_version_mismatch），不调用供应商。
//
// 错误：所有返回给调用方的错误都是 *connect.Error（见 ConnectError）。
package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"slices"
	"strings"
	"sync"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"

	"github.com/agentruntime/agent-runtime/libs/go-core/connecterr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/telemetry"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	modelgatewayv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1/modelgatewayv1connect"

	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/router"
)

// Sleeper 等待 d；ctx 结束时提前返回 ctx.Err()。测试注入记录等待时长的实现，不真睡。
type Sleeper func(ctx context.Context, d time.Duration) error

// RetryPolicy 是 transient 失败的重试策略。
type RetryPolicy struct {
	// MaxAttempts 是每个 Model Profile 的最多尝试次数（含第一次）。
	MaxAttempts int
	// BaseDelay 是第一次重试前的退避；之后每次翻倍，再经 Jitter 抖动。
	BaseDelay time.Duration
	// MaxDelay 是单次等待的上限，RetryAfter 也受它限制：等待 = min(max(抖动后的退避, RetryAfter), MaxDelay)。
	MaxDelay time.Duration
}

// DefaultRetry 是 Options.Retry 为零值时用的策略。
var DefaultRetry = RetryPolicy{MaxAttempts: 3, BaseDelay: 200 * time.Millisecond, MaxDelay: 10 * time.Second}

// DefaultAttemptTimeout 是 Options.AttemptTimeout 为 0 时每次尝试的超时。要比调用方的 deadline 短得多，重试与切换才来得及
// （Worker 的 GrpcModelGateway 默认 deadline 300 秒，并且不超过 activity 剩下的时间）。
const DefaultAttemptTimeout = 90 * time.Second

// Options 是构造参数。
type Options struct {
	// Provider 是供应商适配层（openaicompat 或 mockmodel），必填。
	Provider modelprovider.Provider
	// Router 是选档器，必填。
	Router *router.Router
	// Meter 接收计量记录，每次成功的 RPC 一行 JSON；nil 表示丢弃。并发写入时按行串行。
	Meter io.Writer
	// Logger 记录失败的尝试（只记类别、code、Profile id，不记消息与密钥）；nil 表示丢弃。
	Logger *slog.Logger
	// Retry 为零值时用 DefaultRetry。
	Retry RetryPolicy
	// AttemptTimeout 是每次尝试的超时，受调用方剩余的 deadline 限制；流式调用只限到第一个分片。为 0 时用 DefaultAttemptTimeout。
	AttemptTimeout time.Duration
	// Sleep 为 nil 时真实等待。
	Sleep Sleeper
	// Jitter 把退避抖动到 [d/2, d]；nil 时随机，测试注入恒等函数。
	Jitter func(d time.Duration) time.Duration
	// Telemetry 记每次模型调用的 span（附录 J.1；T-15），按请求的租户绑定；nil 表示不记。网关不在每次调用后 flush，
	// 由 Telemetry 实现按批导出（OpenTelemetry 实现在后台导出，关闭时导出剩下的）。
	Telemetry telemetry.Provider
}

// MeterRecord 是一条计量记录（每次成功的 RPC 一行 JSON）。不含消息内容与密钥。
type MeterRecord struct {
	TenantID string `json:"tenant_id"`
	TaskID   string `json:"task_id"`
	NodeID   string `json:"node_id"`
	// Role 是附录 A 的 role 名（plan、judge 等），未指定为空串。
	Role string `json:"role"`
	// Method 是 RPC 名：generate、stream_generate、score、embed、classify。
	Method          string `json:"method"`
	ModelProfileID  string `json:"model_profile_id"`
	Tier            string `json:"tier"`
	Vendor          string `json:"vendor"`
	InputTokens     int64  `json:"input_tokens"`
	OutputTokens    int64  `json:"output_tokens"`
	CacheReadTokens int64  `json:"cache_read_tokens"`
	CostUSDMicros   int64  `json:"cost_usd_micros"`
	// Status 是 ok（RPC 成功）或 failed（上游已经计费、网关判为失败的尝试）。
	Status string `json:"status"`
	// ErrorCode 是失败时的 code（status 为 failed 时才有）。
	ErrorCode string `json:"error_code,omitempty"`
	// PinnedModelProfile 是请求的 pinned_model_profile；PinIgnored 表示请求了却没用上（实际服务的不是它）。
	PinnedModelProfile string `json:"pinned_model_profile,omitempty"`
	PinIgnored         bool   `json:"pin_ignored"`
	// AvoidVendor 是请求的 avoid_vendor；AvoidVendorUnmet 表示实际服务的模型正是这一家。
	AvoidVendor      string `json:"avoid_vendor,omitempty"`
	AvoidVendorUnmet bool   `json:"avoid_vendor_unmet"`
}

// modelNotFound 是适配层“模型在上游不存在”的 code：只说明这一个 Profile 坏了，切换同档的下一个。
const modelNotFound = "model_not_found"

// Server 实现 ModelGatewayServiceHandler。
type Server struct {
	opts    Options
	meterMu sync.Mutex
}

var _ modelgatewayv1connect.ModelGatewayServiceHandler = (*Server)(nil)

// New 校验参数并构造 Server。Provider 或 Router 为空时报错。
func New(opts Options) (*Server, error) {
	if opts.Provider == nil {
		return nil, errors.New("gateway：缺少 Provider")
	}
	if opts.Router == nil {
		return nil, errors.New("gateway：缺少 Router")
	}
	if opts.Retry == (RetryPolicy{}) {
		opts.Retry = DefaultRetry
	}
	opts.Retry.MaxAttempts = max(opts.Retry.MaxAttempts, 1)
	if opts.AttemptTimeout <= 0 {
		opts.AttemptTimeout = DefaultAttemptTimeout
	}
	if opts.Sleep == nil {
		opts.Sleep = sleep
	}
	if opts.Jitter == nil {
		opts.Jitter = func(d time.Duration) time.Duration {
			if d <= 1 {
				return d
			}
			return d/2 + rand.N(d/2+1)
		}
	}
	if opts.Logger == nil {
		opts.Logger = slog.New(slog.DiscardHandler)
	}
	if opts.Meter == nil {
		opts.Meter = io.Discard
	}
	return &Server{opts: opts}, nil
}

func sleep(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// ---- 公共流程 ----

// call 是一次 RPC 的公共部分：租户、选档结果与绑定的客户端。
type call struct {
	tenant *commonv1.TenantContext
	ctx    *modelgatewayv1.CallContext
	method string
	choice router.Choice
	client modelprovider.Client
	// attempts 是这次 RPC 一共尝试了几次（含重试与切换），记进 span。
	attempts int
}

func invalid(code, message string) error {
	return providererr.New(providererr.Invalid, code, message)
}

// checkTenant：缺租户 Invalid（missing_tenant）；格式不对按 tenancy 的规则（Invalid）。
func checkTenant(t *commonv1.TenantContext) error {
	if t == nil || t.GetTenantId() == "" {
		return invalid("missing_tenant", "缺少租户上下文")
	}
	_, err := tenancy.TenantScope(t)
	return err
}

// prepare 校验租户、选档并绑定客户端。
func (s *Server) prepare(ctx context.Context, tenant *commonv1.TenantContext, cc *modelgatewayv1.CallContext, method string, req router.Request) (*call, error) {
	req.TenantID, req.Role, req.Tier = tenant.GetTenantId(), cc.GetRole(), cc.GetTier()
	req.PinnedProfile, req.AvoidVendor = cc.GetPinnedModelProfile(), cc.GetAvoidVendor()
	choice, err := s.opts.Router.Select(ctx, req)
	if err != nil {
		return nil, err
	}
	client, err := s.opts.Provider.Bind(tenant)
	if err != nil {
		return nil, err
	}
	return &call{tenant: tenant, ctx: cc, method: method, choice: choice, client: client}, nil
}

// attempt 在一个候选上做一次调用；失败时若上游已经计费（适配层带回了 Usage），一并返回 Usage 供计量。
type attempt func(ctx context.Context, cand router.Candidate) (*commonv1.Usage, error)

// run 按重试与切换规则依次尝试候选，返回成功的候选（失败时返回最后尝试的候选）。bounded 为 true 时每次尝试套上
// AttemptTimeout；流式调用传 false，自己只把超时套在第一个分片之前。
func (s *Server) run(ctx context.Context, c *call, candidates []router.Candidate, bounded bool, do attempt) (router.Candidate, error) {
	var last, missing *providererr.Error
	var tried router.Candidate
	for _, cand := range candidates {
		tried = cand
	attempts:
		for n := 1; n <= s.opts.Retry.MaxAttempts; n++ {
			actx, cancel := ctx, context.CancelFunc(func() {})
			if bounded {
				actx, cancel = context.WithTimeout(ctx, s.opts.AttemptTimeout)
			}
			c.attempts++
			usage, err := do(actx, cand)
			cancel()
			if err == nil {
				return cand, nil
			}
			var pe *providererr.Error
			if !errors.As(err, &pe) {
				s.opts.Logger.Warn("模型调用失败", "profile", cand.Profile.ID, "category", "internal", "attempt", n)
				return cand, err
			}
			if usage != nil {
				s.meter(c, cand, priced(cand, usage), pe.Code)
			}
			s.opts.Logger.Warn("模型调用失败", "profile", cand.Profile.ID, "category", pe.Category.String(), "code", pe.Code, "attempt", n)
			if pe.Category == providererr.NotFound && pe.Code == modelNotFound {
				missing = pe
				break attempts // 这个 Profile 的模型在上游不存在：换同档的下一个，不重试、不等待
			}
			if !pe.Retryable() {
				return cand, pe
			}
			last = pe
			if n == s.opts.Retry.MaxAttempts {
				break // 换同档的下一个 Profile，不等待
			}
			wait := s.wait(n, pe.RetryAfter)
			if deadline, ok := ctx.Deadline(); ok && time.Until(deadline) <= wait {
				e := providererr.New(providererr.Transient, "deadline", "调用方剩余的时间不够再试一次")
				e.RetryAfter = wait
				return cand, e
			}
			if err := s.opts.Sleep(ctx, wait); err != nil {
				return cand, err
			}
		}
	}
	if last == nil && missing != nil {
		return tried, missing
	}
	e := providererr.New(providererr.Transient, "models_exhausted", "同档的模型都暂时不可用："+c.choice.Tier)
	if last != nil {
		e.RetryAfter = last.RetryAfter
	}
	return tried, e
}

// wait 是第 n 次失败后的等待：min(max(抖动后的退避, RetryAfter), MaxDelay)。
func (s *Server) wait(n int, retryAfter time.Duration) time.Duration {
	backoff := s.opts.Jitter(s.opts.Retry.BaseDelay << (n - 1))
	return min(max(backoff, retryAfter), s.opts.Retry.MaxDelay)
}

// priced 复制 Usage 并按 Profile 单价填上成本。
func priced(cand router.Candidate, u *commonv1.Usage) *commonv1.Usage {
	out := &commonv1.Usage{}
	if u != nil {
		out = proto.Clone(u).(*commonv1.Usage)
	}
	out.CostUsdMicros = cand.Profile.Cost.USDMicros(out)
	return out
}

func (c *call) choiceOf(cand router.Candidate) *modelgatewayv1.ModelChoice {
	return &modelgatewayv1.ModelChoice{ModelProfileId: cand.Profile.ID, Tier: c.choice.Tier, Vendor: cand.Profile.Vendor}
}

func roleName(r commonv1.Role) string {
	if r == commonv1.Role_ROLE_UNSPECIFIED {
		return ""
	}
	return strings.ToLower(strings.TrimPrefix(r.String(), "ROLE_"))
}

// served 在调用成功后检查请求的偏好有没有满足（按实际服务的 Profile），没满足的记一条日志；再写一行 status ok 的计量。
func (s *Server) served(c *call, cand router.Candidate, u *commonv1.Usage) {
	if pinned := c.ctx.GetPinnedModelProfile(); pinned != "" && pinned != cand.Profile.ID {
		s.opts.Logger.Warn("请求固定的模型没有用上", "pinned_model_profile", pinned, "profile", cand.Profile.ID, "tier", c.choice.Tier, "task_id", c.ctx.GetTaskId())
	}
	if avoid := c.ctx.GetAvoidVendor(); avoid != "" && avoid == cand.Profile.Vendor {
		s.opts.Logger.Warn("avoid_vendor 没能避开：同档只剩这一家", "avoid_vendor", avoid, "profile", cand.Profile.ID, "tier", c.choice.Tier, "task_id", c.ctx.GetTaskId())
	}
	s.meter(c, cand, u, "")
}

// meter 写一条计量记录；errCode 为空表示成功（status ok），否则是已计费的失败（status failed）。并发时按行串行。
func (s *Server) meter(c *call, cand router.Candidate, u *commonv1.Usage, errCode string) {
	status := "ok"
	if errCode != "" {
		status = "failed"
	}
	pinned, avoid := c.ctx.GetPinnedModelProfile(), c.ctx.GetAvoidVendor()
	line, err := json.Marshal(MeterRecord{
		TenantID: c.tenant.GetTenantId(), TaskID: c.ctx.GetTaskId(), NodeID: c.ctx.GetNodeId(), Role: roleName(c.ctx.GetRole()),
		Method: c.method, ModelProfileID: cand.Profile.ID, Tier: c.choice.Tier, Vendor: cand.Profile.Vendor,
		InputTokens: u.GetInputTokens(), OutputTokens: u.GetOutputTokens(), CacheReadTokens: u.GetCacheReadTokens(), CostUSDMicros: u.GetCostUsdMicros(),
		Status: status, ErrorCode: errCode,
		PinnedModelProfile: pinned, PinIgnored: pinned != "" && pinned != cand.Profile.ID,
		AvoidVendor: avoid, AvoidVendorUnmet: avoid != "" && avoid == cand.Profile.Vendor,
	})
	if err != nil {
		return
	}
	s.meterMu.Lock()
	defer s.meterMu.Unlock()
	_, _ = s.opts.Meter.Write(append(line, '\n'))
}

// ---- span（附录 J.1；T-15）----

// traced 是一次 RPC 的模型调用 span；没配 Telemetry 或绑定失败时 span 为 nil，方法都不做事。
type traced struct {
	span telemetry.Span
}

// startSpan 在选档之后开始 span：名字按 GenAI 语义约定是 `{operation} {请求的模型}`（选档的第一个候选），不含消息内容。
func (s *Server) startSpan(ctx context.Context, c *call, operation string, stream bool) *traced {
	if s.opts.Telemetry == nil {
		return &traced{}
	}
	tel, err := s.opts.Telemetry.Bind(c.tenant)
	if err != nil {
		return &traced{}
	}
	model := ""
	if len(c.choice.Candidates) > 0 {
		model = c.choice.Candidates[0].Profile.Model
	}
	attrs := []telemetry.Attribute{
		{Key: "gen_ai.operation.name", Value: operation},
		{Key: "gen_ai.request.model", Value: model},
		{Key: "tier", Value: c.choice.Tier},
		{Key: "task_id", Value: c.ctx.GetTaskId()},
		{Key: "node_id", Value: c.ctx.GetNodeId()},
		{Key: "role", Value: roleName(c.ctx.GetRole())},
		{Key: "runtime.method", Value: c.method},
	}
	if stream {
		attrs = append(attrs, telemetry.Attribute{Key: "runtime.stream", Value: true})
	}
	_, span, err := tel.StartSpan(ctx, strings.TrimSpace(operation+" "+model), attrs...)
	if err != nil {
		return &traced{}
	}
	return &traced{span: span}
}

// end 记下实际服务的模型、token、成本与尝试次数并结束 span；err 不为空时 span 记为 error，error.type 是错误的 code。
func (t *traced) end(c *call, cand router.Candidate, u *commonv1.Usage, err error) {
	if t.span == nil {
		return
	}
	attrs := []telemetry.Attribute{{Key: "runtime.attempts", Value: int64(c.attempts)}}
	if cand.Profile.ID != "" {
		attrs = append(attrs,
			telemetry.Attribute{Key: "gen_ai.response.model", Value: cand.Profile.Model},
			telemetry.Attribute{Key: "gen_ai.provider.name", Value: cand.Profile.Vendor},
			telemetry.Attribute{Key: "model_profile_id", Value: cand.Profile.ID},
		)
	}
	if u != nil {
		attrs = append(attrs,
			telemetry.Attribute{Key: "gen_ai.usage.input_tokens", Value: u.GetInputTokens()},
			telemetry.Attribute{Key: "gen_ai.usage.output_tokens", Value: u.GetOutputTokens()},
			telemetry.Attribute{Key: "runtime.cost_usd", Value: float64(u.GetCostUsdMicros()) / 1e6},
		)
	}
	if err != nil {
		t.span.RecordError(err)
		code := "internal"
		var pe *providererr.Error
		if errors.As(err, &pe) {
			code = pe.Code
		}
		attrs = append(attrs, telemetry.Attribute{Key: "error.type", Value: code})
	}
	t.span.SetAttributes(attrs...)
	t.span.End()
}

// billedFailure 记下一次已计费、但网关判为失败的调用（usage 为空时上游没计费，不记），原样返回 err。
func (s *Server) billedFailure(c *call, cand router.Candidate, usage *commonv1.Usage, err error) error {
	if usage != nil {
		code := "internal"
		var pe *providererr.Error
		if errors.As(err, &pe) {
			code = pe.Code
		}
		s.meter(c, cand, priced(cand, usage), code)
	}
	return err
}

// ---- 转换 ----

var roles = map[modelgatewayv1.ChatRole]modelprovider.Role{
	modelgatewayv1.ChatRole_CHAT_ROLE_SYSTEM:    modelprovider.RoleSystem,
	modelgatewayv1.ChatRole_CHAT_ROLE_USER:      modelprovider.RoleUser,
	modelgatewayv1.ChatRole_CHAT_ROLE_ASSISTANT: modelprovider.RoleAssistant,
	modelgatewayv1.ChatRole_CHAT_ROLE_TOOL:      modelprovider.RoleTool,
}

// payloadJSON 取 JsonPayload 的 JSON；没有值时为 nil。
func payloadJSON(p *commonv1.JsonPayload) (json.RawMessage, error) {
	if p.GetValue() == nil {
		return nil, nil
	}
	raw, err := protojson.Marshal(p.GetValue())
	if err != nil {
		return nil, invalid("bad_schema", "schema 无法编码成 JSON")
	}
	return raw, nil
}

// generateRequest 把 GenerateParams 转成适配层的请求（Model 与思考预算按候选再填）。
func generateRequest(g *modelgatewayv1.GenerateParams) (modelprovider.GenerateRequest, error) {
	out := modelprovider.GenerateRequest{MaxOutputTokens: int(g.GetMaxOutputTokens()), ThinkingBudgetTokens: int(g.GetThinkingBudgetTokens())}
	if len(g.GetMessages()) == 0 {
		return out, invalid("empty_messages", "消息为空")
	}
	for i, m := range g.GetMessages() {
		role, ok := roles[m.GetRole()]
		if !ok {
			return out, invalid("bad_message", fmt.Sprintf("第 %d 条消息的角色未指定", i))
		}
		msg := modelprovider.Message{Role: role, Text: m.GetText(), ToolCallID: m.GetToolCallId(), CacheBreakpoint: m.GetCacheBreakpoint()}
		for _, tc := range m.GetToolCalls() {
			args, err := protojson.Marshal(tc.GetArguments())
			if err != nil {
				return out, invalid("bad_message", "工具调用的参数无法编码成 JSON")
			}
			msg.ToolCalls = append(msg.ToolCalls, modelprovider.ToolCall{ID: tc.GetId(), Name: tc.GetName(), Arguments: args})
		}
		out.Messages = append(out.Messages, msg)
	}
	for _, t := range g.GetTools() {
		params, err := payloadJSON(t.GetParamsSchema())
		if err != nil {
			return out, err
		}
		out.Tools = append(out.Tools, modelprovider.ToolSpec{Name: t.GetName(), Description: t.GetDescription(), ParamsSchema: params})
	}
	schema, err := payloadJSON(g.GetOutputSchema())
	if err != nil {
		return out, err
	}
	out.OutputSchema = schema
	return out, nil
}

// forCandidate 填上候选的模型名与思考预算（请求为 0 时用 Tier 的默认值）。
func forCandidate(req modelprovider.GenerateRequest, cand router.Candidate) modelprovider.GenerateRequest {
	req.Model = cand.Profile.Model
	if req.ThinkingBudgetTokens == 0 {
		req.ThinkingBudgetTokens = cand.Tier.ThinkingBudget
	}
	return req
}

var finishReasons = map[modelprovider.FinishReason]modelgatewayv1.FinishReason{
	modelprovider.FinishStop:          modelgatewayv1.FinishReason_FINISH_REASON_STOP,
	modelprovider.FinishMaxTokens:     modelgatewayv1.FinishReason_FINISH_REASON_MAX_TOKENS,
	modelprovider.FinishToolCalls:     modelgatewayv1.FinishReason_FINISH_REASON_TOOL_CALLS,
	modelprovider.FinishContentFilter: modelgatewayv1.FinishReason_FINISH_REASON_CONTENT_FILTER,
}

func toolCall(tc modelprovider.ToolCall) (*modelgatewayv1.ToolCall, error) {
	var args map[string]any
	if len(tc.Arguments) > 0 {
		if err := json.Unmarshal(tc.Arguments, &args); err != nil {
			return nil, invalid("bad_tool_arguments", "工具调用的参数不是 JSON 对象")
		}
	}
	s, err := structpb.NewStruct(args)
	if err != nil {
		return nil, invalid("bad_tool_arguments", "工具调用的参数无法转换")
	}
	return &modelgatewayv1.ToolCall{Id: tc.ID, Name: tc.Name, Arguments: s}, nil
}

// generateResponse 把适配层的结果转成 GenerateResponse：结构化输出进 result，思考内容只进 rationale。
func (c *call) generateResponse(cand router.Candidate, r modelprovider.GenerateResult) (*modelgatewayv1.GenerateResponse, error) {
	out := &modelgatewayv1.GenerateResponse{
		Text:         r.Text,
		Rationale:    r.Thinking,
		FinishReason: finishReasons[r.FinishReason],
		Model:        c.choiceOf(cand),
		Usage:        priced(cand, r.Usage),
	}
	if len(r.Structured) > 0 {
		var v any
		if err := json.Unmarshal(r.Structured, &v); err != nil {
			return nil, invalid("bad_structured_output", "结构化输出不是合法的 JSON")
		}
		value, err := structpb.NewValue(v)
		if err != nil {
			return nil, invalid("bad_structured_output", "结构化输出无法转换")
		}
		out.Result = value
	}
	for _, tc := range r.ToolCalls {
		call, err := toolCall(tc)
		if err != nil {
			return nil, err
		}
		out.ToolCalls = append(out.ToolCalls, call)
	}
	return out, nil
}

// ---- 方法 ----

// Generate 生成，含结构化输出与工具调用。
func (s *Server) Generate(ctx context.Context, req *connect.Request[modelgatewayv1.GenerateRequest]) (*connect.Response[modelgatewayv1.GenerateResponse], error) {
	m := req.Msg
	if err := checkTenant(m.GetTenant()); err != nil {
		return nil, connecterr.Error(err)
	}
	gen, err := generateRequest(m.GetGeneration())
	if err != nil {
		return nil, connecterr.Error(err)
	}
	c, err := s.prepare(ctx, m.GetTenant(), m.GetCall(), "generate", router.Request{Kind: router.KindGenerate, Structured: len(gen.OutputSchema) > 0, Tools: len(gen.Tools) > 0})
	if err != nil {
		return nil, connecterr.Error(err)
	}
	tr := s.startSpan(ctx, c, "chat", false)
	var result modelprovider.GenerateResult
	cand, err := s.run(ctx, c, c.choice.Candidates, true, func(ctx context.Context, cand router.Candidate) (*commonv1.Usage, error) {
		r, err := c.client.Generate(ctx, forCandidate(gen, cand))
		result = r
		return r.Usage, err
	})
	if err != nil {
		tr.end(c, cand, nil, err)
		return nil, connecterr.Error(err)
	}
	out, err := c.generateResponse(cand, result)
	if err != nil {
		tr.end(c, cand, priced(cand, result.Usage), err)
		return nil, connecterr.Error(s.billedFailure(c, cand, result.Usage, err))
	}
	s.served(c, cand, out.GetUsage())
	tr.end(c, cand, out.GetUsage(), nil)
	return connect.NewResponse(out), nil
}

// StreamGenerate 流式生成。各分片的 text_delta 依次拼接等于最后一个分片 final 的 text；
// 只有最后一个分片带 usage（含成本），final 里也带同样的 usage 与 ModelChoice。
func (s *Server) StreamGenerate(ctx context.Context, req *connect.Request[modelgatewayv1.StreamGenerateRequest], stream *connect.ServerStream[modelgatewayv1.StreamGenerateResponse]) error {
	m := req.Msg
	if err := checkTenant(m.GetTenant()); err != nil {
		return connecterr.Error(err)
	}
	gen, err := generateRequest(m.GetGeneration())
	if err != nil {
		return connecterr.Error(err)
	}
	c, err := s.prepare(ctx, m.GetTenant(), m.GetCall(), "stream_generate", router.Request{Kind: router.KindStream, Structured: len(gen.OutputSchema) > 0, Tools: len(gen.Tools) > 0})
	if err != nil {
		return connecterr.Error(err)
	}
	tr := s.startSpan(ctx, c, "chat", true)
	// span 在流结束时结束：记下实际服务的候选、最后的 Usage 与错误（下面每个出口都先赋值再返回）。
	var spanCand router.Candidate
	var spanUsage *commonv1.Usage
	var spanErr error
	defer func() { tr.end(c, spanCand, spanUsage, spanErr) }()
	// 一次尝试 = 建流并读到第一个分片；只有这之前的失败才重试与切换。每次尝试的超时也只套在第一个分片之前：
	// 流一旦开始，长度不受 AttemptTimeout 限制，只受调用方的 deadline 限制。
	var open modelprovider.Stream
	var first modelprovider.Chunk
	var release context.CancelFunc
	cand, err := s.run(ctx, c, c.choice.Candidates, false, func(ctx context.Context, cand router.Candidate) (*commonv1.Usage, error) {
		sctx, cancel := context.WithCancel(ctx)
		timer := time.AfterFunc(s.opts.AttemptTimeout, cancel)
		fail := func(err error) (*commonv1.Usage, error) {
			timer.Stop()
			cancel()
			return nil, err
		}
		st, err := c.client.StreamGenerate(sctx, forCandidate(gen, cand))
		if err != nil {
			return fail(err)
		}
		chunk, err := st.Recv(sctx)
		if err != nil {
			_ = st.Close()
			if errors.Is(err, io.EOF) {
				err = providererr.New(providererr.Transient, "empty_stream", "上游的流没有内容就结束了")
			}
			return fail(err)
		}
		if !timer.Stop() { // 第一个分片与超时同时到：流已被取消，按超时处理
			_ = st.Close()
			cancel()
			return nil, providererr.New(providererr.Transient, "timeout", "等第一个分片超时")
		}
		open, first, release = st, chunk, cancel
		return nil, nil
	})
	spanCand = cand
	if err != nil {
		spanErr = err
		return connecterr.Error(err)
	}
	defer release()
	defer open.Close()
	for chunk, err := first, error(nil); ; chunk, err = open.Recv(ctx) {
		if errors.Is(err, io.EOF) {
			spanErr = providererr.New(providererr.Transient, "stream_truncated", "上游的流没有给出最终结果")
			return connecterr.Error(spanErr)
		}
		if err != nil {
			spanErr = err
			return connecterr.Error(err)
		}
		out, final, err := c.streamChunk(cand, chunk)
		if err != nil {
			var usage *commonv1.Usage
			if chunk.Final != nil {
				usage = chunk.Final.Usage
				spanUsage = priced(cand, usage)
			}
			spanErr = err
			return connecterr.Error(s.billedFailure(c, cand, usage, err))
		}
		if out != nil {
			if err := stream.Send(out); err != nil {
				spanErr = err
				return err
			}
		}
		if final {
			spanUsage = out.GetUsage()
			s.served(c, cand, out.GetUsage())
			return nil
		}
	}
}

// streamChunk 转换一个分片；空分片返回 nil。final 为 true 表示这是最后一个分片。
func (c *call) streamChunk(cand router.Candidate, chunk modelprovider.Chunk) (*modelgatewayv1.StreamGenerateResponse, bool, error) {
	switch {
	case chunk.Final != nil:
		final, err := c.generateResponse(cand, *chunk.Final)
		if err != nil {
			return nil, false, err
		}
		return &modelgatewayv1.StreamGenerateResponse{Chunk: &modelgatewayv1.StreamGenerateResponse_Final{Final: final}, Usage: final.GetUsage()}, true, nil
	case chunk.ToolCall != nil:
		tc, err := toolCall(*chunk.ToolCall)
		if err != nil {
			return nil, false, err
		}
		return &modelgatewayv1.StreamGenerateResponse{Chunk: &modelgatewayv1.StreamGenerateResponse_ToolCall{ToolCall: tc}}, false, nil
	case chunk.ThinkingDelta != "":
		return &modelgatewayv1.StreamGenerateResponse{Chunk: &modelgatewayv1.StreamGenerateResponse_ThinkingDelta{ThinkingDelta: chunk.ThinkingDelta}}, false, nil
	case chunk.TextDelta != "":
		return &modelgatewayv1.StreamGenerateResponse{Chunk: &modelgatewayv1.StreamGenerateResponse_TextDelta{TextDelta: chunk.TextDelta}}, false, nil
	}
	return nil, false, nil
}

// Score 打分；结果与 candidates 一一对应。
func (s *Server) Score(ctx context.Context, req *connect.Request[modelgatewayv1.ScoreRequest]) (*connect.Response[modelgatewayv1.ScoreResponse], error) {
	m := req.Msg
	if err := checkTenant(m.GetTenant()); err != nil {
		return nil, connecterr.Error(err)
	}
	if len(m.GetCandidates()) == 0 {
		return nil, connecterr.Error(invalid("empty_candidates", "没有候选"))
	}
	c, err := s.prepare(ctx, m.GetTenant(), m.GetCall(), "score", router.Request{Kind: router.KindScore})
	if err != nil {
		return nil, connecterr.Error(err)
	}
	tr := s.startSpan(ctx, c, "chat", false)
	var result modelprovider.ScoreResult
	cand, err := s.run(ctx, c, c.choice.Candidates, true, func(ctx context.Context, cand router.Candidate) (*commonv1.Usage, error) {
		r, err := c.client.Score(ctx, modelprovider.ScoreRequest{Model: cand.Profile.Model, Query: m.GetQuery(), Candidates: m.GetCandidates(), Rubric: m.GetRubric()})
		result = r
		return r.Usage, err
	})
	if err != nil {
		tr.end(c, cand, nil, err)
		return nil, connecterr.Error(err)
	}
	if len(result.Scores) != len(m.GetCandidates()) {
		err := invalid("bad_scores", "分数与候选的个数不一致")
		tr.end(c, cand, priced(cand, result.Usage), err)
		return nil, connecterr.Error(s.billedFailure(c, cand, result.Usage, err))
	}
	out := &modelgatewayv1.ScoreResponse{Scores: result.Scores, Model: c.choiceOf(cand), Usage: priced(cand, result.Usage)}
	s.served(c, cand, out.GetUsage())
	tr.end(c, cand, out.GetUsage(), nil)
	return connect.NewResponse(out), nil
}

// Embed 嵌入；结果与 texts 一一对应。
func (s *Server) Embed(ctx context.Context, req *connect.Request[modelgatewayv1.EmbedRequest]) (*connect.Response[modelgatewayv1.EmbedResponse], error) {
	m := req.Msg
	if err := checkTenant(m.GetTenant()); err != nil {
		return nil, connecterr.Error(err)
	}
	if len(m.GetTexts()) == 0 {
		return nil, connecterr.Error(invalid("empty_texts", "没有要嵌入的文本"))
	}
	c, err := s.prepare(ctx, m.GetTenant(), m.GetCall(), "embed", router.Request{Kind: router.KindEmbed})
	if err != nil {
		return nil, connecterr.Error(err)
	}
	candidates := c.choice.Candidates
	if v := m.GetEmbeddingVersion(); v != "" {
		// 向量不能混用：只用这一个 Profile，不切换。
		i := slices.IndexFunc(candidates, func(cand router.Candidate) bool { return cand.Profile.ID == v })
		if i < 0 {
			return nil, connecterr.Error(invalid("embedding_version_mismatch", "embedding_version 不是可选的嵌入模型："+v))
		}
		candidates = candidates[i : i+1]
	}
	tr := s.startSpan(ctx, c, "embeddings", false)
	var result modelprovider.EmbedResult
	cand, err := s.run(ctx, c, candidates, true, func(ctx context.Context, cand router.Candidate) (*commonv1.Usage, error) {
		r, err := c.client.Embed(ctx, modelprovider.EmbedRequest{Model: cand.Profile.Model, Texts: m.GetTexts()})
		result = r
		return r.Usage, err
	})
	if err != nil {
		tr.end(c, cand, nil, err)
		return nil, connecterr.Error(err)
	}
	if len(result.Vectors) != len(m.GetTexts()) {
		err := providererr.New(providererr.Transient, "bad_embeddings", "向量与文本的个数不一致")
		tr.end(c, cand, priced(cand, result.Usage), err)
		return nil, connecterr.Error(s.billedFailure(c, cand, result.Usage, err))
	}
	out := &modelgatewayv1.EmbedResponse{EmbeddingVersion: cand.Profile.ID, Model: c.choiceOf(cand), Usage: priced(cand, result.Usage)}
	for _, v := range result.Vectors {
		out.Embeddings = append(out.Embeddings, &modelgatewayv1.Embedding{Values: v})
	}
	s.served(c, cand, out.GetUsage())
	tr.end(c, cand, out.GetUsage(), nil)
	return connect.NewResponse(out), nil
}

// classifySchema 是 Classify 的 output_schema：label 只能取给定的 labels。
func classifySchema(labels []string) json.RawMessage {
	enum, _ := json.Marshal(labels)
	return json.RawMessage(`{"type":"object","properties":{"results":{"type":"array","items":{"type":"object",` +
		`"properties":{"label":{"type":"string","enum":` + string(enum) + `},"confidence":{"type":"number"}},` +
		`"required":["label","confidence"],"additionalProperties":false}}},"required":["results"],"additionalProperties":false}`)
}

// Classify 分类；每条文本的 label 只能是请求给的 labels 之一。
func (s *Server) Classify(ctx context.Context, req *connect.Request[modelgatewayv1.ClassifyRequest]) (*connect.Response[modelgatewayv1.ClassifyResponse], error) {
	m := req.Msg
	if err := checkTenant(m.GetTenant()); err != nil {
		return nil, connecterr.Error(err)
	}
	labels, texts := m.GetLabels(), m.GetTexts()
	if len(labels) == 0 || len(texts) == 0 {
		return nil, connecterr.Error(invalid("empty_classify", "labels 与 texts 都不能为空"))
	}
	c, err := s.prepare(ctx, m.GetTenant(), m.GetCall(), "classify", router.Request{Kind: router.KindClassify, Structured: true})
	if err != nil {
		return nil, connecterr.Error(err)
	}
	var prompt strings.Builder
	fmt.Fprintf(&prompt, "可选的类别：%s\n下面按顺序列出 %d 条文本。给每条文本选一个类别并给出 0 到 1 之间的置信度；results 按文本的顺序排列，条数与文本相同。\n", strings.Join(labels, "、"), len(texts))
	for i, text := range texts {
		fmt.Fprintf(&prompt, "文本 %d：%s\n", i+1, text)
	}
	gen := modelprovider.GenerateRequest{
		Messages:     []modelprovider.Message{{Role: modelprovider.RoleSystem, Text: "你是分类器，只能从给定的类别里选。"}, {Role: modelprovider.RoleUser, Text: prompt.String()}},
		OutputSchema: classifySchema(labels),
	}
	tr := s.startSpan(ctx, c, "chat", false)
	var result modelprovider.GenerateResult
	cand, err := s.run(ctx, c, c.choice.Candidates, true, func(ctx context.Context, cand router.Candidate) (*commonv1.Usage, error) {
		r, err := c.client.Generate(ctx, forCandidate(gen, cand))
		result = r
		return r.Usage, err
	})
	if err != nil {
		tr.end(c, cand, nil, err)
		return nil, connecterr.Error(err)
	}
	var parsed struct {
		Results []struct {
			Label      string  `json:"label"`
			Confidence float64 `json:"confidence"`
		} `json:"results"`
	}
	if err := json.Unmarshal(result.Structured, &parsed); err != nil || len(parsed.Results) != len(texts) {
		err := invalid("bad_classification", "分类结果不是合法的 JSON，或条数与文本不一致")
		tr.end(c, cand, priced(cand, result.Usage), err)
		return nil, connecterr.Error(s.billedFailure(c, cand, result.Usage, err))
	}
	out := &modelgatewayv1.ClassifyResponse{Model: c.choiceOf(cand), Usage: priced(cand, result.Usage)}
	for _, r := range parsed.Results {
		if !slices.Contains(labels, r.Label) {
			err := invalid("bad_classification", "分类结果里有不在 labels 里的值")
			tr.end(c, cand, priced(cand, result.Usage), err)
			return nil, connecterr.Error(s.billedFailure(c, cand, result.Usage, err))
		}
		out.Results = append(out.Results, &modelgatewayv1.Classification{Label: r.Label, Confidence: min(max(r.Confidence, 0), 1)})
	}
	s.served(c, cand, out.GetUsage())
	tr.end(c, cand, out.GetUsage(), nil)
	return connect.NewResponse(out), nil
}
