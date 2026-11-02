package profiles_test

import (
	"testing"

	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/profiles"
)

func routingMetadata() map[string]any {
	return map[string]any{
		"task_family": "coding.patch", "role": "transform", "tier": "low",
		"score": 0.85, "source": "internal:patch-eval-2026-10", "at": "2026-10-30",
	}
}

func TestRoutingMetadataAndLegacyProfilesLoad(t *testing.T) {
	mustParse(t, validFile)
	mustParse(t, mutate(t, func(_, p map[string]any) {
		p["released_at"] = "2026-09-01"
		p["availability"] = false
		p["capability_scores"] = []any{routingMetadata()}
		p["reliability"] = map[string]any{"score": 0.99, "source": "internal:uptime", "at": "2026-10-30"}
	}))
}

func TestRoutingMetadataRejectsInvalidAndUnknownFields(t *testing.T) {
	for name, change := range map[string]func(map[string]any){
		"bad release date": func(p map[string]any) { p["released_at"] = "2026-02-30" },
		"bad availability": func(p map[string]any) { p["availability"] = "yes" },
		"bad reliability": func(p map[string]any) {
			p["reliability"] = map[string]any{"score": 1.1, "source": "test", "at": "2026-10-30"}
		},
		"missing reliability score": func(p map[string]any) { p["reliability"] = map[string]any{"source": "test", "at": "2026-10-30"} },
		"duplicate dimension":       func(p map[string]any) { p["capability_scores"] = []any{routingMetadata(), routingMetadata()} },
	} {
		t.Run(name, func(t *testing.T) {
			_, err := profiles.Parse([]byte(mutate(t, func(_, p map[string]any) { change(p) })))
			wantInvalid(t, name, err)
		})
	}
	for field, value := range map[string]any{
		"score": -0.1, "role": "judeg", "tier": "missing", "task_family": "",
		"source": "", "at": "yesterday", "socre": 0.9,
	} {
		t.Run(field, func(t *testing.T) {
			entry := routingMetadata()
			entry[field] = value
			_, err := profiles.Parse([]byte(mutate(t, func(_, p map[string]any) { p["capability_scores"] = []any{entry} })))
			wantInvalid(t, field, err)
		})
	}
	entry := routingMetadata()
	delete(entry, "score")
	_, err := profiles.Parse([]byte(mutate(t, func(_, p map[string]any) { p["capability_scores"] = []any{entry} })))
	wantInvalid(t, "missing score must not become zero", err)
}
