package taskapi

import (
	"context"
	"sync"
	"time"

	"google.golang.org/protobuf/proto"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/durable"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// durableCall 是对 fakeDurable 的一次调用。Scope 是绑定的租户。
type durableCall struct {
	Scope        string
	Op           string
	WorkflowType string
	Opts         durable.StartOptions
	WorkflowID   string
	Name         string // 信号名或查询名
	Payload      proto.Message
}

type fakeWorkflow struct {
	scope   string
	typ     string
	status  durable.Status
	queries map[string]proto.Message
}

// fakeDurable 是 DurableEngine 的替身：记录调用，按 Temporal 适配器的语义核对作用域
// （别的作用域的工作流按不存在处理；SignalWithStart 遇到别的作用域的同 ID 工作流报 Conflict）。
type fakeDurable struct {
	mu        sync.Mutex
	calls     []durableCall
	workflows map[string]*fakeWorkflow
	faults    *testkit.Faults
}

func newFakeDurable() *fakeDurable {
	return &fakeDurable{workflows: map[string]*fakeWorkflow{}, faults: testkit.NewFaults()}
}

func (f *fakeDurable) Bind(t *commonv1.TenantContext) (durable.Engine, error) {
	s, err := tenancy.TenantScope(t)
	if err != nil {
		return nil, err
	}
	return &fakeEngine{f: f, scope: s.TenantID}, nil
}

func (f *fakeDurable) BindPlatform(string) (durable.Engine, error) {
	return nil, providererr.New(providererr.Denied, "platform", "Task API 不应使用平台作用域")
}

func (f *fakeDurable) Close() error { return nil }

// addWorkflow 放入一个工作流，例如已经启动的 Task 子工作流。
func (f *fakeDurable) addWorkflow(id, scope, typ string, status durable.Status, queries map[string]proto.Message) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.workflows[id] = &fakeWorkflow{scope: scope, typ: typ, status: status, queries: queries}
}

func (f *fakeDurable) callsOf(op string) []durableCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []durableCall
	for _, c := range f.calls {
		if c.Op == op {
			out = append(out, c)
		}
	}
	return out
}

type fakeEngine struct {
	f     *fakeDurable
	scope string
}

func (e *fakeEngine) record(c durableCall) error {
	c.Scope = e.scope
	e.f.calls = append(e.f.calls, c)
	return e.f.faults.Check(c.Op)
}

func notFound() error { return providererr.New(providererr.NotFound, "not_found", "工作流不存在") }

func (e *fakeEngine) owned(id string) (*fakeWorkflow, error) {
	w := e.f.workflows[id]
	if w == nil || w.scope != e.scope {
		return nil, notFound()
	}
	return w, nil
}

func (e *fakeEngine) StartWorkflow(_ context.Context, typ string, opts durable.StartOptions) (durable.Run, error) {
	e.f.mu.Lock()
	defer e.f.mu.Unlock()
	return durable.Run{}, providererr.New(providererr.Invalid, "unexpected", "Task API 只用 SignalWithStart 启动 Session 工作流")
}

func (e *fakeEngine) SignalWithStart(_ context.Context, typ string, opts durable.StartOptions, signal string, payload proto.Message) (durable.Run, error) {
	e.f.mu.Lock()
	defer e.f.mu.Unlock()
	if err := e.record(durableCall{Op: "SignalWithStart", WorkflowType: typ, Opts: opts, WorkflowID: opts.WorkflowID, Name: signal, Payload: proto.Clone(payload)}); err != nil {
		return durable.Run{}, err
	}
	if w := e.f.workflows[opts.WorkflowID]; w != nil {
		if w.scope != e.scope {
			return durable.Run{}, providererr.New(providererr.Conflict, "id_in_use", "工作流 ID 已被别的作用域使用")
		}
		return durable.Run{WorkflowID: opts.WorkflowID, RunID: "run-1", AlreadyStarted: true}, nil
	}
	e.f.workflows[opts.WorkflowID] = &fakeWorkflow{scope: e.scope, typ: typ, status: durable.StatusRunning}
	return durable.Run{WorkflowID: opts.WorkflowID, RunID: "run-1"}, nil
}

func (e *fakeEngine) Signal(_ context.Context, id, signal string, payload proto.Message) error {
	e.f.mu.Lock()
	defer e.f.mu.Unlock()
	if err := e.record(durableCall{Op: "Signal", WorkflowID: id, Name: signal, Payload: proto.Clone(payload)}); err != nil {
		return err
	}
	_, err := e.owned(id)
	return err
}

func (e *fakeEngine) Query(_ context.Context, id, query string, result proto.Message) error {
	e.f.mu.Lock()
	defer e.f.mu.Unlock()
	if err := e.record(durableCall{Op: "Query", WorkflowID: id, Name: query}); err != nil {
		return err
	}
	w, err := e.owned(id)
	if err != nil {
		return err
	}
	r, ok := w.queries[query]
	if !ok {
		return providererr.New(providererr.Invalid, "unknown_query", "工作流没有这个查询")
	}
	proto.Reset(result)
	proto.Merge(result, r)
	return nil
}

func (e *fakeEngine) Cancel(_ context.Context, id, reason string) error {
	e.f.mu.Lock()
	defer e.f.mu.Unlock()
	if err := e.record(durableCall{Op: "Cancel", WorkflowID: id, Name: reason}); err != nil {
		return err
	}
	_, err := e.owned(id)
	return err
}

func (e *fakeEngine) Describe(_ context.Context, id string) (durable.Description, error) {
	e.f.mu.Lock()
	defer e.f.mu.Unlock()
	if err := e.record(durableCall{Op: "Describe", WorkflowID: id}); err != nil {
		return durable.Description{}, err
	}
	w, err := e.owned(id)
	if err != nil {
		return durable.Description{}, err
	}
	return durable.Description{WorkflowID: id, RunID: "run-1", WorkflowType: w.typ, Status: w.status, StartTime: time.Unix(0, 0)}, nil
}

func (e *fakeEngine) Result(context.Context, string, proto.Message, time.Duration) (durable.Outcome, error) {
	return durable.Outcome{}, providererr.New(providererr.Invalid, "unexpected", "Task API 不等待工作流结束")
}
