package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/registryref"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/registry"
)

func TestRegistryExportUsesPublishedVersionAndKeepsOldExportImmutable(t *testing.T) {
	env := seedEnv(t.TempDir())
	seed := func(document string) {
		t.Helper()
		var out, logs bytes.Buffer
		if code := run([]string{"registry-seed", writeSeed(t, document)}, &out, &logs, env); code != 0 {
			t.Fatalf("seed code %d: %s", code, logs.String())
		}
	}
	export := func() registry.ProfileExport {
		t.Helper()
		var out, logs bytes.Buffer
		if code := run([]string{"registry-export-profiles"}, &out, &logs, env); code != 0 {
			t.Fatalf("export code %d: %s", code, logs.String())
		}
		var result registry.ProfileExport
		if err := json.Unmarshal(out.Bytes(), &result); err != nil {
			t.Fatal(err)
		}
		return result
	}
	seed(seedFile)
	first := export()
	const id = "mock/model@2026-10"
	if len(first.Profiles) != 1 || first.RegistryRevisions[id].Version != "v1" {
		t.Fatalf("unexpected export: %+v", first)
	}
	hash, err := registryref.Digest(first.Profiles[0])
	if err != nil || hash != first.RegistryRevisions[id].SHA256 {
		t.Fatal("export digest does not identify its document")
	}
	seed(strings.Replace(seedFile, `"in_per_m": 0`, `"in_per_m": 1.25`, 1))
	second := export()
	if second.RegistryRevisions[id].Version != "v2" || second.RegistryRevisions[id].SHA256 == hash {
		t.Fatal("new published version not exported")
	}
	if first.RegistryRevisions[id].Version != "v1" {
		t.Fatal("old snapshot changed")
	}
	third := export()
	if third.RegistryRevisions[id] != second.RegistryRevisions[id] {
		t.Fatal("read-only export changed the version")
	}
}
