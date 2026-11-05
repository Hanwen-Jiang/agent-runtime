package sandboxagent

// 取消一次 Exec（docs/plan/running-task-interaction.md 第 2 节；从 Python 的 tests/adapters/test_sandbox_exec_cancel.py 搬来）：
// 调用方取消时断开到 agent 的连接，另发 /v1/exec/cancel；agent 没确认停下（旧的 agent 没有取消接口是 404，经桥时 503）时
// 错误里带 sandbox.ErrExecNotStopped，调用方不能当命令停了。每个后端的命令真的停了在一致性测试里（conformance/sandboxtest）。

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
)

// fakeAgent 收到 /v1/exec 后不回答，一直等到连接断开；/v1/exec/cancel 按 cancelStatus 回答。
type fakeAgent struct {
	*httptest.Server
	started      chan struct{}
	disconnected chan struct{}
	cancels      chan string
}

func newFakeAgent(t *testing.T, cancelStatus int) *fakeAgent {
	a := &fakeAgent{started: make(chan struct{}, 1), disconnected: make(chan struct{}, 1), cancels: make(chan string, 1)}
	a.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if r.Header.Get(TokenHeader) != "t-token" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		switch r.URL.Path {
		case "/v1/exec/cancel":
			a.cancels <- string(body)
			w.WriteHeader(cancelStatus)
		case "/v1/exec":
			a.started <- struct{}{}
			select {
			case <-r.Context().Done():
				a.disconnected <- struct{}{}
			case <-time.After(30 * time.Second):
			}
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(a.Close)
	return a
}

func (a *fakeAgent) endpoint() Endpoint { return Endpoint{BaseURL: a.URL, Token: "t-token"} }

func wait(t *testing.T, ch <-chan struct{}, what string) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(10 * time.Second):
		t.Fatal(what)
	}
}

func TestCancellingExecClosesTheConnectionToTheAgent(t *testing.T) {
	agent := newFakeAgent(t, http.StatusNoContent)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		_, err := Client{}.Exec(ctx, agent.endpoint(), []string{"sleep", "600"}, sandbox.ExecOptions{Timeout: 600 * time.Second})
		done <- err
	}()
	wait(t, agent.started, "命令没有发到 agent")
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("取消之后 err = %v，要 context.Canceled", err)
		}
		if errors.Is(err, sandbox.ErrExecNotStopped) {
			t.Fatal("agent 确认了停下，不该带 ErrExecNotStopped")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("取消之后 Exec 没有返回")
	}
	wait(t, agent.disconnected, "连接没断：agent 不知道调用方走了，命令会一直跑")
	select {
	case body := <-agent.cancels:
		if body == "" {
			t.Fatal("取消请求没带 exec_id")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("没有另发 /v1/exec/cancel")
	}
}

func TestTheCallerLearnsWhetherTheAgentConfirmedTheStop(t *testing.T) {
	for _, tc := range []struct {
		status    int
		confirmed bool
	}{{http.StatusNoContent, true}, {http.StatusNotFound, false}, {http.StatusServiceUnavailable, false}} {
		t.Run(http.StatusText(tc.status), func(t *testing.T) {
			agent := newFakeAgent(t, tc.status)
			ctx, cancel := context.WithCancel(context.Background())
			done := make(chan error, 1)
			go func() {
				_, err := Client{}.Exec(ctx, agent.endpoint(), []string{"sleep", "600"}, sandbox.ExecOptions{Timeout: 600 * time.Second})
				done <- err
			}()
			wait(t, agent.started, "命令没有发到 agent")
			cancel()
			err := <-done
			if errors.Is(err, sandbox.ErrExecNotStopped) == tc.confirmed {
				t.Fatalf("cancel 回 %d：err = %v", tc.status, err)
			}
			if !errors.Is(err, context.Canceled) {
				t.Fatalf("err = %v，要能认出是取消", err)
			}
		})
	}
}

func TestStatusCodesMapToErrorCategories(t *testing.T) {
	for status, want := range map[int]providererr.Category{
		400: providererr.Invalid, 409: providererr.Invalid, 413: providererr.Invalid, 422: providererr.Invalid,
		404: providererr.NotFound, 401: providererr.Denied, 403: providererr.Denied,
		429: providererr.Transient, 500: providererr.Transient, 503: providererr.Transient,
	} {
		if got := providererr.CategoryOf(RaiseFor(status, []byte(`{"code":"x","message":"m"}`))); got != want {
			t.Errorf("RaiseFor(%d) = %v，要 %v", status, got, want)
		}
	}
	if err := RaiseFor(200, nil); err != nil {
		t.Fatalf("RaiseFor(200) = %v", err)
	}
}
