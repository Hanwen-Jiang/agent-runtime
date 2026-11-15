package registry

import (
	"context"
	"testing"
	"time"
)

func TestBenchmarkLifecycleLimitsRequireOperatorProfileAndExplicitPolicy(t *testing.T) {
	reg, err := parse(t, map[string]any{"default": "ordinary", "backends": map[string]any{
		"ordinary":   map[string]any{"kind": "firecracker", "workspace": "archive", "manager_url": "http://manager"},
		"benchmark":  map[string]any{"kind": "firecracker", "workspace": "archive", "manager_url": "http://manager", "guest_profile": "benchmark-system", "root_disk_mb": 10240},
		"benchmark2": map[string]any{"kind": "firecracker", "workspace": "archive", "manager_url": "http://manager", "guest_profile": "benchmark-system", "root_disk_mb": 10240},
	}})
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name           string
		sandbox        map[string]any
		idle, lifetime time.Duration
	}{
		{"ordinary_cannot_expand", map[string]any{"backend": "ordinary", "idle_timeout_s": 12600, "max_lifetime_s": 25200}, DefaultIdle, DefaultMaxLifetime},
		{"tenant_profile_cannot_elevate", map[string]any{"guest_profile": "benchmark-system", "idle_timeout_s": 12600, "max_lifetime_s": 25200}, DefaultIdle, DefaultMaxLifetime},
		{"benchmark_explicit", map[string]any{"backend": "benchmark", "idle_timeout_s": 12600, "max_lifetime_s": 25200}, BenchmarkMaxIdle, BenchmarkMaxLifetime},
		{"benchmark_missing_uses_defaults", map[string]any{"backend": "benchmark"}, DefaultIdle, DefaultMaxLifetime},
		{"benchmark_can_reduce", map[string]any{"backend": "benchmark", "idle_timeout_s": 60, "max_lifetime_s": 120}, time.Minute, 2 * time.Minute},
		{"benchmark_is_capped", map[string]any{"backend": "benchmark", "idle_timeout_s": 1e30, "max_lifetime_s": 1e30}, BenchmarkMaxIdle, BenchmarkMaxLifetime},
		{"benchmark_only_idle_explicit", map[string]any{"backend": "benchmark", "idle_timeout_s": 12600}, BenchmarkMaxIdle, DefaultMaxLifetime},
		{"benchmark_only_lifetime_explicit", map[string]any{"backend": "benchmark", "max_lifetime_s": 25200}, DefaultIdle, BenchmarkMaxLifetime},
		{"benchmark_to_ordinary_fallback", map[string]any{"backend": "benchmark", "fallback": []any{"ordinary"}, "idle_timeout_s": 12600, "max_lifetime_s": 25200}, DefaultIdle, DefaultMaxLifetime},
		{"ordinary_to_benchmark_fallback", map[string]any{"backend": "ordinary", "fallback": []any{"benchmark"}, "idle_timeout_s": 12600, "max_lifetime_s": 25200}, DefaultIdle, DefaultMaxLifetime},
		{"benchmark_to_benchmark_fallback", map[string]any{"backend": "benchmark", "fallback": []any{"benchmark2"}, "idle_timeout_s": 12600, "max_lifetime_s": 25200}, BenchmarkMaxIdle, BenchmarkMaxLifetime},
	} {
		t.Run(tc.name, func(t *testing.T) {
			policy, err := NewPolicies(reg, func(context.Context, string) (map[string]any, error) {
				return map[string]any{"sandbox": tc.sandbox}, nil
			}, -1).For(context.Background(), "eval")
			if err != nil {
				t.Fatal(err)
			}
			if policy.IdleTimeout != tc.idle || policy.MaxLifetime != tc.lifetime {
				t.Fatalf("got idle=%s lifetime=%s; want %s %s", policy.IdleTimeout, policy.MaxLifetime, tc.idle, tc.lifetime)
			}
		})
	}
}

func TestDirectPolicyParsingNeverGrantsBenchmarkLifecycle(t *testing.T) {
	p := FromDocument(map[string]any{"sandbox": map[string]any{"backend": "benchmark", "guest_profile": "benchmark-system", "idle_timeout_s": 1e30, "max_lifetime_s": 1e30}}, "ordinary")
	if p.IdleTimeout != DefaultIdle || p.MaxLifetime != DefaultMaxLifetime {
		t.Fatalf("untrusted policy bypassed operator registration: %+v", p)
	}
}
