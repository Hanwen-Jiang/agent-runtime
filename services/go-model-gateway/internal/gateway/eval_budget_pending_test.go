package gateway_test

// Paid evaluations set CallContext.max_cost_usd_micros. An attempt whose usage
// is unknown must fail closed: a retained tenant reservation is not permission
// to retry/fallback under a request limit that only tracks settled charges.
// These tests call the real Server methods and SQLite ledger directly, reusing
// the existing profile/provider/budget helpers without requiring a TCP socket.

import (
	"context"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/modelbudget"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	mg "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1/modelgatewayv1connect"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/gateway"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/profiles"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/router"
)

type evalBudgetDirect struct {
	server *gateway.Server
	fake   *fakeProvider
	store  *sqlitestore.Provider
	ledger *modelbudget.Ledger
	sleeps *sleepRecorder
}

func newEvalBudgetDirect(t *testing.T) *evalBudgetDirect {
	t.Helper()
	store, err := sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { store.Close() })
	if err := store.Migrate(ctx, modelbudget.Migrations()); err != nil {
		t.Fatal(err)
	}
	catalog, err := profiles.Parse([]byte(testProfiles))
	if err != nil {
		t.Fatal(err)
	}
	h := &evalBudgetDirect{fake: newFakeProvider(), store: store, ledger: modelbudget.New(store), sleeps: &sleepRecorder{}}
	h.server, err = gateway.New(gateway.Options{
		Provider: h.fake, Router: router.New(catalog, nil), ModelBudget: h.ledger,
		BudgetPolicies: fixedBudget{Daily: modelbudget.Limit{Enabled: true, USDMicros: 5_000_000}},
		Now:            func() time.Time { return budgetTime }, Retry: testRetry,
		Sleep: h.sleeps.sleep, Jitter: func(d time.Duration) time.Duration { return d },
		Meter: &syncBuffer{}, Logger: slog.New(slog.NewTextHandler(&syncBuffer{}, nil)),
	})
	if err != nil {
		t.Fatal(err)
	}
	return h
}

func (h *evalBudgetDirect) generate(limit int64, params *mg.GenerateParams) (*connect.Response[mg.GenerateResponse], error) {
	cc := call(transform)
	cc.MaxCostUsdMicros = limit
	return h.server.Generate(context.Background(), connect.NewRequest(&mg.GenerateRequest{Tenant: tenant("t-eval"), Call: cc, Generation: params}))
}

func assertEvalPendingError(t *testing.T, err error, knownMicros int64) {
	t.Helper()
	if err == nil {
		t.Error("request with a hard cost limit succeeded while an attempt's bill is unknown")
		return
	}
	_, detail := errorDetail(t, err)
	if detail.GetCategory() == commonv1.ErrorCategory_ERROR_CATEGORY_TRANSIENT {
		t.Errorf("pending usage must not invite another paid attempt: %v", detail)
	}
	// The implementation owns the precise pending/unsettled code, but callers
	// must be able to distinguish ledger uncertainty from a model output error.
	if !strings.HasPrefix(detail.GetCode(), "model_usage_") {
		t.Errorf("unknown charge must be surfaced as a model_usage_* error: %v", detail)
	}
	actual, _ := billedMicros(detail)
	if actual != knownMicros {
		t.Errorf("known billed cost = %d, want %d (pending estimate is not actual spend)", actual, knownMicros)
	}
}

func TestEvalBudgetUnknownAttemptStopsSuccessRetryAndFallback(t *testing.T) {
	cases := []struct {
		name string
		fn   generateFunc
	}{
		{"successful_text_without_usage", func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
			// Non-nil zero usage prevents the shared fake from filling defaults.
			return modelprovider.GenerateResult{Text: "apparently successful", Usage: &commonv1.Usage{}}, nil
		}},
		{"transient_without_usage", func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
			return modelprovider.GenerateResult{}, providererr.New(providererr.Transient, "provider_disconnected", "no usage received")
		}},
		{"unexpected_tool_without_usage", func(r modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
			result, err := callsTool("unoffered_tool")(r)
			result.Usage = &commonv1.Usage{}
			return result, err
		}},
		{"invalid_output_without_usage", func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
			return modelprovider.GenerateResult{}, providererr.New(providererr.Invalid, "bad_structured_output", "output may already have been billed")
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			h := newEvalBudgetDirect(t)
			h.fake.onGenerate("mid-a", tc.fn)
			_, err := h.generate(1_000_000, say("evaluate the candidate"))
			assertEvalPendingError(t, err, 0)
			if calls := h.fake.allCalls(); len(calls) != 1 || calls[0].Model != "mid-a" {
				t.Errorf("unknown usage must stop before retry or fallback, provider calls: %v", calls)
			}
			if len(h.sleeps.all()) != 0 {
				t.Error("unknown charge incorrectly entered retry backoff")
			}
			rows := usageRows(t, h.store, "t-eval")
			if len(rows) != 1 {
				t.Errorf("want exactly the original pending reservation, got %d rows", len(rows))
			}
			pending := 0
			for _, row := range rows {
				if row["actual_usd_micros"] == nil && row["input_tokens"] == nil && row["output_tokens"] == nil {
					pending++
				}
			}
			if pending != 1 {
				t.Errorf("unknown charge was refunded or repeated: pending rows %d", pending)
			}
			if err := h.ledger.Check(ctx, "t-eval", budgetTime, modelbudget.Limits{Daily: modelbudget.Limit{Enabled: true, USDMicros: 1}}); !modelbudget.IsTenantBudgetExceeded(err) {
				t.Errorf("unknown attempt must retain its reservation: %v", err)
			}
		})
	}
}

func TestEvalBudgetKnownChargeThenUnknownRetainsBothAndStops(t *testing.T) {
	h := newEvalBudgetDirect(t)
	attempt := 0
	h.fake.onGenerate("mid-a", func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		attempt++
		if attempt == 1 {
			return modelprovider.GenerateResult{Usage: &commonv1.Usage{InputTokens: 1000, OutputTokens: 500, CacheReadTokens: 200}}, transient("first_attempt_billed", 0)
		}
		return modelprovider.GenerateResult{}, transient("second_attempt_unknown", 0)
	})
	_, err := h.generate(1_000_000, say("evaluate the candidate"))
	assertEvalPendingError(t, err, midACost)
	if calls := h.fake.allCalls(); len(calls) != 2 || h.fake.callsTo("mid-b") != 0 {
		t.Errorf("only the known billed failure may retry; got %d provider calls", len(calls))
	}
	if len(h.sleeps.all()) != 1 {
		t.Errorf("only one retry before uncertainty is permitted: %v", h.sleeps.all())
	}
	rows := usageRows(t, h.store, "t-eval")
	if len(rows) != 2 {
		t.Errorf("known + pending should be two attempts; got %d", len(rows))
	}
	settled, pending := 0, 0
	for _, row := range rows {
		if row["actual_usd_micros"] == int64(midACost) && row["input_tokens"] == int64(1000) {
			settled++
		} else if row["actual_usd_micros"] == nil && row["input_tokens"] == nil {
			pending++
		}
	}
	if settled != 1 || pending != 1 {
		t.Errorf("must preserve one frozen actual charge and one unknown reservation; settled=%d pending=%d", settled, pending)
	}
}

func TestEvalBudgetScoreCannotHideUnknownUsage(t *testing.T) {
	h := newEvalBudgetDirect(t)
	h.fake.setUsage(&commonv1.Usage{})
	cc := call(commonv1.Role_ROLE_JUDGE)
	cc.MaxCostUsdMicros = 1_000_000
	_, err := h.server.Score(ctx, connect.NewRequest(&mg.ScoreRequest{Tenant: tenant("t-eval"), Call: cc, Query: "quality", Candidates: []string{"candidate"}}))
	assertEvalPendingError(t, err, 0)
	if len(h.fake.allCalls()) != 1 {
		t.Errorf("Score repeated an unpriced attempt: %v", h.fake.allCalls())
	}
	rows := usageRows(t, h.store, "t-eval")
	if len(rows) != 1 || rows[0]["actual_usd_micros"] != nil {
		t.Errorf("Score must retain unknown usage, got %v", rows)
	}
}

func TestEvalBudgetKnownUnbilledRefusalStillAllowsFallback(t *testing.T) {
	h := newEvalBudgetDirect(t)
	h.fake.failNext("mid-a", providererr.New(providererr.NotFound, "model_not_found", "model not hosted"))
	response, err := h.generate(1_000_000, say("evaluate the candidate"))
	if err != nil {
		t.Fatal(err)
	}
	if response.Msg.GetModel().GetModelProfileId() != "test/mid-b@1" || response.Msg.GetUsage().GetCostUsdMicros() != midBCost {
		t.Errorf("known unbilled refusal should allow one correctly billed fallback: %v", response.Msg)
	}
	rows := usageRows(t, h.store, "t-eval")
	if len(rows) != 2 || rows[0]["actual_usd_micros"] != int64(0) || rows[1]["actual_usd_micros"] != int64(midBCost) {
		t.Errorf("a known refusal is zero, not pending: %v", rows)
	}
}

func TestEvalBudgetKnownChargeConsumesRemainingRequestAllowance(t *testing.T) {
	h := newEvalBudgetDirect(t)
	h.fake.onGenerate("mid-a", func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		return modelprovider.GenerateResult{Usage: &commonv1.Usage{InputTokens: 1000, OutputTokens: 500, CacheReadTokens: 200}}, transient("billed_failure", 0)
	})
	params := say("hi")
	params.MaxOutputTokens = 1
	_, err := h.generate(500, params)
	_, detail := errorDetail(t, err)
	if detail.GetCode() != "model_call_budget_exceeded" {
		t.Errorf("known 435-micro charge must reduce the remaining 500-micro allowance: %v", detail)
	}
	if cost, _ := billedMicros(detail); cost != midACost {
		t.Errorf("budget refusal lost known provider spend: %d", cost)
	}
	if len(h.fake.allCalls()) != 1 {
		t.Errorf("request allowance allowed another provider call: %v", h.fake.allCalls())
	}
	rows := usageRows(t, h.store, "t-eval")
	if len(rows) != 1 || rows[0]["actual_usd_micros"] != int64(midACost) {
		t.Errorf("known billed attempt must remain settled: %v", rows)
	}
}

type evalBudgetRoundTrip func(*http.Request) (*http.Response, error)

func (f evalBudgetRoundTrip) RoundTrip(request *http.Request) (*http.Response, error) {
	return f(request)
}

func TestEvalBudgetStreamCannotRetryOrEmitFinalWithUnknownUsage(t *testing.T) {
	for _, mode := range []string{"before_first_chunk", "final_without_usage"} {
		t.Run(mode, func(t *testing.T) {
			h := newEvalBudgetDirect(t)
			if mode == "before_first_chunk" {
				h.fake.failNext("mid-a", transient("upstream_disconnected", 0))
			} else {
				h.fake.setUsage(&commonv1.Usage{})
			}
			_, handler := modelgatewayv1connect.NewModelGatewayServiceHandler(h.server)
			// Exercise the actual gRPC handler/framing/trailer path in memory.
			// No listening socket or external HTTP request is involved.
			client := modelgatewayv1connect.NewModelGatewayServiceClient(&http.Client{
				Transport: evalBudgetRoundTrip(func(request *http.Request) (*http.Response, error) {
					request.Proto, request.ProtoMajor = "HTTP/2.0", 2
					recorder := httptest.NewRecorder()
					handler.ServeHTTP(recorder, request)
					response := recorder.Result()
					response.Proto, response.ProtoMajor = "HTTP/2.0", 2
					response.Request = request
					return response, nil
				}),
			}, "http://in-memory.invalid", connect.WithGRPC())
			cc := call(transform)
			cc.MaxCostUsdMicros = 1_000_000
			stream, err := client.StreamGenerate(ctx, connect.NewRequest(&mg.StreamGenerateRequest{
				Tenant: tenant("t-eval"), Call: cc, Generation: say("evaluate the candidate"),
			}))
			if err != nil {
				t.Fatal(err)
			}
			defer stream.Close()
			final := false
			for stream.Receive() {
				final = final || stream.Msg().GetFinal() != nil
			}
			assertEvalPendingError(t, stream.Err(), 0)
			if final {
				t.Error("unknown billed usage was advertised as a successful final response")
			}
			if len(h.fake.allCalls()) != 1 {
				t.Errorf("unknown stream attempt retried or fell back: %d provider calls", len(h.fake.allCalls()))
			}
			rows := usageRows(t, h.store, "t-eval")
			if len(rows) != 1 || rows[0]["actual_usd_micros"] != nil {
				t.Errorf("stream must retain exactly the unknown attempt: %v", rows)
			}
		})
	}
}
