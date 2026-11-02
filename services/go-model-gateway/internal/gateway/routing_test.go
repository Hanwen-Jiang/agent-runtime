package gateway_test

import (
	"encoding/json"
	"testing"

	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/gateway"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/profiles"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/router"
)

func TestCallContextTaskFamilyReachesRouting(t *testing.T) {
	var document map[string]any
	if err := json.Unmarshal([]byte(testProfiles), &document); err != nil {
		t.Fatal(err)
	}
	for _, value := range document["profiles"].([]any) {
		p := value.(map[string]any)
		if p["model"] == "mid-b" {
			p["capability_scores"] = []any{map[string]any{
				"task_family": "coding.patch", "role": "transform", "tier": "low",
				"score": 0.95, "source": "synthetic:gateway-test", "at": "2026-11-01",
			}}
		}
	}
	data, err := json.Marshal(document)
	if err != nil {
		t.Fatal(err)
	}
	catalog, err := profiles.Parse(data)
	if err != nil {
		t.Fatal(err)
	}
	h := newHarnessOpts(t, nil, func(o *gateway.Options) { o.Router = router.New(catalog, nil) })
	for family, want := range map[string]string{"coding.patch": "test/mid-b@1", "research.report": "test/mid-a@1"} {
		c := call(transform)
		c.TaskFamily = family
		resp := h.mustGenerate(t, "t1", c, say("task"))
		if got := resp.GetModel().GetModelProfileId(); got != want {
			t.Fatalf("family %s picked %s, want %s", family, got, want)
		}
	}
}
