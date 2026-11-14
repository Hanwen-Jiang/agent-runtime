package taskapi

import (
	"encoding/json"
	"errors"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
	"net/http"
	"testing"
)

func TestNetworkScopesRespectTenantModes(t *testing.T) {
	for _, tc := range []struct {
		name  string
		modes []string
		scope taskapiv1.NetworkScope
		hosts []string
		ports []int32
		code  string
	}{
		{"legacy", nil, 0, []string{"api.example.com"}, nil, ""},
		{"explicit hosts", nil, 1, []string{"api.example.com"}, nil, ""},
		{"default public denied", nil, 2, nil, nil, "network_open_not_allowed"},
		{"public allowed", []string{"deny", "open"}, 2, nil, nil, ""},
		{"public with hosts", []string{"open"}, 2, []string{"api.example.com"}, nil, "invalid_network_request"},
		{"hosts disabled", []string{"deny", "open"}, 1, []string{"api.example.com"}, nil, "network_allowlist_not_allowed"},
		{"unknown scope", []string{"open"}, 9, nil, nil, "invalid_network_request"},
		{"public smtp", []string{"open"}, 2, nil, []int32{25}, "port_25"},
		{"hosts smtp", nil, 1, []string{"api.example.com"}, []int32{25}, "port_25"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req := &taskapiv1.NetworkRequest{Scope: tc.scope, Hosts: tc.hosts, Ports: tc.ports, Minutes: 15}
			out, err := CheckNetworkRequest(NetworkPolicy{Allowed: true, Modes: tc.modes, Hosts: []string{"api.example.com"}}, req, commonv1.Effect_EFFECT_WRITE_REVERSIBLE, nil)
			if tc.code == "" {
				if err != nil {
					t.Fatal(err)
				}
				want := "hosts"
				if tc.scope == 2 {
					want = "public"
				}
				if out.Scope != want {
					t.Fatalf("scope %q", out.Scope)
				}
				return
			}
			var pe *providererr.Error
			if !errors.As(err, &pe) || pe.Code != tc.code {
				t.Fatalf("want %s, got %v", tc.code, err)
			}
		})
	}
}

func TestSubmitPublicGrantPersistsScope(t *testing.T) {
	f := networkAPI(t)
	req := newRequest("public-test")
	req.NetworkRequest = &taskapiv1.NetworkRequest{Scope: taskapiv1.NetworkScope_NETWORK_SCOPE_PUBLIC, Minutes: 15}
	result := f.mustSubmit(f.token("tp", "u1"), req)
	rows := f.grantRows("tp")
	if len(rows) != 1 || rows[0]["scope"] != "public" || rows[0]["hosts"] != "[]" || rows[0]["task_id"] != result.GetTaskId() {
		t.Fatalf("grant: %v", rows)
	}

}

func TestNetworkPolicyViewUsesAuthenticatedTenant(t *testing.T) {
	f := networkAPI(t)
	for _, tc := range []struct {
		token   string
		status  int
		allowed bool
	}{
		{"", 401, false}, {f.token("tn", "u1"), 200, true}, {f.token("tx", "u1"), 200, false},
	} {
		req, _ := http.NewRequest(http.MethodGet, f.url+"/v1/network-policy?tenant=tn", nil)
		if tc.token != "" {
			req.Header.Set("Authorization", "Bearer "+tc.token)
		}
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		var body map[string]any
		_ = json.NewDecoder(res.Body).Decode(&body)
		res.Body.Close()
		if res.StatusCode != tc.status {
			t.Fatalf("%d %v", res.StatusCode, body)
		}
		if tc.status == 200 && body["allowed"] != tc.allowed {
			t.Fatalf("tenant leak: %v", body)
		}
		if res.Header.Get("Cache-Control") != "no-store" {
			t.Fatal("policy must not be cached")
		}
	}
}

func TestUnlimitedRequestIsExplicitAndPolicyGated(t *testing.T) {
	policy := NetworkPolicy{Allowed: true, AllowUnlimited: true, Modes: []string{"deny", "allowlist", "open"}, Hosts: []string{"github.com"}, MaxGrantMinutes: 60}
	request := &taskapiv1.NetworkRequest{Scope: taskapiv1.NetworkScope_NETWORK_SCOPE_PUBLIC, Minutes: 0, Unlimited: true}
	if _, err := CheckNetworkRequest(policy, request, commonv1.Effect_EFFECT_WRITE_REVERSIBLE, nil); err != nil {
		t.Fatal(err)
	}
	request.Unlimited = false
	if _, err := CheckNetworkRequest(policy, request, commonv1.Effect_EFFECT_WRITE_REVERSIBLE, nil); err == nil {
		t.Fatal("omitted minutes silently became unlimited")
	}
	request.Unlimited = true
	policy.AllowUnlimited = false
	if _, err := CheckNetworkRequest(policy, request, commonv1.Effect_EFFECT_WRITE_REVERSIBLE, nil); err == nil {
		t.Fatal("policy did not authorize unlimited")
	}
	request.Unlimited = false
	request.Minutes = 60
	if _, err := CheckNetworkRequest(policy, request, commonv1.Effect_EFFECT_WRITE_REVERSIBLE, nil); err != nil {
		t.Fatal(err)
	}
	request.Scope = taskapiv1.NetworkScope_NETWORK_SCOPE_HOSTS
	request.Hosts = []string{"api.github.com"}
	if _, err := CheckNetworkRequest(policy, request, commonv1.Effect_EFFECT_WRITE_REVERSIBLE, nil); err != nil {
		t.Fatal("open scope should allow its narrower host subset", err)
	}
	policy.Modes = []string{"deny", "allowlist"}
	if _, err := CheckNetworkRequest(policy, request, commonv1.Effect_EFFECT_WRITE_REVERSIBLE, nil); err == nil {
		t.Fatal("restricted host policy was bypassed")
	}
}
