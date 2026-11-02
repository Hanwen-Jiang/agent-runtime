package modelbudget

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"math"
	"os"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Policy uses the same current policy source for Task admission and Gateway.
// File default is the deployment's hard ceiling; tenant entries and Registry
// documents may lower it. Missing values inherit $3/day and $20/natural month.
type Policy struct {
	Path   string
	Source func(context.Context, *commonv1.TenantContext) ([]byte, bool, error)
}

type PolicyResolver interface {
	Resolve(context.Context, string) (Limits, error)
}

func DefaultLimits() Limits {
	return Limits{Daily: Limit{Enabled: true, USDMicros: 3_000_000}, Monthly: Limit{Enabled: true, USDMicros: 20_000_000}}
}

type budgetDocument struct {
	DailyUSD   *float64 `json:"daily_usd"`
	MonthlyUSD *float64 `json:"monthly_usd"`
}

// Other policy domains are validated by their owner/Registry schema. Budget
// keys and top-level keys are strict here, including development files.
type policyDocument struct {
	EffectCeiling  json.RawMessage `json:"effect_ceiling"`
	ModelAllowlist json.RawMessage `json:"model_allowlist"`
	Hooks          json.RawMessage `json:"hooks"`
	Budget         json.RawMessage `json:"budget"`
	ModelBudget    *budgetDocument `json:"model_budget"`
	Sandbox        json.RawMessage `json:"sandbox"`
	Workspace      json.RawMessage `json:"workspace"`
}

func decodePolicy(data []byte, out any) error {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(out); err != nil {
		return policyInvalid()
	}
	if err := dec.Decode(new(any)); err != io.EOF {
		return policyInvalid()
	}
	return nil
}
func policyInvalid() error {
	return providererr.New(providererr.Invalid, "invalid_tenant_policy", "invalid model budget policy")
}
func applyBudget(out Limits, doc *budgetDocument) (Limits, error) {
	if doc == nil {
		return out, nil
	}
	for _, v := range []struct {
		src *float64
		dst *Limit
	}{{doc.DailyUSD, &out.Daily}, {doc.MonthlyUSD, &out.Monthly}} {
		if v.src == nil {
			continue
		}
		usd := *v.src
		if math.IsNaN(usd) || math.IsInf(usd, 0) || usd < 0 || usd*1e6 >= float64(math.MaxInt64) {
			return Limits{}, policyInvalid()
		}
		*v.dst = Limit{Enabled: true, USDMicros: int64(math.Round(usd * 1e6))}
	}
	return out, nil
}
func (p Policy) Resolve(ctx context.Context, tenantID string) (Limits, error) {
	var file struct {
		Default policyDocument            `json:"default"`
		Tenants map[string]policyDocument `json:"tenants"`
	}
	if p.Path != "" {
		data, err := os.ReadFile(p.Path)
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return Limits{}, providererr.New(providererr.Transient, "tenant_policy_unavailable", "cannot read model budget policy")
		}
		if err == nil {
			if err = decodePolicy(data, &file); err != nil {
				return Limits{}, err
			}
		}
	}
	hard, err := applyBudget(DefaultLimits(), file.Default.ModelBudget)
	if err != nil {
		return Limits{}, err
	}
	out := hard
	if p.Source != nil {
		data, found, err := p.Source(ctx, &commonv1.TenantContext{TenantId: tenantID})
		if err != nil {
			return Limits{}, providererr.New(providererr.Transient, "tenant_policy_unavailable", "cannot read model budget policy")
		}
		if found {
			var doc policyDocument
			if err := decodePolicy(data, &doc); err != nil {
				return Limits{}, err
			}
			out, err = applyBudget(out, doc.ModelBudget)
			if err != nil {
				return Limits{}, err
			}
		}
	}
	out, err = applyBudget(out, file.Tenants[tenantID].ModelBudget)
	if err != nil {
		return Limits{}, err
	}
	if out.Daily.USDMicros > hard.Daily.USDMicros || out.Monthly.USDMicros > hard.Monthly.USDMicros {
		return Limits{}, providererr.New(providererr.Invalid, "model_budget_exceeds_platform_limit", "tenant budget exceeds the platform hard ceiling")
	}
	return out, nil
}
