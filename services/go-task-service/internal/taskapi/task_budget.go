package taskapi

import (
	"crypto/sha256"
	"encoding/hex"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

// submissionHash binds a request ID to its original meaning. Authentication is
// authoritative for tenant identity, so omitting/repeating tenant_id is harmless.
func submissionHash(req *taskapiv1.SubmitTaskRequest) (string, error) {
	// Existing clients keep their original retry semantics. Explicit per-task
	// budgets opt into binding the entire request, including the new limit.
	if req.BudgetUsdMicros == nil {
		return "", nil
	}
	normalized := proto.Clone(req).(*taskapiv1.SubmitTaskRequest)
	normalized.TenantId = ""
	raw, err := (proto.MarshalOptions{Deterministic: true}).Marshal(normalized)
	if err != nil {
		return "", invalid("提交内容无法编码")
	}
	digest := sha256.Sum256(raw)
	return hex.EncodeToString(digest[:]), nil
}
func checkSubmissionBudget(row taskstore.Task, req *taskapiv1.SubmitTaskRequest, hash string) error {
	if row.SubmissionHash != "" {
		if row.SubmissionHash != hash {
			return providererr.New(providererr.Conflict, "submission_mismatch", "同一提交 ID 的预算与请求内容不能改变")
		}
	} else if req.BudgetUsdMicros != nil {
		// An old, potentially signaled task has no pinned budget. Do not retrofit a
		// limit that may differ from what the workflow already received.
		return providererr.New(providererr.Conflict, "submission_budget_unpinned", "旧提交没有固定预算；请使用新的提交 ID")
	}
	return nil
}
func requestedBudget(policy *workflowv1.Budget, req *taskapiv1.SubmitTaskRequest) (*workflowv1.Budget, error) {
	var out *workflowv1.Budget
	if policy != nil {
		out = proto.Clone(policy).(*workflowv1.Budget)
	} else {
		out = &workflowv1.Budget{}
	}
	if req.BudgetUsdMicros == nil {
		return out, nil
	}
	want := req.GetBudgetUsdMicros()
	if want <= 0 {
		return nil, invalid("budget_usd_micros 必须为正数")
	}
	if out.GetUsdMicros() > 0 && want > out.GetUsdMicros() {
		return nil, providererr.New(providererr.Denied, "budget_exceeds_policy", "请求的美元预算高于租户策略允许的")
	}
	out.UsdMicros = want
	return out, nil
}
func frozenBudget(row taskstore.Task, fallback *workflowv1.Budget) (*workflowv1.Budget, error) {
	if row.SubmissionBudget == "" {
		return fallback, nil
	}
	out := &workflowv1.Budget{}
	if err := protojson.Unmarshal([]byte(row.SubmissionBudget), out); err != nil {
		return nil, providererr.New(providererr.Conflict, "submission_budget_invalid", "固定的提交预算无法读取")
	}
	return out, nil
}
