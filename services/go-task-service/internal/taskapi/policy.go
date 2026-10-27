package taskapi

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"strings"

	"google.golang.org/protobuf/proto"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
)

// PolicyResolver 给出新 Task 的租户策略：预算与 effect 上限。租户由认证身份确定，不来自请求。
// 租户策略接入 Registry 之前用 StaticPolicy 或 FilePolicyResolver。
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
// effect_ceiling 取附录 A 的 effect 名。文件不存在、或租户没有列出时用 Fallback。条目里的 sandbox.network 与租户策略的
// 写法相同（D-16），FilePolicyResolver 同时是 NetworkPolicySource。
type FilePolicyResolver struct {
	Path     string
	Fallback Defaults
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

func (r FilePolicyResolver) Resolve(_ context.Context, tenant *commonv1.TenantContext) (Defaults, error) {
	out := r.Fallback.clone()
	file, found, err := r.read()
	if err != nil {
		return Defaults{}, err
	}
	if !found {
		return out, nil
	}
	entry, ok := file.Tenants[tenant.GetTenantId()]
	if !ok || entry.EffectCeiling == "" {
		return out, nil
	}
	effect, ok := commonv1.Effect_value["EFFECT_"+strings.ToUpper(entry.EffectCeiling)]
	if !ok || effect == int32(commonv1.Effect_EFFECT_UNSPECIFIED) {
		return Defaults{}, providererr.New(providererr.Invalid, "tenant_policy_malformed", "租户策略里的 effect_ceiling 不是附录 A 的 effect")
	}
	out.EffectCeiling = commonv1.Effect(effect)
	return out, nil
}

func (d Defaults) clone() Defaults {
	out := Defaults{EffectCeiling: d.EffectCeiling}
	if d.Budget != nil {
		out.Budget = proto.Clone(d.Budget).(*workflowv1.Budget)
	}
	return out
}
