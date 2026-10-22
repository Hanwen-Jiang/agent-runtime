package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1/registryv1connect"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/config"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/registry"
)

// newRegistry 构造 Registry（T-08）：业务库加上 Registry 的表；发布令牌在 SecretStore 开发实现（AR_SECRETS_FILE）的平台作用域。
func newRegistry(ctx context.Context, store relational.Provider, secretsFile, schemasDir string, logger *slog.Logger) (*registry.Registry, error) {
	if err := store.Migrate(ctx, registry.Migrations()); err != nil {
		return nil, err
	}
	secrets, err := filesecrets.New(filesecrets.Options{Path: secretsFile})
	if err != nil {
		return nil, err
	}
	return registry.New(registry.Options{Store: store, Secrets: secrets, SchemaDir: schemasDir, Logger: logger})
}

// registryServer 在单独的内部端口上提供 RegistryService（明文 HTTP/2，gRPC 客户端直连；不经 Ingress）与 /healthz。
func registryServer(addr string, reg *registry.Registry) *http.Server {
	mux := http.NewServeMux()
	mux.Handle(registryv1connect.NewRegistryServiceHandler(reg))
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, "ok") })
	protocols := new(http.Protocols)
	protocols.SetHTTP1(true)
	protocols.SetUnencryptedHTTP2(true)
	return &http.Server{Addr: addr, Handler: mux, Protocols: protocols, ReadHeaderTimeout: 10 * time.Second}
}

// registrySeed 是 registry-seed 子命令：把种子文件里的文档发布并设为默认（开发与端到端测试用），格式见 registry.SeedEntry。
// 写 serve 用的同一个业务库（AR_DATA_DIR 下的 SQLite，或 AR_RELATIONAL_DSN），按 AR_SCHEMAS_DIR 校验。
func registrySeed(args []string, stdout, stderr io.Writer, getenv func(string) string) int {
	if len(args) != 1 {
		fmt.Fprint(stderr, usage)
		return 2
	}
	data, err := os.ReadFile(args[0])
	if err != nil {
		fmt.Fprintf(stderr, "registry-seed：读不了种子文件 %s\n", args[0])
		return 1
	}
	var file struct {
		Entries []registry.SeedEntry `json:"entries"`
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&file); err != nil {
		fmt.Fprintf(stderr, "registry-seed：种子文件格式不对：%v\n", err)
		return 1
	}
	ctx := context.Background()
	where := config.StoreFromEnv(getenv)
	store, err := newRelationalStore(ctx, where.DataDir, where.RelationalDSN)
	if err != nil {
		fmt.Fprintf(stderr, "registry-seed：打不开业务库：%v\n", err)
		return 1
	}
	defer store.Close()
	if err := store.Migrate(ctx, registry.Migrations()); err != nil {
		fmt.Fprintf(stderr, "registry-seed：迁移业务库失败：%v\n", err)
		return 1
	}
	reg, err := registry.New(registry.Options{Store: store, SchemaDir: where.SchemasDir})
	if err != nil {
		fmt.Fprintf(stderr, "registry-seed：%v\n", err)
		return 1
	}
	results, err := reg.Seed(ctx, file.Entries)
	for _, r := range results {
		scope := "平台"
		if r.Entry.Tenant != "" {
			scope = "租户 " + r.Entry.Tenant
		}
		note := "（默认）"
		if r.Skipped {
			note = "（与默认版本相同，跳过）"
		}
		fmt.Fprintf(stdout, "%s %s %s %s%s\n", scope, r.Entry.Kind, r.Entry.Name, r.Version, note)
	}
	if err != nil {
		fmt.Fprintf(stderr, "registry-seed：%v\n", err)
		return 1
	}
	return 0
}
