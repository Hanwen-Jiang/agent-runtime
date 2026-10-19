// Package durabletest 是 DurableEngine 的一致性测试，与 Python 侧 agentruntime_core.conformance.durable_engine 一一对应。
//
// harness 必须在 TaskQueue() 上注册两个测试工作流（用 Go 的 Temporal SDK 实现）：
//   - conformance.Echo：输入 StringValue。信号 append（StringValue）把值追加到列表；查询 state 返回 StringValue，
//     内容是输入与已追加的值用逗号连接；信号 finish 让工作流结束，结果为同样的 StringValue。被取消时以取消结束。
//   - conformance.Fail：启动后立即失败，失败信息为 boom。
package durabletest

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/protobuf/types/known/wrapperspb"

	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/durable"
)

const (
	echo = "conformance.Echo"
	fail = "conformance.Fail"
)

// Harness 由实现所在包提供。
type Harness interface {
	Provider() durable.Provider
	TaskQueue() string
	conformance.Faultable
}

func newID(label string) string {
	b := make([]byte, 6)
	_, _ = rand.Read(b)
	return "conformance-" + label + "-" + hex.EncodeToString(b)
}

func str(s string) *wrapperspb.StringValue { return wrapperspb.String(s) }

// Run 跑完整的 DurableEngine 一致性测试。
func Run(t *testing.T, newHarness func(t *testing.T) Harness) {
	var mu sync.Mutex
	ids := map[string]string{}
	var queue string
	probe := conformance.Probe[durable.Engine]{
		Operation: "StartWorkflow",
		Write: func(ctx context.Context, e durable.Engine, name string) error {
			mu.Lock()
			if _, ok := ids[name]; !ok {
				ids[name] = newID(name)
			}
			id := ids[name]
			mu.Unlock()
			_, err := e.StartWorkflow(ctx, echo, durable.StartOptions{WorkflowID: id, TaskQueue: queue, Input: str(name)})
			return err
		},
		Visible: func(ctx context.Context, e durable.Engine, name string) (bool, error) {
			mu.Lock()
			id := ids[name]
			mu.Unlock()
			_, err := e.Describe(ctx, id)
			if providererr.CategoryOf(err) == providererr.NotFound {
				return false, nil
			}
			return err == nil, err
		},
	}
	harness := func(t *testing.T) Harness {
		h := newHarness(t)
		queue = h.TaskQueue()
		mu.Lock()
		ids = map[string]string{}
		mu.Unlock()
		return h
	}
	conformance.RunBindingRules(t, func(t *testing.T) conformance.Binder[durable.Engine] { return harness(t).Provider() })
	conformance.RunIsolationRules(t, func(t *testing.T) conformance.Binder[durable.Engine] { return harness(t).Provider() }, probe)
	conformance.RunErrorRules(t, func(t *testing.T) (conformance.Binder[durable.Engine], conformance.Faultable) {
		h := harness(t)
		return h.Provider(), h
	}, probe)

	ctx := context.Background()
	setup := func(t *testing.T) (Harness, durable.Engine) {
		h := harness(t)
		return h, conformance.Must(h.Provider().Bind(conformance.TenantA()))
	}
	startEcho := func(t *testing.T, h Harness, e durable.Engine, label string) string {
		t.Helper()
		id := newID(label)
		conformance.Must(e.StartWorkflow(ctx, echo, durable.StartOptions{WorkflowID: id, TaskQueue: h.TaskQueue(), Input: str("start")}))
		return id
	}
	state := func(t *testing.T, e durable.Engine, id string) string {
		t.Helper()
		got := &wrapperspb.StringValue{}
		conformance.NoError(t, e.Query(ctx, id, "state", got))
		return got.GetValue()
	}

	conformance.Case(t, "StartAndDescribe", func(t *testing.T) {
		h, e := setup(t)
		id := newID("describe")
		run := conformance.Must(e.StartWorkflow(ctx, echo, durable.StartOptions{WorkflowID: id, TaskQueue: h.TaskQueue(), Input: str("start")}))
		d := conformance.Must(e.Describe(ctx, id))
		if run.AlreadyStarted || run.RunID == "" || d.Status != durable.StatusRunning || d.WorkflowType != echo || d.TaskQueue != h.TaskQueue() || d.RunID != run.RunID {
			t.Fatalf("run = %+v, description = %+v", run, d)
		}
	})
	conformance.Case(t, "StartIsIdempotentByWorkflowID", func(t *testing.T) {
		h, e := setup(t)
		id := newID("idempotent")
		first := conformance.Must(e.StartWorkflow(ctx, echo, durable.StartOptions{WorkflowID: id, TaskQueue: h.TaskQueue(), Input: str("a")}))
		second := conformance.Must(e.StartWorkflow(ctx, echo, durable.StartOptions{WorkflowID: id, TaskQueue: h.TaskQueue(), Input: str("b")}))
		if !second.AlreadyStarted || second.RunID != first.RunID || state(t, e, id) != "a" {
			t.Fatal("second start must return the existing run unchanged")
		}
		_, err := e.StartWorkflow(ctx, fail, durable.StartOptions{WorkflowID: id, TaskQueue: h.TaskQueue()})
		conformance.ExpectError(t, err, providererr.Conflict, "")
	})
	conformance.Case(t, "SignalQueryAndSignalWithStart", func(t *testing.T) {
		h, e := setup(t)
		id := startEcho(t, h, e, "signal")
		conformance.NoError(t, e.Signal(ctx, id, "append", str("x")))
		conformance.NoError(t, e.Signal(ctx, id, "append", str("y")))
		if got := state(t, e, id); got != "start,x,y" {
			t.Fatalf("state = %q", got)
		}
		sws := newID("signal-with-start")
		opts := durable.StartOptions{WorkflowID: sws, TaskQueue: h.TaskQueue(), Input: str("s")}
		first := conformance.Must(e.SignalWithStart(ctx, echo, opts, "append", str("1")))
		opts.Input = str("ignored")
		second := conformance.Must(e.SignalWithStart(ctx, echo, opts, "append", str("2")))
		if first.AlreadyStarted || !second.AlreadyStarted || state(t, e, sws) != "s,1,2" {
			t.Fatal("SignalWithStart semantics broken")
		}
	})
	conformance.Case(t, "ResultOfCompletedAndFailedWorkflows", func(t *testing.T) {
		h, e := setup(t)
		id := startEcho(t, h, e, "result")
		conformance.NoError(t, e.Signal(ctx, id, "append", str("done")))
		conformance.NoError(t, e.Signal(ctx, id, "finish", nil))
		got := &wrapperspb.StringValue{}
		outcome := conformance.Must(e.Result(ctx, id, got, 30*time.Second))
		if outcome.Status != durable.StatusCompleted || got.GetValue() != "start,done" {
			t.Fatalf("outcome = %+v, value = %q", outcome, got.GetValue())
		}
		failed := newID("fail")
		conformance.Must(e.StartWorkflow(ctx, fail, durable.StartOptions{WorkflowID: failed, TaskQueue: h.TaskQueue()}))
		outcome = conformance.Must(e.Result(ctx, failed, &wrapperspb.StringValue{}, 30*time.Second))
		if outcome.Status != durable.StatusFailed || !strings.Contains(outcome.Failure, "boom") {
			t.Fatalf("failed outcome = %+v", outcome)
		}
	})
	conformance.Case(t, "ResultWaitTimeoutIsTransient", func(t *testing.T) {
		h, e := setup(t)
		id := startEcho(t, h, e, "wait")
		_, err := e.Result(ctx, id, &wrapperspb.StringValue{}, 300*time.Millisecond)
		conformance.ExpectError(t, err, providererr.Transient, "")
	})
	conformance.Case(t, "CancelIsIdempotent", func(t *testing.T) {
		h, e := setup(t)
		id := startEcho(t, h, e, "cancel")
		conformance.NoError(t, e.Cancel(ctx, id, "user stopped"))
		outcome := conformance.Must(e.Result(ctx, id, &wrapperspb.StringValue{}, 30*time.Second))
		if outcome.Status != durable.StatusCancelled {
			t.Fatalf("status = %v", outcome.Status)
		}
		conformance.NoError(t, e.Cancel(ctx, id, "again"))
	})
	conformance.Case(t, "UnknownWorkflowIsNotFound", func(t *testing.T) {
		_, e := setup(t)
		missing := newID("missing")
		conformance.ExpectError(t, e.Signal(ctx, missing, "append", str("x")), providererr.NotFound, "")
		conformance.ExpectError(t, e.Query(ctx, missing, "state", &wrapperspb.StringValue{}), providererr.NotFound, "")
		_, err := e.Describe(ctx, missing)
		conformance.ExpectError(t, err, providererr.NotFound, "")
		conformance.ExpectError(t, e.Cancel(ctx, missing, ""), providererr.NotFound, "")
	})
	conformance.Case(t, "OtherTenantCannotTouchWorkflow", func(t *testing.T) {
		h, e := setup(t)
		id := startEcho(t, h, e, "foreign")
		b := conformance.Must(h.Provider().Bind(conformance.TenantB()))
		conformance.ExpectError(t, b.Signal(ctx, id, "append", str("evil")), providererr.NotFound, "")
		conformance.ExpectError(t, b.Query(ctx, id, "state", &wrapperspb.StringValue{}), providererr.NotFound, "")
		conformance.ExpectError(t, b.Cancel(ctx, id, ""), providererr.NotFound, "")
		_, err := b.Result(ctx, id, &wrapperspb.StringValue{}, time.Second)
		conformance.ExpectError(t, err, providererr.NotFound, "")
		_, err = b.StartWorkflow(ctx, echo, durable.StartOptions{WorkflowID: id, TaskQueue: h.TaskQueue(), Input: str("x")})
		conformance.ExpectError(t, err, providererr.Conflict, "")
		if state(t, e, id) != "start" || conformance.Must(e.Describe(ctx, id)).Status != durable.StatusRunning {
			t.Fatal("foreign tenant affected the workflow")
		}
	})
	conformance.Case(t, "MalformedWorkflowIDIsInvalid", func(t *testing.T) {
		h, e := setup(t)
		for _, id := range []string{"", strings.Repeat("x", 256), "bad\nid"} {
			_, err := e.StartWorkflow(ctx, echo, durable.StartOptions{WorkflowID: id, TaskQueue: h.TaskQueue()})
			conformance.ExpectError(t, err, providererr.Invalid, "")
		}
	})
}
