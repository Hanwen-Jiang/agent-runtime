package registry

import (
	"context"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
)

// 部署的默认值：会话级沙箱空闲 15 分钟回收，一个环境最多活 6 小时；租户策略只能调低。
const (
	DefaultIdle        = 15 * time.Minute
	DefaultMaxLifetime = 6 * time.Hour
)

// Quota 是租户策略的 sandbox.quota（W10）；0 表示不限。
type Quota struct {
	MaxConcurrent        int
	DailySandboxSeconds  int64
	DailyEgressBytes     int64
	DailyMirrorMissBytes int64
	DailyCostUSD         float64
	MonthlyCostUSD       float64
}

// WorkspaceLimits 是租户策略顶层的 workspace 一节（D-06）。
type WorkspaceLimits struct {
	MaxWorkspaces int
	MaxBytes      int64
	ColdAfterDays int
}

// Policy 是这个租户的沙箱策略。
type Policy struct {
	Backend      string
	Fallback     []string
	MinIsolation string
	Quota        Quota
	IdleTimeout  time.Duration
	MaxLifetime  time.Duration
	Network      map[string]any
	Workspace    WorkspaceLimits
}

// Names 是首选加回退，去重保序。
func (p Policy) Names() []string {
	out := []string{}
	for _, n := range append([]string{p.Backend}, p.Fallback...) {
		dup := false
		for _, o := range out {
			dup = dup || o == n
		}
		if !dup {
			out = append(out, n)
		}
	}
	return out
}

func number(v any) (float64, bool) {
	switch x := v.(type) {
	case float64:
		return x, true
	case int:
		return float64(x), true
	case int64:
		return float64(x), true
	}
	return 0, false
}

// FromDocument 按租户策略文档（没有时 nil）与部署的默认后端给出策略。
func FromDocument(doc map[string]any, defaultBackend string) Policy {
	sb, _ := doc["sandbox"].(map[string]any)
	p := Policy{Backend: defaultBackend, IdleTimeout: DefaultIdle, MaxLifetime: DefaultMaxLifetime, Network: map[string]any{}}
	if b, _ := sb["backend"].(string); b != "" {
		p.Backend = b
	}
	if list, ok := sb["fallback"].([]any); ok {
		for _, v := range list {
			if s, ok := v.(string); ok {
				p.Fallback = append(p.Fallback, s)
			}
		}
	}
	p.MinIsolation, _ = sb["min_isolation"].(string)
	if v, ok := number(sb["idle_timeout_s"]); ok && v > 0 {
		p.IdleTimeout = min(DefaultIdle, time.Duration(v)*time.Second)
	}
	if v, ok := number(sb["max_lifetime_s"]); ok && v > 0 {
		p.MaxLifetime = min(DefaultMaxLifetime, time.Duration(v)*time.Second)
	}
	if q, ok := sb["quota"].(map[string]any); ok {
		get := func(k string) float64 { v, _ := number(q[k]); return v }
		p.Quota = Quota{
			MaxConcurrent: int(get("max_concurrent")), DailySandboxSeconds: int64(get("daily_sandbox_seconds")),
			DailyEgressBytes: int64(get("daily_egress_bytes")), DailyMirrorMissBytes: int64(get("daily_mirror_miss_bytes")),
			DailyCostUSD: get("daily_cost_usd"), MonthlyCostUSD: get("monthly_cost_usd"),
		}
	}
	if n, ok := sb["network"].(map[string]any); ok {
		p.Network = n
	}
	p.Workspace = WorkspaceLimits{MaxWorkspaces: 20, MaxBytes: 2 << 30}
	if ws, ok := doc["workspace"].(map[string]any); ok {
		if v, ok := number(ws["max_workspaces"]); ok && v > 0 {
			p.Workspace.MaxWorkspaces = int(v)
		}
		if v, ok := number(ws["max_bytes"]); ok {
			p.Workspace.MaxBytes = int64(v)
		}
		if v, ok := number(ws["cold_after_days"]); ok {
			p.Workspace.ColdAfterDays = int(v)
		}
	}
	return p
}

// Lookup 读一个租户的当前策略文档；没有策略时 nil。
type Lookup func(ctx context.Context, tenantID string) (map[string]any, error)

// Policies 按租户缓存策略（默认一分钟）。
type Policies struct {
	reg    *Registry
	lookup Lookup
	ttl    time.Duration
	mu     sync.Mutex
	cache  map[string]cached
}

type cached struct {
	at     time.Time
	policy Policy
}

// NewPolicies 构造；lookup 为 nil 时所有租户都用部署的默认后端。
func NewPolicies(reg *Registry, lookup Lookup, ttl time.Duration) *Policies {
	return &Policies{reg: reg, lookup: lookup, ttl: ttl, cache: map[string]cached{}}
}

// For 是这个租户的沙箱策略。首选或回退里有没登记的名字、写错的隔离强度一律拒绝，不悄悄换后端。
func (p *Policies) For(ctx context.Context, tenantID string) (Policy, error) {
	p.mu.Lock()
	if c, ok := p.cache[tenantID]; ok && p.ttl > 0 && time.Since(c.at) < p.ttl {
		p.mu.Unlock()
		return c.policy, nil
	}
	p.mu.Unlock()
	var doc map[string]any
	if p.lookup != nil {
		var err error
		doc, err = p.lookup(ctx, tenantID)
		if err != nil && providererr.CategoryOf(err) != providererr.NotFound {
			return Policy{}, err
		}
	}
	policy := FromDocument(doc, p.reg.Default)
	for _, name := range policy.Names() {
		if !p.reg.Has(name) {
			return Policy{}, providererr.New(providererr.Invalid, "unknown_sandbox_backend", "租户策略要的沙箱后端 "+name+" 在这个部署里没有登记")
		}
	}
	if policy.MinIsolation != "" {
		if _, ok := IsolationRank[policy.MinIsolation]; !ok {
			return Policy{}, providererr.New(providererr.Invalid, "invalid_min_isolation", "租户策略的 min_isolation 只能是 none、container、microvm")
		}
	}
	p.mu.Lock()
	p.cache[tenantID] = cached{at: time.Now(), policy: policy}
	p.mu.Unlock()
	return policy, nil
}
