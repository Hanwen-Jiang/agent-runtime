package modelbudget

import (
	"context"
	"strings"
)

// WithOperatorUnlimitedTenants disables monetary admission caps only for the
// exact tenant IDs explicitly configured by the service operator. An empty
// list delegates unchanged to base; policy documents cannot grant exemptions.
// Usage reservations and settlements are still recorded by the ledger.
func WithOperatorUnlimitedTenants(base PolicyResolver, configured string) PolicyResolver {
	tenants := make(map[string]struct{})
	for _, item := range strings.Split(configured, ",") {
		if tenant := strings.TrimSpace(item); tenant != "" {
			tenants[tenant] = struct{}{}
		}
	}
	return operatorMonetaryPolicy{base: base, tenants: tenants}
}

type operatorMonetaryPolicy struct {
	base    PolicyResolver
	tenants map[string]struct{}
}

func (p operatorMonetaryPolicy) Resolve(ctx context.Context, tenant string) (Limits, error) {
	if err := ctx.Err(); err != nil {
		return Limits{}, err
	}
	if _, configured := p.tenants[tenant]; configured {
		return Limits{}, nil
	}
	return p.base.Resolve(ctx, tenant)
}
