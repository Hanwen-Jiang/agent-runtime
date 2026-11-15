package taskapi

import (
	"encoding/json"
	"net/http"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
)

// networkPolicyView exposes only the authenticated tenant's network controls.
// It is a current-policy projection; SubmitTask always checks policy again.
func (s *Server) networkPolicyView(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	id, err := s.authenticate(r.Context(), r.Header, "network_policy")
	if err != nil {
		http.Error(w, "authentication required", downloadStatus(err))
		return
	}
	tenant, err := ingress.TenantContextFor(id, "", "", "", nil)
	if err != nil {
		http.Error(w, "invalid identity", downloadStatus(err))
		return
	}
	policy, err := s.networkPolicy(r.Context(), tenant)
	if err != nil {
		http.Error(w, "network policy unavailable", downloadStatus(err))
		return
	}
	modes := policy.Modes
	if modes == nil {
		modes = []string{"deny", "allowlist"}
	}
	if !policy.Allowed {
		modes = []string{"deny"}
	}
	hosts := policy.Hosts
	if hosts == nil {
		hosts = []string{}
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"allowed": policy.Allowed, "modes": modes, "hosts": hosts, "max_grant_minutes": policy.maxMinutes(), "approver": policy.approver(), "supports_unlimited": policy.Allowed && policy.AllowUnlimited})
}
