package gateway_test

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/modelbudget"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/registryref"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	mg "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/gateway"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/profiles"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/router"
)

type fixedBudget modelbudget.Limits

func (b fixedBudget) Resolve(context.Context, string) (modelbudget.Limits, error) {
	return modelbudget.Limits(b), nil
}

var budgetTime = time.Date(2026, 11, 3, 0, 0, 0, 0, time.UTC)

func budgetHarness(t *testing.T, amount int64, adjust ...func(*gateway.Options)) (*harness, *modelbudget.Ledger) {
	t.Helper()
	h, ledger, _ := budgetHarnessStore(t, amount, adjust...)
	return h, ledger
}

func budgetHarnessStore(t *testing.T, amount int64, adjust ...func(*gateway.Options)) (*harness, *modelbudget.Ledger, *sqlitestore.Provider) {
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
	return h, ledger, p
}

// usageRows reads the tenant's model_usage rows ordered by profile.
func usageRows(t *testing.T, p *sqlitestore.Provider, tenantID string) []relational.Row {
	t.Helper()
	s, err := p.Bind(&commonv1.TenantContext{TenantId: tenantID})
	if err != nil {
		t.Fatal(err)
	}
	var rows []relational.Row
	err = s.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		var err error
		rows, err = tx.Query(ctx, `SELECT model_profile_id, actual_usd_micros, price_currency, price_unit, price_at,
			in_usd_micros_per_m, out_usd_micros_per_m, cache_read_usd_micros_per_m, input_tokens, output_tokens, cache_read_tokens,
			registry_scope, registry_revision, registry_document_sha256
			FROM model_usage ORDER BY model_profile_id, reservation_id`)
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	return rows
}

// R-06-C: each attempt's row carries the profile price it was charged at and
// the provider usage it was settled with, so the charge can be recomputed
// after the price table changes.
func TestAttemptRowFreezesProfilePriceAndUsage(t *testing.T) {
	h, _, p := budgetHarnessStore(t, 1_000_000)
	h.mustGenerate(t, "t1", call(transform), say("hi"))
	rows := usageRows(t, p, "t1")
	if len(rows) != 1 {
		t.Fatalf("rows: %v", rows)
	}
	want := relational.Row{
		"model_profile_id": "test/mid-a@1", "actual_usd_micros": int64(435),
		"price_currency": "USD", "price_unit": "usd_micros_per_1m_tokens", "price_at": "2026-10-21",
		"in_usd_micros_per_m": int64(150_000), "out_usd_micros_per_m": int64(600_000), "cache_read_usd_micros_per_m": int64(75_000),
		"input_tokens": int64(1000), "output_tokens": int64(500), "cache_read_tokens": int64(200),
	}
	for k, v := range want {
		if rows[0][k] != v {
			t.Errorf("%s = %#v, want %#v", k, rows[0][k], v)
		}
	}
	frozen := modelbudget.Price{InPerMMicros: 150_000, OutPerMMicros: 600_000, CacheReadPerMMicros: 75_000, PriceAt: "2026-10-21"}
	if got := frozen.USDMicros(modelbudget.Usage{InputTokens: 1000, OutputTokens: 500, CacheReadTokens: 200}); got != 435 {
		t.Fatalf("recomputed %d, settled 435", got)
	}
}

func TestFallbackAttemptsEachFreezeTheirOwnPrice(t *testing.T) {
	var file map[string]json.RawMessage
	if err := json.Unmarshal([]byte(testProfiles), &file); err != nil {
		t.Fatal(err)
	}
	var docs []json.RawMessage
	if err := json.Unmarshal(file["profiles"], &docs); err != nil {
		t.Fatal(err)
	}
	refs := map[string]registryref.Profile{}
	for _, doc := range docs {
		var profile struct {
			ID string `json:"id"`
		}
		if err := json.Unmarshal(doc, &profile); err != nil {
			t.Fatal(err)
		}
		hash, err := registryref.Digest(doc)
		if err != nil {
			t.Fatal(err)
		}
		version := "v11"
		if profile.ID == "test/mid-b@1" {
			version = "v12"
		}
		refs[profile.ID] = registryref.Profile{Scope: "platform", Version: version, SHA256: hash}
	}
	file["registry_revisions"], _ = json.Marshal(refs)
	encoded, _ := json.Marshal(file)
	catalog, err := profiles.Parse(encoded)
	if err != nil {
		t.Fatal(err)
	}
	h, _, p := budgetHarnessStore(t, 1_000_000, func(o *gateway.Options) { o.Router = router.New(catalog, nil) })
	h.fake.onGenerate("mid-a", callsTool("Bash"))
	h.mustGenerate(t, "t1", call(commonv1.Role_ROLE_ACT), withSearchTool(t, "找一下"))
	rows := usageRows(t, p, "t1")
	if len(rows) != 2 || rows[0]["model_profile_id"] != "test/mid-a@1" || rows[1]["model_profile_id"] != "test/mid-b@1" {
		t.Fatalf("a billed failure and its fallback are two rows: %v", rows)
	}
	prices := []modelbudget.Price{
		{InPerMMicros: 150_000, OutPerMMicros: 600_000, CacheReadPerMMicros: 75_000, PriceAt: "2026-10-21"},
		{InPerMMicros: 300_000, OutPerMMicros: 2_500_000, CacheReadPerMMicros: 30_000, PriceAt: "2026-10-28"},
	}
	for i, r := range rows {
		ref := refs[r["model_profile_id"].(string)]
		if r["registry_scope"] != ref.Scope || r["registry_revision"] != ref.Version || r["registry_document_sha256"] != ref.SHA256 {
			t.Errorf("fallback attempt did not freeze its actual Registry version: %v", r)
		}
		got := modelbudget.Price{
			InPerMMicros: r["in_usd_micros_per_m"].(int64), OutPerMMicros: r["out_usd_micros_per_m"].(int64),
			CacheReadPerMMicros: r["cache_read_usd_micros_per_m"].(int64), PriceAt: r["price_at"].(string),
		}
		if got != prices[i] {
			t.Errorf("%s froze %+v, want %+v", r["model_profile_id"], got, prices[i])
		}
		u := modelbudget.Usage{InputTokens: r["input_tokens"].(int64), OutputTokens: r["output_tokens"].(int64), CacheReadTokens: r["cache_read_tokens"].(int64)}
		if again := got.USDMicros(u); again != r["actual_usd_micros"] {
			t.Errorf("%s recomputes to %d, settled %v", r["model_profile_id"], again, r["actual_usd_micros"])
		}
	}
}

func TestPendingAttemptKeepsItsPriceAndNoUsage(t *testing.T) {
	h, _, p := budgetHarnessStore(t, 1_000_000)
	h.fake.onGenerate("mid-a", func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		return modelprovider.GenerateResult{}, providererr.New(providererr.Invalid, "bad_structured_output", "invalid output without usage")
	})
	if _, err := h.generate(t, "t1", call(transform), say("hi")); err == nil {
		t.Fatal("expected invalid output")
	}
	rows := usageRows(t, p, "t1")
	if len(rows) != 1 {
		t.Fatalf("rows: %v", rows)
	}
	// Unknown usage stays unknown (NULL), never zero tokens.
	if r := rows[0]; r["price_at"] != "2026-10-21" || r["in_usd_micros_per_m"] != int64(150_000) || r["input_tokens"] != nil || r["actual_usd_micros"] != nil {
		t.Fatalf("pending row: %v", r)
	}
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
