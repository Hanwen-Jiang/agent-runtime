package gateway_test

// R-06 首版的“价格版本固定”（docs/plan/r04-r06-secret-cost-contract-baseline-2026-11-03.md）：每条计量记录写上计价用的
// Profile 单价的取得日期（price_at），价格表更新之后历史调用仍能按当时的单价复算。取的是实际计价的那个 Profile 的日期：
// 切换之后的成功、已计费的失败、取消都一样。

import (
	"context"
	"testing"

	"connectrpc.com/connect"

	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	mg "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1"
)

func TestMeterRecordsCarryThePriceDateOfTheProfileThatPricedThem(t *testing.T) {
	h := newHarness(t)
	h.mustGenerate(t, "t1", call(transform), say("hi"))
	if _, err := h.client.Embed(context.Background(), connect.NewRequest(&mg.EmbedRequest{Tenant: tenant("t1"), Call: call(commonv1.Role_ROLE_FETCH), Texts: []string{"x"}})); err != nil {
		t.Fatalf("Embed：%v", err)
	}
	lines := h.meterLines(t)
	if len(lines) != 2 {
		t.Fatalf("两次调用两行计量：%s", h.meter.String())
	}
	if lines[0]["model_profile_id"] != "test/mid-a@1" || lines[0]["price_at"] != "2026-10-21" {
		t.Errorf("generate 的计量记 mid-a 的取价日期：%v", lines[0])
	}
	if lines[1]["model_profile_id"] != "test/embed-a@1" || lines[1]["price_at"] != "2026-10-30" {
		t.Errorf("embed 的计量记 embed-a 的取价日期：%v", lines[1])
	}
}

func TestAFailedAttemptAndTheFallbackEachCarryTheirOwnPriceDate(t *testing.T) {
	h := newHarness(t)
	h.fake.onGenerate("mid-a", callsTool("Bash"))
	h.mustGenerate(t, "t1", call(commonv1.Role_ROLE_ACT), withSearchTool(t, "找一下"))
	lines := h.meterLines(t)
	if len(lines) != 2 {
		t.Fatalf("两次尝试各一行计量：%s", h.meter.String())
	}
	if lines[0]["status"] != "failed" || lines[0]["model_profile_id"] != "test/mid-a@1" || lines[0]["price_at"] != "2026-10-21" {
		t.Errorf("已计费的失败按 mid-a 的单价，记 mid-a 的取价日期：%v", lines[0])
	}
	if lines[1]["status"] != "ok" || lines[1]["model_profile_id"] != "test/mid-b@1" || lines[1]["price_at"] != "2026-10-28" {
		t.Errorf("切换之后的成功按 mid-b 的单价，记 mid-b 的取价日期：%v", lines[1])
	}
}
