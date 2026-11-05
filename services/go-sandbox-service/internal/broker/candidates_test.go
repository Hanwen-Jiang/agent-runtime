package broker

// 回退的候选（D-12，SBX-01 从 Worker 的 tests/test_sandbox_quota.py 搬来）：绑定工作区时只在与首选共用工作区的后端之间回退
// （同一个 PVC，或同为存档模式）；不绑工作区时照常按回退列表；停用的、隔离强度低于下限的跳过并说明。

import (
	"slices"
	"strings"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/registry"
)

func TestWorkspaceBoundFallbackStaysOnTheSameVolume(t *testing.T) {
	reg, err := registry.Parse([]byte(`{"default": "gvisor", "backends": {
	  "gvisor": {"kind": "k8s", "isolation": "container", "workspace_claim": "workspaces"},
	  "kata": {"kind": "k8s", "isolation": "microvm", "workspace_claim": "workspaces"},
	  "cloudflare": {"kind": "cloudflare", "isolation": "microvm", "bridge_url": "https://bridge.example.workers.dev"}}}`), registry.Options{})
	if err != nil {
		t.Fatal(err)
	}
	b := New(Options{Registry: reg})
	policy := registry.FromDocument(map[string]any{"sandbox": map[string]any{"backend": "gvisor", "fallback": []any{"cloudflare", "kata"}}}, "gvisor")
	names, skipped := b.candidates(policy, sandbox.Spec{WorkspaceID: "w1"}, false)
	if !slices.Equal(names, []string{"gvisor", "kata"}) || !slices.ContainsFunc(skipped, func(s string) bool {
		return strings.Contains(s, "cloudflare") && strings.Contains(s, "工作区")
	}) {
		t.Fatalf("绑定工作区：%v，跳过 %v", names, skipped)
	}
	if names, _ = b.candidates(policy, sandbox.Spec{}, false); !slices.Equal(names, []string{"gvisor", "cloudflare", "kata"}) {
		t.Fatalf("不绑工作区时照常回退：%v", names)
	}
}
