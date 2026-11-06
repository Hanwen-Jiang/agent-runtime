package personalconfigapi

import (
	"bytes"
	"connectrpc.com/connect"
	"context"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/localingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	pb "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/personalconfig/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/personalconfig/v1/personalconfigv1connect"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/registry"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestPersonalConfigHTTPIdentityAndNoBodyLogs(t *testing.T) {
	ctx := context.Background()
	store, err := sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if err = store.Migrate(ctx, registry.Migrations()); err != nil {
		t.Fatal(err)
	}
	logs := &bytes.Buffer{}
	logger := slog.New(slog.NewTextHandler(logs, nil))
	reg, err := registry.New(registry.Options{Store: store, SchemaDir: "../../../../schemas", Logger: logger})
	if err != nil {
		t.Fatal(err)
	}
	ing := localingress.New(localingress.Options{SigningKey: []byte(strings.Repeat("k", 32)), Limit: 1000, Window: time.Minute})
	mux := http.NewServeMux()
	(&Server{Ingress: ing, Registry: reg, Logger: logger}).Mount(mux)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	client := personalconfigv1connect.NewPersonalConfigServiceClient(srv.Client(), srv.URL)
	request := connect.NewRequest(&pb.PutPersonalConfigRequest{TenantId: "t1", Document: &pb.PersonalConfigDocument{Instructions: "personal-body-marker"}})
	if _, err = client.PutPersonalConfig(ctx, request); connect.CodeOf(err) != connect.CodeUnauthenticated {
		t.Fatalf("anonymous: %v", err)
	}
	request.Header().Set("Authorization", "Bearer "+ing.IssueToken("t1", "alice", time.Hour))
	out, err := client.PutPersonalConfig(ctx, request)
	if err != nil || out.Msg.Config.Version != "user/v1" {
		t.Fatalf("put: %v %v", out, err)
	}
	get := connect.NewRequest(&pb.GetPersonalConfigRequest{TenantId: "t1", Version: "user/v1"})
	get.Header().Set("Authorization", "Bearer "+ing.IssueToken("t1", "bob", time.Hour))
	if _, err = client.GetPersonalConfig(ctx, get); connect.CodeOf(err) != connect.CodeNotFound {
		t.Fatalf("other user: %v", err)
	}
	get.Msg.TenantId = "t2"
	get.Header().Set("Authorization", request.Header().Get("Authorization"))
	if _, err = client.GetPersonalConfig(ctx, get); connect.CodeOf(err) != connect.CodePermissionDenied {
		t.Fatalf("other tenant: %v", err)
	}
	if strings.Contains(logs.String(), "personal-body-marker") || !strings.Contains(logs.String(), out.Msg.Config.Digest) {
		t.Fatalf("audit must log digest only: %s", logs.String())
	}
}
