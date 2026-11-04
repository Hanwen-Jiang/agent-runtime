// Package profiles 加载与校验 Model Profile（设计书附录 H.1 的子集），解析 Tier 字符串，并按单价计价。
//
// 开发环境从 JSON 文件读取（AR_MODEL_PROFILES_FILE，示例见 deploy/dev/model-profiles.json）：
//
//	{"version": 1, "profiles": [{
//	  "id": "a6api/gpt-4o-mini@2026-10", "vendor": "openai", "model": "gpt-4o-mini", "class": "small",
//	  "tiers": [{"name": "low", "effort": "", "thinking_budget": 0}],
//	  "capabilities": ["generate", "structured", "tools", "stream"],
//	  "cost": {"in_per_m": 0.15, "out_per_m": 0.6, "cache_read_per_m": 0.075},
//	  "constraints": {"context_window": 128000, "max_output": 16384},
//	  "freshness": {"price_at": "2026-10-21"}}]}
//
// 规则：
//   - version 必须为 1；id、vendor、model、class 必填；id 不得重复；未知字段报错（防止单价字段拼错后被当成 0 而被选成最便宜）。
//   - class 与 Tier 名是小写 slug（[a-z0-9][a-z0-9_-]*），不含 @；每个 Profile 至少一个 Tier，Tier 名在 Profile 内不重复。
//   - capabilities 至少一项，只能取 generate、structured、tools、stream、embed。
//   - 单价单位是“美元每百万 token”，三项（in_per_m、out_per_m、cache_read_per_m）都必填、不得为负，免费的写 0；
//     freshness.price_at 必填，格式 YYYY-MM-DD。
//   - Tier 字符串写作 <class>@<tier 名>，例如 strong@medium；嵌入模型的 class 为 embed、Tier 为 default。
//   - 嵌入模型的 embedding_version 等于 Profile id。
package profiles

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Capability 是 Model Profile 声明的能力。
type Capability string

// 已知的能力；Profile 文件里出现其他取值时报错。
const (
	CapGenerate   Capability = "generate"
	CapStructured Capability = "structured"
	CapTools      Capability = "tools"
	CapStream     Capability = "stream"
	CapEmbed      Capability = "embed"
)

var knownCapabilities = []Capability{CapGenerate, CapStructured, CapTools, CapStream, CapEmbed}

// Tier 是同一模型的一档推理强度（附录 H.1：不同推理强度或思考预算视为不同的 Tier）。
type Tier struct {
	Name string `json:"name"`
	// Effort 是供应商的推理强度（low、medium、high）；空表示不发送。
	Effort string `json:"effort"`
	// ThinkingBudget 是该档默认的思考预算（token）；请求没有指定时由 Model Gateway 传给适配层。
	ThinkingBudget int `json:"thinking_budget"`
}

// Cost 是单价，单位“美元每百万 token”；乘以 token 数正好是微美元。
type Cost struct {
	InPerM        float64 `json:"in_per_m"`
	OutPerM       float64 `json:"out_per_m"`
	CacheReadPerM float64 `json:"cache_read_per_m"`
}

// USDMicros 按单价计算一次调用的成本（微美元），四舍五入到整数：
//
//	round((input_tokens - cache_read_tokens) * in_per_m + cache_read_tokens * cache_read_per_m + output_tokens * out_per_m)
//
// usage 为 nil 时返回 0。
func (c Cost) USDMicros(usage *commonv1.Usage) int64 {
	if usage == nil {
		return 0
	}
	input := max(usage.GetInputTokens(), 0)
	cached := min(max(usage.GetCacheReadTokens(), 0), input)
	total := float64(input-cached)*c.InPerM + float64(cached)*c.CacheReadPerM + float64(usage.GetOutputTokens())*c.OutPerM
	// 先取到 1e-6 微美元，消掉 0.15*800 = 120.00000000000001 一类的浮点误差，再四舍五入。
	return int64(math.Round(math.Round(total*1e6) / 1e6))
}

// UnitPrice 是性价比分量与同分决胜使用的基础价：输入单价加输出单价。
func (c Cost) UnitPrice() float64 {
	return c.InPerM + c.OutPerM
}

// Constraints 是模型的约束。
type Constraints struct {
	ContextWindow int `json:"context_window"`
	MaxOutput     int `json:"max_output"`
}

// Freshness 记录数据的取得日期。
type Freshness struct {
	PriceAt string `json:"price_at"`
}

// Measurement 是有来源、观测日期的 [0,1] 分数。nil 区分漏填与显式的零分。
// Source 指向内部评测批次或外部来源；它不是运行时请求的地址。
type Measurement struct {
	Score  *float64 `json:"score"`
	Source string   `json:"source"`
	At     string   `json:"at"`
}

// CapabilityScore 只适用于指定任务族、节点职责和推理档，不跨工作种类或阶段借用分数。
type CapabilityScore struct {
	TaskFamily string `json:"task_family"`
	Role       string `json:"role"`
	Tier       string `json:"tier"`
	Measurement
}

// Profile 是一个 Model Profile。
type Profile struct {
	ID string `json:"id"`
	// Vendor 是模型所属的家（openai、anthropic、google、deepseek 等），judge 用 avoid_vendor 选另一家。
	Vendor string `json:"vendor"`
	// Model 是供应商端点上的模型名。
	Model string `json:"model"`
	// Class 是模型档：small、mid、strong、embed 等。
	Class        string       `json:"class"`
	Tiers        []Tier       `json:"tiers"`
	Capabilities []Capability `json:"capabilities"`
	Cost         Cost         `json:"cost"`
	Constraints  Constraints  `json:"constraints"`
	Freshness    Freshness    `json:"freshness"`
	// ReleasedAt 是模型真实发布日期；未知时省略，不能用 price_at 或 Profile 版本代替。
	ReleasedAt *string `json:"released_at,omitempty"`
	// Availability 为 false 时不参与路由；省略保持旧 Profile 可用。
	Availability     *bool             `json:"availability,omitempty"`
	CapabilityScores []CapabilityScore `json:"capability_scores,omitempty"`
	Reliability      *Measurement      `json:"reliability,omitempty"`
}

// Has 判断 Profile 是否声明了某项能力。
func (p Profile) Has(c Capability) bool {
	return slices.Contains(p.Capabilities, c)
}

// Tier 按名字取 Tier。
func (p Profile) Tier(name string) (Tier, bool) {
	for _, t := range p.Tiers {
		if t.Name == name {
			return t, true
		}
	}
	return Tier{}, false
}

// Catalog 是校验过的一组 Model Profile，构造后只读。
type Catalog struct {
	profiles []Profile
}

// Load 读取并校验 Profile 文件。读不到文件时返回读取错误；内容不合法时同 Parse。
func Load(path string) (*Catalog, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return Parse(data)
}

var slug = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]*$`)
var taskFamily = regexp.MustCompile(`^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`)

func invalid(format string, args ...any) error {
	return providererr.New(providererr.Invalid, "invalid_profiles", fmt.Sprintf(format, args...))
}

// Parse 解析并校验 Profile 文件的内容（规则见包注释）。不是合法 JSON、有未知字段、违反任一规则或一个 Profile 都没有时，
// 返回 Invalid 的 *providererr.Error（code invalid_profiles），说明里指出哪个 Profile 的哪个字段。
func Parse(data []byte) (*Catalog, error) {
	var file struct {
		Version  *int      `json:"version"`
		Profiles []Profile `json:"profiles"`
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&file); err != nil {
		return nil, invalid("Profile 文件不是合法的 JSON 或有未知字段：%v", err)
	}
	if file.Version == nil || *file.Version != 1 {
		return nil, invalid("version 必须为 1")
	}
	if len(file.Profiles) == 0 {
		return nil, invalid("一个 Profile 都没有")
	}
	// 解码之后缺的单价与 null 都成了 0，分不出“免费”与“不知道”：另按原文核对三项单价都写了（R-06：未知价格不得静默按 0 计费）。
	var prices struct {
		Profiles []struct {
			Cost map[string]json.RawMessage `json:"cost"`
		} `json:"profiles"`
	}
	if err := json.Unmarshal(data, &prices); err != nil {
		return nil, invalid("Profile 文件不是合法的 JSON：%v", err)
	}
	seen := map[string]bool{}
	for i, p := range file.Profiles {
		name := fmt.Sprintf("第 %d 个 Profile（%s）", i+1, p.ID)
		for _, key := range []string{"in_per_m", "out_per_m", "cache_read_per_m"} {
			if raw, ok := prices.Profiles[i].Cost[key]; !ok || string(raw) == "null" {
				return nil, invalid("%s：cost.%s 必填（不知道单价时不能按 0 计）", name, key)
			}
		}
		if err := check(p, name); err != nil {
			return nil, err
		}
		if seen[p.ID] {
			return nil, invalid("%s：id 重复", name)
		}
		seen[p.ID] = true
	}
	out := slices.Clone(file.Profiles)
	slices.SortFunc(out, func(a, b Profile) int { return strings.Compare(a.ID, b.ID) })
	return &Catalog{profiles: out}, nil
}

func check(p Profile, name string) error {
	for _, f := range []struct{ field, value string }{{"id", p.ID}, {"vendor", p.Vendor}, {"model", p.Model}, {"class", p.Class}} {
		if strings.TrimSpace(f.value) == "" {
			return invalid("%s：缺 %s", name, f.field)
		}
	}
	if !slug.MatchString(p.Class) {
		return invalid("%s：class 必须是小写 slug，不含 @", name)
	}
	if len(p.Tiers) == 0 {
		return invalid("%s：至少要有一个 Tier", name)
	}
	tiers := map[string]bool{}
	for _, t := range p.Tiers {
		if !slug.MatchString(t.Name) {
			return invalid("%s：Tier 名 %q 必须是小写 slug，不含 @", name, t.Name)
		}
		if tiers[t.Name] {
			return invalid("%s：Tier 名 %q 重复", name, t.Name)
		}
		if t.ThinkingBudget < 0 {
			return invalid("%s：Tier %q 的 thinking_budget 不能为负", name, t.Name)
		}
		tiers[t.Name] = true
	}
	if len(p.Capabilities) == 0 {
		return invalid("%s：capabilities 至少一项", name)
	}
	for _, c := range p.Capabilities {
		if !slices.Contains(knownCapabilities, c) {
			return invalid("%s：不认识的 capability %q", name, c)
		}
	}
	if p.Cost.InPerM < 0 || p.Cost.OutPerM < 0 || p.Cost.CacheReadPerM < 0 {
		return invalid("%s：单价不能为负", name)
	}
	if _, err := time.Parse(time.DateOnly, p.Freshness.PriceAt); err != nil {
		return invalid("%s：freshness.price_at 必填，格式 YYYY-MM-DD", name)
	}
	if p.ReleasedAt != nil {
		if _, err := time.Parse(time.DateOnly, *p.ReleasedAt); err != nil {
			return invalid("%s：released_at 必须是实际发布日期，格式 YYYY-MM-DD", name)
		}
	}
	seenScores := map[[3]string]bool{}
	for _, score := range p.CapabilityScores {
		key := [3]string{score.TaskFamily, score.Role, score.Tier}
		if seenScores[key] {
			return invalid("%s：capability_scores 的 task_family/role/tier 重复", name)
		}
		seenScores[key] = true
		role, ok := commonv1.Role_value["ROLE_"+strings.ToUpper(score.Role)]
		if !taskFamily.MatchString(score.TaskFamily) || !ok || role == 0 || score.Role != strings.ToLower(score.Role) || !tiers[score.Tier] {
			return invalid("%s：capability_scores 必须指定合法的 task_family、role 与本 Profile 的 tier", name)
		}
		if err := checkMeasurement(score.Measurement, name+".capability_scores"); err != nil {
			return err
		}
	}
	if p.Reliability != nil {
		if err := checkMeasurement(*p.Reliability, name+".reliability"); err != nil {
			return err
		}
	}
	return nil
}

func checkMeasurement(m Measurement, name string) error {
	if m.Score == nil || math.IsNaN(*m.Score) || math.IsInf(*m.Score, 0) || *m.Score < 0 || *m.Score > 1 {
		return invalid("%s：score 必填且在 [0,1] 内", name)
	}
	if strings.TrimSpace(m.Source) == "" {
		return invalid("%s：source 必填", name)
	}
	if _, err := time.Parse(time.DateOnly, m.At); err != nil {
		return invalid("%s：at 必填，格式 YYYY-MM-DD", name)
	}
	return nil
}

// Get 按 id 取 Profile。
func (c *Catalog) Get(id string) (Profile, bool) {
	for _, p := range c.profiles {
		if p.ID == id {
			return p, true
		}
	}
	return Profile{}, false
}

// All 返回全部 Profile，按 id 升序。
func (c *Catalog) All() []Profile {
	return slices.Clone(c.profiles)
}

// TierRef 是解析后的 Tier 字符串 <class>@<tier 名>。
type TierRef struct {
	Class string
	Name  string
}

// String 返回 <class>@<tier 名>。
func (t TierRef) String() string {
	return t.Class + "@" + t.Name
}

// ParseTier 解析 Tier 字符串。格式不对（缺 @、多个 @、任一部分为空或不是小写 slug）时返回 Invalid。
func ParseTier(s string) (TierRef, error) {
	class, name, ok := strings.Cut(s, "@")
	if !ok || !slug.MatchString(class) || !slug.MatchString(name) {
		return TierRef{}, providererr.New(providererr.Invalid, "invalid_tier", fmt.Sprintf("Tier 要写成 <class>@<tier 名>，得到 %q", s))
	}
	return TierRef{Class: class, Name: name}, nil
}
