package fcsandbox_test

import (
	"context"
	"encoding/json"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/fcsandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestPreparedImageUsesExactRootfsAliasAndCannotCrossTenant(t *testing.T) {
	image := "registry.example/envs/t1/env@sha256:" + strings.Repeat("a", 64)
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		var request map[string]any
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Fatal(err)
		}
		if request["runtime"] != "tb0123456789" || r.Header.Get("X-Sandbox-Owner") != "t1" {
			t.Fatalf("wrong actual VM request: %+v", request)
		}
		w.WriteHeader(201)
		_, _ = w.Write([]byte(`{"env_id":"fc-0123456789abcdef","image":"sha256:` + strings.Repeat("b", 64) + `"}`))
	}))
	defer server.Close()
	provider, err := fcsandbox.New(fcsandbox.Config{ManagerURL: server.URL, Runtimes: []string{"universal"}, CustomImagePrefix: "registry.example/envs", ImageRuntimes: map[string]fcsandbox.PreparedImage{image: {Runtime: "tb0123456789", SourceDigest: "sha256:" + strings.Repeat("b", 64)}}}, strings.Repeat("t", 32), nil)
	if err != nil {
		t.Fatal(err)
	}
	own, _ := provider.Bind(&commonv1.TenantContext{TenantId: "t1"})
	got, err := own.Create(context.Background(), sandbox.Spec{Runtime: "universal", Image: image, Network: "none"})
	if err != nil || !got.HealthConfirmed || got.Image != image {
		t.Fatalf("create %+v %v", got, err)
	}
	foreign, _ := provider.Bind(&commonv1.TenantContext{TenantId: "t2"})
	if _, err = foreign.Create(context.Background(), sandbox.Spec{Runtime: "universal", Image: image, Network: "none"}); err == nil {
		t.Fatal("cross tenant image accepted")
	}
	if _, err = own.Create(context.Background(), sandbox.Spec{Runtime: "universal", Image: strings.Replace(image, "aaaa", "bbbb", 1), Network: "none"}); err == nil {
		t.Fatal("unprepared digest accepted")
	}
	if calls != 1 {
		t.Fatalf("rejected requests reached manager: %d", calls)
	}
}

func TestImageMappingsRequireImmutableScopedReference(t *testing.T) {
	for _, image := range []string{"registry.example/envs/t1/env:latest", "other.example/envs/t1/env@sha256:" + strings.Repeat("a", 64), "registry.example/envs/../env@sha256:" + strings.Repeat("a", 64)} {
		if _, err := fcsandbox.New(fcsandbox.Config{ManagerURL: "http://127.0.0.1", CustomImagePrefix: "registry.example/envs", ImageRuntimes: map[string]fcsandbox.PreparedImage{image: {Runtime: "tb0123456789", SourceDigest: "sha256:" + strings.Repeat("b", 64)}}}, strings.Repeat("t", 32), nil); err == nil {
			t.Fatalf("bad mapping accepted %s", image)
		}
	}
}

func TestWrongOrMissingPreparedImageIdentityDestroysVM(t *testing.T) {
	for _, actual := range []string{"", "sha256:" + strings.Repeat("c", 64)} {
		t.Run(actual, func(t *testing.T) {
			image := "registry.example/envs/t1/env@sha256:" + strings.Repeat("a", 64)
			deleted := false
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method == http.MethodDelete {
					deleted = true
					w.WriteHeader(204)
					return
				}
				w.WriteHeader(201)
				_ = json.NewEncoder(w).Encode(map[string]string{"env_id": "fc-0123456789abcdef", "image": actual})
			}))
			defer server.Close()
			p, err := fcsandbox.New(fcsandbox.Config{ManagerURL: server.URL, Runtimes: []string{"universal"}, CustomImagePrefix: "registry.example/envs", ImageRuntimes: map[string]fcsandbox.PreparedImage{image: {Runtime: "tb0123456789", SourceDigest: "sha256:" + strings.Repeat("b", 64)}}}, strings.Repeat("t", 32), nil)
			if err != nil {
				t.Fatal(err)
			}
			box, _ := p.Bind(&commonv1.TenantContext{TenantId: "t1"})
			env, err := box.Create(context.Background(), sandbox.Spec{Runtime: "universal", Image: image, Network: "none"})
			if err == nil || !deleted || env.HealthConfirmed || env.ID != "" {
				t.Fatalf("mismatched rootfs accepted/leaked: %+v %v deleted=%v", env, err, deleted)
			}
		})
	}
}
