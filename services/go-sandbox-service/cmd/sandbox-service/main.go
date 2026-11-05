// Command sandbox-service 是 Sandbox Service（SBX-01；docs/plan/sbx-01-sandbox-service-2026-11-03.md）：沙箱环境的借还、执行与
// 只读的工作区操作。Worker 与 task-service 经它用沙箱，不直接持有后端。
//
//	sandbox-service serve                 起服务（环境变量见 internal/config）
//	sandbox-service token <调用方>         生成一个调用方令牌写进 AR_SECRETS_FILE 的平台作用域（sandbox-service/tokens/<调用方>），
//	                                      已有时不动；标准输出只写名字，不写值（开发与 dev_stack 用）
//	sandbox-service kill-local <目录>      杀掉本地后端在这个目录下起的 sandbox-agent 进程（测试与 dev_stack down 用）
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"connectrpc.com/connect"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/oteltelemetry"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/postgres"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/s3objects"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/diskobjects"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/telemetry"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1/registryv1connect"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/config"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/registry"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/server"
)

func main() {
	if len(os.Args) < 2 {
		log.Fatal("用法：sandbox-service serve | token <调用方> | kill-local <目录>")
	}
	switch os.Args[1] {
	case "serve":
		if err := serve(); err != nil {
			log.Fatal(err)
		}
	case "token":
		if len(os.Args) != 3 {
			log.Fatal("用法：sandbox-service token <调用方>")
		}
		if err := token(os.Args[2]); err != nil {
			log.Fatal(err)
		}
	case "kill-local":
		if len(os.Args) != 3 {
			log.Fatal("用法：sandbox-service kill-local <目录>")
		}
		if err := registry.KillLocalEnvironments(os.Args[2]); err != nil {
			log.Fatal(err)
		}
	default:
		log.Fatalf("不认识的子命令 %s", os.Args[1])
	}
}

func token(caller string) error {
	path := os.Getenv("AR_SECRETS_FILE")
	if path == "" {
		return errors.New("要设 AR_SECRETS_FILE")
	}
	secrets, err := filesecrets.New(filesecrets.Options{Path: path})
	if err != nil {
		return err
	}
	store, err := secrets.BindPlatform(registry.SecretPurpose)
	if err != nil {
		return err
	}
	name := server.TokenSecretPrefix + caller
	if _, err := store.Read(context.Background(), name); err == nil {
		fmt.Println(name)
		return nil
	}
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return err
	}
	w, ok := store.(interface {
		Write(context.Context, string, secret.Value) error
	})
	if !ok {
		return errors.New("这个 SecretStore 写不了")
	}
	if err := w.Write(context.Background(), name, secret.New(hex.EncodeToString(raw))); err != nil {
		return err
	}
	fmt.Println(name)
	return nil
}

func serve() error {
	cfg, err := config.FromEnv(os.Getenv)
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	if cfg.SecretsSeedFile != "" {
		if err := filesecrets.MergeSeed(cfg.SecretsFile, cfg.SecretsSeedFile); err != nil {
			return fmt.Errorf("并入 AR_SECRETS_SEED_FILE 失败：%w", err)
		}
	}
	secrets, err := filesecrets.New(filesecrets.Options{Path: cfg.SecretsFile})
	if err != nil {
		return err
	}
	store, err := relationalStore(ctx, cfg)
	if err != nil {
		return err
	}
	defer store.Close()
	objects, err := objectStore(cfg)
	if err != nil {
		return err
	}
	agent := cfg.AgentBinary
	if agent == "" {
		agent, _ = exec.LookPath("sandbox-agent")
	}
	reg, err := registry.Load(cfg.BackendsFile, registry.Options{WorkspaceRoot: cfg.WorkspaceRoot, StateDir: cfg.StateDir, AgentBinary: agent, Secrets: secrets})
	if err != nil {
		return err
	}
	// 设了 OTEL_EXPORTER_OTLP_ENDPOINT 时对账每一轮报一次卷用量给 Collector（DISK-A 的面板与告警）；没设时不报。
	var tel telemetry.Provider
	if strings.TrimSpace(os.Getenv(oteltelemetry.EnvEndpoint)) != "" {
		if tel, err = oteltelemetry.FromEnv("sandbox-service", os.Getenv, os.Stderr); err != nil {
			return fmt.Errorf("Telemetry 配置不合法：%w", err)
		}
		defer tel.Close()
	}
	svc, err := server.New(ctx, server.Options{
		Registry: reg, Relational: store, Objects: objects, Secrets: secrets, Policy: policyLookup(cfg), Callers: cfg.Callers,
		MirrorURL: cfg.MirrorURL, EgressURL: cfg.EgressURL, Runtime: cfg.Runtime, HoldMax: cfg.HoldMax, ColdDays: cfg.ColdDays, PolicyTTL: cfg.PolicyTTL, Logger: logger,
		Telemetry: tel,
	})
	if err != nil {
		return err
	}
	defer svc.Close()
	if cfg.ReconcileEvery > 0 {
		go func() {
			ticker := time.NewTicker(cfg.ReconcileEvery)
			defer ticker.Stop()
			for {
				select {
				case <-ctx.Done():
					return
				case <-ticker.C:
					report, err := svc.Reconcile(ctx)
					if err != nil {
						logger.Warn("对账失败", "error", err)
						continue
					}
					if report.Reaped+report.Lost+report.OrphansDestroyed+report.StaleFailed+report.Cooled+len(report.Errors) > 0 {
						logger.Info("对账", "reaped", report.Reaped, "lost", report.Lost, "orphans", report.OrphansDestroyed,
							"stale_failed", report.StaleFailed, "held", report.Held, "cooled", report.Cooled, "errors", len(report.Errors))
					}
				}
			}
		}()
	}
	protocols := new(http.Protocols)
	protocols.SetHTTP1(true)
	protocols.SetUnencryptedHTTP2(true)
	srv := &http.Server{Addr: cfg.Addr, Handler: svc.Handler(), Protocols: protocols, ReadHeaderTimeout: 10 * time.Second}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdown)
	}()
	logger.Info("sandbox-service 在运行", "addr", cfg.Addr, "backends", reg.Names())
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}

func relationalStore(ctx context.Context, cfg config.Config) (relational.Provider, error) {
	if cfg.RelationalDSN != "" {
		return postgres.New(ctx, postgres.Options{DSN: cfg.RelationalDSN, MaxConns: int32(cfg.MaxConns)})
	}
	dir := filepath.Join(cfg.DataDir, "relational")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	return sqlitestore.New(sqlitestore.Options{Dir: dir})
}

func objectStore(cfg config.Config) (objectstore.Provider, error) {
	if opts, ok, err := s3objects.OptionsFromEnv(os.Getenv); err != nil {
		return nil, err
	} else if ok {
		return s3objects.New(opts)
	}
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		return nil, err
	}
	return diskobjects.New(diskobjects.Options{Root: cfg.ObjectStoreDir, SigningKey: key})
}

// policyLookup 读租户策略：开发用的策略文件里列出的租户以文件为准（同 task-service），其余经 Registry；都没有时 nil（默认后端）。
func policyLookup(cfg config.Config) registry.Lookup {
	var client registryv1connect.RegistryServiceClient
	if cfg.RegistryAddress != "" {
		h2c := &http.Client{Transport: &http.Transport{Protocols: func() *http.Protocols {
			p := new(http.Protocols)
			p.SetUnencryptedHTTP2(true)
			return p
		}()}}
		client = registryv1connect.NewRegistryServiceClient(h2c, "http://"+cfg.RegistryAddress, connect.WithGRPC())
	}
	return func(ctx context.Context, tenantID string) (map[string]any, error) {
		if cfg.PoliciesFile != "" {
			if raw, err := os.ReadFile(cfg.PoliciesFile); err == nil {
				var doc struct {
					Tenants map[string]map[string]any `json:"tenants"`
				}
				if json.Unmarshal(raw, &doc) == nil {
					if p, ok := doc.Tenants[tenantID]; ok {
						return p, nil
					}
				}
			}
		}
		if client == nil {
			return nil, nil
		}
		resp, err := client.GetTenantPolicy(ctx, connect.NewRequest(&registryv1.GetTenantPolicyRequest{Tenant: &commonv1.TenantContext{TenantId: tenantID}}))
		if err != nil {
			if connect.CodeOf(err) == connect.CodeNotFound {
				return nil, nil
			}
			return nil, err
		}
		value := resp.Msg.GetTenantPolicy().GetDocument().GetValue()
		if value == nil {
			return nil, nil
		}
		doc, _ := value.AsInterface().(map[string]any)
		return doc, nil
	}
}
