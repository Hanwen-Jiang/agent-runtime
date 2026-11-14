package registry

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/secretstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

func profileDocument(fields map[string]any) map[string]any {
	backend := map[string]any{"kind": "firecracker", "workspace": "archive", "manager_url": "http://127.0.0.1:8097"}
	for k, v := range fields {
		backend[k] = v
	}
	return map[string]any{"default": "fc-eval", "backends": map[string]any{"fc-eval": backend}}
}

func TestGuestProfileRegistryRejectsMalformedOperatorSettings(t *testing.T) {
	for _, fields := range []map[string]any{
		{"guest_profile": true}, {"guest_profile": nil}, {"guest_profile": "root"},
		{"guest_profile": "benchmark-system"}, {"guest_profile": "benchmark-system", "root_disk_mb": 63},
		{"guest_profile": "benchmark-system", "root_disk_mb": 10240.5},
		{"guest_profile": "benchmark-system", "root_disk_mb": "10240"},
		{"guest_profile": "benchmark-system", "root_disk_mb": true},
		{"guest_profile": "benchmark-system", "root_disk_mb": nil},
		{"guest_profile": "benchmark-system", "root_disk_mb": -1},
		{"guest_profile": "benchmark-system", "root_disk_mb": 1e30},
		{"root_disk_mb": 10240}, {"guest_profile": "workspace", "root_disk_mb": 10240},
		{"kind": "local", "guest_profile": "benchmark-system"}, {"kind": "local", "root_disk_mb": 10240},
	} {
		_, err := parse(t, profileDocument(fields))
		if err == nil || !strings.Contains(err.Error(), "fc-eval") {
			t.Errorf("invalid profile accepted or backend omitted: %v err=%v", fields, err)
		}
	}
	for _, fields := range []map[string]any{
		{}, {"guest_profile": "workspace"}, {"guest_profile": "workspace", "root_disk_mb": 0},
		{"guest_profile": "benchmark-system", "root_disk_mb": 64},
		{"guest_profile": "benchmark-system", "root_disk_mb": 10240},
	} {
		if _, err := parse(t, profileDocument(fields)); err != nil {
			t.Errorf("valid profile rejected: %v %v", fields, err)
		}
	}
}

func TestBenchmarkBaseFieldsAreStrictAndCannotAffectStandardBackends(t *testing.T) {
	digest := "sha256:" + strings.Repeat("a", 64)
	for _, fields := range []map[string]any{
		{"base_runtime": "base-agent"}, {"base_image_digest": digest},
		{"base_runtime": "../base", "base_image_digest": digest},
		{"base_runtime": 42, "base_image_digest": digest},
		{"base_runtime": "base-agent", "base_image_digest": false},
		{"base_runtime": "base-agent", "base_image_digest": "sha256:bad"},
		{"base_runtime": "", "base_image_digest": ""},
		{"guest_profile": "workspace", "base_runtime": "base-agent", "base_image_digest": digest, "root_disk_mb": 0},
		{"kind": "local", "base_runtime": "base-agent", "base_image_digest": digest},
	} {
		entry := map[string]any{"guest_profile": "benchmark-system", "root_disk_mb": 10240}
		for k, v := range fields {
			entry[k] = v
		}
		if _, err := parse(t, profileDocument(entry)); err == nil {
			t.Errorf("malformed operator base accepted: %v", entry)
		}
	}
	if _, err := parse(t, profileDocument(map[string]any{"guest_profile": "benchmark-system", "root_disk_mb": 10240, "base_runtime": "base-agent", "base_image_digest": digest})); err != nil {
		t.Fatal(err)
	}
}

type profileSecrets struct{ secretstore.Provider }

func (profileSecrets) BindPlatform(purpose string) (secretstore.Store, error) {
	if purpose != SecretPurpose {
		return nil, fmt.Errorf("unexpected secret purpose")
	}
	return profileSecret{}, nil
}

type profileSecret struct{ secretstore.Store }

func (profileSecret) Read(_ context.Context, name string) (secret.Value, error) {
	if name != FCManagerSecret {
		return secret.Value{}, fmt.Errorf("unexpected secret name")
	}
	return secret.New(strings.Repeat("s", 32)), nil
}

func TestRegistryPassesOnlySelectedBackendProfileToManager(t *testing.T) {
	manager := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if r.Header.Get("X-Sandbox-Owner") != "eval-tb20" || r.Header.Get("Authorization") != "Bearer "+strings.Repeat("s", 32) {
			t.Error("scope or credential lost")
		}
		out := map[string]any{"env_id": "fc-0123456789abcdef"}
		if body["runtime"] == "base-new-agent" {
			if body["guest_profile"] != "benchmark-system" || body["root_disk_mb"] != float64(10240) || body["disk_mb"] != float64(2048) {
				t.Errorf("profile not forwarded: %v", body)
			}
			out["guest_profile"], out["root_disk_mb"] = "benchmark-system", 10240
			out["image"] = "sha256:" + strings.Repeat("a", 64)
		} else {
			if _, ok := body["guest_profile"]; ok {
				t.Error("benchmark profile leaked into default backend")
			}
			if _, ok := body["root_disk_mb"]; ok {
				t.Error("benchmark root capacity leaked into default backend")
			}
		}
		w.WriteHeader(201)
		json.NewEncoder(w).Encode(out)
	}))
	defer manager.Close()
	doc := map[string]any{"default": "fc", "backends": map[string]any{
		"fc":      map[string]any{"kind": "firecracker", "workspace": "archive", "manager_url": manager.URL, "runtimes": []string{"python"}},
		"fc-eval": map[string]any{"kind": "firecracker", "workspace": "archive", "manager_url": manager.URL, "runtimes": []string{"benchmark"}, "guest_profile": "benchmark-system", "root_disk_mb": 10240, "disk_mb": 2048, "base_runtime": "base-new-agent", "base_image_digest": "sha256:" + strings.Repeat("a", 64)},
	}}
	raw, _ := json.Marshal(doc)
	reg, err := Parse(raw, Options{Secrets: profileSecrets{}})
	if err != nil {
		t.Fatal(err)
	}
	for name, runtime := range map[string]string{"fc": "python", "fc-eval": "benchmark"} {
		provider, err := reg.Provider(context.Background(), name)
		if err != nil {
			t.Fatal(err)
		}
		box, err := provider.Bind(&commonv1.TenantContext{TenantId: "eval-tb20"})
		if err != nil {
			t.Fatal(err)
		}
		if _, err = box.Create(context.Background(), sandbox.Spec{Runtime: runtime}); err != nil {
			t.Fatal(err)
		}
	}
}
