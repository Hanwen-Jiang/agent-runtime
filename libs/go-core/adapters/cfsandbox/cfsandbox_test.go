package cfsandbox_test

// D-07：Sandbox 的 Cloudflare 实现（SBX-01 从 Python 的 tests/adapters/test_cloudflare_sandbox.py 搬来）。
//
//   - 对着本机的假桥（cfbridgetest：同一套接口，每个环境一个真的 sandbox-agent 进程）跑与本地实现相同的一致性测试。
//   - 桥的令牌不对时 denied；按域名放行时把 allow_hosts 交给桥，主机名格式不对的 invalid；不能挂工作区（invalid），
//     工作区按存档模式用；runtime 只接桥的镜像登记的。
//   - 设 AR_LIVE_CLOUDFLARE=1（另要 AR_CLOUDFLARE_BRIDGE_URL、AR_CLOUDFLARE_BRIDGE_TOKEN）时对真实的桥再跑一遍一致性测试，
//     并验证 allowlist 之外的主机连不上（花钱、不进 CI）。

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/cfsandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/cfsandbox/cfbridgetest"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance/sandboxtest"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
)

type harness struct {
	p      *cfsandbox.Provider
	faults *testkit.Faults
}

func (h *harness) Provider() sandbox.Provider { return h.p }
func (h *harness) InjectFault(op string, c providererr.Category) error {
	h.faults.FailNext(op, c)
	return nil
}
func (h *harness) WorkspacePath(string, string) string { return "" }

func bridge(t *testing.T) *cfbridgetest.Bridge {
	t.Helper()
	b, err := cfbridgetest.New()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(b.Close)
	return b
}

func provider(t *testing.T, url, token string, runtimes ...string) *cfsandbox.Provider {
	t.Helper()
	p, err := cfsandbox.New(url, token, runtimes, nil)
	if err != nil {
		t.Fatal(err)
	}
	return p
}

func TestCloudflareSandboxAgainstTheFakeBridge(t *testing.T) {
	b := bridge(t)
	sandboxtest.Run(t, func(t *testing.T) sandboxtest.Harness {
		faults := testkit.NewFaults()
		p, err := cfsandbox.New(b.URL, b.Token, nil, faults)
		if err != nil {
			t.Fatal(err)
		}
		return &harness{p: p, faults: faults}
	})
}

func TestCloudflareSandboxLive(t *testing.T) {
	if os.Getenv("AR_LIVE_CLOUDFLARE") != "1" {
		t.Skip("设 AR_LIVE_CLOUDFLARE=1 才对真实的桥跑（花钱）")
	}
	sandboxtest.Run(t, func(t *testing.T) sandboxtest.Harness {
		faults := testkit.NewFaults()
		p, err := cfsandbox.New(os.Getenv("AR_CLOUDFLARE_BRIDGE_URL"), os.Getenv("AR_CLOUDFLARE_BRIDGE_TOKEN"), nil, faults)
		if err != nil {
			t.Fatal(err)
		}
		return &harness{p: p, faults: faults}
	})
}

func TestWrongBridgeTokenIsDenied(t *testing.T) {
	b := bridge(t)
	box := conformance.Must(provider(t, b.URL, strings.Repeat("x", 48)).Bind(conformance.TenantA()))
	_, err := box.Create(context.Background(), sandbox.Spec{})
	conformance.ExpectError(t, err, providererr.Denied, "")
}

func TestAllowlistIsPassedToTheBridge(t *testing.T) {
	b := bridge(t)
	box := conformance.Must(provider(t, b.URL, b.Token).Bind(conformance.TenantA()))
	ctx := context.Background()
	env := conformance.Must(box.Create(ctx, sandbox.Spec{Network: "allowlist", AllowHosts: []string{"pypi.org", "*.pythonhosted.org"}, IdleTimeout: 5 * time.Minute}))
	created := b.Created()
	last := created[len(created)-1]
	hosts, _ := last["allow_hosts"].([]any)
	if len(hosts) != 2 || hosts[0] != "pypi.org" || hosts[1] != "*.pythonhosted.org" || last["idle_seconds"] != float64(300) {
		t.Fatalf("桥收到 %v", last)
	}
	conformance.NoError(t, box.Destroy(ctx, env.ID))
}

func TestBadAllowHostsAreInvalid(t *testing.T) {
	b := bridge(t)
	box := conformance.Must(provider(t, b.URL, b.Token).Bind(conformance.TenantA()))
	for _, hosts := range [][]string{{}, {"not a host"}, {"https://pypi.org"}, {strings.Repeat("x", 300) + ".com"}} {
		_, err := box.Create(context.Background(), sandbox.Spec{Network: "allowlist", AllowHosts: hosts})
		conformance.ExpectError(t, err, providererr.Invalid, "")
	}
}

func TestWorkspaceCannotBeMounted(t *testing.T) {
	// 工作区不挂卷：按存档模式用（D-06，Sandbox Service 建环境时 WorkspaceID 置空）。
	b := bridge(t)
	box := conformance.Must(provider(t, b.URL, b.Token).Bind(conformance.TenantA()))
	_, err := box.Create(context.Background(), sandbox.Spec{WorkspaceID: "w1"})
	conformance.ExpectError(t, err, providererr.Invalid, "unsupported_workspace")
}

func TestRuntimesFollowTheBridgeImage(t *testing.T) {
	// 桥的镜像在部署时定：默认只有 python；登记成 universal 镜像的桥也接 universal（里面有 python），别的仍拒绝。
	b := bridge(t)
	ctx := context.Background()
	def := conformance.Must(provider(t, b.URL, b.Token).Bind(conformance.TenantA()))
	_, err := def.Create(ctx, sandbox.Spec{Runtime: "universal"})
	conformance.ExpectError(t, err, providererr.Invalid, "unsupported_runtime")
	universal := conformance.Must(provider(t, b.URL, b.Token, "universal", "python").Bind(conformance.TenantA()))
	env := conformance.Must(universal.Create(ctx, sandbox.Spec{Runtime: "universal"}))
	conformance.NoError(t, universal.Destroy(ctx, env.ID))
	_, err = universal.Create(ctx, sandbox.Spec{Runtime: "go"})
	conformance.ExpectError(t, err, providererr.Invalid, "unsupported_runtime")
}

func TestBridgeMustBeHTTPS(t *testing.T) {
	if _, err := cfsandbox.New("http://example.com", strings.Repeat("x", 48), nil, nil); err == nil {
		t.Fatal("http 的桥地址要拒绝")
	}
}

func TestPlatformListsEnvironments(t *testing.T) {
	b := bridge(t)
	p := provider(t, b.URL, b.Token)
	ctx := context.Background()
	env := conformance.Must(conformance.Must(p.Bind(conformance.TenantA())).Create(ctx, sandbox.Spec{}))
	listed := conformance.Must(p.ListEnvironments(ctx))
	if !slices.ContainsFunc(listed, func(e sandbox.ListedEnvironment) bool { return e.ID == env.ID && e.Owner == conformance.TenantAID }) {
		t.Fatalf("listed = %+v", listed)
	}
}

func TestLiveAllowlistBlocksOtherHosts(t *testing.T) {
	if os.Getenv("AR_LIVE_CLOUDFLARE") != "1" {
		t.Skip("设 AR_LIVE_CLOUDFLARE=1 才对真实的桥跑（花钱）")
	}
	box := conformance.Must(provider(t, os.Getenv("AR_CLOUDFLARE_BRIDGE_URL"), os.Getenv("AR_CLOUDFLARE_BRIDGE_TOKEN")).Bind(conformance.TenantA()))
	ctx := context.Background()
	env := conformance.Must(box.Create(ctx, sandbox.Spec{Network: "allowlist", AllowHosts: []string{"pypi.org"}}))
	defer box.Destroy(ctx, env.ID)
	code := "import urllib.request\n" +
		"for url in ['https://pypi.org/simple/', 'https://example.com/']:\n" +
		"    try:\n" +
		"        print(url, urllib.request.urlopen(url, timeout=10).status)\n" +
		"    except Exception as e:\n" +
		"        print(url, 'blocked', type(e).__name__)\n"
	res := conformance.Must(box.Exec(ctx, env.ID, []string{"python", "-c", code}, sandbox.ExecOptions{Timeout: 60 * time.Second}))
	out := string(res.Stdout)
	if !strings.Contains(out, "https://pypi.org/simple/ 200") || !strings.Contains(out, "https://example.com/ blocked") {
		t.Fatal(out)
	}
}

func TestScopedTransferBatchGrantAndFallback(t *testing.T) {
	token := strings.Repeat("t", 32)
	envID := "cfs-0123456789abcdef"
	id1 := "sbg-" + strings.Repeat("a", 32)
	id2 := "sbg-" + strings.Repeat("b", 32)

	t.Run("batch_endpoint", func(t *testing.T) {
		var batchCalls atomic.Int32
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/v1/sandboxes/"+envID+"/transfer-grants:batch" {
				t.Fatalf("unexpected path %s", r.URL.Path)
			}
			batchCalls.Add(1)
			var payload struct {
				Action string            `json:"action"`
				Items  []json.RawMessage `json:"items"`
				IDs    []string          `json:"ids"`
			}
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				t.Fatal(err)
			}
			switch payload.Action {
			case "grant":
				_ = json.NewEncoder(w).Encode(map[string]any{
					"urls": map[string]string{
						id1: "http://ar-transfer.internal/" + id1,
						id2: "http://ar-transfer.internal/" + id2,
					},
				})
			case "revoke":
				_ = json.NewEncoder(w).Encode(map[string]any{
					"bytes":   map[string]int64{id1: 11, id2: 22},
					"revoked": true,
				})
			case "ack":
				w.WriteHeader(http.StatusNoContent)
			default:
				t.Fatalf("unexpected action %q", payload.Action)
			}
		}))
		defer srv.Close()

		box := conformance.Must(provider(t, srv.URL, token).Bind(conformance.TenantA())).(sandbox.ScopedTransferBatchNetwork)
		ctx := context.Background()
		reqs := []sandbox.TransferGrantRequest{
			{ID: id1, Task: "t", Method: "PUT", SizeBytes: 11, Expires: time.Now().Add(time.Minute), Target: sandbox.TransferTarget{URL: "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/b/1"}},
			{ID: id2, Task: "t", Method: "PUT", SizeBytes: 22, Expires: time.Now().Add(time.Minute), Target: sandbox.TransferTarget{URL: "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/b/2"}},
		}
		targets, err := box.GrantTransferTargets(ctx, envID, reqs)
		if err != nil || len(targets) != 2 || targets[0].URL != "http://ar-transfer.internal/"+id1 {
			t.Fatalf("batch grant failed: targets=%+v err=%v", targets, err)
		}
		used, err := box.RevokeTransferTargets(ctx, envID, []string{id1, id2})
		if err != nil || used[id1] != 11 || used[id2] != 22 {
			t.Fatalf("batch revoke failed: used=%+v err=%v", used, err)
		}
		if err := box.AcknowledgeTransferTargets(ctx, envID, []string{id1, id2}); err != nil {
			t.Fatalf("batch ack failed: %v", err)
		}
		if batchCalls.Load() != 3 {
			t.Fatalf("expected 3 batch RTTs, got %d", batchCalls.Load())
		}
	})

	t.Run("fallback_on_404", func(t *testing.T) {
		var singleCalls atomic.Int32
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if strings.HasSuffix(r.URL.Path, ":batch") {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			singleCalls.Add(1)
			switch r.Method {
			case http.MethodPut:
				id := strings.TrimPrefix(r.URL.Path, "/v1/sandboxes/"+envID+"/transfer-grants/")
				_ = json.NewEncoder(w).Encode(map[string]string{"url": "http://ar-transfer.internal/" + id})
			case http.MethodDelete:
				_ = json.NewEncoder(w).Encode(map[string]any{"bytes": 7, "revoked": true})
			case http.MethodPost:
				w.WriteHeader(http.StatusNoContent)
			default:
				t.Fatalf("unexpected method %s", r.Method)
			}
		}))
		defer srv.Close()

		box := conformance.Must(provider(t, srv.URL, token).Bind(conformance.TenantA())).(sandbox.ScopedTransferBatchNetwork)
		ctx := context.Background()
		reqs := []sandbox.TransferGrantRequest{
			{ID: id1, Task: "t", Method: "GET", SizeBytes: 7, Expires: time.Now().Add(time.Minute), Target: sandbox.TransferTarget{URL: "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/b/1"}},
			{ID: id2, Task: "t", Method: "GET", SizeBytes: 7, Expires: time.Now().Add(time.Minute), Target: sandbox.TransferTarget{URL: "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/b/2"}},
		}
		targets, err := box.GrantTransferTargets(ctx, envID, reqs)
		if err != nil || len(targets) != 2 {
			t.Fatalf("fallback grant failed: %v", err)
		}
		used, err := box.RevokeTransferTargets(ctx, envID, []string{id1, id2})
		if err != nil || used[id1] != 7 || used[id2] != 7 {
			t.Fatalf("fallback revoke failed: %+v %v", used, err)
		}
		if err := box.AcknowledgeTransferTargets(ctx, envID, []string{id1, id2}); err != nil {
			t.Fatalf("fallback ack failed: %v", err)
		}
		if singleCalls.Load() != 6 {
			t.Fatalf("expected 6 fallback single-grant calls, got %d", singleCalls.Load())
		}
	})
}
