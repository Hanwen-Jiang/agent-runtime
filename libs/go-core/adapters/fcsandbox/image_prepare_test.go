package fcsandbox_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/fcsandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

func TestAutomaticImagePreparationPreservesIdentityAndOwner(t *testing.T) {
	image := "registry.example/envs/t1/env@sha256:" + strings.Repeat("a", 64)
	digest := "sha256:" + strings.Repeat("b", 64)
	prepared, created := 0, 0
	service := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+strings.Repeat("s", 32) || r.Header.Get("X-Sandbox-Owner") != "t1" {
			t.Error("missing private service identity")
		}
		var body map[string]string
		json.NewDecoder(r.Body).Decode(&body)
		if r.URL.Path == "/v1/images/prepare" {
			prepared++
			if body["image"] != image {
				t.Fatal("changed image")
			}
			json.NewEncoder(w).Encode(map[string]string{"state": "ready", "image": image, "runtime": "oci-prepared", "source_digest": digest})
			return
		}
		created++
		if body["runtime"] != "oci-prepared" {
			t.Fatal("wrong runtime")
		}
		w.WriteHeader(201)
		json.NewEncoder(w).Encode(map[string]string{"env_id": "fc-0123456789abcdef", "image": digest})
	}))
	defer service.Close()
	p, err := fcsandbox.New(fcsandbox.Config{ManagerURL: service.URL, ImagePrepareURL: service.URL, CustomImagePrefix: "registry.example/envs", Runtimes: []string{"universal"}}, strings.Repeat("s", 32), nil)
	if err != nil {
		t.Fatal(err)
	}
	own, _ := p.Bind(&commonv1.TenantContext{TenantId: "t1"})
	env, err := own.Create(context.Background(), sandbox.Spec{Runtime: "universal", Network: "none", Image: image})
	if err != nil || env.Image != image || !env.HealthConfirmed {
		t.Fatal(env, err)
	}
	other, _ := p.Bind(&commonv1.TenantContext{TenantId: "t2"})
	if _, err = other.Create(context.Background(), sandbox.Spec{Runtime: "universal", Image: image}); err == nil {
		t.Fatal("cross tenant preparation")
	}
	if prepared != 1 || created != 1 {
		t.Fatal(prepared, created)
	}
}

func TestPreparingImageNeverFallsBackAndHonorsCancellation(t *testing.T) {
	image := "registry.example/envs/t1/env@sha256:" + strings.Repeat("a", 64)
	managerCalls := 0
	service := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/images/prepare" {
			managerCalls++
			t.Error("VM started before image ready")
		}
		w.WriteHeader(202)
		json.NewEncoder(w).Encode(map[string]string{"state": "preparing", "image": image})
	}))
	defer service.Close()
	p, _ := fcsandbox.New(fcsandbox.Config{ManagerURL: service.URL, ImagePrepareURL: service.URL, CustomImagePrefix: "registry.example/envs", Runtimes: []string{"universal"}}, strings.Repeat("s", 32), nil)
	own, _ := p.Bind(&commonv1.TenantContext{TenantId: "t1"})
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	if _, err := own.Create(ctx, sandbox.Spec{Runtime: "universal", Image: image}); err == nil {
		t.Fatal("accepted pending image")
	}
	if managerCalls != 0 {
		t.Fatal(managerCalls)
	}
}

func TestInvalidPreparationCannotCreateVMOrRedirectCredentials(t *testing.T) {
	image := "registry.example/envs/t1/env@sha256:" + strings.Repeat("a", 64)
	for _, failure := range []string{"wrong_image", "missing_digest", "wrong_alias", "redirect"} {
		t.Run(failure, func(t *testing.T) {
			vmCalls := 0
			service := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/v1/images/prepare" {
					vmCalls++
					return
				}
				result := map[string]string{"state": "ready", "image": image, "runtime": "valid", "source_digest": "sha256:" + strings.Repeat("b", 64)}
				switch failure {
				case "wrong_image":
					result["image"] = "other"
				case "missing_digest":
					delete(result, "source_digest")
				case "wrong_alias":
					result["runtime"] = "../../other"
				case "redirect":
					w.Header().Set("Location", "/credential-sink")
					w.WriteHeader(302)
				}
				json.NewEncoder(w).Encode(result)
			}))
			defer service.Close()
			p, _ := fcsandbox.New(fcsandbox.Config{ManagerURL: service.URL, ImagePrepareURL: service.URL, CustomImagePrefix: "registry.example/envs", Runtimes: []string{"universal"}}, strings.Repeat("s", 32), nil)
			own, _ := p.Bind(&commonv1.TenantContext{TenantId: "t1"})
			if _, err := own.Create(context.Background(), sandbox.Spec{Runtime: "universal", Image: image}); err == nil {
				t.Fatal("accepted bad preparation")
			}
			if vmCalls != 0 {
				t.Fatal("VM or redirected request", vmCalls)
			}
		})
	}
}
