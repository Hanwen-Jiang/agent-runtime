package gateway_test

// BUDGET-DEFAULT（docs/plan/all-plans-dag-2026-11-03.md；第 27 版冒烟 3）：默认档（每天 3 美元）的 Task 在 Planner 的第一次模型调用就被拒，
// 实际花费 0。原因：请求没写 max_output_tokens 时，网关按 Profile 的 max_output 预留输出（kimi-k3.1-pro 是 1,048,576 个 token，
// 每百万 10 美元，一次就预留 10.49 美元），一次就超过日额度。
//
// 改法：请求没写输出上限时，网关用默认上限：可见输出 DefaultMaxOutputTokens（16384）加上这次的思考预算，不超过 Profile 的 max_output；
// 发给供应商的与预留的是同一个数，预留仍是真实费用的上界。请求写了的照旧（不超过 Profile 的）。Options.DefaultMaxOutput 为负数时
// 回到原来的做法（用 Profile 的 max_output）。

import (
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/modelbudget"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"

	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/gateway"
)

func TestUncappedRequestsUseTheGatewayDefaultOutputLimit(t *testing.T) {
	h := newHarness(t)
	h.mustGenerate(t, "t1", call(commonv1.Role_ROLE_PLAN), say("plan it")) // strong@medium，思考预算 8192
	high := call(commonv1.Role_ROLE_PLAN)
	high.Tier = "strong@high" // 思考预算 32768
	h.mustGenerate(t, "t1", high, say("plan it harder"))
	asked := say("plan it")
	asked.MaxOutputTokens = 256
	h.mustGenerate(t, "t1", call(commonv1.Role_ROLE_PLAN), asked)
	calls := h.fake.allCalls()
	if len(calls) != 3 {
		t.Fatalf("应有 3 次调用：%+v", calls)
	}
	if got := calls[0].Generate.MaxOutputTokens; got != gateway.DefaultMaxOutputTokens+8192 {
		t.Errorf("strong@medium 没写上限：%d，want %d", got, gateway.DefaultMaxOutputTokens+8192)
	}
	if got := calls[1].Generate.MaxOutputTokens; got != gateway.DefaultMaxOutputTokens+32768 {
		t.Errorf("strong@high 没写上限：%d，want %d（思考预算之外留出可见输出）", got, gateway.DefaultMaxOutputTokens+32768)
	}
	if got := calls[2].Generate.MaxOutputTokens; got != 256 {
		t.Errorf("写了的照旧：%d", got)
	}
}

func TestTheDefaultNeverExceedsTheProfileLimit(t *testing.T) {
	h := newHarnessOpts(t, nil, func(o *gateway.Options) { o.DefaultMaxOutput = 100_000 })
	h.mustGenerate(t, "t1", call(transform), say("hi")) // mid@low：mid-a 的 max_output 是 16384，没有思考预算
	if got := h.fake.allCalls()[0].Generate.MaxOutputTokens; got != 16384 {
		t.Errorf("不超过 Profile 的 max_output：%d", got)
	}
}

// 冒烟 3 的情形：日额度够付一次调用的默认上限，不够付整个 max_output。之前一次就被拒，现在照常调用；关掉默认上限时回到之前。
func TestASmallDailyBudgetIsNotExhaustedByTheProfileOutputLimit(t *testing.T) {
	// strong-a：max_output 64000，输出每百万 15 美元，整个 max_output 是 0.96 美元；默认上限（16384+8192）约 0.37 美元。
	h, _ := budgetHarness(t, 900_000)
	if _, err := h.generate(t, "t1", call(commonv1.Role_ROLE_PLAN), say("plan it")); err != nil {
		t.Fatalf("默认上限的预留在日额度之内，应能调用：%v", err)
	}
	old, _ := budgetHarness(t, 900_000, func(o *gateway.Options) { o.DefaultMaxOutput = -1 })
	_, err := old.generate(t, "t1", call(commonv1.Role_ROLE_PLAN), say("plan it"))
	if code, detail := errorDetail(t, err); detail.GetCode() != modelbudget.TenantBudgetExceededCode {
		t.Errorf("关掉默认上限时按 Profile 的 max_output 预留、被拒：%v %q", code, detail.GetCode())
	}
	if len(old.fake.allCalls()) != 0 {
		t.Error("被拒时不调供应商")
	}
}
