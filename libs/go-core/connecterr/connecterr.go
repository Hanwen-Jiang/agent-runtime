// Package connecterr 把错误转成返回给 gRPC 调用方的 *connect.Error，并在 trailer 里附上 agentruntime.common.v1.Error。
// Go 服务（Model Gateway、Registry）共用；Python 侧（GrpcModelGateway、GrpcRegistry）按 trailer 还原 ProviderError。
package connecterr

import (
	"context"
	"errors"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Trailer 是携带错误详情的 trailer：值为序列化的 agentruntime.common.v1.Error（proto 二进制；
// 按 gRPC 的 -bin 约定在线上以 base64 传输，grpcio 读到的是解码后的字节）。
const Trailer = "ar-error-bin"

// RetryAfterDetail 是 common.v1.Error.details 里的字段名：transient 且有 RetryAfter 时为等待的毫秒数（整数）。
const RetryAfterDetail = "retry_after_ms"

// LastErrorCodeDetail 是 details 里的字段名：汇总了几次失败的错误里最后一次失败的 code（providererr.Error.LastCode）。
const LastErrorCodeDetail = "last_error_code"

// Error 把错误转成返回给调用方的 *connect.Error，并在 Trailer 里附上 common.v1.Error。
// 类别到 Connect 码：transient → Unavailable；invalid → InvalidArgument；denied → PermissionDenied；
// not_found → NotFound；conflict → Aborted。
//
//   - *providererr.Error：类别、code、message 原样放进 trailer；Connect 错误的文本只用 message，不含底层原因
//     （底层原因可能带上游回显的密钥）。transient 且 RetryAfter > 0 时 details 带 retry_after_ms；有 LastCode 时带 last_error_code。
//   - context.DeadlineExceeded、context.Canceled：transient（code timeout、canceled），Unavailable。
//   - 其他错误：Internal，trailer 为 transient（code internal），文本固定为“内部错误”，不带原始错误文本。
//
// err 为 nil 时返回 nil。
func Error(err error) *connect.Error {
	if err == nil {
		return nil
	}
	var pe *providererr.Error
	switch {
	case errors.As(err, &pe):
	case errors.Is(err, context.DeadlineExceeded):
		pe = providererr.New(providererr.Transient, "timeout", "调用超时")
	case errors.Is(err, context.Canceled):
		pe = providererr.New(providererr.Transient, "canceled", "调用被取消")
	default:
		return withDetail(connect.NewError(connect.CodeInternal, errors.New("内部错误")), &commonv1.Error{Category: providererr.Transient, Code: "internal", Message: "内部错误"})
	}
	detail := pe.Proto()
	details := map[string]any{}
	if pe.Category == providererr.Transient && pe.RetryAfter > 0 {
		details[RetryAfterDetail] = float64(pe.RetryAfter.Milliseconds())
	}
	if pe.LastCode != "" {
		details[LastErrorCodeDetail] = pe.LastCode
	}
	if len(details) > 0 {
		detail.Details, _ = structpb.NewStruct(details)
	}
	return withDetail(connect.NewError(codes[pe.Category], errors.New(pe.Message)), detail)
}

var codes = map[providererr.Category]connect.Code{
	providererr.Transient: connect.CodeUnavailable,
	providererr.Invalid:   connect.CodeInvalidArgument,
	providererr.Denied:    connect.CodePermissionDenied,
	providererr.NotFound:  connect.CodeNotFound,
	providererr.Conflict:  connect.CodeAborted,
}

// withDetail 把 common.v1.Error 序列化进 Trailer。
func withDetail(ce *connect.Error, detail *commonv1.Error) *connect.Error {
	if raw, err := proto.Marshal(detail); err == nil {
		ce.Meta().Set(Trailer, connect.EncodeBinaryHeader(raw))
	}
	return ce
}
