package router_test

// 验收（docs/plan/model-gateway-direct.md）：选档——默认 Tier、显式 Tier、pinned、avoid_vendor、白名单、无可用模型各有测试；
// 难点“确定性的选档”：同样的输入与 Profile 选出同一个模型（单价相同按 id）。

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strings"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"

	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/profiles"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/router"
)

// profileJSON 构造一个 Profile 的 JSON。
func profileJSON(id, vendor, class string, tiers []string, caps []string, in, out float64) string {
	var ts []string
	for _, name := range tiers {
		budget := 0
		switch name {
		case "medium":
			budget = 8192
		case "high":
			budget = 32768
		}
		ts = append(ts, fmt.Sprintf(`{"name":%q,"effort":"","thinking_budget":%d}`, name, budget))
	}
	var cs []string
	for _, c := range caps {
		cs = append(cs, fmt.Sprintf("%q", c))
	}
	return fmt.Sprintf(`{"id":%q,"vendor":%q,"model":%q,"class":%q,"tiers":[%s],"capabilities":[%s],`+
		`"cost":{"in_per_m":%v,"out_per_m":%v,"cache_read_per_m":0},"constraints":{"context_window":128000,"max_output":8192},`+
		`"freshness":{"price_at":"2026-10-21"}}`,
		id, vendor, strings.TrimPrefix(id, "x/"), class, strings.Join(ts, ","), strings.Join(cs, ","), in, out)
}

var (
	full  = []string{"generate", "structured", "tools", "stream"}
	plain = []string{"generate", "stream"}
	low   = []string{"low"}
	both  = []string{"medium", "high"}
)

// fixture 是选档测试用的 Profile：每档都有两家，单价与能力各不相同。
var fixture = []string{
	profileJSON("x/small-openai", "openai", "small", low, full, 0.15, 0.6),
	profileJSON("x/small-google", "google", "small", low, plain, 0.10, 0.40), // 更便宜，但没有 structured、tools
	profileJSON("x/mid-deepseek", "deepseek", "mid", low, full, 0.28, 0.42),
	profileJSON("x/mid-google", "google", "mid", low, full, 0.50, 3.0),
	profileJSON("x/strong-anthropic", "anthropic", "strong", both, full, 3, 15),
	profileJSON("x/strong-openai", "openai", "strong", both, full, 1.75, 14),
	profileJSON("x/embed-openai", "openai", "embed", []string{"default"}, []string{"embed"}, 0.02, 0),
}

func catalogOf(t *testing.T, items []string) *profiles.Catalog {
	t.Helper()
	c, err := profiles.Parse([]byte(`{"version":1,"profiles":[` + strings.Join(items, ",") + `]}`))
	if err != nil {
		t.Fatalf("测试用 Profile 应能解析：%v", err)
	}
	return c
}

// staticAllowlist 是测试用的白名单：没列出的租户不限制。
type staticAllowlist map[string][]string

func (s staticAllowlist) ModelAllowlist(_ context.Context, tenantID string) ([]string, bool, error) {
	ids, ok := s[tenantID]
	return ids, ok, nil
}

func newRouter(t *testing.T, allow router.Allowlist) *router.Router {
	t.Helper()
	return router.New(catalogOf(t, fixture), allow)
}

func ids(c router.Choice) []string {
	var out []string
	for _, cand := range c.Candidates {
		out = append(out, cand.Profile.ID)
	}
	return out
}

func mustSelect(t *testing.T, r *router.Router, req router.Request) router.Choice {
	t.Helper()
	c, err := r.Select(context.Background(), req)
	if err != nil {
		t.Fatalf("Select(%+v)：%v", req, err)
	}
	if len(c.Candidates) == 0 {
		t.Fatalf("Select(%+v) 没有候选", req)
	}
	return c
}

func wantCategory(t *testing.T, what string, err error, category providererr.Category) {
	t.Helper()
	if err == nil {
		t.Fatalf("%s：应报错", what)
	}
	if got := providererr.CategoryOf(err); got != category {
		t.Fatalf("%s：类别应为 %v，实际 %v（%v）", what, category, got, err)
	}
}

func TestDefaultTierForEachRole(t *testing.T) {
	want := map[commonv1.Role]string{
		commonv1.Role_ROLE_PLAN:  "strong@medium",
		commonv1.Role_ROLE_JUDGE: "strong@medium",
		commonv1.Role_ROLE_ROUTE: "small@low",
	}
	var roles []int
	for v := range commonv1.Role_name {
		roles = append(roles, int(v))
	}
	sort.Ints(roles)
	r := newRouter(t, nil)
	for _, v := range roles {
		role := commonv1.Role(v)
		expected, ok := want[role]
		if !ok {
			expected = "mid@low" // 其余 role（含未指定）
		}
		if got := router.DefaultTier(router.KindGenerate, role); got != expected {
			t.Errorf("DefaultTier(Generate, %s) = %q, want %q", role, got, expected)
		}
		if got := router.DefaultTier(router.KindStream, role); got != expected {
			t.Errorf("DefaultTier(Stream, %s) = %q, want %q", role, got, expected)
		}
		// Classify 一律 small@low，Embed 一律 embed@default，与 role 无关。
		if got := router.DefaultTier(router.KindClassify, role); got != "small@low" {
			t.Errorf("DefaultTier(Classify, %s) = %q, want small@low", role, got)
		}
		if got := router.DefaultTier(router.KindEmbed, role); got != "embed@default" {
			t.Errorf("DefaultTier(Embed, %s) = %q, want embed@default", role, got)
		}
		c := mustSelect(t, r, router.Request{TenantID: "t1", Kind: router.KindGenerate, Role: role})
		if c.Tier != expected {
			t.Errorf("Select 的 Tier（role %s）= %q, want %q", role, c.Tier, expected)
		}
		for _, cand := range c.Candidates {
			if cand.Profile.Class+"@"+cand.Tier.Name != expected {
				t.Errorf("role %s 的候选 %s@%s 不在默认 Tier %s", role, cand.Profile.Class, cand.Tier.Name, expected)
			}
		}
	}
	if c := mustSelect(t, r, router.Request{TenantID: "t1", Kind: router.KindEmbed}); c.Tier != "embed@default" || ids(c)[0] != "x/embed-openai" {
		t.Errorf("Embed 默认用 embed@default：%q %v", c.Tier, ids(c))
	}
	if c := mustSelect(t, r, router.Request{TenantID: "t1", Kind: router.KindClassify, Role: commonv1.Role_ROLE_PLAN}); c.Tier != "small@low" {
		t.Errorf("Classify 默认用 small@low（与 role 无关），实际 %q", c.Tier)
	}
}

func TestCheapestFirstThenSameTierFallbacks(t *testing.T) {
	r := newRouter(t, nil)
	c := mustSelect(t, r, router.Request{TenantID: "t1", Kind: router.KindGenerate, Role: commonv1.Role_ROLE_TRANSFORM})
	if got := ids(c); !slices.Equal(got, []string{"x/mid-deepseek", "x/mid-google"}) {
		t.Errorf("mid@low 按单价升序：%v", got)
	}
	if c.Candidates[0].Tier.Name != "low" {
		t.Errorf("候选的 Tier 应为 low：%+v", c.Candidates[0].Tier)
	}
	if c.PinApplied || c.AvoidVendorUnmet {
		t.Errorf("没有 pinned 与 avoid_vendor 时两个标记都为 false：%+v", c)
	}
}

func TestExplicitTier(t *testing.T) {
	r := newRouter(t, nil)
	c := mustSelect(t, r, router.Request{TenantID: "t1", Kind: router.KindGenerate, Role: commonv1.Role_ROLE_TRANSFORM, Tier: "strong@high"})
	if c.Tier != "strong@high" {
		t.Errorf("显式 Tier 优先于 role 默认：%q", c.Tier)
	}
	if got := ids(c); !slices.Equal(got, []string{"x/strong-openai", "x/strong-anthropic"}) {
		t.Errorf("strong@high 按单价升序：%v", got)
	}
	if c.Candidates[0].Tier.Name != "high" || c.Candidates[0].Tier.ThinkingBudget != 32768 {
		t.Errorf("候选应带 high 这一档：%+v", c.Candidates[0].Tier)
	}
	// 显式 Tier 对 Embed 同样适用。
	if c := mustSelect(t, r, router.Request{TenantID: "t1", Kind: router.KindEmbed, Tier: "embed@default"}); ids(c)[0] != "x/embed-openai" {
		t.Errorf("Embed 显式 Tier：%v", ids(c))
	}
}

func TestMalformedTierIsInvalid(t *testing.T) {
	r := newRouter(t, nil)
	for _, tier := range []string{"strong", "strong@", "@low", "a@b@c", "Strong@Medium"} {
		_, err := r.Select(context.Background(), router.Request{TenantID: "t1", Kind: router.KindGenerate, Tier: tier})
		wantCategory(t, "Tier "+tier, err, providererr.Invalid)
	}
}

func TestNoEligibleModelIsNotFound(t *testing.T) {
	r := newRouter(t, staticAllowlist{"t-none": {}, "t-google": {"x/small-google"}})
	cases := map[string]router.Request{
		"没有这个 class":         {TenantID: "t1", Kind: router.KindGenerate, Tier: "huge@low"},
		"class 里没有这个 Tier 名": {TenantID: "t1", Kind: router.KindGenerate, Tier: "strong@ultra"},
		"嵌入模型不能生成":           {TenantID: "t1", Kind: router.KindGenerate, Tier: "embed@default"},
		"生成模型不能嵌入":           {TenantID: "t1", Kind: router.KindEmbed, Tier: "mid@low"},
		"白名单为空数组":            {TenantID: "t-none", Kind: router.KindGenerate},
		"白名单剩下的模型不支持工具":      {TenantID: "t-google", Kind: router.KindGenerate, Tier: "small@low", Tools: true},
	}
	for name, req := range cases {
		_, err := r.Select(context.Background(), req)
		wantCategory(t, name, err, providererr.NotFound)
		var pe *providererr.Error
		if errors.As(err, &pe) && pe.Code != "no_eligible_model" {
			t.Errorf("%s：code 应为 no_eligible_model，实际 %q", name, pe.Code)
		}
	}
}

func TestPinnedProfileComesFirst(t *testing.T) {
	r := newRouter(t, nil)
	c := mustSelect(t, r, router.Request{TenantID: "t1", Kind: router.KindGenerate, Role: commonv1.Role_ROLE_PLAN, PinnedProfile: "x/strong-anthropic"})
	if got := ids(c); !slices.Equal(got, []string{"x/strong-anthropic", "x/strong-openai"}) || !c.PinApplied {
		t.Errorf("pinned 排在最前（即使更贵），其余仍可切换：%v PinApplied=%v", got, c.PinApplied)
	}
	// pinned 不在该 Tier、或不存在：忽略，按单价选。
	for _, pinned := range []string{"x/small-openai", "x/does-not-exist"} {
		c := mustSelect(t, r, router.Request{TenantID: "t1", Kind: router.KindGenerate, Role: commonv1.Role_ROLE_PLAN, PinnedProfile: pinned})
		if ids(c)[0] != "x/strong-openai" || c.PinApplied {
			t.Errorf("pinned %s 不可用时忽略：%v PinApplied=%v", pinned, ids(c), c.PinApplied)
		}
	}
	// pinned 被白名单排除：忽略。
	allow := newRouter(t, staticAllowlist{"t1": {"x/strong-openai"}})
	c = mustSelect(t, allow, router.Request{TenantID: "t1", Kind: router.KindGenerate, Role: commonv1.Role_ROLE_PLAN, PinnedProfile: "x/strong-anthropic"})
	if got := ids(c); !slices.Equal(got, []string{"x/strong-openai"}) || c.PinApplied {
		t.Errorf("pinned 不在白名单里时不能绕过白名单：%v", got)
	}
}

func TestAvoidVendor(t *testing.T) {
	r := newRouter(t, nil)
	// 有别家：排除这一家（不作为切换候选）。
	c := mustSelect(t, r, router.Request{TenantID: "t1", Kind: router.KindGenerate, Role: commonv1.Role_ROLE_JUDGE, AvoidVendor: "openai"})
	if got := ids(c); !slices.Equal(got, []string{"x/strong-anthropic"}) || c.AvoidVendorUnmet {
		t.Errorf("judge 避开 openai：%v AvoidVendorUnmet=%v", got, c.AvoidVendorUnmet)
	}
	// 只剩这一家（small 档要结构化输出时只有 openai）：仍可用，并标明。
	c = mustSelect(t, r, router.Request{TenantID: "t1", Kind: router.KindGenerate, Role: commonv1.Role_ROLE_ROUTE, Structured: true, AvoidVendor: "openai"})
	if got := ids(c); !slices.Equal(got, []string{"x/small-openai"}) || !c.AvoidVendorUnmet {
		t.Errorf("只剩同家时仍可用并标明：%v AvoidVendorUnmet=%v", got, c.AvoidVendorUnmet)
	}
	// avoid_vendor 优先于 pinned：judge 不能被 pinned 拉回生成者那一家。
	c = mustSelect(t, r, router.Request{TenantID: "t1", Kind: router.KindGenerate, Role: commonv1.Role_ROLE_JUDGE, AvoidVendor: "anthropic", PinnedProfile: "x/strong-anthropic"})
	if got := ids(c); !slices.Equal(got, []string{"x/strong-openai"}) || c.PinApplied {
		t.Errorf("avoid_vendor 排除了 pinned 的那一家：%v PinApplied=%v", got, c.PinApplied)
	}
}

func TestTenantAllowlist(t *testing.T) {
	r := newRouter(t, staticAllowlist{"t-limited": {"x/mid-google", "x/strong-anthropic"}})
	c := mustSelect(t, r, router.Request{TenantID: "t-limited", Kind: router.KindGenerate, Role: commonv1.Role_ROLE_TRANSFORM})
	if got := ids(c); !slices.Equal(got, []string{"x/mid-google"}) {
		t.Errorf("白名单只剩 mid-google：%v", got)
	}
	c = mustSelect(t, r, router.Request{TenantID: "t-free", Kind: router.KindGenerate, Role: commonv1.Role_ROLE_TRANSFORM})
	if got := ids(c); !slices.Equal(got, []string{"x/mid-deepseek", "x/mid-google"}) {
		t.Errorf("没列出的租户不限制：%v", got)
	}
	_, err := r.Select(context.Background(), router.Request{TenantID: "t-limited", Kind: router.KindEmbed})
	wantCategory(t, "白名单里没有嵌入模型", err, providererr.NotFound)
}

func TestCapabilityFilter(t *testing.T) {
	r := newRouter(t, nil)
	// small 档：google 更便宜，但没有 structured、tools。
	if c := mustSelect(t, r, router.Request{TenantID: "t1", Kind: router.KindGenerate, Role: commonv1.Role_ROLE_ROUTE}); ids(c)[0] != "x/small-google" {
		t.Errorf("不要求能力时选最便宜的：%v", ids(c))
	}
	for name, req := range map[string]router.Request{
		"结构化输出":    {TenantID: "t1", Kind: router.KindGenerate, Role: commonv1.Role_ROLE_ROUTE, Structured: true},
		"工具":       {TenantID: "t1", Kind: router.KindGenerate, Role: commonv1.Role_ROLE_ROUTE, Tools: true},
		"Classify": {TenantID: "t1", Kind: router.KindClassify},
		"Score":    {TenantID: "t1", Kind: router.KindScore, Tier: "small@low"},
	} {
		if c := mustSelect(t, r, req); !slices.Equal(ids(c), []string{"x/small-openai"}) {
			t.Errorf("%s 要 structured 或 tools，只剩 small-openai：%v", name, ids(c))
		}
	}
	// 嵌入：只有声明 embed 的 Profile。
	if c := mustSelect(t, r, router.Request{TenantID: "t1", Kind: router.KindEmbed}); !slices.Equal(ids(c), []string{"x/embed-openai"}) {
		t.Errorf("Embed 只用 embed 能力的 Profile：%v", ids(c))
	}
}

func TestStreamNeedsStreamCapability(t *testing.T) {
	items := []string{
		profileJSON("x/mid-nostream", "deepseek", "mid", low, []string{"generate", "structured"}, 0.1, 0.1),
		profileJSON("x/mid-stream", "google", "mid", low, full, 0.5, 3),
	}
	r := router.New(catalogOf(t, items), nil)
	if c := mustSelect(t, r, router.Request{TenantID: "t1", Kind: router.KindGenerate}); ids(c)[0] != "x/mid-nostream" {
		t.Errorf("Generate 不要求 stream：%v", ids(c))
	}
	if c := mustSelect(t, r, router.Request{TenantID: "t1", Kind: router.KindStream}); !slices.Equal(ids(c), []string{"x/mid-stream"}) {
		t.Errorf("StreamGenerate 要 stream 能力：%v", ids(c))
	}
}

func TestTiesBrokenByIDAndSelectionIsDeterministic(t *testing.T) {
	items := []string{
		profileJSON("x/mid-c", "c", "mid", low, full, 0.2, 0.8),
		profileJSON("x/mid-a", "a", "mid", low, full, 0.2, 0.8),
		profileJSON("x/mid-b", "b", "mid", low, full, 0.2, 0.8),
	}
	want := []string{"x/mid-a", "x/mid-b", "x/mid-c"}
	req := router.Request{TenantID: "t1", Kind: router.KindGenerate, Role: commonv1.Role_ROLE_TRANSFORM}
	r := router.New(catalogOf(t, items), nil)
	for i := range 50 {
		if got := ids(mustSelect(t, r, req)); !slices.Equal(got, want) {
			t.Fatalf("第 %d 次：单价相同按 id 升序，实际 %v", i, got)
		}
	}
	// 文件里的顺序不影响结果。
	reversed := []string{items[2], items[1], items[0]}
	if got := ids(mustSelect(t, router.New(catalogOf(t, reversed), nil), req)); !slices.Equal(got, want) {
		t.Errorf("Profile 顺序不同，结果应相同：%v", got)
	}
}

func writePolicies(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "tenant-policies.json")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

// 约定 2：租户模型白名单读 AR_DEV_TENANT_POLICIES_FILE（与 task-service 同一个文件，effect_ceiling 等字段忽略）。
func TestFileAllowlist(t *testing.T) {
	ctx := context.Background()
	path := writePolicies(t, `{"tenants":{
		"t1":{"effect_ceiling":"read","model_allowlist":["x/mid-google","x/strong-openai"]},
		"t2":{"effect_ceiling":"write_reversible"},
		"t3":{"model_allowlist":[]}}}`)
	f := router.FileAllowlist{Path: path}
	got, restricted, err := f.ModelAllowlist(ctx, "t1")
	if err != nil || !restricted || !slices.Equal(got, []string{"x/mid-google", "x/strong-openai"}) {
		t.Errorf("t1 的白名单：%v %v %v", got, restricted, err)
	}
	for _, tenant := range []string{"t2", "t-unlisted"} {
		if _, restricted, err := f.ModelAllowlist(ctx, tenant); err != nil || restricted {
			t.Errorf("%s 没有 model_allowlist：不限制，实际 restricted=%v err=%v", tenant, restricted, err)
		}
	}
	if got, restricted, err := f.ModelAllowlist(ctx, "t3"); err != nil || !restricted || len(got) != 0 {
		t.Errorf("t3 的白名单是空数组：一个模型都不允许，实际 %v %v %v", got, restricted, err)
	}
	for name, fa := range map[string]router.FileAllowlist{
		"Path 为空": {},
		"文件不存在":   {Path: filepath.Join(t.TempDir(), "missing.json")},
	} {
		if _, restricted, err := fa.ModelAllowlist(ctx, "t1"); err != nil || restricted {
			t.Errorf("%s：不限制，实际 restricted=%v err=%v", name, restricted, err)
		}
	}
	_, _, err = router.FileAllowlist{Path: writePolicies(t, `{"tenants":`)}.ModelAllowlist(ctx, "t1")
	wantCategory(t, "格式不对的策略文件", err, providererr.Invalid)
	_, _, err = router.FileAllowlist{Path: writePolicies(t, `{"tenants":{"t1":{"model_allowlist":"x/mid-google"}}}`)}.ModelAllowlist(ctx, "t1")
	wantCategory(t, "model_allowlist 不是数组", err, providererr.Invalid)

	// 经 Router 使用文件白名单。
	r := router.New(catalogOf(t, fixture), f)
	if c := mustSelect(t, r, router.Request{TenantID: "t1", Kind: router.KindGenerate, Role: commonv1.Role_ROLE_TRANSFORM}); !slices.Equal(ids(c), []string{"x/mid-google"}) {
		t.Errorf("t1 经文件白名单只剩 mid-google：%v", ids(c))
	}
}

// 白名单读取失败时不能当成“不限制”放行。
func TestAllowlistErrorIsReturned(t *testing.T) {
	r := router.New(catalogOf(t, fixture), failingAllowlist{})
	_, err := r.Select(context.Background(), router.Request{TenantID: "t1", Kind: router.KindGenerate})
	wantCategory(t, "白名单读取失败", err, providererr.Transient)
}

type failingAllowlist struct{}

func (failingAllowlist) ModelAllowlist(context.Context, string) ([]string, bool, error) {
	return nil, false, providererr.New(providererr.Transient, "tenant_policy_unavailable", "读不到租户策略")
}
