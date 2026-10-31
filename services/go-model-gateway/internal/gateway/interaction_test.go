package gateway_test

// docs/plan/running-task-interaction.md 第 2、3 节验收。
//
//   - 第 3 节的分母是模型的真实窗口：回答里带上这次用的 Model Profile 的 context_window。
//   - 第 2 节的立刻打断：Worker 取消调用，网关随之断开上游，不再重试、不再切换模型；上游可能已对部分输出计费，网关拿不到用量，
//     这部分不进成本，计量记一行 cancelled，看得到发生过。

import (
	"context"
	"testing"
	"time"

	"connectrpc.com/connect"

	mg "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1"
)

func TestTheResponseCarriesTheModelsContextWindow(t *testing.T) {
	h := newHarness(t)
	resp := h.mustGenerate(t, "t1", call(transform), say("hi"))
	if got := resp.GetModel().GetContextWindow(); got != 128000 {
		t.Errorf("mid-a 的 context_window 是 128000，实际 %d", got)
	}
}

func TestTheStreamsFinalChunkCarriesTheContextWindow(t *testing.T) {
	h := newHarness(t)
	chunks, err := h.streamAll(t, &mg.StreamGenerateRequest{Tenant: tenant("t1"), Call: call(transform), Generation: say("hi")})
	if err != nil {
		t.Fatalf("流式调用：%v", err)
	}
	final := chunks[len(chunks)-1].GetFinal()
	if final == nil || final.GetModel().GetContextWindow() != 128000 {
		t.Errorf("最后一个分片要带窗口：%v", final)
	}
}

func callCount(p *fakeProvider) int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.calls)
}

// cancelledCall 发一个上游一直不回答的调用，等上游收到后取消。
func cancelledCall(t *testing.T, h *harness) error {
	t.Helper()
	h.fake.hangNext("mid-a", 1)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		_, err := h.client.Generate(ctx, connect.NewRequest(&mg.GenerateRequest{Tenant: tenant("t1"), Call: call(transform), Generation: say("hi")}))
		done <- err
	}()
	deadline := time.Now().Add(5 * time.Second)
	for callCount(h.fake) == 0 {
		if time.Now().After(deadline) {
			t.Fatal("上游没有收到调用")
		}
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	select {
	case err := <-done:
		return err
	case <-time.After(5 * time.Second):
		t.Fatal("取消之后调用没有结束")
		return nil
	}
}

func TestCancellingACallStopsItWithoutRetryingOrFallingBack(t *testing.T) {
	h := newHarness(t)
	if err := cancelledCall(t, h); connect.CodeOf(err) != connect.CodeCanceled {
		t.Errorf("应是 canceled，实际 %v", err)
	}
	time.Sleep(200 * time.Millisecond)
	if n := callCount(h.fake); n != 1 {
		t.Errorf("取消之后不重试、不切换模型：上游收到 %d 次调用", n)
	}
}

func TestACancelledCallIsMeteredWithoutCost(t *testing.T) {
	h := newHarness(t)
	_ = cancelledCall(t, h)
	var lines []map[string]any
	deadline := time.Now().Add(5 * time.Second)
	for len(lines) == 0 && time.Now().Before(deadline) {
		lines = h.meterLines(t)
		time.Sleep(10 * time.Millisecond)
	}
	if len(lines) != 1 {
		t.Fatalf("计量记一行，看得到发生过：%v", lines)
	}
	if lines[0]["status"] != "cancelled" {
		t.Errorf("status 应是 cancelled：%v", lines[0])
	}
	if cost, ok := lines[0]["cost_usd_micros"]; ok && cost != float64(0) {
		t.Errorf("网关拿不到用量，这部分不进成本：%v", lines[0])
	}
}

func TestADeadlineIsNotACancellation(t *testing.T) {
	// 超时还是原来的路径（transient，可重试、可切换），不算用户打断。
	h := newHarness(t)
	h.fake.hangNext("mid-a", 1)
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	_, _ = h.client.Generate(ctx, connect.NewRequest(&mg.GenerateRequest{Tenant: tenant("t1"), Call: call(transform), Generation: say("hi")}))
	time.Sleep(100 * time.Millisecond)
	for _, line := range h.meterLines(t) {
		if line["status"] == "cancelled" {
			t.Errorf("调用方的 deadline 不记 cancelled：%v", line)
		}
	}
}
