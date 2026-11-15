package gateway_test

import (
	"context"
	"reflect"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
	mg "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/gateway"
)

func singleAttemptPolicy(t *testing.T) *gateway.SingleAttemptTenants {
	t.Helper()
	p, e := gateway.ParseSingleAttemptTenants("eval-tb20-20261109")
	if e != nil {
		t.Fatal(e)
	}
	return p
}

func TestOperatorSingleAttemptSkipsRepeatedTimeoutAndRetainsUnknownUsage(t *testing.T) {
	h, _, store := budgetHarnessStore(t, 10_000_000, func(o *gateway.Options) { o.SingleAttemptTenants = singleAttemptPolicy(t) })
	h.fake.failNext("mid-a", transient("timeout", 0), transient("timeout", 0), transient("timeout", 0))
	response := h.mustGenerate(t, "eval-tb20-20261109", call(transform), say("work"))
	if response.Model.ModelProfileId != "test/mid-b@1" {
		t.Fatal("existing next candidate was not used")
	}
	models := []string{}
	for _, c := range h.fake.allCalls() {
		models = append(models, c.Model)
	}
	if !reflect.DeepEqual(models, []string{"mid-a", "mid-b"}) {
		t.Fatalf("same-profile retries remain: %v", models)
	}
	rows := usageRows(t, store, "eval-tb20-20261109")
	if len(rows) != 2 || rows[0]["actual_usd_micros"] != nil || rows[1]["actual_usd_micros"] == nil {
		t.Fatalf("unknown timeout charge was lost or synthesized: %+v", rows)
	}
	if len(h.sleeps.all()) != 0 {
		t.Fatal("fallback should not wait for retry backoff")
	}
}

func TestOperatorSingleAttemptRealDeadlineImmediatelyFallsBack(t *testing.T) {
	h := newHarnessOpts(t, nil, func(o *gateway.Options) {
		o.SingleAttemptTenants = singleAttemptPolicy(t)
		o.AttemptTimeout = 50 * time.Millisecond
	})
	h.fake.hangNext("mid-a", 3)
	started := time.Now()
	response := h.mustGenerate(t, "eval-tb20-20261109", call(transform), say("work"))
	if response.Model.ModelProfileId != "test/mid-b@1" || h.fake.callsTo("mid-a") != 1 || h.fake.callsTo("mid-b") != 1 {
		t.Fatal("deadline did not advance directly to existing fallback")
	}
	if time.Since(started) > time.Second || len(h.sleeps.all()) != 0 {
		t.Fatal("repeated timeout or retry backoff delayed fallback")
	}
}

func TestOperatorSingleAttemptDoesNotChangeOtherTenantRetries(t *testing.T) {
	h := newHarnessOpts(t, nil, func(o *gateway.Options) { o.SingleAttemptTenants = singleAttemptPolicy(t) })
	h.fake.failNext("mid-a", transient("timeout", 0), transient("timeout", 0), transient("timeout", 0))
	h.mustGenerate(t, "ordinary-user", call(transform), say("work"))
	models := []string{}
	for _, c := range h.fake.allCalls() {
		models = append(models, c.Model)
	}
	if !reflect.DeepEqual(models, []string{"mid-a", "mid-a", "mid-a", "mid-b"}) {
		t.Fatalf("ordinary retry behavior changed: %v", models)
	}
}

func TestOperatorSingleAttemptStillHonorsCallerCancellation(t *testing.T) {
	h := newHarnessOpts(t, nil, func(o *gateway.Options) {
		o.SingleAttemptTenants = singleAttemptPolicy(t)
		o.AttemptTimeout = time.Minute
	})
	h.fake.onGenerate("mid-a", func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		time.Sleep(100 * time.Millisecond)
		return modelprovider.GenerateResult{}, transient("timeout", 0)
	})
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	_, err := h.client.Generate(ctx, connect.NewRequest(&mg.GenerateRequest{Tenant: tenant("eval-tb20-20261109"), Call: call(transform), Generation: say("work")}))
	if err == nil {
		t.Fatal("caller cancellation ignored")
	}
}

func TestOperatorSingleAttemptConfigurationRequiresExactTenantIDs(t *testing.T) {
	for _, bad := range []string{"*", "eval-*", "_platform", "t1,", "t1,t1", "t1/other"} {
		if _, e := gateway.ParseSingleAttemptTenants(bad); e == nil {
			t.Fatalf("accepted %q", bad)
		}
	}
	if p, e := gateway.ParseSingleAttemptTenants(""); e != nil || p != nil {
		t.Fatal("empty override must preserve defaults")
	}
}
