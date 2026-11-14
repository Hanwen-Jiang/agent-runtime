package fcsandbox_test

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/fcsandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

func TestGuestBaseConfigurationIsAnOperatorOnlyPair(t *testing.T) {
	digest := "sha256:" + strings.Repeat("a", 64)
	for _, tc := range []struct{ profile, alias, sum string }{
		{"", "base", digest}, {"workspace", "base", digest}, {"benchmark-system", "base", ""},
		{"benchmark-system", "", digest}, {"benchmark-system", "../base", digest}, {"benchmark-system", strings.Repeat("a", 64), digest},
		{"benchmark-system", "base", "sha256:bad"},
	} {
		if fcsandbox.ValidateGuestBase(tc.profile, tc.alias, tc.sum) == nil {
			t.Fatalf("accepted invalid base: %+v", tc)
		}
	}
	if err := fcsandbox.ValidateGuestBase("benchmark-system", "base-agent-v2", digest); err != nil {
		t.Fatal(err)
	}
}

func TestGuestBaseAliasesPreservePublicRuntimeAndDoNotOverrideCustomImage(t *testing.T) {
	baseDigest := "sha256:" + strings.Repeat("a", 64)
	customDigest := "sha256:" + strings.Repeat("b", 64)
	image := "registry/envs/t1/env@sha256:" + strings.Repeat("c", 64)
	for _, custom := range []bool{false, true} {
		t.Run(map[bool]string{false: "base", true: "custom"}[custom], func(t *testing.T) {
			manager := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body map[string]any
				json.NewDecoder(r.Body).Decode(&body)
				alias, digest := "base-agent-v2", baseDigest
				if custom {
					alias, digest = "custom-agent-v2", customDigest
				}
				if body["runtime"] != alias {
					t.Errorf("incorrect manager runtime: %v", body)
				}
				w.WriteHeader(201)
				json.NewEncoder(w).Encode(map[string]any{"env_id": "fc-0123456789abcdef", "image": digest, "guest_profile": "benchmark-system", "root_disk_mb": 10240})
			}))
			defer manager.Close()
			cfg := fcsandbox.Config{ManagerURL: manager.URL, GuestProfile: "benchmark-system", RootDiskMB: 10240, Runtimes: []string{"universal"}, BaseRuntime: "base-agent-v2", BaseImageDigest: baseDigest, CustomImagePrefix: "registry/envs", ImageRuntimes: map[string]fcsandbox.PreparedImage{image: {Runtime: "custom-agent-v2", SourceDigest: customDigest}}}
			p, err := fcsandbox.New(cfg, strings.Repeat("s", 32), nil)
			if err != nil {
				t.Fatal(err)
			}
			box, _ := p.Bind(&commonv1.TenantContext{TenantId: "t1"})
			spec := sandbox.Spec{Runtime: "universal"}
			expected := baseDigest
			if custom {
				spec.Image = image
				expected = image
			}
			env, err := box.Create(context.Background(), spec)
			if err != nil || env.Spec.Runtime != "universal" || env.Image != expected {
				t.Fatal(env, err)
			}
			// The internal alias is never an additional public runtime.
			if _, err = box.Create(context.Background(), sandbox.Spec{Runtime: "base-agent-v2"}); err == nil {
				t.Fatal("public spec selected operator alias")
			}
		})
	}
}

func TestGuestBaseMissingMappingFailsBeforeCreatingAndWrongIdentityCleansUp(t *testing.T) {
	for _, mode := range []string{"missing", "wrong_digest", "wrong_digest_cleanup_failure"} {
		t.Run(mode, func(t *testing.T) {
			var creates, deletes atomic.Int32
			manager := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method == "DELETE" {
					deletes.Add(1)
					if mode == "wrong_digest_cleanup_failure" {
						w.WriteHeader(503)
						return
					}
					w.WriteHeader(204)
					return
				}
				creates.Add(1)
				w.WriteHeader(201)
				json.NewEncoder(w).Encode(map[string]any{"env_id": "fc-0123456789abcdef", "image": "sha256:" + strings.Repeat("b", 64), "guest_profile": "benchmark-system", "root_disk_mb": 10240})
			}))
			defer manager.Close()
			cfg := fcsandbox.Config{ManagerURL: manager.URL, GuestProfile: "benchmark-system", RootDiskMB: 10240}
			if mode != "missing" {
				cfg.BaseRuntime = "base-agent-v2"
				cfg.BaseImageDigest = "sha256:" + strings.Repeat("a", 64)
			}
			p, err := fcsandbox.New(cfg, strings.Repeat("s", 32), nil)
			if err != nil {
				t.Fatal(err)
			}
			box, _ := p.Bind(&commonv1.TenantContext{TenantId: "t1"})
			_, err = box.Create(context.Background(), sandbox.Spec{Runtime: "python"})
			var pe *providererr.Error
			if !errors.As(err, &pe) {
				t.Fatal(err)
			}
			if mode == "missing" {
				if pe.Code != "profile_base_unavailable" || creates.Load() != 0 {
					t.Fatal(pe, creates.Load())
				}
				return
			}
			if pe.Code != "image_identity_mismatch" || creates.Load() != 1 || deletes.Load() != 1 {
				t.Fatal(pe, creates.Load(), deletes.Load())
			}
			if mode == "wrong_digest_cleanup_failure" && pe.Category != providererr.Transient {
				t.Fatal(pe)
			}
		})
	}
}
