package connecterr_test

// 错误转成 *connect.Error 的约定（Model Gateway 与 Registry 共用，Python 侧据此还原 ProviderError）：
// 类别到 Connect 码一一对应；trailer ar-error-bin 是序列化的 common.v1.Error；底层原因与未知错误的文本不外泄。

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/proto"

	"github.com/agentruntime/agent-runtime/libs/go-core/connecterr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

func detail(t *testing.T, ce *connect.Error) *commonv1.Error {
	t.Helper()
	values := ce.Meta().Values(connecterr.Trailer)
	if len(values) != 1 {
		t.Fatalf("应带一个 %s trailer，实际 %d 个", connecterr.Trailer, len(values))
	}
	raw, err := connect.DecodeBinaryHeader(values[0])
	if err != nil {
		t.Fatal(err)
	}
	var out commonv1.Error
	if err := proto.Unmarshal(raw, &out); err != nil {
		t.Fatal(err)
	}
	return &out
}

func TestCategoriesMapToConnectCodes(t *testing.T) {
	cases := map[providererr.Category]connect.Code{
		providererr.Transient: connect.CodeUnavailable,
		providererr.Invalid:   connect.CodeInvalidArgument,
		providererr.Denied:    connect.CodePermissionDenied,
		providererr.NotFound:  connect.CodeNotFound,
		providererr.Conflict:  connect.CodeAborted,
	}
	for category, code := range cases {
		ce := connecterr.Error(fmt.Errorf("包了一层：%w", providererr.New(category, "some_code", "说明")))
		if ce.Code() != code || ce.Message() != "说明" {
			t.Errorf("%v 应映射为 %v 且文本只用 message，实际 %v %q", category, code, ce.Code(), ce.Message())
		}
		if d := detail(t, ce); d.GetCategory() != category || d.GetCode() != "some_code" || d.GetMessage() != "说明" {
			t.Errorf("trailer 原样带类别、code、message：%+v", d)
		}
	}
}

func TestCauseAndUnknownErrorsDoNotLeak(t *testing.T) {
	ce := connecterr.Error(providererr.Wrap(providererr.Invalid, "bad", "请求不合格", errors.New("sk-secret-in-cause")))
	if strings.Contains(ce.Error(), "sk-secret") || strings.Contains(detail(t, ce).GetMessage(), "sk-secret") {
		t.Errorf("底层原因不外泄：%v", ce)
	}
	ce = connecterr.Error(errors.New("pq: password sk-secret-unknown"))
	if ce.Code() != connect.CodeInternal || strings.Contains(ce.Error(), "sk-secret") {
		t.Errorf("未知错误是 Internal 且不带原文：%v", ce)
	}
	if d := detail(t, ce); d.GetCategory() != providererr.Transient || d.GetCode() != "internal" {
		t.Errorf("未知错误的 trailer 是 transient/internal：%+v", d)
	}
}

func TestContextErrorsAreTransient(t *testing.T) {
	for _, err := range []error{context.DeadlineExceeded, context.Canceled} {
		ce := connecterr.Error(err)
		if ce.Code() != connect.CodeUnavailable || detail(t, ce).GetCategory() != providererr.Transient {
			t.Errorf("%v 应是 transient：%v", err, ce)
		}
	}
}

func TestRetryAfterIsCarriedInDetails(t *testing.T) {
	pe := providererr.New(providererr.Transient, "rate_limited", "限流")
	pe.RetryAfter = 1500 * time.Millisecond
	got := detail(t, connecterr.Error(pe)).GetDetails().GetFields()[connecterr.RetryAfterDetail].GetNumberValue()
	if got != 1500 {
		t.Errorf("retry_after_ms 应为 1500，实际 %v", got)
	}
	if connecterr.Error(nil) != nil {
		t.Error("nil 进 nil 出")
	}
}
