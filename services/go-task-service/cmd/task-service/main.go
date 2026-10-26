// Command task-service 是 Control Plane 的入口（设计书 3.1）：
//
//	task-service serve                                        Task API、SSE 端点与 Session 工作流的 Worker
//	task-service dev-token --tenant t1 --user u1 [--ttl 1h] [--scope task.approve]   用 AR_DEV_SIGNING_KEY 签发开发令牌
//	task-service replay --dir <事件历史目录>                    用当前代码回放 Session 工作流的事件历史
//	task-service registry-seed <种子文件>                       把一批文档发布到 Registry 并设为默认（开发与端到端测试用）
//
// 配置见 internal/config 的环境变量。serve 目前接的是开发实现（内置 Ingress、内存 Cache、SQLite），
// 生产实现接入后在这里替换。
package main

import (
	"context"
	"crypto/rand"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"syscall"
	"time"

	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/converter"
	tlog "go.temporal.io/sdk/log"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
	"google.golang.org/protobuf/types/known/durationpb"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/postgres"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/s3objects"
	temporaladapter "github.com/agentruntime/agent-runtime/libs/go-core/adapters/temporal"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/temporal/claimcheck"
	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/diskobjects"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/localingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/memcache"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/config"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/events"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/ids"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/registry"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/session"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskapi"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
)

const usage = `用法：
  task-service serve
  task-service dev-token --tenant <租户> --user <用户> [--ttl 1h] [--scope <scope>]...
  task-service replay --dir <事件历史目录>
  task-service registry-seed <种子文件>
`

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr, os.Getenv))
}

// run 执行一个子命令，返回退出码：0 成功，1 执行失败，2 用法错误。
func run(args []string, stdout, stderr io.Writer, getenv func(string) string) int {
	if len(args) == 0 {
		fmt.Fprint(stderr, usage)
		return 2
	}
	switch args[0] {
	case "serve":
		return serve(stderr, getenv)
	case "dev-token":
		return devToken(args[1:], stdout, stderr, getenv)
	case "registry-seed":
		return registrySeed(args[1:], stdout, stderr, getenv)
	case "replay":
		return replay(args[1:], stdout, stderr, getenv)
	default:
		fmt.Fprint(stderr, usage)
		return 2
	}
}

func devToken(args []string, stdout, stderr io.Writer, getenv func(string) string) int {
	fs := flag.NewFlagSet("dev-token", flag.ContinueOnError)
	fs.SetOutput(stderr)
	tenant := fs.String("tenant", "", "租户 ID")
	user := fs.String("user", "", "用户 ID")
	ttl := fs.Duration("ttl", time.Hour, "有效期")
	var scopes []string
	fs.Func("scope", "令牌带的 scope，可以写多次（例如 task.approve：审批、回复与取消同租户里别人提交的 Task）", func(s string) error {
		scopes = append(scopes, s)
		return nil
	})
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *tenant == "" || *user == "" || *ttl <= 0 {
		fmt.Fprint(stderr, usage)
		return 2
	}
	key := getenv(config.EnvDevSigningKey)
	if key == "" {
		fmt.Fprintf(stderr, "缺少 %s\n", config.EnvDevSigningKey)
		return 1
	}
	fmt.Fprintln(stdout, localingress.New(localingress.Options{SigningKey: []byte(key)}).IssueTokenWithScopes(*tenant, *user, *ttl, scopes...))
	return 0
}

func quietLogger() tlog.Logger {
	return tlog.NewStructuredLogger(slog.New(slog.NewTextHandler(io.Discard, nil)))
}

// newClaimCheck 用 ObjectStore、SecretStore 的开发实现构造 Claim-Check（S-06）；与 Python Worker 指向同一处。
func newClaimCheck(s3 *s3objects.Options, objectStoreDir, secretsFile string, threshold int) (*claimcheck.ClaimCheck, error) {
	var objects objectstore.Provider
	if s3 != nil {
		p, err := s3objects.New(*s3)
		if err != nil {
			return nil, err
		}
		objects = p
	} else {
		signingKey := make([]byte, 32)
		if _, err := rand.Read(signingKey); err != nil {
			return nil, err
		}
		p, err := diskobjects.New(diskobjects.Options{Root: objectStoreDir, SigningKey: signingKey})
		if err != nil {
			return nil, err
		}
		objects = p
	}
	secrets, err := filesecrets.New(filesecrets.Options{Path: secretsFile})
	if err != nil {
		return nil, err
	}
	return claimcheck.New(claimcheck.Options{Objects: objects, Secrets: secrets, Threshold: threshold})
}

// newRelationalStore 构造业务库：配置了 AR_RELATIONAL_DSN 时用 Postgres（行级安全，S-12a），否则用 SQLite 开发实现。
func newRelationalStore(ctx context.Context, dataDir string, dsn secret.Value) (relational.Provider, error) {
	if !dsn.IsZero() {
		return postgres.New(ctx, postgres.Options{DSN: string(dsn.Reveal())})
	}
	dir := filepath.Join(dataDir, "relational")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	return sqlitestore.New(sqlitestore.Options{Dir: dir})
}

func replay(args []string, stdout, stderr io.Writer, getenv func(string) string) int {
	fs := flag.NewFlagSet("replay", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dir := fs.String("dir", "", "事件历史目录，每个 *.json 是一份 Session 工作流的历史")
	if err := fs.Parse(args); err != nil || *dir == "" {
		fmt.Fprint(stderr, usage)
		return 2
	}
	files, err := filepath.Glob(filepath.Join(*dir, "*.json"))
	if err != nil || len(files) == 0 {
		fmt.Fprintf(stderr, "%s 里没有事件历史\n", *dir)
		return 2
	}
	sort.Strings(files)
	// 带 Claim-Check 引用的历史从 ObjectStore 取回对象；回放时工作流里按 memo 的作用域确定租户。
	objectStoreDir, secretsFile, threshold, err := config.StoreLocations(getenv)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 2
	}
	var s3 *s3objects.Options
	if opts, ok, err := s3objects.OptionsFromEnv(getenv); err != nil {
		fmt.Fprintln(stderr, err)
		return 2
	} else if ok {
		s3 = &opts
	}
	cc, err := newClaimCheck(s3, objectStoreDir, secretsFile, threshold)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	r, err := worker.NewWorkflowReplayerWithOptions(worker.WorkflowReplayerOptions{
		DataConverter: cc.DataConverter(converter.GetDefaultDataConverter(), claimcheck.NewMemoResolver(nil)),
	})
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	r.RegisterWorkflowWithOptions(session.NewWorkflow(session.DefaultConfig()), workflow.RegisterOptions{Name: session.WorkflowName})
	failed := 0
	for _, f := range files {
		if err := replayFile(r, f); err != nil {
			failed++
			fmt.Fprintf(stderr, "回放失败 %s：%v\n", filepath.Base(f), err)
		}
	}
	fmt.Fprintf(stdout, "回放 %d 份，失败 %d 份\n", len(files), failed)
	if failed > 0 {
		return 1
	}
	return 0
}

func replayFile(r worker.WorkflowReplayer, file string) (err error) {
	defer func() {
		if p := recover(); p != nil {
			err = fmt.Errorf("回放时 panic：%v", p)
		}
	}()
	return r.ReplayWorkflowHistoryFromJSONFile(quietLogger(), file)
}

func serve(stderr io.Writer, getenv func(string) string) int {
	logger := slog.New(slog.NewTextHandler(stderr, nil))
	cfg, err := config.FromEnv(getenv)
	if err != nil {
		logger.Error("配置不合法", "error", err)
		return 2
	}
	if err := serveWith(cfg, logger); err != nil {
		logger.Error("task-service 退出", "error", err)
		return 1
	}
	return 0
}

func serveWith(cfg config.Config, logger *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	raw, err := client.Dial(client.Options{
		HostPort:  cfg.TemporalAddress,
		Namespace: cfg.TemporalNamespace,
		Logger:    tlog.NewStructuredLogger(logger),
	})
	if err != nil {
		return fmt.Errorf("连不上 Temporal：%w", err)
	}
	defer raw.Close()
	// 大载荷经 Claim-Check 卸载到 ObjectStore（S-06）：客户端调用按绑定的租户（DurableEngine.Bind），
	// Session 工作流里按 memo 的作用域，activity 按所属工作流 describe 出的作用域（describe 用不带 Claim-Check 的 raw）。
	if cfg.SecretsSeedFile != "" {
		// 先并种子，后面建的 SecretStore（Claim-Check、Registry 的发布令牌）读到的就是并好的文件。
		if err := filesecrets.MergeSeed(cfg.SecretsFile, cfg.SecretsSeedFile); err != nil {
			return fmt.Errorf("并入 %s 失败：%w", config.EnvSecretsSeedFile, err)
		}
	}
	cc, err := newClaimCheck(cfg.ObjectStoreS3, cfg.ObjectStoreDir, cfg.SecretsFile, cfg.ClaimCheckThreshold)
	if err != nil {
		return err
	}
	c, err := client.NewClientFromExisting(raw, client.Options{
		Namespace:     cfg.TemporalNamespace,
		Logger:        tlog.NewStructuredLogger(logger),
		DataConverter: cc.DataConverter(converter.GetDefaultDataConverter(), claimcheck.NewMemoResolver(raw)),
	})
	if err != nil {
		return err
	}
	defer c.Close()

	clk := clock.System{}
	store, err := newRelationalStore(ctx, cfg.DataDir, cfg.RelationalDSN)
	if err != nil {
		return err
	}
	defer store.Close()
	if err := store.Migrate(ctx, taskstore.Migrations()); err != nil {
		return err
	}
	// 固定版本：配了 Registry（AR_REGISTRY_ADDR）时由它给出，并在单独的内部端口上提供 RegistryService；否则读固定版本文件。
	var pins taskapi.PinResolver = taskapi.FilePinResolver{Path: cfg.PinnedVersionsFile}
	var registrySrv *http.Server
	if cfg.RegistryAddr != "" {
		reg, err := newRegistry(ctx, store, cfg.SecretsFile, cfg.SchemasDir, logger)
		if err != nil {
			return fmt.Errorf("Registry 起不来：%w", err)
		}
		pins = registry.Pins{Registry: reg}
		registrySrv = registryServer(cfg.RegistryAddr, reg)
	}
	cacheProvider := memcache.New(memcache.Options{Clock: clk})
	ing := localingress.New(localingress.Options{
		SigningKey: cfg.DevSigningKey.Reveal(), Clock: clk, Limit: cfg.RateLimitPerMinute, Window: time.Minute,
	})

	w := worker.New(c, session.SessionTaskQueue, worker.Options{})
	session.Register(w, cfg.Session, &session.Activities{Cache: cacheProvider, Store: store, Clock: clk, Retention: 24 * time.Hour})
	if err := w.Start(); err != nil {
		return fmt.Errorf("Session Worker 启动失败：%w", err)
	}
	defer w.Stop()

	defaults := taskapi.Defaults{
		Budget:        &workflowv1.Budget{UsdMicros: 2_000_000, ToolCalls: 50, WallClock: durationpb.New(20 * time.Minute)},
		EffectCeiling: commonv1.Effect_EFFECT_READ,
	}
	api, err := taskapi.New(taskapi.Deps{
		Ingress:       ing,
		Durable:       temporaladapter.New(c),
		Store:         store,
		Pins:          pins,
		Clock:         clk,
		IDs:           ids.New(clk, nil),
		PublicBaseURL: cfg.PublicBaseURL,
		Defaults:      defaults,
		Policies:      taskapi.FilePolicyResolver{Path: cfg.TenantPoliciesFile, Fallback: defaults},
	})
	if err != nil {
		return err
	}
	mux := http.NewServeMux()
	api.Mount(mux)
	mux.Handle("/v1/tasks/", events.NewHandler(events.HandlerDeps{Ingress: ing, Cache: cacheProvider, Store: store, Clock: clk}))
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, "ok") })

	protocols := new(http.Protocols)
	protocols.SetHTTP1(true)
	protocols.SetUnencryptedHTTP2(true) // gRPC 客户端走明文 HTTP/2
	srv := &http.Server{Addr: cfg.HTTPAddr, Handler: mux, Protocols: protocols, ReadHeaderTimeout: 10 * time.Second}
	errs := make(chan error, 2)
	go func() { errs <- srv.ListenAndServe() }()
	if registrySrv != nil {
		go func() { errs <- registrySrv.ListenAndServe() }()
	}
	logger.Info("task-service 已启动", "http", cfg.HTTPAddr, "registry", cfg.RegistryAddr, "temporal", cfg.TemporalAddress)

	select {
	case err := <-errs:
		if !errors.Is(err, http.ErrServerClosed) {
			return err
		}
		return nil
	case <-ctx.Done():
	}
	shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if registrySrv != nil {
		_ = registrySrv.Shutdown(shutdown)
	}
	return srv.Shutdown(shutdown)
}
