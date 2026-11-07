package registry

import (
	"context"
	"encoding/json"
	"math"
	"math/big"
	"reflect"
	"strings"

	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
)

const capabilityEvaluationSchema = "registry_capability_evaluation"

type capabilityMeasurement struct {
	TaskFamily string  `json:"task_family"`
	Role       string  `json:"role"`
	Tier       string  `json:"tier"`
	Score      float64 `json:"score"`
	Source     string  `json:"source"`
	At         string  `json:"at"`
}

func (m capabilityMeasurement) key() string { return m.TaskFamily + "\x00" + m.Role + "\x00" + m.Tier }

type capabilityEvidence struct {
	BaselineVersion string `json:"baseline_version"`
	Measurements    []struct {
		Measurement    capabilityMeasurement `json:"measurement"`
		Items          int                   `json:"items"`
		RawSuccessRate float64               `json:"raw_success_rate"`
		CI95           []float64             `json:"ci95"`
		Eligible       int                   `json:"eligible_candidates"`
		Total          int                   `json:"total_candidates"`
		EvidenceSHA    string                `json:"evidence_sha256"`
	} `json:"measurements"`
}

func capabilityScores(document string) (map[string]any, map[string]capabilityMeasurement, error) {
	var doc map[string]any
	decoder := json.NewDecoder(strings.NewReader(document))
	decoder.UseNumber()
	if err := decoder.Decode(&doc); err != nil {
		return nil, nil, err
	}
	var profile struct {
		Scores []capabilityMeasurement `json:"capability_scores"`
	}
	if err := json.Unmarshal([]byte(document), &profile); err != nil {
		return nil, nil, err
	}
	delete(doc, "capability_scores")
	// Registry seeds retain decimal spelling while the protobuf JSON API may
	// emit 4 for 4.0 or 1000000 for 1e6. Compare exact decimal values, without
	// rounding large integers through float64 or rewriting stored documents.
	normalizeCapabilityNumbers(doc)
	scores := map[string]capabilityMeasurement{}
	for _, m := range profile.Scores {
		scores[m.key()] = m
	}
	return doc, scores, nil
}

// An exact decimal is coefficient * 10^exponent. Keep the exponent symbolic:
// a valid input such as 1e1000000000 must not allocate a billion-digit integer.
// A separate type prevents numeric values from comparing equal to JSON strings.
type capabilityDecimal struct {
	coefficient string
	exponent    string
}

func normalizeCapabilityNumbers(value any) any {
	switch value := value.(type) {
	case map[string]any:
		for key, item := range value {
			value[key] = normalizeCapabilityNumbers(item)
		}
	case []any:
		for i, item := range value {
			value[i] = normalizeCapabilityNumbers(item)
		}
	case json.Number:
		// Decoder.UseNumber already validated JSON number syntax.
		mantissa := string(value)
		exponent := new(big.Int)
		if at := strings.IndexAny(mantissa, "eE"); at >= 0 {
			exponent.SetString(mantissa[at+1:], 10)
			mantissa = mantissa[:at]
		}
		negative := strings.HasPrefix(mantissa, "-")
		mantissa = strings.TrimPrefix(mantissa, "-")
		if at := strings.IndexByte(mantissa, '.'); at >= 0 {
			exponent.Sub(exponent, big.NewInt(int64(len(mantissa)-at-1)))
			mantissa = mantissa[:at] + mantissa[at+1:]
		}
		mantissa = strings.TrimLeft(mantissa, "0")
		if mantissa == "" {
			return capabilityDecimal{coefficient: "0", exponent: "0"}
		}
		coefficient := strings.TrimRight(mantissa, "0")
		exponent.Add(exponent, big.NewInt(int64(len(mantissa)-len(coefficient))))
		if negative {
			coefficient = "-" + coefficient
		}
		return capabilityDecimal{coefficient: coefficient, exponent: exponent.String()}
	}
	return value
}

// Capability publication cannot change the served model, pricing, supported
// operations, tiers or any unrelated metadata. It does not invent a baseline
// model experiment: the model itself is unchanged; only measured scores move.
func (r *Registry) checkCapabilityPromotion(ctx context.Context, sc scope, kind registryv1.RegistryKind, name string, version int64, raw []byte) (string, error) {
	if kind != kindProfile {
		return "", invalid("capability_profile_only", "能力分证据只能用于 Model Profile")
	}
	var evidence capabilityEvidence
	if err := json.Unmarshal(raw, &evidence); err != nil {
		return "", err
	}
	candidate, found, err := sc.get(ctx, kind, name, version)
	if err != nil {
		return "", err
	}
	if !found {
		return "", notFound("version_not_found", "没有这个版本")
	}
	if candidate.state == registryv1.ReleaseState_RELEASE_STATE_DEFAULT && candidate.evaluation == string(raw) {
		// Rechecked while holding the promotion lock; this must not rewrite
		// evidence or timestamps on an already published revision.
		return evidence.BaselineVersion, nil
	}
	if candidate.state != registryv1.ReleaseState_RELEASE_STATE_SHADOW {
		return "", invalid("capability_version_not_shadow", "能力分只能晋升未发布的影子版本")
	}
	baseline, found, err := sc.get(ctx, kind, name, 0)
	if err != nil {
		return "", err
	}
	if !found || baseline.label() != evidence.BaselineVersion {
		return "", invalid("capability_baseline_changed", "能力分的现行基线版本不匹配")
	}
	oldDoc, want, err := capabilityScores(baseline.document)
	if err != nil {
		return "", err
	}
	newDoc, actual, err := capabilityScores(candidate.document)
	if err != nil {
		return "", err
	}
	if !reflect.DeepEqual(oldDoc, newDoc) {
		return "", invalid("capability_profile_changed", "能力分发布不能修改模型、价格或其他 Profile 字段")
	}
	seen := map[string]bool{}
	for _, e := range evidence.Measurements {
		m := e.Measurement
		if seen[m.key()] {
			return "", invalid("capability_duplicate", "能力分证据维度重复")
		}
		seen[m.key()] = true
		if !capabilityCoverage(e.Eligible, e.Total) || len(e.CI95) != 2 || e.CI95[1] < e.CI95[0] || (e.CI95[1]-e.CI95[0])/2 > 0.15+1e-9 {
			return "", invalid("capability_gate_failed", "能力分覆盖或置信区间未达门槛")
		}
		expected := math.Round((float64(e.Items)*e.RawSuccessRate+5)/(float64(e.Items)+10)*10000) / 10000
		if math.Abs(m.Score-expected) > 0.0000001 || !strings.HasPrefix(m.Source, "capmatrix:") || !strings.HasSuffix(m.Source, ":evidence="+e.EvidenceSHA) {
			return "", invalid("capability_evidence_mismatch", "能力分数或来源与冻结证据不匹配")
		}
		want[m.key()] = m
	}
	if !reflect.DeepEqual(want, actual) {
		return "", invalid("capability_evidence_mismatch", "候选能力分包含未验收的改动")
	}
	return evidence.BaselineVersion, nil
}

func capabilityCoverage(eligible, total int) bool {
	return total > 0 && eligible > 0 && eligible <= total && eligible >= total/2+total%2
}
