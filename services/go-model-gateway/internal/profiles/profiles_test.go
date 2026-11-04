package profiles_test

// 验收（docs/plan/model-gateway-direct.md）：Model Profile 从 JSON 文件读取（附录 H.1 的子集），Tier 写作 <class>@<tier>；
// 成本按 Model Profile 单价计算，精确到 micros。仓库里的开发用 Profile 文件必须能加载，并覆盖 small、mid、strong、embed 四档。

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"

	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/profiles"
)

// validFile 是约定 2 的示例，外加一个嵌入模型与一个强档模型。
const validFile = `{"version":1,"profiles":[
 {"id":"a6api/gpt-4o-mini@2026-10","vendor":"openai","model":"gpt-4o-mini","class":"small",
  "tiers":[{"name":"low","effort":"","thinking_budget":0}],"capabilities":["generate","structured","tools","stream"],
  "cost":{"in_per_m":0.15,"out_per_m":0.6,"cache_read_per_m":0.075},"constraints":{"context_window":128000,"max_output":16384},
  "freshness":{"price_at":"2026-10-21"}},
 {"id":"a6api/strong-x@2026-10","vendor":"anthropic","model":"strong-x","class":"strong",
  "tiers":[{"name":"medium","effort":"medium","thinking_budget":8192},{"name":"high","effort":"high","thinking_budget":32768}],
  "capabilities":["generate","structured","tools","stream"],
  "cost":{"in_per_m":3,"out_per_m":15,"cache_read_per_m":0.3},"constraints":{"context_window":200000,"max_output":64000},
  "freshness":{"price_at":"2026-10-21"}},
 {"id":"a6api/text-embedding-3-small@2026-10","vendor":"openai","model":"text-embedding-3-small","class":"embed",
  "tiers":[{"name":"default"}],"capabilities":["embed"],
  "cost":{"in_per_m":0.02,"out_per_m":0,"cache_read_per_m":0},"constraints":{"context_window":8191,"max_output":0},
  "freshness":{"price_at":"2026-10-21"}}
]}`

// wantInvalid 断言 err 是 Invalid 类别的 *providererr.Error。
func wantInvalid(t *testing.T, what string, err error) {
	t.Helper()
	if err == nil {
		t.Errorf("%s：应报错", what)
		return
	}
	if got := providererr.CategoryOf(err); got != providererr.Invalid {
		t.Errorf("%s：应是 Invalid，实际 %v（%v）", what, got, err)
	}
}

func mustParse(t *testing.T, data string) *profiles.Catalog {
	t.Helper()
	c, err := profiles.Parse([]byte(data))
	if err != nil {
		t.Fatalf("合法的 Profile 文件应能解析：%v", err)
	}
	return c
}

func TestParseValidFile(t *testing.T) {
	c := mustParse(t, validFile)
	all := c.All()
	if len(all) != 3 {
		t.Fatalf("应有 3 个 Profile，实际 %d", len(all))
	}
	for i := 1; i < len(all); i++ {
		if all[i-1].ID >= all[i].ID {
			t.Errorf("All 应按 id 升序：%q 在 %q 之前", all[i-1].ID, all[i].ID)
		}
	}
	p, ok := c.Get("a6api/gpt-4o-mini@2026-10")
	if !ok {
		t.Fatal("Get 应能按 id 取到 Profile")
	}
	if p.Vendor != "openai" || p.Model != "gpt-4o-mini" || p.Class != "small" {
		t.Errorf("基本字段：%+v", p)
	}
	if p.Cost != (profiles.Cost{InPerM: 0.15, OutPerM: 0.6, CacheReadPerM: 0.075}) {
		t.Errorf("单价：%+v", p.Cost)
	}
	if p.Constraints != (profiles.Constraints{ContextWindow: 128000, MaxOutput: 16384}) || p.Freshness.PriceAt != "2026-10-21" {
		t.Errorf("约束与取价日期：%+v %+v", p.Constraints, p.Freshness)
	}
	for _, capability := range []profiles.Capability{profiles.CapGenerate, profiles.CapStructured, profiles.CapTools, profiles.CapStream} {
		if !p.Has(capability) {
			t.Errorf("应有能力 %s", capability)
		}
	}
	if p.Has(profiles.CapEmbed) {
		t.Error("gpt-4o-mini 没有声明 embed")
	}
	if tier, ok := p.Tier("low"); !ok || tier.Effort != "" || tier.ThinkingBudget != 0 {
		t.Errorf("Tier low：%+v %v", tier, ok)
	}
	if _, ok := p.Tier("high"); ok {
		t.Error("没有声明的 Tier 不应取到")
	}
	strong, _ := c.Get("a6api/strong-x@2026-10")
	if tier, ok := strong.Tier("high"); !ok || tier.Effort != "high" || tier.ThinkingBudget != 32768 {
		t.Errorf("强档的 high：%+v %v", tier, ok)
	}
	embed, _ := c.Get("a6api/text-embedding-3-small@2026-10")
	if !embed.Has(profiles.CapEmbed) || embed.Has(profiles.CapGenerate) {
		t.Errorf("嵌入模型的能力：%v", embed.Capabilities)
	}
	if _, ok := embed.Tier("default"); !ok {
		t.Error("嵌入模型的 Tier 为 default")
	}
	if _, ok := c.Get("a6api/none@1"); ok {
		t.Error("不存在的 id 不应取到")
	}
}

func TestLoadReadsFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "profiles.json")
	if err := os.WriteFile(path, []byte(validFile), 0o644); err != nil {
		t.Fatal(err)
	}
	c, err := profiles.Load(path)
	if err != nil {
		t.Fatalf("Load：%v", err)
	}
	if len(c.All()) != 3 {
		t.Errorf("应有 3 个 Profile，实际 %d", len(c.All()))
	}
	if _, err := profiles.Load(filepath.Join(t.TempDir(), "missing.json")); err == nil {
		t.Error("文件不存在时应报错")
	}
}

// mutate 解析 validFile，对第一个 Profile 应用 fn 后重新序列化。
func mutate(t *testing.T, fn func(file map[string]any, first map[string]any)) string {
	t.Helper()
	var file map[string]any
	if err := json.Unmarshal([]byte(validFile), &file); err != nil {
		t.Fatal(err)
	}
	first := file["profiles"].([]any)[0].(map[string]any)
	fn(file, first)
	out, err := json.Marshal(file)
	if err != nil {
		t.Fatal(err)
	}
	return string(out)
}

func TestParseRejectsInvalidFiles(t *testing.T) {
	cases := map[string]func(file, first map[string]any){
		"缺 id":        func(_, p map[string]any) { delete(p, "id") },
		"id 为空":       func(_, p map[string]any) { p["id"] = "" },
		"缺 vendor":    func(_, p map[string]any) { delete(p, "vendor") },
		"缺 model":     func(_, p map[string]any) { delete(p, "model") },
		"缺 class":     func(_, p map[string]any) { delete(p, "class") },
		"class 含 @":   func(_, p map[string]any) { p["class"] = "small@x" },
		"缺 tiers":     func(_, p map[string]any) { delete(p, "tiers") },
		"tiers 为空":    func(_, p map[string]any) { p["tiers"] = []any{} },
		"Tier 缺 name": func(_, p map[string]any) { p["tiers"] = []any{map[string]any{"effort": "low"}} },
		"Tier 名含 @":   func(_, p map[string]any) { p["tiers"] = []any{map[string]any{"name": "low@x"}} },
		"Tier 名大写":    func(_, p map[string]any) { p["tiers"] = []any{map[string]any{"name": "Low"}} },
		"Tier 名重复": func(_, p map[string]any) {
			p["tiers"] = []any{map[string]any{"name": "low"}, map[string]any{"name": "low"}}
		},
		"缺 capabilities":       func(_, p map[string]any) { delete(p, "capabilities") },
		"capabilities 为空":      func(_, p map[string]any) { p["capabilities"] = []any{} },
		"未知 capability":        func(_, p map[string]any) { p["capabilities"] = []any{"generate", "telepathy"} },
		"单价为负":                 func(_, p map[string]any) { p["cost"].(map[string]any)["in_per_m"] = -1 },
		"缺 freshness.price_at": func(_, p map[string]any) { p["freshness"] = map[string]any{} },
		"price_at 不是日期":        func(_, p map[string]any) { p["freshness"] = map[string]any{"price_at": "yesterday"} },
		// 单价字段拼错后不能被当成 0 而被选成最便宜的。
		"未知字段": func(_, p map[string]any) { p["cost"].(map[string]any)["in_per_million"] = 0.15 },
		// 未知的单价不能静默按 0 计费（R-06；schemas/model_profile.schema.json 的 cost 三项都必填）。显式写 0 的照常接受（嵌入模型的 out_per_m）。
		"缺 cost":                  func(_, p map[string]any) { delete(p, "cost") },
		"cost 为 null":             func(_, p map[string]any) { p["cost"] = nil },
		"缺 in_per_m":              func(_, p map[string]any) { delete(p["cost"].(map[string]any), "in_per_m") },
		"缺 out_per_m":             func(_, p map[string]any) { delete(p["cost"].(map[string]any), "out_per_m") },
		"缺 cache_read_per_m":      func(_, p map[string]any) { delete(p["cost"].(map[string]any), "cache_read_per_m") },
		"cache_read_per_m 为 null": func(_, p map[string]any) { p["cost"].(map[string]any)["cache_read_per_m"] = nil },
		"重复 id": func(f, p map[string]any) {
			second := f["profiles"].([]any)[1].(map[string]any)
			second["id"] = p["id"]
		},
		"version 不是 1": func(f, _ map[string]any) { f["version"] = 2 },
		"缺 version":    func(f, _ map[string]any) { delete(f, "version") },
	}
	for name, fn := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := profiles.Parse([]byte(mutate(t, fn)))
			wantInvalid(t, name, err)
		})
	}
	for name, data := range map[string]string{"不是 JSON": "{", "空文件": "", "profiles 为空": `{"version":1,"profiles":[]}`} {
		_, err := profiles.Parse([]byte(data))
		wantInvalid(t, name, err)
	}
}

func TestParseTier(t *testing.T) {
	for in, want := range map[string]profiles.TierRef{
		"strong@medium": {Class: "strong", Name: "medium"},
		"small@low":     {Class: "small", Name: "low"},
		"embed@default": {Class: "embed", Name: "default"},
		"mid-2@low_1":   {Class: "mid-2", Name: "low_1"},
	} {
		got, err := profiles.ParseTier(in)
		if err != nil || got != want {
			t.Errorf("ParseTier(%q) = %+v, %v；want %+v", in, got, err, want)
			continue
		}
		if got.String() != in {
			t.Errorf("String() = %q, want %q", got.String(), in)
		}
	}
	for _, bad := range []string{"", "strong", "@medium", "strong@", "a@b@c", " strong@medium", "strong@medium ", "Strong@medium", "strong@Medium", "str ong@medium"} {
		_, err := profiles.ParseTier(bad)
		wantInvalid(t, "ParseTier("+bad+")", err)
	}
}

func TestCostUSDMicrosIsExact(t *testing.T) {
	mini := profiles.Cost{InPerM: 0.15, OutPerM: 0.6, CacheReadPerM: 0.075}
	strong := profiles.Cost{InPerM: 3, OutPerM: 15, CacheReadPerM: 0.3}
	cases := []struct {
		name  string
		cost  profiles.Cost
		usage *commonv1.Usage
		want  int64
	}{
		// (1000-200)*0.15 + 200*0.075 + 500*0.6 = 120 + 15 + 300
		{"有缓存命中", mini, &commonv1.Usage{InputTokens: 1000, OutputTokens: 500, CacheReadTokens: 200}, 435},
		// 2000*3 + 1000*15
		{"强档无缓存", strong, &commonv1.Usage{InputTokens: 2000, OutputTokens: 1000}, 21000},
		// 500000*0.15 + 500000*0.075
		{"一半命中缓存", mini, &commonv1.Usage{InputTokens: 1_000_000, CacheReadTokens: 500_000}, 112500},
		// 7*0.1 + 3*0.4 = 1.9，四舍五入为 2
		{"四舍五入进位", profiles.Cost{InPerM: 0.1, OutPerM: 0.4}, &commonv1.Usage{InputTokens: 7, OutputTokens: 3}, 2},
		// 2*0.15 = 0.3，四舍五入为 0
		{"四舍五入舍去", mini, &commonv1.Usage{InputTokens: 2}, 0},
		// 推理 token 已计入 output_tokens，不再另算；cache_write 不在本期单价里
		{"推理与缓存写入不另算", mini, &commonv1.Usage{InputTokens: 1000, OutputTokens: 500, CacheReadTokens: 200, ReasoningTokens: 300, CacheWriteTokens: 50}, 435},
		{"Usage 为空", mini, nil, 0},
	}
	for _, c := range cases {
		if got := c.cost.USDMicros(c.usage); got != c.want {
			t.Errorf("%s：cost_usd_micros = %d, want %d", c.name, got, c.want)
		}
	}
}

func TestUnitPriceIsInputPlusOutput(t *testing.T) {
	if got := (profiles.Cost{InPerM: 0.15, OutPerM: 0.6, CacheReadPerM: 0.075}).UnitPrice(); got < 0.7499 || got > 0.7501 {
		t.Errorf("单价 = %v, want 0.75", got)
	}
}

// 交付 2：仓库里的开发用 Profile 文件，模型取自真实端点。
func TestDevProfilesFileLoads(t *testing.T) {
	path := filepath.Join("..", "..", "..", "..", "deploy", "dev", "model-profiles.json")
	c, err := profiles.Load(path)
	if err != nil {
		t.Fatalf("deploy/dev/model-profiles.json 应能加载：%v", err)
	}
	want := map[string]struct {
		vendor, class string
		tiers         []string
	}{
		"gpt-4o-mini":            {"openai", "small", []string{"low"}},
		"gemini-3.5-flash-lite":  {"google", "small", []string{"low"}},
		"deepseek-v4-flash":      {"deepseek", "mid", []string{"low"}},
		"gemini-3.5-flash":       {"google", "mid", []string{"low"}},
		"claude-sonnet-5-5":      {"anthropic", "strong", []string{"medium", "high"}},
		"gpt-5.5":                {"openai", "strong", []string{"medium", "high"}},
		"gpt-6.1-sol":            {"openai", "strong", []string{"medium", "high"}},
		"text-embedding-3-small": {"openai", "embed", []string{"default"}},
		// 2026-11-03 加的能力矩阵候选：模型名都在中转站的 /v1/models 里，单价与发布日期取自 models.dev 各家自己的条目。
		"gpt-6-astra":      {"openai", "strong", []string{"medium", "high"}},
		"claude-fable-5-1": {"anthropic", "strong", []string{"medium", "high"}},
		"gemini-4-argon":   {"google", "strong", []string{"medium", "high"}},
		"deepseek-v4-pro":  {"deepseek", "strong", []string{"medium", "high"}},
		"grok-4.7":         {"xai", "strong", []string{"medium", "high"}},
		"kimi-k3.1-pro":    {"moonshotai", "strong", []string{"medium", "high"}},
		"qwen3.8-max":      {"alibaba", "strong", []string{"medium", "high"}},
		"glm-5.3":          {"zhipuai", "strong", []string{"medium", "high"}},
		"gemini-3.8-flash": {"google", "mid", []string{"low"}},
		"gpt-6.1-terra":    {"openai", "mid", []string{"low"}},
		"gpt-6-luna":       {"openai", "small", []string{"low"}},
	}
	seen := map[string]bool{}
	for _, p := range c.All() {
		w, ok := want[p.Model]
		if !ok {
			continue
		}
		seen[p.Model] = true
		if p.Vendor != w.vendor || p.Class != w.class {
			t.Errorf("%s：vendor/class = %s/%s, want %s/%s", p.Model, p.Vendor, p.Class, w.vendor, w.class)
		}
		for _, name := range w.tiers {
			if _, ok := p.Tier(name); !ok {
				t.Errorf("%s 应有 Tier %s", p.Model, name)
			}
		}
		// 单价由 scripts/sync_model_profiles.py 从 models.dev 取（docs/plan/feedback-2026-11-01.md N-07），日期随同步变化。
		if p.Freshness.PriceAt < "2026-10-21" {
			t.Errorf("%s 的取价日期应不早于 2026-10-21，实际 %q", p.Model, p.Freshness.PriceAt)
		}
		if p.Class == "embed" {
			if !p.Has(profiles.CapEmbed) || p.Cost.InPerM <= 0 {
				t.Errorf("%s：嵌入模型要有 embed 能力与输入单价", p.Model)
			}
		} else {
			if !p.Has(profiles.CapGenerate) || !p.Has(profiles.CapStructured) || p.Cost.InPerM <= 0 || p.Cost.OutPerM <= 0 {
				t.Errorf("%s：生成模型要有 generate、structured 能力与单价", p.Model)
			}
		}
		if !strings.HasPrefix(p.ID, "a6api/"+p.Model+"@") {
			t.Errorf("%s 的 id 应形如 a6api/<模型>@<版本>，实际 %q", p.Model, p.ID)
		}
	}
	for model := range want {
		if !seen[model] {
			t.Errorf("开发用 Profile 缺少 %s", model)
		}
	}
}
