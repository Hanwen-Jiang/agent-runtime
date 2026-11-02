package taskapi

import (
	"context"
	"testing"

	"connectrpc.com/connect"
	"github.com/agentruntime/agent-runtime/libs/go-core/modelbudget"
)

func TestTaskAdmissionReadsSharedModelBudgetAndKeepsDeduplication(t *testing.T) {
	var ledger *modelbudget.Ledger
	f := newAPIWith(t, func(d *Deps) {
		if err := d.Store.Migrate(context.Background(), modelbudget.Migrations()); err != nil {
			t.Fatal(err)
		}
		ledger = modelbudget.New(d.Store)
		d.ModelBudget = ledger
		d.BudgetPolicies = modelbudget.Policy{}
	})
	token := f.token("t1", "u1")
	first := f.mustSubmit(token, newRequest("first"))
	r, err := ledger.Reserve(context.Background(), modelbudget.ReservationRequest{
		TenantID: "t1", At: f.clock.Now(), USDMicros: 3_000_000, Limits: modelbudget.DefaultLimits(),
		TaskID: first.GetTaskId(), ModelProfileID: "test/model", Vendor: "test", Method: "generate",
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := ledger.Settle(context.Background(), r, 3_000_000); err != nil {
		t.Fatal(err)
	}
	_, err = f.submit(token, newRequest("second"))
	wantCode(t, "exhausted model budget", err, connect.CodePermissionDenied)
	again := f.mustSubmit(token, newRequest("first"))
	if !again.GetDeduplicated() || again.GetTaskId() != first.GetTaskId() {
		t.Fatalf("existing Task lost: %v", again)
	}
	f.mustSubmit(f.token("t2", "u1"), newRequest("second"))
}
