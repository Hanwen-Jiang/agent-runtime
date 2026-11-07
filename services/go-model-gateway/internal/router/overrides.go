package router

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"regexp"
	"strings"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/profiles"
)

// RoutingTarget pins the actual served profile and full class@tier. Unlike a
// caller's preferred pin it is a deployment constraint, not a sorting hint.
type RoutingTarget struct {
	Profile string `json:"profile"`
	Tier    string `json:"tier"`
}

// RoutingOverrides is immutable after parsing. Only explicitly named tenants
// are affected; each of their actual logical call roles must be listed.
type RoutingOverrides struct {
	tenants map[string]map[string]RoutingTarget
}

var overrideTenant = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)

func validOverrideRole(role string) bool {
	if role == "summary" || role == "probe" || role == "embedding" {
		return true
	}
	v, ok := commonv1.Role_value["ROLE_"+strings.ToUpper(role)]
	return ok && v != 0 && role == strings.ToLower(strings.TrimPrefix(commonv1.Role(v).String(), "ROLE_"))
}

func LoadRoutingOverrides(path string, catalog *profiles.Catalog) (*RoutingOverrides, error) {
	if path == "" {
		return nil, nil
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, errors.New("routing overrides file unavailable")
	}
	return ParseRoutingOverrides(raw, catalog)
}

func ParseRoutingOverrides(raw []byte, catalog *profiles.Catalog) (*RoutingOverrides, error) {
	invalid := func() (*RoutingOverrides, error) {
		return nil, errors.New("routing overrides must name valid tenants, logical roles, profiles and matching class@tiers")
	}
	if len(raw) > 1<<20 || catalog == nil {
		return invalid()
	}
	var doc struct {
		Tenants map[string]map[string]RoutingTarget `json:"tenants"`
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&doc); err != nil {
		return invalid()
	}
	var tail any
	if err := dec.Decode(&tail); err != io.EOF {
		return invalid()
	}
	if doc.Tenants == nil {
		return invalid()
	}
	for tenant, roles := range doc.Tenants {
		if !overrideTenant.MatchString(tenant) || tenant == "_platform" || len(roles) == 0 {
			return invalid()
		}
		for role, target := range roles {
			if !validOverrideRole(role) {
				return invalid()
			}
			p, ok := catalog.Get(target.Profile)
			if !ok {
				return invalid()
			}
			ref, err := profiles.ParseTier(target.Tier)
			if err != nil || p.Class != ref.Class {
				return invalid()
			}
			if _, ok = p.Tier(ref.Name); !ok {
				return invalid()
			}
			if role == "embedding" && !p.Has(profiles.CapEmbed) {
				return invalid()
			}
		}
	}
	return &RoutingOverrides{tenants: doc.Tenants}, nil
}

// LogicalRole follows actual CallContext.Role. Main and child tool loops both
// use ACT even if their node's display role is fetch/deliver; child attribution
// remains a Temporal/ledger concern. Auxiliary calls use stable node suffixes.
func LogicalRole(kind Kind, role commonv1.Role, nodeID string) string {
	if kind == KindEmbed {
		return "embedding"
	}
	if strings.Contains(nodeID, "#summary") {
		return "summary"
	}
	if strings.Contains(nodeID, "#probe") {
		return "probe"
	}
	if role == commonv1.Role_ROLE_UNSPECIFIED {
		return ""
	}
	return strings.ToLower(strings.TrimPrefix(role.String(), "ROLE_"))
}

func (o *RoutingOverrides) Apply(req Request, nodeID string) (Request, error) {
	if o == nil {
		return req, nil
	}
	roles, forced := o.tenants[req.TenantID]
	if !forced {
		return req, nil
	}
	target, found := roles[LogicalRole(req.Kind, req.Role, nodeID)]
	if !found {
		return req, providererr.New(providererr.Denied, "routing_override_role_missing", "configured tenant has no route for this logical role")
	}
	if req.PinnedProfile != "" && req.PinnedProfile != target.Profile {
		return req, providererr.New(providererr.Denied, "routing_override_pin_conflict", "requested profile conflicts with the configured route")
	}
	req.Tier = target.Tier
	req.RequiredProfile = target.Profile
	return req, nil
}
