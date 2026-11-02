package taskapi

// S-07 支撑：开发用的租户策略文件。故障注入套件里只有写操作租户能跑写节点，其余租户的 effect 上限仍是 read；
// 租户来自认证身份，请求无法提高自己的上限。

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"connectrpc.com/connect"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
)

var fallback = Defaults{Budget: &workflowv1.Budget{UsdMicros: 2_000_000}, EffectCeiling: commonv1.Effect_EFFECT_READ}

func writePolicies(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "tenant_policies.json")
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func resolveFor(t *testing.T, r PolicyResolver, tenantID string) (Defaults, error) {
	t.Helper()
	return r.Resolve(context.Background(), &commonv1.TenantContext{TenantId: tenantID})
}

func TestFilePolicyResolverOverridesListedTenantsOnly(t *testing.T) {
	r := FilePolicyResolver{Path: writePolicies(t, `{"tenants": {"tw": {"effect_ceiling": "write_irreversible"}}}`), Fallback: fallback}
	tw, err := resolveFor(t, r, "tw")
	if err != nil || tw.EffectCeiling != commonv1.Effect_EFFECT_WRITE_IRREVERSIBLE {
		t.Fatalf("列出的租户用文件里的上限，实际 %v / %v", tw.EffectCeiling, err)
	}
	if tw.Budget.GetUsdMicros() != 2_000_000 {
		t.Errorf("没写的字段沿用默认值，实际 %v", tw.Budget)
	}
	other, err := resolveFor(t, r, "t1")
	if err != nil || other.EffectCeiling != commonv1.Effect_EFFECT_READ {
		t.Errorf("没列出的租户用默认值，实际 %v / %v", other.EffectCeiling, err)
	}
	tw.Budget.UsdMicros = 1
	if again, _ := resolveFor(t, r, "tw"); again.Budget.GetUsdMicros() != 2_000_000 {
		t.Error("返回的预算是副本，调用方改了不影响默认值")
	}
}

func TestFilePolicyResolverRereadsAndFallsBack(t *testing.T) {
	missing := FilePolicyResolver{Path: filepath.Join(t.TempDir(), "absent.json"), Fallback: fallback}
	if d, err := resolveFor(t, missing, "tw"); err != nil || d.EffectCeiling != commonv1.Effect_EFFECT_READ {
		t.Errorf("文件不存在时用默认值，实际 %v / %v", d.EffectCeiling, err)
	}
	path := writePolicies(t, `{"tenants": {}}`)
	r := FilePolicyResolver{Path: path, Fallback: fallback}
	if d, _ := resolveFor(t, r, "tw"); d.EffectCeiling != commonv1.Effect_EFFECT_READ {
		t.Fatal("改文件之前是默认值")
	}
	if err := os.WriteFile(path, []byte(`{"tenants": {"tw": {"effect_ceiling": "write_reversible"}}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if d, _ := resolveFor(t, r, "tw"); d.EffectCeiling != commonv1.Effect_EFFECT_WRITE_REVERSIBLE {
		t.Errorf("每次都重新读文件，实际 %v", d.EffectCeiling)
	}
}

// 开了 Registry：effect 上限与预算按 Registry 的租户策略（没有自己策略的租户是平台默认的）；开发用的文件里列出的租户以文件为准。
func TestRegistryPolicyGivesEffectCeilingAndBudget(t *testing.T) {
	docs := map[string]string{
		"open":    `{"effect_ceiling": "write_reversible", "budget": {"usd": 0.5, "tool_calls": 20, "wall_clock_min": 10}, "sandbox": {"backend": "kata"}}`,
		"partial": `{"sandbox": {"backend": "gvisor"}}`,
		"broken":  `{"effect_ceiling": "everything"}`,
	}
	source := func(_ context.Context, tenant *commonv1.TenantContext) ([]byte, bool, error) {
		switch tenant.GetTenantId() {
		case "down":
			return nil, false, providererr.New(providererr.Transient, "unavailable", "Registry 连不上")
		case "none":
			return nil, false, nil
		}
		return []byte(docs[tenant.GetTenantId()]), true, nil
	}
	reg := DocumentPolicyResolver{Source: source, Fallback: fallback}
	open, err := resolveFor(t, reg, "open")
	if err != nil || open.EffectCeiling != commonv1.Effect_EFFECT_WRITE_REVERSIBLE {
		t.Fatalf("effect 上限取自策略：%v（%v）", open.EffectCeiling, err)
	}
	if b := open.Budget; b.GetUsdMicros() != 500_000 || b.GetToolCalls() != 20 || b.GetWallClock().AsDuration().Minutes() != 10 {
		t.Errorf("预算取自策略：%v", b)
	}
	if fallback.Budget.GetUsdMicros() != 2_000_000 {
		t.Error("改的是副本，默认值不变")
	}
	for _, id := range []string{"partial", "none"} {
		if d, err := resolveFor(t, reg, id); err != nil || d.EffectCeiling != commonv1.Effect_EFFECT_READ || d.Budget.GetUsdMicros() != 2_000_000 {
			t.Errorf("%s：没写的项、没有策略时用默认值：%v（%v）", id, d, err)
		}
	}
	if _, err := resolveFor(t, reg, "broken"); providererr.CategoryOf(err) != providererr.Invalid {
		t.Errorf("写错的策略 invalid：%v", err)
	}
	if _, err := resolveFor(t, reg, "down"); providererr.CategoryOf(err) != providererr.Transient {
		t.Errorf("Registry 不可用时 transient（准入失败，不按默认值放行）：%v", err)
	}

	chain := FilePolicyResolver{Path: writePolicies(t, `{"tenants": {"open": {"effect_ceiling": "read"}}}`), Fallback: fallback, Else: reg}
	if d, _ := resolveFor(t, chain, "open"); d.EffectCeiling != commonv1.Effect_EFFECT_READ {
		t.Errorf("文件里列出的租户以文件为准：%v", d.EffectCeiling)
	}
	missing := FilePolicyResolver{Path: filepath.Join(t.TempDir(), "absent.json"), Fallback: fallback, Else: reg}
	if d, _ := resolveFor(t, missing, "open"); d.EffectCeiling != commonv1.Effect_EFFECT_WRITE_REVERSIBLE {
		t.Errorf("没列出的租户按 Registry：%v", d.EffectCeiling)
	}
}

func TestFilePolicyResolverRejectsMalformedPolicies(t *testing.T) {
	for _, body := range []string{`{not json`, `{"tenants": {"tw": {"effect_ceiling": "everything"}}}`, `{"tenants": {"tw": {"effect_ceiling": "unspecified"}}}`} {
		r := FilePolicyResolver{Path: writePolicies(t, body), Fallback: fallback}
		if _, err := resolveFor(t, r, "tw"); providererr.CategoryOf(err) != providererr.Invalid {
			t.Errorf("%s 应报 invalid，实际 %v", body, err)
		}
	}
}

func TestSubmitUsesTheAuthenticatedTenantsPolicy(t *testing.T) {
	path := writePolicies(t, `{"tenants": {"tw": {"effect_ceiling": "write_irreversible"}}}`)
	f := newAPIWith(t, func(d *Deps) { d.Policies = FilePolicyResolver{Path: path, Fallback: d.Defaults} })

	f.mustSubmit(f.token("tw", "u1"), newRequest("c-tw"))
	_, sig := f.lastSubmit()
	if got := sig.GetTask().GetEffectCeiling(); got != commonv1.Effect_EFFECT_WRITE_IRREVERSIBLE {
		t.Errorf("写操作租户的 Task 带它的 effect 上限，实际 %v", got)
	}
	if sig.GetTask().GetBudget().GetUsdMicros() != 2_000_000 {
		t.Errorf("预算沿用默认值，实际 %v", sig.GetTask().GetBudget())
	}

	// 别的租户在请求里冒充 tw 不行：tenant_id 只用来校验一致。
	req := newRequest("c-t1")
	req.TenantId = "tw"
	if _, err := f.client.SubmitTask(context.Background(), authed(f.token("t1", "u1"), req)); err == nil {
		t.Fatal("请求里的 tenant_id 与身份不一致应被拒绝")
	}
	f.mustSubmit(f.token("t1", "u1"), newRequest("c-t1"))
	_, sig = f.lastSubmit()
	if got := sig.GetTask().GetEffectCeiling(); got != commonv1.Effect_EFFECT_READ {
		t.Errorf("其他租户仍是默认的 read，实际 %v", got)
	}
}

// D-08：请求里的 effect 上限只能调低；调高被拒（denied，不悄悄截断），被拒的请求不留 Task。
func TestSubmitEffectCeilingCanOnlyLowerThePolicy(t *testing.T) {
	path := writePolicies(t, `{"tenants": {"tw": {"effect_ceiling": "write_reversible"}}}`)
	f := newAPIWith(t, func(d *Deps) { d.Policies = FilePolicyResolver{Path: path, Fallback: d.Defaults} })
	token := f.token("tw", "u1")

	req := newRequest("c-low")
	req.EffectCeiling = commonv1.Effect_EFFECT_READ
	first := f.mustSubmit(token, req)
	if _, sig := f.lastSubmit(); sig.GetTask().GetEffectCeiling() != commonv1.Effect_EFFECT_READ {
		t.Errorf("调低到 read，实际 %v", sig.GetTask().GetEffectCeiling())
	}

	req = newRequest("c-same")
	req.EffectCeiling = commonv1.Effect_EFFECT_WRITE_REVERSIBLE
	f.mustSubmit(token, req)
	if _, sig := f.lastSubmit(); sig.GetTask().GetEffectCeiling() != commonv1.Effect_EFFECT_WRITE_REVERSIBLE {
		t.Errorf("与策略相同可以，实际 %v", sig.GetTask().GetEffectCeiling())
	}

	before := len(f.durable.callsOf("SignalWithStart"))
	req = newRequest("c-high")
	req.EffectCeiling = commonv1.Effect_EFFECT_WRITE_IRREVERSIBLE
	_, err := f.submit(token, req)
	wantCode(t, "调高", err, connect.CodePermissionDenied)
	if after := len(f.durable.callsOf("SignalWithStart")); after != before {
		t.Errorf("被拒的请求不应投递给 Session 工作流")
	}
	// This test exercises effect policy, so free one slot before the third
	// valid submission. The separate admission tests cover the two-task cap.
	finishSeededTask(t, f, "tw", first.GetTaskId())
	req.EffectCeiling = commonv1.Effect_EFFECT_READ // 同一个 client_request_id 改低了再提交：照常受理
	f.mustSubmit(token, req)

	req = newRequest("c-bogus")
	req.EffectCeiling = commonv1.Effect(42)
	_, err = f.submit(token, req)
	wantCode(t, "不认识的 effect", err, connect.CodeInvalidArgument)
}
