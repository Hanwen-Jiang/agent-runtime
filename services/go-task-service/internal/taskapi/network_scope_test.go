package taskapi

// NET-OPEN 的契约草案（docs/plan/net-open-contract.md，等人审）落地之前：scope 为 PUBLIC 的申请一律 denied
// （network_scope_unavailable），不留授权；不写 scope 与写 HOSTS 照旧。

import (
	"errors"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
)

func TestPublicNetworkScopeIsUnavailableUntilTheContractIsApproved(t *testing.T) {
	policy := NetworkPolicy{Allowed: true, Hosts: []string{"api.example.com"}}
	public := &taskapiv1.NetworkRequest{Scope: taskapiv1.NetworkScope_NETWORK_SCOPE_PUBLIC, Minutes: 15, Reason: "查资料"}
	_, err := CheckNetworkRequest(policy, public, commonv1.Effect_EFFECT_WRITE_IRREVERSIBLE, nil)
	var pe *providererr.Error
	if !errors.As(err, &pe) || pe.Category != providererr.Denied || pe.Code != "network_scope_unavailable" {
		t.Fatalf("PUBLIC 还没实现，应 denied / network_scope_unavailable：%v", err)
	}
	for _, scope := range []taskapiv1.NetworkScope{taskapiv1.NetworkScope_NETWORK_SCOPE_UNSPECIFIED, taskapiv1.NetworkScope_NETWORK_SCOPE_HOSTS} {
		req := &taskapiv1.NetworkRequest{Scope: scope, Hosts: []string{"api.example.com"}, Minutes: 15, Reason: "拉取 API 文档"}
		if _, err := CheckNetworkRequest(policy, req, commonv1.Effect_EFFECT_WRITE_IRREVERSIBLE, nil); err != nil {
			t.Errorf("%v 照旧按 hosts 核对：%v", scope, err)
		}
	}
}
