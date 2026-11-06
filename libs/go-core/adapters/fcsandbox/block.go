package fcsandbox

import (
	"context"
	"encoding/json"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/sandboxagent"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"net/http"
)

func (b *box) blockTarget(ctx context.Context, envID, method string, p sandbox.PreparedTransfer, target sandbox.TransferTarget) (sandbox.TransferTarget, error) {
	status, data, err := b.p.call(ctx, http.MethodPost, "/v1/envs/"+envID+"/blob-grants", b.owner, map[string]any{"objects": []any{map[string]any{"method": method, "sha256": p.SHA256, "size_bytes": p.SizeBytes, "target": target}}})
	if err != nil {
		return sandbox.TransferTarget{}, err
	}
	if err = sandboxagent.RaiseFor(status, data); err != nil {
		return sandbox.TransferTarget{}, err
	}
	var out struct {
		Objects []sandbox.TransferTarget `json:"objects"`
	}
	if json.Unmarshal(data, &out) != nil || len(out.Objects) != 1 {
		return sandbox.TransferTarget{}, providererr.New(providererr.Transient, "sandbox_bad_response", "invalid block grant")
	}
	return out.Objects[0], nil
}
