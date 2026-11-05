package localsandbox_test

import (
	"path/filepath"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/conformance/sandboxtest"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/localsandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
)

type harness struct {
	p          *localsandbox.Provider
	faults     *testkit.Faults
	workspaces string
}

func (h *harness) Provider() sandbox.Provider { return h.p }
func (h *harness) InjectFault(op string, c providererr.Category) error {
	h.faults.FailNext(op, c)
	return nil
}
func (h *harness) WorkspacePath(tenant, ws string) string { return filepath.Join(h.workspaces, tenant, ws) }

func TestLocalSandboxConformance(t *testing.T) {
	agent, err := testkit.SandboxAgentBinary()
	if err != nil {
		t.Fatal(err)
	}
	sandboxtest.Run(t, func(t *testing.T) sandboxtest.Harness {
		root := t.TempDir()
		faults := testkit.NewFaults()
		p, err := localsandbox.New(localsandbox.Options{Root: filepath.Join(root, "envs"), Workspaces: filepath.Join(root, "workspaces"), AgentBinary: agent, Faults: faults})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { localsandbox.KillAll(filepath.Join(root, "envs")) })
		return &harness{p: p, faults: faults, workspaces: filepath.Join(root, "workspaces")}
	})
}
