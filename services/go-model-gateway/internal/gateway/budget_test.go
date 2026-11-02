package gateway_test

import (
	"context"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/modelbudget"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	mg "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/gateway"
)

type fixedBudget modelbudget.Limits

func (b fixedBudget) Resolve(context.Context, string) (modelbudget.Limits, error) {
	return modelbudget.Limits(b), nil
}

var budgetTime = time.Date(2026, 11, 3, 0, 0, 0, 0, time.UTC)

func budgetHarness(t *testing.T, amount int64, adjust ...func(*gateway.Options)) (*harness, *modelbudget.Ledger) {
	t.Helper()
	p, err := sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { p.Close() })
	if err := p.Migrate(ctx, modelbudget.Migrations()); err != nil {
		t.Fatal(err)
	}
	ledger := modelbudget.New(p)
	h := newHarnessOpts(t, nil, func(o *gateway.Options) {
		o.ModelBudget = ledger
		o.BudgetPolicies = fixedBudget{Daily: modelbudget.Limit{Enabled: true, USDMicros: amount}}
		o.Now = func() time.Time { return budgetTime }
		for _, f := range adjust {
			f(o)
		}
	})
	return h, ledger
}

func TestPendingCallPreventsConcurrentOverspend(t *testing.T) {
	h, _ := budgetHarness(t, 15_000)
	entered, release := make(chan struct{}), make(chan struct{})
	h.fake.onGenerate("mid-a", func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		close(entered)
		<-release
		return modelprovider.GenerateResult{Text: "done"}, nil
	})
	done := make(chan error, 1)
	go func() { _, err := h.generate(t, "t1", call(transform), say("hi")); done <- err }()
	defer func() {
		close(release)
		if err := <-done; err != nil {
			t.Errorf("already running request must finish: %v", err)
		}
	}()
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("provider not entered")
	}
	_, err := h.generate(t, "t1", call(transform), say("hi"))
	_, detail := errorDetail(t, err)
	if detail.GetCode() != modelbudget.TenantBudgetExceededCode {
		t.Fatalf("concurrent admission: %v", detail)
	}
	if len(h.fake.allCalls()) != 1 {
		t.Fatal("second provider call bypassed reservation")
	}
}

type settlementFailure struct{ modelbudget.Provider }

func (s settlementFailure) Settle(context.Context, modelbudget.Reservation, int64) error {
	return providererr.New(providererr.Transient, "storage_down", "storage down")
}
func TestSettlementFailureDoesNotRepeatBilledCall(t *testing.T) {
	h, _ := budgetHarness(t, 1_000_000, func(o *gateway.Options) { o.ModelBudget = settlementFailure{o.ModelBudget} })
	_, err := h.generate(t, "t1", call(transform), say("hi"))
	code, detail := errorDetail(t, err)
	if code != connect.CodeAborted || detail.GetCode() != "model_usage_unsettled" {
		t.Fatalf("settlement failure: %v %v", code, detail)
	}
	if len(h.fake.allCalls()) != 1 {
		t.Fatal("settlement error retried model")
	}
}

type partialStreamProvider struct{ *fakeProvider }

func (p partialStreamProvider) Bind(t *commonv1.TenantContext) (modelprovider.Client, error) {
	c, err := p.fakeProvider.Bind(t)
	if err != nil {
		return nil, err
	}
	return partialStreamClient{Client: c}, nil
}

type partialStreamClient struct{ modelprovider.Client }

func (c partialStreamClient) StreamGenerate(context.Context, modelprovider.GenerateRequest) (modelprovider.Stream, error) {
	return &partialUsageStream{}, nil
}

type partialUsageStream struct{ sent bool }

func (s *partialUsageStream) Recv(context.Context) (modelprovider.Chunk, error) {
	if !s.sent {
		s.sent = true
		return modelprovider.Chunk{TextDelta: "partial"}, nil
	}
	return modelprovider.Chunk{}, providererr.New(providererr.Transient, "stream_truncated", "upstream disconnected")
}
func (s *partialUsageStream) Close() error { return nil }
func (s *partialUsageStream) Usage() *commonv1.Usage {
	return &commonv1.Usage{InputTokens: 1000, OutputTokens: 500, CacheReadTokens: 200}
}
func TestInterruptedStreamSettlesReceivedUsage(t *testing.T) {
	h, ledger := budgetHarness(t, 1_000_000, func(o *gateway.Options) { o.Provider = partialStreamProvider{newFakeProvider()} })
	_, err := h.streamAll(t, &mg.StreamGenerateRequest{Tenant: tenant("t1"), Call: call(transform), Generation: say("hi")})
	if err == nil {
		t.Fatal("truncated stream succeeded")
	}
	for _, limit := range []int64{435, 436} {
		err := ledger.Check(ctx, "t1", budgetTime, modelbudget.Limits{Daily: modelbudget.Limit{Enabled: true, USDMicros: limit}})
		if (limit == 435) != modelbudget.IsTenantBudgetExceeded(err) {
			t.Fatalf("partial usage settlement at %d: %v", limit, err)
		}
	}
}

func TestBudgetDenialDoesNotCallRetryOrFallback(t *testing.T) {
	h, _ := budgetHarness(t, 0)
	methods := []func() error{
		func() error { _, e := h.generate(t, "t1", call(transform), say("hi")); return e },
		func() error {
			_, e := h.streamAll(t, &mg.StreamGenerateRequest{Tenant: tenant("t1"), Call: call(transform), Generation: say("hi")})
			return e
		},
		func() error {
			_, e := h.client.Score(ctx, connect.NewRequest(&mg.ScoreRequest{Tenant: tenant("t1"), Call: call(commonv1.Role_ROLE_JUDGE), Query: "q", Candidates: []string{"a"}}))
			return e
		},
		func() error {
			_, e := h.client.Embed(ctx, connect.NewRequest(&mg.EmbedRequest{Tenant: tenant("t1"), Texts: []string{"a"}}))
			return e
		},
		func() error {
			_, e := h.client.Classify(ctx, connect.NewRequest(&mg.ClassifyRequest{Tenant: tenant("t1"), Call: call(commonv1.Role_ROLE_ROUTE), Texts: []string{"a"}, Labels: []string{"x"}}))
			return e
		},
	}
	for _, do := range methods {
		code, detail := errorDetail(t, do())
		if code != connect.CodePermissionDenied || detail.GetCode() != modelbudget.TenantBudgetExceededCode {
			t.Fatalf("budget denial: %v %v", code, detail)
		}
	}
	if len(h.fake.allCalls()) != 0 {
		t.Fatal("budget denial invoked provider")
	}
}

func TestGatewaySettlesActualCachedUsageAndRefunds(t *testing.T) {
	h, ledger := budgetHarness(t, 1_000_000)
	h.mustGenerate(t, "t1", call(transform), say("hi"))
	limit := modelbudget.Limits{Daily: modelbudget.Limit{Enabled: true, USDMicros: 435}}
	if err := ledger.Check(ctx, "t1", budgetTime, limit); !modelbudget.IsTenantBudgetExceeded(err) {
		t.Fatalf("expected actual cached price 435: %v", err)
	}
	limit.Daily.USDMicros = 436
	if err := ledger.Check(ctx, "t1", budgetTime, limit); err != nil {
		t.Fatalf("reservation surplus was not refunded: %v", err)
	}
	if err := ledger.Check(ctx, "t2", budgetTime, limit); err != nil {
		t.Fatalf("tenant isolation: %v", err)
	}
}

func TestGatewayAccountsBilledFailureBeforeRetry(t *testing.T) {
	h, ledger := budgetHarness(t, 1_000_000)
	h.fake.onGenerate("mid-a", func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		return modelprovider.GenerateResult{Usage: &commonv1.Usage{InputTokens: 1000, OutputTokens: 500, CacheReadTokens: 200}}, providererr.New(providererr.Denied, "upstream_denied", "denied after usage")
	})
	_, err := h.generate(t, "t1", call(transform), say("hi"))
	if err == nil || len(h.fake.allCalls()) != 1 {
		t.Fatalf("nonretry billed failure: %v", err)
	}
	if err := ledger.Check(ctx, "t1", budgetTime, modelbudget.Limits{Daily: modelbudget.Limit{Enabled: true, USDMicros: 435}}); !modelbudget.IsTenantBudgetExceeded(err) {
		t.Fatalf("billed failure lost: %v", err)
	}
}

func TestMissingUsageOnInvalidOutputRetainsReservation(t *testing.T) {
	h, ledger := budgetHarness(t, 1_000_000)
	h.fake.onGenerate("mid-a", func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		return modelprovider.GenerateResult{}, providererr.New(providererr.Invalid, "bad_structured_output", "invalid output without usage")
	})
	if _, err := h.generate(t, "t1", call(transform), say("hi")); err == nil {
		t.Fatal("expected invalid output")
	}
	if err := ledger.Check(ctx, "t1", budgetTime, modelbudget.Limits{Daily: modelbudget.Limit{Enabled: true, USDMicros: 1}}); !modelbudget.IsTenantBudgetExceeded(err) {
		t.Fatalf("unknown usage incorrectly refunded: %v", err)
	}
}
