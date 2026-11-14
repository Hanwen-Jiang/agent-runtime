package gateway

import (
	"errors"
	"regexp"
	"strings"
)

// SingleAttemptTenants is an immutable operator-only override. It changes only
// retries of the same selected profile, never routing, tier, output or timeout.
type SingleAttemptTenants struct{ exact map[string]struct{} }

var retryTenantID = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)

func ParseSingleAttemptTenants(value string) (*SingleAttemptTenants, error) {
	if strings.TrimSpace(value) == "" {
		return nil, nil
	}
	if len(value) > 8192 {
		return nil, errors.New("single-attempt tenant list exceeds limit")
	}
	policy := &SingleAttemptTenants{exact: map[string]struct{}{}}
	for _, part := range strings.Split(value, ",") {
		id := strings.TrimSpace(part)
		if !retryTenantID.MatchString(id) {
			return nil, errors.New("single-attempt policy requires exact tenant IDs")
		}
		if _, duplicate := policy.exact[id]; duplicate {
			return nil, errors.New("single-attempt policy contains duplicate tenant")
		}
		policy.exact[id] = struct{}{}
	}
	return policy, nil
}

func (p *SingleAttemptTenants) maxAttempts(tenant string, fallback int) int {
	if p != nil {
		if _, ok := p.exact[tenant]; ok {
			return 1
		}
	}
	return fallback
}
