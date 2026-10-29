package gateway_test

// TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 4 节：网关的 models_exhausted 带上最后一次失败的代码，
// Worker 在 decision_retry 事件里显示“模型供应商暂时不可用”的具体原因（细化见该计划第 11 节）。

import (
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
)

// lastErrorCodeDetail 是 common.v1.Error.details 里的字段名（只加字段，不改契约）。
const lastErrorCodeDetail = "last_error_code"

func TestExhaustedCarriesTheLastErrorCode(t *testing.T) {
	h := newHarness(t)
	h.fake.failNext("mid-a", transient("timeout", 0), transient("timeout", 0), transient("timeout", 0))
	h.fake.failNext("mid-b", transient("timeout", 0), transient("timeout", 0), transient("rate_limit", 2*time.Second))
	_, err := h.generate(t, "t1", call(transform), say("hi"))
	_, pe := errorDetail(t, err)
	if pe.GetCode() != "models_exhausted" || pe.GetCategory() != providererr.Transient {
		t.Fatalf("全部失败：%v/%s", pe.GetCategory(), pe.GetCode())
	}
	if got := pe.GetDetails().GetFields()[lastErrorCodeDetail].GetStringValue(); got != "rate_limit" {
		t.Errorf("details.%s = %q，want rate_limit（最后一次失败的代码）", lastErrorCodeDetail, got)
	}
	if ms, ok := retryAfterMS(pe); !ok || ms != 2000 {
		t.Errorf("retry_after_ms 仍要带上：%v（%v）", ms, ok)
	}
}
