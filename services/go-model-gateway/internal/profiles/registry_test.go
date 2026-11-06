package profiles_test

import (
	"encoding/json"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/registryref"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/profiles"
)

func TestRegistryExportIsVerifiedAndLegacyEvidenceStaysUnknown(t *testing.T) {
	legacy := mustParse(t, validFile)
	if legacy.All()[0].Registry != nil || legacy.RequireRegistry() == nil {
		t.Fatal("legacy file must not invent a Registry revision")
	}
	var file struct {
		Version           int                            `json:"version"`
		Profiles          []json.RawMessage              `json:"profiles"`
		RegistryRevisions map[string]registryref.Profile `json:"registry_revisions"`
	}
	if err := json.Unmarshal([]byte(validFile), &file); err != nil {
		t.Fatal(err)
	}
	file.RegistryRevisions = map[string]registryref.Profile{}
	for _, raw := range file.Profiles {
		var p struct {
			ID string `json:"id"`
		}
		if err := json.Unmarshal(raw, &p); err != nil {
			t.Fatal(err)
		}
		hash, err := registryref.Digest(raw)
		if err != nil {
			t.Fatal(err)
		}
		file.RegistryRevisions[p.ID] = registryref.Profile{Scope: "platform", Version: "v7", SHA256: hash}
	}
	encoded, err := json.Marshal(file)
	if err != nil {
		t.Fatal(err)
	}
	catalog := mustParse(t, string(encoded))
	if err := catalog.RequireRegistry(); err != nil {
		t.Fatal(err)
	}
	for _, p := range catalog.All() {
		if p.Registry == nil || p.Registry.Version != "v7" {
			t.Fatalf("missing published version: %+v", p)
		}
	}
	// A later price edit cannot continue to claim the previously exported revision.
	var changed map[string]any
	if err := json.Unmarshal(file.Profiles[0], &changed); err != nil {
		t.Fatal(err)
	}
	changed["cost"].(map[string]any)["in_per_m"] = 99.0
	file.Profiles[0], err = json.Marshal(changed)
	if err != nil {
		t.Fatal(err)
	}
	encoded, err = json.Marshal(file)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := profiles.Parse(encoded); err == nil {
		t.Fatal("stale Registry digest accepted after price edit")
	}
	delete(file.RegistryRevisions, changed["id"].(string))
	encoded, _ = json.Marshal(file)
	if _, err := profiles.Parse(encoded); err == nil {
		t.Fatal("partial Registry provenance accepted")
	}
}
