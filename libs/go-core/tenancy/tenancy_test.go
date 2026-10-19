package tenancy_test

import (
	"testing"

	"google.golang.org/protobuf/proto"

	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

const session = "ses_01JA8Q2N4H6K8M0P2R4T6V8X0Z"

func fullContext() *commonv1.TenantContext {
	return &commonv1.TenantContext{
		TenantId: "t1", UserId: "u7", WorkspaceId: "w3", SessionId: session,
		Trace: &commonv1.TraceContext{Traceparent: "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"},
	}
}

func TestTenantScopeCopiesAndRoundTrips(t *testing.T) {
	ctx := fullContext()
	scope, err := tenancy.TenantScope(ctx)
	if err != nil {
		t.Fatal(err)
	}
	ctx.TenantId = "t2"
	if scope.TenantID != "t1" || scope.IsPlatform() {
		t.Fatalf("scope = %+v", scope)
	}
	back, err := scope.TenantContext()
	if err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(back, fullContext()) {
		t.Fatalf("round trip = %v", back)
	}
}

func TestMissingTenantIsDenied(t *testing.T) {
	for _, ctx := range []*commonv1.TenantContext{nil, {UserId: "u7"}} {
		_, err := tenancy.TenantScope(ctx)
		conformance.ExpectError(t, err, providererr.Denied, "missing_tenant")
	}
}

func TestMalformedTenantIsInvalid(t *testing.T) {
	for _, id := range conformance.MalformedTenantIDs {
		_, err := tenancy.TenantScope(&commonv1.TenantContext{TenantId: id})
		conformance.ExpectError(t, err, providererr.Invalid, "malformed_tenant")
	}
	for _, ctx := range []*commonv1.TenantContext{
		{TenantId: "t1", UserId: "U7"},
		{TenantId: "t1", WorkspaceId: "../w"},
		{TenantId: "t1", SessionId: "ses_42"},
	} {
		_, err := tenancy.TenantScope(ctx)
		conformance.ExpectError(t, err, providererr.Invalid, "malformed_tenant")
	}
}

func TestPlatformScope(t *testing.T) {
	scope, err := tenancy.PlatformScope("  registry defaults ")
	if err != nil || !scope.IsPlatform() || scope.Purpose != "registry defaults" || scope.TenantID != "" {
		t.Fatalf("scope = %+v, err = %v", scope, err)
	}
	for _, purpose := range []string{"", "   "} {
		_, err := tenancy.PlatformScope(purpose)
		conformance.ExpectError(t, err, providererr.Invalid, "missing_purpose")
	}
	_, err = scope.TenantContext()
	conformance.ExpectError(t, err, providererr.Denied, "")
}
