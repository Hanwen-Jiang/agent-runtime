package fcsandbox_test

// D-20：Sandbox 的 Firecracker 实现对着假的 fc-manager（cfbridgetest.NewFCManager：同一套接口，每个环境一个本机的
// sandbox-agent 进程）跑与其他实现相同的一致性测试。真 VM 只在独服上（services/rs-fc-manager 的 tests/real_vm.rs）。

import (
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/cfsandbox/cfbridgetest"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/fcsandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance/sandboxtest"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
)

type harness struct {
	p      *fcsandbox.Provider
	faults *testkit.Faults
}

func (h *harness) Provider() sandbox.Provider { return h.p }
func (h *harness) InjectFault(op string, c providererr.Category) error {
	h.faults.FailNext(op, c)
	return nil
}
func (h *harness) WorkspacePath(string, string) string { return "" }

func TestFirecrackerSandboxAgainstAFakeManager(t *testing.T) {
	m, err := cfbridgetest.NewFCManager()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(m.Close)
	sandboxtest.Run(t, func(t *testing.T) sandboxtest.Harness {
		faults := testkit.NewFaults()
		p, err := fcsandbox.New(fcsandbox.Config{ManagerURL: m.URL}, m.Token, faults)
		if err != nil {
			t.Fatal(err)
		}
		return &harness{p: p, faults: faults}
	})
}
