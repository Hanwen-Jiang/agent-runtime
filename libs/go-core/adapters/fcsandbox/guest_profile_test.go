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

func TestGuestProfileOperatorConfigurationIsValidated(t *testing.T) {
	for _, c := range []fcsandbox.Config{
		{}, {GuestProfile: "workspace"}, {GuestProfile: "benchmark-system", RootDiskMB: 64},
		{GuestProfile: "benchmark-system", RootDiskMB: 10240},
	} {
		c.ManagerURL = "http://127.0.0.1:1"
		if _, err := fcsandbox.New(c, strings.Repeat("s", 32), nil); err != nil {
			t.Fatalf("valid config rejected: %+v %v", c, err)
		}
	}
	for _, c := range []fcsandbox.Config{
		{GuestProfile: "root"}, {GuestProfile: "benchmark-system"}, {GuestProfile: "benchmark-system", RootDiskMB: 63},
		{GuestProfile: "benchmark-system", RootDiskMB: -1}, {RootDiskMB: 10240}, {GuestProfile: "workspace", RootDiskMB: -1},
	} {
		c.ManagerURL = "http://127.0.0.1:1"
		if _, err := fcsandbox.New(c, strings.Repeat("s", 32), nil); err == nil {
			t.Fatalf("invalid config accepted: %+v", c)
		}
	}
}

func TestGuestProfileRoundTripAndOldManagerCompatibility(t *testing.T) {
	for _, profile := range []string{"", "workspace", "benchmark-system"} {
		t.Run("profile_"+profile, func(t *testing.T) {
			manager := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodPost || r.URL.Path != "/v1/envs" || r.Header.Get("X-Sandbox-Owner") != "eval-tb20" {
					t.Errorf("unexpected manager request: %s %s", r.Method, r.URL.Path)
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				out := map[string]any{"env_id": "fc-0123456789abcdef", "image": "sha256:" + strings.Repeat("a", 64)}
				if profile == "benchmark-system" {
					if body["guest_profile"] != profile || body["root_disk_mb"] != float64(10240) || body["disk_mb"] != float64(4096) {
						t.Errorf("operator profile/root and scratch capacities not propagated: %v", body)
					}
					out["guest_profile"], out["root_disk_mb"] = profile, 10240
				} else {
					if _, ok := body["guest_profile"]; ok {
						t.Error("standard guest must retain old-manager request shape")
					}
					if _, ok := body["root_disk_mb"]; ok {
						t.Error("standard guest requested writable root")
					}
					if profile == "workspace" {
						out["guest_profile"], out["root_disk_mb"] = "workspace", 0
					}
					// Empty profile models an existing manager omitting both fields.
				}
				w.WriteHeader(http.StatusCreated)
				json.NewEncoder(w).Encode(out)
			}))
			defer manager.Close()
			cfg := fcsandbox.Config{ManagerURL: manager.URL, GuestProfile: profile, DiskMB: 4096}
			if profile == "benchmark-system" {
				cfg.RootDiskMB = 10240
				cfg.BaseRuntime = "benchmark-base"
				cfg.BaseImageDigest = "sha256:" + strings.Repeat("a", 64)
			}
			provider, err := fcsandbox.New(cfg, strings.Repeat("s", 32), nil)
			if err != nil {
				t.Fatal(err)
			}
			box, err := provider.Bind(&commonv1.TenantContext{TenantId: "eval-tb20"})
			if err != nil {
				t.Fatal(err)
			}
			env, err := box.Create(context.Background(), sandbox.Spec{Runtime: "python", Network: "none"})
			if err != nil || env.ID != "fc-0123456789abcdef" || !env.HealthConfirmed {
				t.Fatal(env, err)
			}
		})
	}
}

func TestUnexpectedGuestProfileIsDestroyedAndCleanupFailureIsVisible(t *testing.T) {
	for _, tc := range []struct {
		name, requested, returned string
		capacity                  any
		failDelete                bool
	}{
		{"old_agent", "benchmark-system", "", 0, false},
		{"wrong_profile", "benchmark-system", "workspace", 10240, false},
		{"wrong_capacity", "benchmark-system", "benchmark-system", 4096, false},
		{"invalid_capacity_type", "benchmark-system", "benchmark-system", "10240", false},
		{"cleanup_failure", "benchmark-system", "workspace", 0, true},
		{"unexpected_elevation", "workspace", "benchmark-system", 10240, false},
		{"unexpected_root_disk", "workspace", "workspace", 10240, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var deletes atomic.Int32
			manager := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method == http.MethodDelete {
					deletes.Add(1)
					if r.URL.Path != "/v1/envs/fc-0123456789abcdef" || r.Header.Get("X-Sandbox-Owner") != "eval-tb20" {
						t.Error("cleanup used wrong VM/scope")
					}
					if tc.failDelete {
						w.WriteHeader(503)
						w.Write([]byte(`{"code":"host_busy","message":"private details must not leak"}`))
						return
					}
					w.WriteHeader(204)
					return
				}
				w.WriteHeader(201)
				json.NewEncoder(w).Encode(map[string]any{"env_id": "fc-0123456789abcdef", "guest_profile": tc.returned, "root_disk_mb": tc.capacity})
			}))
			defer manager.Close()
			cfg := fcsandbox.Config{ManagerURL: manager.URL, GuestProfile: tc.requested}
			if tc.requested == "benchmark-system" {
				cfg.RootDiskMB = 10240
				cfg.BaseRuntime = "benchmark-base"
				cfg.BaseImageDigest = "sha256:" + strings.Repeat("a", 64)
			}
			provider, err := fcsandbox.New(cfg, strings.Repeat("s", 32), nil)
			if err != nil {
				t.Fatal(err)
			}
			box, _ := provider.Bind(&commonv1.TenantContext{TenantId: "eval-tb20"})
			env, err := box.Create(context.Background(), sandbox.Spec{Runtime: "python"})
			var pe *providererr.Error
			if !errors.As(err, &pe) || pe.Code != "guest_profile_mismatch" || env.ID != "" || deletes.Load() != 1 {
				t.Fatalf("mismatched guest accepted or orphaned: %+v %v deletes=%d", env, err, deletes.Load())
			}
			if tc.failDelete {
				if pe.Category != providererr.Transient || !strings.Contains(pe.Message, "cleanup requires retry") || strings.Contains(pe.Message, "private details") {
					t.Fatal(pe)
				}
			} else if pe.Category != providererr.Invalid || !strings.Contains(pe.Message, "destroyed") {
				t.Fatal(pe)
			}
		})
	}
}
