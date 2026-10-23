package events

// R-05 评审的本期修（docs/plan/review-R-05-ingress-sse.md 第 4 节）：
//   1. SSE 在令牌过期时结束这条流，客户端带新令牌与 Last-Event-ID 重连；
//   2. SSE 经 Ingress.Admit 限流（按租户与路由计数，与提交等路由分开），超限回 429 并带 Retry-After。

import (
	"context"
	"net/http"
	"strconv"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
)

func TestStreamEndsWhenTheTokenExpires(t *testing.T) {
	f := newSSEWith(t, sseOptions{heartbeat: 20 * time.Millisecond})
	f.append(taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED, nil)
	s := f.open(path(testTask), f.ingress.IssueToken("t1", "u1", 10*time.Minute), nil)
	if ev := s.next(nil); ev == nil || ev.ID != 1 {
		t.Fatalf("令牌有效时照常推送，实际 %+v", ev)
	}

	f.clock.Advance(11 * time.Minute)
	f.append(taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED, nil)
	if ev := s.next(nil); ev != nil {
		t.Fatalf("令牌过期后不再推事件，应结束这条流；实际收到 %+v", ev)
	}

	again := f.open(path(testTask), f.ingress.IssueToken("t1", "u1", time.Hour), map[string]string{"Last-Event-ID": "1"})
	if ev := again.next(nil); ev == nil || ev.ID != 2 || ev.Event != "task_finished" {
		t.Fatalf("带新令牌与 Last-Event-ID 重连，接着收到之后的事件；实际 %+v", ev)
	}
}

func TestStreamsAreRateLimitedPerTenant(t *testing.T) {
	f := newSSEWith(t, sseOptions{limit: 2, window: time.Minute})
	f.append(taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED, nil)
	tok := f.ingress.IssueToken("t1", "u1", time.Hour)
	for i := range 2 {
		if s := f.open(path(testTask), tok, nil); s.resp.StatusCode != http.StatusOK {
			t.Fatalf("第 %d 个连接在限额内，应返回 200，实际 %d", i+1, s.resp.StatusCode)
		}
	}

	over := f.open(path(testTask), tok, nil)
	if over.resp.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("超过限额应返回 429，实际 %d", over.resp.StatusCode)
	}
	if n, err := strconv.Atoi(over.resp.Header.Get("Retry-After")); err != nil || n < 1 {
		t.Errorf("429 应带 Retry-After（秒），实际 %q", over.resp.Header.Get("Retry-After"))
	}

	// 别的租户不受影响：t2 没有这个 Task，照常走到归属检查（404，不是 429）。
	if other := f.open(path(testTask), f.ingress.IssueToken("t2", "u2", time.Hour), nil); other.resp.StatusCode != http.StatusNotFound {
		t.Errorf("限流按租户计数，别的租户应照常处理（404），实际 %d", other.resp.StatusCode)
	}

	// 推流与提交等路由分开计数：SSE 用满了，同一租户照样能提交。
	ctx := context.Background()
	id, err := f.ingress.Authenticate(ctx, ingress.Credentials{BearerToken: tok})
	if err != nil {
		t.Fatal(err)
	}
	if err := f.ingress.Admit(ctx, id, "submit_task"); err != nil {
		t.Errorf("SSE 的限额不应占用提交的限额：%v", err)
	}

	f.clock.Advance(time.Minute + time.Second)
	if s := f.open(path(testTask), tok, nil); s.resp.StatusCode != http.StatusOK {
		t.Errorf("窗口过去后恢复，应返回 200，实际 %d", s.resp.StatusCode)
	}
}
