// DurableEngine 的开发实现：Temporal 适配器 + Temporal 本地开发服务（runtime-quickref 第 6 节）。
// 适配器与生产实现是同一份代码，只是连的服务不同。开发服务用 PATH 上的 temporal（Temporal CLI）启动。
package temporaladapter_test

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"os/exec"
	"strings"
	"testing"
	"time"

	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/wrapperspb"

	temporaladapter "github.com/agentruntime/agent-runtime/libs/go-core/adapters/temporal"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance/durabletest"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/durable"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// echoWorkflow 与 failWorkflow 的语义见 durabletest 的包注释。
func echoWorkflow(ctx workflow.Context, start *wrapperspb.StringValue) (*wrapperspb.StringValue, error) {
	values := []string{start.GetValue()}
	if err := workflow.SetQueryHandler(ctx, "state", func() (*wrapperspb.StringValue, error) {
		return wrapperspb.String(strings.Join(values, ",")), nil
	}); err != nil {
		return nil, err
	}
	appendCh := workflow.GetSignalChannel(ctx, "append")
	finishCh := workflow.GetSignalChannel(ctx, "finish")
	finished, cancelled := false, false
	for !finished && !cancelled {
		sel := workflow.NewSelector(ctx)
		sel.AddReceive(appendCh, func(c workflow.ReceiveChannel, _ bool) {
			var v *wrapperspb.StringValue
			c.Receive(ctx, &v)
			values = append(values, v.GetValue())
		})
		sel.AddReceive(finishCh, func(c workflow.ReceiveChannel, _ bool) {
			c.Receive(ctx, nil)
			finished = true
		})
		sel.AddReceive(ctx.Done(), func(workflow.ReceiveChannel, bool) { cancelled = true })
		sel.Select(ctx)
	}
	if cancelled {
		return nil, temporal.NewCanceledError()
	}
	return wrapperspb.String(strings.Join(values, ",")), nil
}

func failWorkflow(workflow.Context) (*wrapperspb.StringValue, error) {
	return nil, temporal.NewNonRetryableApplicationError("boom", "conformance", nil)
}

// faultyProvider 在真实客户端前加一层故障注入。
type faultyProvider struct {
	inner  durable.Provider
	faults *testkit.Faults
}

func (p faultyProvider) Bind(t *commonv1.TenantContext) (durable.Engine, error) {
	e, err := p.inner.Bind(t)
	return faultyEngine{e, p.faults}, err
}

func (p faultyProvider) BindPlatform(purpose string) (durable.Engine, error) {
	e, err := p.inner.BindPlatform(purpose)
	return faultyEngine{e, p.faults}, err
}

func (p faultyProvider) Close() error { return p.inner.Close() }

type faultyEngine struct {
	inner  durable.Engine
	faults *testkit.Faults
}

func (e faultyEngine) StartWorkflow(ctx context.Context, wt string, o durable.StartOptions) (durable.Run, error) {
	if err := e.faults.Check("StartWorkflow"); err != nil {
		return durable.Run{}, err
	}
	return e.inner.StartWorkflow(ctx, wt, o)
}

func (e faultyEngine) SignalWithStart(ctx context.Context, wt string, o durable.StartOptions, s string, p proto.Message) (durable.Run, error) {
	if err := e.faults.Check("SignalWithStart"); err != nil {
		return durable.Run{}, err
	}
	return e.inner.SignalWithStart(ctx, wt, o, s, p)
}

func (e faultyEngine) Signal(ctx context.Context, id, s string, p proto.Message) error {
	if err := e.faults.Check("Signal"); err != nil {
		return err
	}
	return e.inner.Signal(ctx, id, s, p)
}

func (e faultyEngine) Query(ctx context.Context, id, q string, r proto.Message) error {
	if err := e.faults.Check("Query"); err != nil {
		return err
	}
	return e.inner.Query(ctx, id, q, r)
}

func (e faultyEngine) Cancel(ctx context.Context, id, reason string) error {
	if err := e.faults.Check("Cancel"); err != nil {
		return err
	}
	return e.inner.Cancel(ctx, id, reason)
}

func (e faultyEngine) Describe(ctx context.Context, id string) (durable.Description, error) {
	if err := e.faults.Check("Describe"); err != nil {
		return durable.Description{}, err
	}
	return e.inner.Describe(ctx, id)
}

func (e faultyEngine) Result(ctx context.Context, id string, r proto.Message, timeout time.Duration) (durable.Outcome, error) {
	if err := e.faults.Check("Result"); err != nil {
		return durable.Outcome{}, err
	}
	return e.inner.Result(ctx, id, r, timeout)
}

type harness struct {
	provider durable.Provider
	queue    string
	faults   *testkit.Faults
}

func (h *harness) Provider() durable.Provider { return h.provider }
func (h *harness) TaskQueue() string          { return h.queue }
func (h *harness) InjectFault(op string, c providererr.Category) error {
	h.faults.FailNext(op, c)
	return nil
}

func TestConformance(t *testing.T) {
	ctx := context.Background()
	// 用本机装好的 Temporal CLI，不让 SDK 自行下载。
	path, err := exec.LookPath("temporal")
	if err != nil {
		t.Fatal("找不到 temporal 可执行文件（Temporal CLI），见 CLAUDE.md 的常用命令")
	}
	server, err := testsuite.StartDevServer(ctx, testsuite.DevServerOptions{ExistingPath: path, LogLevel: "error"})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = server.Stop() }()
	client := server.Client()

	b := make([]byte, 4)
	_, _ = rand.Read(b)
	queue := "conformance-" + hex.EncodeToString(b)
	w := worker.New(client, queue, worker.Options{})
	w.RegisterWorkflowWithOptions(echoWorkflow, workflow.RegisterOptions{Name: "conformance.Echo"})
	w.RegisterWorkflowWithOptions(failWorkflow, workflow.RegisterOptions{Name: "conformance.Fail"})
	if err := w.Start(); err != nil {
		t.Fatal(err)
	}
	defer w.Stop()

	durabletest.Run(t, func(t *testing.T) durabletest.Harness {
		faults := testkit.NewFaults()
		return &harness{provider: faultyProvider{temporaladapter.New(client), faults}, queue: queue, faults: faults}
	})
}
