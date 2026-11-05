package server_test

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"testing"

	"connectrpc.com/connect"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/diskobjects"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	sandboxv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1/sandboxv1connect"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/registry"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/server"
)

// 集群里 SecretStore 的文件在共用卷上：调用方的令牌没有时服务启动时生成（同逐字增量的 delta/<调用方>），Worker 与 task-service
// 读同一份；已有的不动。
func TestMissingCallerTokensAreGeneratedIntoTheSharedSecretStore(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "secrets.json")
	if err := os.WriteFile(path, []byte(`{"version": 1, "scopes": {"_platform": {"secrets": {"sandbox-service/tokens/task-service": "kept-task-service-token-0123456789abcdef"}}}}`), 0o600); err != nil {
		t.Fatal(err)
	}
	start := func() *server.Service {
		secrets, err := filesecrets.New(filesecrets.Options{Path: path})
		if err != nil {
			t.Fatal(err)
		}
		store, err := sqlitestore.New(sqlitestore.Options{Dir: filepath.Join(dir, "relational")})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = store.Close() })
		objects, err := diskobjects.New(diskobjects.Options{Root: filepath.Join(dir, "objects"), SigningKey: []byte("0123456789abcdef0123456789abcdef")})
		if err != nil {
			t.Fatal(err)
		}
		reg, err := registry.Parse([]byte(localMount), registry.Options{WorkspaceRoot: filepath.Join(dir, "ws"), StateDir: filepath.Join(dir, "state"),
			AgentBinary: agentBin, Secrets: secrets})
		if err != nil {
			t.Fatal(err)
		}
		svc, err := server.New(context.Background(), server.Options{Registry: reg, Relational: store, Objects: objects, Secrets: secrets,
			Callers: []string{"worker", "task-service"}})
		if err != nil {
			t.Fatal(err)
		}
		return svc
	}
	read := func() map[string]string {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		var doc struct {
			Scopes map[string]struct {
				Secrets map[string]string `json:"secrets"`
			} `json:"scopes"`
		}
		if err := json.Unmarshal(raw, &doc); err != nil {
			t.Fatal(err)
		}
		return doc.Scopes["_platform"].Secrets
	}
	svc := start()
	first := read()
	worker := first["sandbox-service/tokens/worker"]
	if !regexp.MustCompile(`^[0-9a-f]{64}$`).MatchString(worker) {
		t.Fatalf("Worker 的令牌生成了：%q", worker)
	}
	if first["sandbox-service/tokens/task-service"] != "kept-task-service-token-0123456789abcdef" {
		t.Fatal("已有的令牌不动")
	}
	srv := httptest.NewUnstartedServer(svc.Handler())
	srv.EnableHTTP2 = true
	srv.Start()
	defer srv.Close()
	client := sandboxv1connect.NewSandboxServiceClient(srv.Client(), srv.URL, connect.WithInterceptors(caller("worker", worker)))
	if _, err := client.DescribeTenant(context.Background(), connect.NewRequest(&sandboxv1.DescribeTenantRequest{Tenant: tenant("t1", "", "")})); err != nil {
		t.Fatalf("生成的令牌能用：%v", err)
	}
	_ = svc.Close()
	start()
	if read()["sandbox-service/tokens/worker"] != worker {
		t.Fatal("重启不换令牌")
	}
}
