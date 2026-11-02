// Package router 是 Model Gateway 的选档（设计书 3.3.1 核心逻辑 1、2，附录 H.3、H.6；本期为第一期规则）。
//
// 选档顺序：
//  1. 确定 Tier：CallContext.tier 非空时用它（<class>@<tier 名>，格式不对 Invalid）；为空时按调用种类与 role 的默认 Tier
//     （附录 H.3）：Embed → embed@default；Classify → small@low；role 为 plan → strong@medium；judge → strong@medium；
//     route → small@low；其余 → mid@low。replan 也是 role plan 的调用，同为 strong@medium。
//  2. 过滤：只留 class 与 Tier 匹配、并且有该 Tier 名的 Profile；再过租户约束（模型白名单）与能力
//     （Generate 要 generate；Stream 另要 stream；要结构化输出另要 structured；带工具另要 tools；Score、Classify 要 generate 与 structured；
//     Embed 要 embed）。
//  3. avoid_vendor：剩下的 Profile 里有别家时，排除这一家；只剩这一家时仍可用，并在结果里标明（AvoidVendorUnmet）。
//  4. 排序：单价（输入单价 + 输出单价）升序，单价相同按 id 升序；pinned_model_profile 在剩余候选里时排到最前（PinApplied）。
//     pinned 不在剩余候选里（不在该 Tier、被白名单或能力过滤、被 avoid_vendor 排除）时忽略，不报错。
//  5. 第一个候选是首选，其余是同档切换的顺序（重试用尽后依次切换）。
//
// 没有可选的 Profile 时返回 NotFound（code no_eligible_model）。同样的输入、Profile 与白名单总是选出同样的结果。
package router

import (
	"bytes"
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"slices"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"

	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/profiles"
)

// Kind 是调用种类，对应 ModelGatewayService 的方法。
type Kind int

const (
	KindGenerate Kind = iota + 1
	KindStream
	KindScore
	KindEmbed
	KindClassify
)

// 默认 Tier（附录 H.3）。
const (
	TierStrongMedium = "strong@medium"
	TierMidLow       = "mid@low"
	TierSmallLow     = "small@low"
	TierEmbedDefault = "embed@default"
)

// DefaultTier 返回调用种类与 role 的默认 Tier。
func DefaultTier(kind Kind, role commonv1.Role) string {
	switch kind {
	case KindEmbed:
		return TierEmbedDefault
	case KindClassify:
		return TierSmallLow
	}
	switch role {
	case commonv1.Role_ROLE_PLAN, commonv1.Role_ROLE_JUDGE:
		return TierStrongMedium
	case commonv1.Role_ROLE_ROUTE:
		return TierSmallLow
	default:
		return TierMidLow
	}
}

// Request 是一次选档的输入。
type Request struct {
	TenantID string
	Kind     Kind
	Role     commonv1.Role
	// Tier 是请求期望的 Tier；为空时用 DefaultTier。
	Tier          string
	PinnedProfile string
	AvoidVendor   string
	// Structured 表示请求要结构化输出（output_schema 非空）。
	Structured bool
	// Tools 表示请求带了工具。
	Tools bool
}

// Candidate 是一个可用的 Model Profile 及其 Tier。
type Candidate struct {
	Profile profiles.Profile
	Tier    profiles.Tier
}

// Choice 是选档结果。
type Choice struct {
	// Tier 是实际使用的 Tier 字符串，例如 strong@medium。
	Tier string
	// Candidates 非空；第一个是首选，其余是同档切换的顺序。
	Candidates []Candidate
	// PinApplied 表示首选是 pinned_model_profile。
	PinApplied bool
	// AvoidVendorUnmet 表示只剩 avoid_vendor 那一家，没能避开。
	AvoidVendorUnmet bool
}

// Allowlist 给出租户的模型白名单。
type Allowlist interface {
	// ModelAllowlist 返回租户允许的 Profile id；restricted 为 false 表示不限制。
	ModelAllowlist(ctx context.Context, tenantID string) (ids []string, restricted bool, err error)
}

// FileAllowlist 每次都重新读取开发用的租户策略文件（与 task-service 同一个文件，AR_DEV_TENANT_POLICIES_FILE）：
//
//	{"default": {"model_allowlist": ["<profile id>"]}, "tenants": {"t1": {"effect_ceiling": "read", "model_allowlist": ["<profile id>"]}}}
//
// Path 为空、文件不存在、租户没有列出且没有 default.model_allowlist，或对应条目没有 model_allowlist：不限制。
// 租户条目优先于 default；model_allowlist 为空数组：一个模型都不允许。文件读不到：Transient；格式不对：Invalid。
// 其他字段（例如 effect_ceiling）忽略。
type FileAllowlist struct {
	Path string
}

// ModelAllowlist 实现 Allowlist。
func (f FileAllowlist) ModelAllowlist(_ context.Context, tenantID string) ([]string, bool, error) {
	if f.Path == "" {
		return nil, false, nil
	}
	data, err := os.ReadFile(f.Path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, providererr.Wrap(providererr.Transient, "tenant_policy_unavailable", "读不到租户策略文件", err)
	}
	var file struct {
		Default struct {
			ModelAllowlist *[]string `json:"model_allowlist"`
		} `json:"default"`
		Tenants map[string]struct {
			ModelAllowlist *[]string `json:"model_allowlist"`
		} `json:"tenants"`
	}
	if err := json.NewDecoder(bytes.NewReader(data)).Decode(&file); err != nil {
		return nil, false, providererr.New(providererr.Invalid, "invalid_tenant_policy", "租户策略文件格式不对")
	}
	tenant, ok := file.Tenants[tenantID]
	if ok && tenant.ModelAllowlist != nil {
		return slices.Clone(*tenant.ModelAllowlist), true, nil
	}
	if file.Default.ModelAllowlist == nil {
		return nil, false, nil
	}
	return slices.Clone(*file.Default.ModelAllowlist), true, nil
}

// Router 在一组 Model Profile 里选档，构造后只读，可并发使用。
type Router struct {
	catalog   *profiles.Catalog
	allowlist Allowlist
}

// New 构造选档器；allowlist 为 nil 表示不限制。
func New(catalog *profiles.Catalog, allowlist Allowlist) *Router {
	return &Router{catalog: catalog, allowlist: allowlist}
}

// needs 是调用种类与请求要求的能力。
func needs(req Request) []profiles.Capability {
	switch req.Kind {
	case KindEmbed:
		return []profiles.Capability{profiles.CapEmbed}
	case KindScore, KindClassify:
		return []profiles.Capability{profiles.CapGenerate, profiles.CapStructured}
	}
	out := []profiles.Capability{profiles.CapGenerate}
	if req.Kind == KindStream {
		out = append(out, profiles.CapStream)
	}
	if req.Structured {
		out = append(out, profiles.CapStructured)
	}
	if req.Tools {
		out = append(out, profiles.CapTools)
	}
	return out
}

// Select 按包注释的顺序选档。Tier 格式不对：Invalid；没有可选的 Profile：NotFound（code no_eligible_model）；
// 白名单读取失败：原样返回其错误。
func (r *Router) Select(ctx context.Context, req Request) (Choice, error) {
	tierName := req.Tier
	if tierName == "" {
		tierName = DefaultTier(req.Kind, req.Role)
	}
	ref, err := profiles.ParseTier(tierName)
	if err != nil {
		return Choice{}, err
	}
	var allowed map[string]bool
	if r.allowlist != nil {
		ids, restricted, err := r.allowlist.ModelAllowlist(ctx, req.TenantID)
		if err != nil {
			return Choice{}, err
		}
		if restricted {
			allowed = map[string]bool{}
			for _, id := range ids {
				allowed[id] = true
			}
		}
	}
	required := needs(req)
	var candidates []Candidate
	for _, p := range r.catalog.All() {
		tier, ok := p.Tier(ref.Name)
		if p.Class != ref.Class || !ok || (allowed != nil && !allowed[p.ID]) {
			continue
		}
		if !slices.ContainsFunc(required, func(c profiles.Capability) bool { return !p.Has(c) }) {
			candidates = append(candidates, Candidate{Profile: p, Tier: tier})
		}
	}
	choice := Choice{Tier: ref.String()}
	if req.AvoidVendor != "" {
		others := slices.DeleteFunc(slices.Clone(candidates), func(c Candidate) bool { return c.Profile.Vendor == req.AvoidVendor })
		if len(others) > 0 {
			candidates = others
		} else if len(candidates) > 0 {
			choice.AvoidVendorUnmet = true
		}
	}
	if len(candidates) == 0 {
		return Choice{}, providererr.New(providererr.NotFound, "no_eligible_model", "没有可选的模型："+ref.String())
	}
	slices.SortStableFunc(candidates, func(a, b Candidate) int {
		return cmp.Or(cmp.Compare(a.Profile.Cost.UnitPrice(), b.Profile.Cost.UnitPrice()), cmp.Compare(a.Profile.ID, b.Profile.ID))
	})
	if req.PinnedProfile != "" {
		if i := slices.IndexFunc(candidates, func(c Candidate) bool { return c.Profile.ID == req.PinnedProfile }); i >= 0 {
			pinned := candidates[i]
			candidates = append([]Candidate{pinned}, slices.Delete(candidates, i, i+1)...)
			choice.PinApplied = true
		}
	}
	choice.Candidates = candidates
	return choice, nil
}
