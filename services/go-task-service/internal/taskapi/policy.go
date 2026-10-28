package taskapi

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"os"
	"strings"
	"time"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/durationpb"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
)

// PolicyResolver 给出新 Task 的租户策略：预算与 effect 上限。租户由认证身份确定，不来自请求。
// 开了 Registry 时按它的租户策略（DocumentPolicyResolver），开发用的策略文件里列出的租户以文件为准；没开时用 FilePolicyResolver。
type PolicyResolver interface {
	Resolve(ctx context.Context, tenant *commonv1.TenantContext) (Defaults, error)
}

// StaticPolicy 对所有租户给同一份默认值。
type StaticPolicy struct{ Defaults Defaults }

func (p StaticPolicy) Resolve(context.Context, *commonv1.TenantContext) (Defaults, error) {
	return p.Defaults.clone(), nil
}

// FilePolicyResolver 每次都重新读取一个 JSON 文件（开发实现），按租户覆盖默认值：
//
//	{"tenants": {"<tenant_id>": {"effect_ceiling": "write_irreversible"}}}
//
// effect_ceiling 取附录 A 的 effect 名。文件不存在、或租户没有列出时交给 Else（接了 Registry 时是 DocumentPolicyResolver），
// 没有 Else 时用 Fallback。条目里的 sandbox.network 与租户策略的写法相同（D-16），FilePolicyResolver 同时是 NetworkPolicySource。
type FilePolicyResolver struct {
	Path     string
	Fallback Defaults
	Else     PolicyResolver
}

// DocumentPolicyResolver 从租户策略文档（Registry 的当前租户策略，没有自己策略的租户是平台默认的）取 effect_ceiling 与
// budget；文档里没写的项、没有文档时用 Fallback。
type DocumentPolicyResolver struct {
	Source   func(ctx context.Context, tenant *commonv1.TenantContext) (doc []byte, found bool, err error)
	Fallback Defaults
}

func (r DocumentPolicyResolver) Resolve(ctx context.Context, tenant *commonv1.TenantContext) (Defaults, error) {
	out := r.Fallback.clone()
	doc, found, err := r.Source(ctx, tenant)
	if err != nil {
		return Defaults{}, providererr.Wrap(providererr.Transient, "tenant_policy_unavailable", "读不到租户策略", err)
	}
	if !found {
		return out, nil
	}
	var policy struct {
		EffectCeiling string `json:"effect_ceiling"`
		Budget        *struct {
			USD          *float64 `json:"usd"`
			ToolCalls    *int32   `json:"tool_calls"`
			WallClockMin *float64 `json:"wall_clock_min"`
		} `json:"budget"`
	}
	if err := json.Unmarshal(doc, &policy); err != nil {
		return Defaults{}, providererr.Wrap(providererr.Invalid, "tenant_policy_malformed", "租户策略解析不了", err)
	}
	if policy.EffectCeiling != "" {
		effect, err := parseEffect(policy.EffectCeiling)
		if err != nil {
			return Defaults{}, err
		}
		out.EffectCeiling = effect
	}
	if b := policy.Budget; b != nil {
		if out.Budget == nil {
			out.Budget = &workflowv1.Budget{}
		}
		if b.USD != nil {
			out.Budget.UsdMicros = int64(math.Round(*b.USD * 1e6))
		}
		if b.ToolCalls != nil {
			out.Budget.ToolCalls = *b.ToolCalls
		}
		if b.WallClockMin != nil {
			out.Budget.WallClock = durationpb.New(time.Duration(*b.WallClockMin * float64(time.Minute)))
		}
	}
	return out, nil
}

func parseEffect(name string) (commonv1.Effect, error) {
	effect, ok := commonv1.Effect_value["EFFECT_"+strings.ToUpper(name)]
	if !ok || effect == int32(commonv1.Effect_EFFECT_UNSPECIFIED) {
		return 0, providererr.New(providererr.Invalid, "tenant_policy_malformed", "租户策略里的 effect_ceiling 不是附录 A 的 effect")
	}
	return commonv1.Effect(effect), nil
}

type policyEntry struct {
	EffectCeiling string `json:"effect_ceiling"`
	Sandbox       struct {
		Network NetworkPolicy `json:"network"`
	} `json:"sandbox"`
}

type policyFile struct {
	Tenants map[string]policyEntry `json:"tenants"`
}

func (r FilePolicyResolver) read() (policyFile, bool, error) {
	if r.Path == "" {
		return policyFile{}, false, nil
	}
	data, err := os.ReadFile(r.Path)
	if errors.Is(err, os.ErrNotExist) {
		return policyFile{}, false, nil
	}
	if err != nil {
		return policyFile{}, false, providererr.Wrap(providererr.Transient, "tenant_policy_unavailable", "读不到租户策略文件", err)
	}
	var file policyFile
	if err := json.Unmarshal(data, &file); err != nil {
		return policyFile{}, false, providererr.Wrap(providererr.Invalid, "tenant_policy_malformed", "租户策略文件格式不对", err)
	}
	return file, true, nil
}

// NetworkPolicy 实现 NetworkPolicySource：租户条目里的 sandbox.network；没有时不允许联网。
func (r FilePolicyResolver) NetworkPolicy(_ context.Context, tenant *commonv1.TenantContext) (NetworkPolicy, error) {
	file, ok, err := r.read()
	if err != nil || !ok {
		return NetworkPolicy{}, err
	}
	return file.Tenants[tenant.GetTenantId()].Sandbox.Network, nil
}

func (r FilePolicyResolver) Resolve(ctx context.Context, tenant *commonv1.TenantContext) (Defaults, error) {
	out := r.Fallback.clone()
	file, found, err := r.read()
	if err != nil {
		return Defaults{}, err
	}
	entry, listed := file.Tenants[tenant.GetTenantId()]
	if !found || !listed || entry.EffectCeiling == "" {
		if r.Else != nil {
			return r.Else.Resolve(ctx, tenant)
		}
		return out, nil
	}
	effect, err := parseEffect(entry.EffectCeiling)
	if err != nil {
		return Defaults{}, err
	}
	out.EffectCeiling = effect
	return out, nil
}

func (d Defaults) clone() Defaults {
	out := Defaults{EffectCeiling: d.EffectCeiling}
	if d.Budget != nil {
		out.Budget = proto.Clone(d.Budget).(*workflowv1.Budget)
	}
	return out
}
