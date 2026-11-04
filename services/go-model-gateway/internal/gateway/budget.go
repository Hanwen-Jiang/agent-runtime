package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/modelbudget"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/router"
)

func streamUsage(st modelprovider.Stream) *commonv1.Usage {
	if source, ok := st.(interface{ Usage() *commonv1.Usage }); ok {
		return source.Usage()
	}
	return nil
}

// estimateInput reserves one token per serialized byte, including a second
// schema/prompt copy used by the adapter and fixed message framing. This is an
// estimate, not billed usage; settlement always records the provider's usage.
// maxOutput and thinking are what the request asked for (0 when unset).
func (c *call) estimateInput(input any, maxOutput, thinking int) {
	raw, _ := json.Marshal(input)
	c.inputEstimate = int64(len(raw))*2 + 1024
	c.maxOutput = maxOutput
	c.thinking = thinking
}

// outputLimit is the output cap sent to the provider and reserved against the
// budget. A requested cap is kept (never above the profile's max_output). An
// unset one gets the gateway default: DefaultMaxOutputTokens of visible output
// plus this call's thinking budget, never above the profile's max_output; without
// it a profile declaring a 1M-token max_output reserved more than a whole daily
// budget for one call (BUDGET-DEFAULT). A negative Options.DefaultMaxOutput keeps
// the profile's max_output.
func (s *Server) outputLimit(requested, thinking int, cand router.Candidate) int {
	limit := cand.Profile.Constraints.MaxOutput
	if requested > 0 {
		return min(requested, limit)
	}
	if s.defaultOutput <= 0 {
		return limit
	}
	if thinking <= 0 {
		thinking = cand.Tier.ThinkingBudget
	}
	return min(limit, s.defaultOutput+max(thinking, 0))
}

// frozenPrice is the profile's rate card in integer USD micros per 1M tokens,
// stored on the attempt's row so the charge can be recomputed later (R-06-C).
func frozenPrice(cand router.Candidate) (*modelbudget.Price, error) {
	rates := [3]float64{cand.Profile.Cost.InPerM, cand.Profile.Cost.OutPerM, cand.Profile.Cost.CacheReadPerM}
	var micros [3]int64
	for i, rate := range rates {
		m := math.Round(rate * 1e6)
		if math.IsInf(m, 0) || math.IsNaN(m) || m < 0 || m >= float64(math.MaxInt64) {
			return nil, invalid("invalid_model_cost", "model cost cannot be reserved")
		}
		micros[i] = int64(m)
	}
	return &modelbudget.Price{InPerMMicros: micros[0], OutPerMMicros: micros[1], CacheReadPerMMicros: micros[2], PriceAt: cand.Profile.Freshness.PriceAt}, nil
}

func (s *Server) reserve(ctx context.Context, c *call, cand router.Candidate) error {
	if s.opts.ModelBudget == nil {
		return nil
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	limits, err := s.opts.BudgetPolicies.Resolve(ctx, c.tenant.GetTenantId())
	if err != nil {
		return err
	}
	output := s.outputLimit(c.maxOutput, c.thinking, cand)
	if c.method == "embed" {
		output = 0
	}
	if output < 0 || (c.method != "embed" && output == 0) {
		return invalid("missing_model_limits", "model profile must declare a positive output limit")
	}
	cost := math.Ceil(float64(c.inputEstimate)*max(cand.Profile.Cost.InPerM, cand.Profile.Cost.CacheReadPerM) + float64(output)*cand.Profile.Cost.OutPerM)
	if math.IsInf(cost, 0) || math.IsNaN(cost) || cost < 0 || cost >= float64(math.MaxInt64) {
		return invalid("invalid_model_cost", "model cost cannot be reserved")
	}
	if limit := c.ctx.GetMaxCostUsdMicros(); limit > 0 && int64(cost) > limit-c.billed.GetCostUsdMicros() {
		return providererr.New(providererr.Denied, "model_call_budget_exceeded", "model call exceeds the request budget")
	}
	price, err := frozenPrice(cand)
	if err != nil {
		return err
	}
	r, err := s.opts.ModelBudget.Reserve(ctx, modelbudget.ReservationRequest{
		TenantID: c.tenant.GetTenantId(), At: s.opts.Now(), USDMicros: int64(cost), Limits: limits,
		TaskID: c.ctx.GetTaskId(), NodeID: c.ctx.GetNodeId(), ModelProfileID: cand.Profile.ID,
		Vendor: cand.Profile.Vendor, Method: c.method, Price: price,
	})
	if err == nil {
		c.reservation = &r
	}
	return err
}

// settle is independent of client cancellation: a disconnect cannot erase a
// known provider charge. An unknown outcome retains its reservation; it must
// not be silently refunded as if the model call were free.
func (s *Server) settle(ctx context.Context, c *call, cand router.Candidate, u *commonv1.Usage, callErr error) error {
	if c.reservation == nil {
		return nil
	}
	r := *c.reservation
	c.reservation = nil
	known := u.GetInputTokens() > 0 || u.GetOutputTokens() > 0 || u.GetCacheReadTokens() > 0
	var pe *providererr.Error
	// Invalid may mean malformed output after an already billed generation.
	// Only an upstream refusal/not-found is treated as an unbilled rejection.
	unbilled := errors.As(callErr, &pe) && (pe.Category == providererr.Denied || pe.Category == providererr.NotFound)
	if !known && !unbilled {
		s.opts.Logger.Warn("model usage pending", "reservation_id", r.ID, "tenant_id", r.TenantID, "model_profile_id", cand.Profile.ID)
		return nil
	}
	actual := priced(cand, u).GetCostUsdMicros()
	settleCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancel()
	var err error
	if us, ok := s.opts.ModelBudget.(modelbudget.UsageSettler); ok {
		err = us.SettleUsage(settleCtx, r, actual, modelbudget.Usage{
			InputTokens: max(u.GetInputTokens(), 0), OutputTokens: max(u.GetOutputTokens(), 0), CacheReadTokens: max(u.GetCacheReadTokens(), 0),
		})
	} else {
		err = s.opts.ModelBudget.Settle(settleCtx, r, actual)
	}
	if err != nil {
		s.opts.Logger.Error("model usage settlement failed", "reservation_id", r.ID, "tenant_id", r.TenantID)
		// Conflict is nonretryable: retrying the model would charge it again.
		e := providererr.New(providererr.Conflict, "model_usage_unsettled", "model usage settlement failed; reservation retained")
		e.BilledCostUSDMicros = actual + c.billed.GetCostUsdMicros()
		return e
	}
	return nil
}
