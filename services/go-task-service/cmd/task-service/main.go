// Command task-service 是 Control Plane 的入口（设计书 3.1）：
//
//	task-service serve                                        Task API、SSE 端点与 Session 工作流的 Worker
//	task-service dev-token --tenant t1 --user u1 [--ttl 1h]   用 AR_DEV_SIGNING_KEY 签发开发令牌
//	task-service replay --dir <事件历史目录>                    用当前代码回放 Session 工作流的事件历史
//
// 配置见 internal/config 的环境变量。serve 目前接的是开发实现（内置 Ingress、内存 Cache、SQLite），
// 生产实现接入后在这里替换。
package main

import (
	"context"
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
	tlog "go.temporal.io/sdk/log"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
	"google.golang.org/protobuf/types/known/durationpb"

	temporaladapter "github.com/agentruntime/agent-runtime/libs/go-core/adapters/temporal"
	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/localingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/memcache"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/config"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/events"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/ids"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/session"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskapi"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
)

const usage = `用法：
  task-service serve
  task-service dev-token --tenant <租户> --user <用户> [--ttl 1h]
  task-service replay --dir <事件历史目录>
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
	case "replay":
		return replay(args[1:], stdout, stderr)
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
	fmt.Fprintln(stdout, localingress.New(localingress.Options{SigningKey: []byte(key)}).IssueToken(*tenant, *user, *ttl))
	return 0
}

func quietLogger() tlog.Logger {
	return tlog.NewStructuredLogger(slog.New(slog.NewTextHandler(io.Discard, nil)))
}

func replay(args []string, stdout, stderr io.Writer) int {
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
	r := worker.NewWorkflowReplayer()
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

	c, err := client.Dial(client.Options{
		HostPort:  cfg.TemporalAddress,
		Namespace: cfg.TemporalNamespace,
		Logger:    tlog.NewStructuredLogger(logger),
	})
	if err != nil {
		return fmt.Errorf("连不上 Temporal：%w", err)
	}
	defer c.Close()

	clk := clock.System{}
	relDir := filepath.Join(cfg.DataDir, "relational")
	if err := os.MkdirAll(relDir, 0o755); err != nil {
		return err
	}
	store, err := sqlitestore.New(sqlitestore.Options{Dir: relDir})
	if err != nil {
		return err
	}
	defer store.Close()
	if err := store.Migrate(ctx, taskstore.Migrations()); err != nil {
		return err
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

	api, err := taskapi.New(taskapi.Deps{
		Ingress:       ing,
		Durable:       temporaladapter.New(c),
		Store:         store,
		Pins:          taskapi.FilePinResolver{Path: cfg.PinnedVersionsFile},
		Clock:         clk,
		IDs:           ids.New(clk, nil),
		PublicBaseURL: cfg.PublicBaseURL,
		Defaults: taskapi.Defaults{
			Budget:        &workflowv1.Budget{UsdMicros: 2_000_000, ToolCalls: 50, WallClock: durationpb.New(20 * time.Minute)},
			EffectCeiling: commonv1.Effect_EFFECT_READ,
		},
	})
	if err != nil {
		return err
	}
	mux := http.NewServeMux()
	api.Mount(mux)
	mux.Handle("/v1/tasks/", events.NewHandler(events.HandlerDeps{Ingress: ing, Cache: cacheProvider, Store: store}))
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, "ok") })

	protocols := new(http.Protocols)
	protocols.SetHTTP1(true)
	protocols.SetUnencryptedHTTP2(true) // gRPC 客户端走明文 HTTP/2
	srv := &http.Server{Addr: cfg.HTTPAddr, Handler: mux, Protocols: protocols, ReadHeaderTimeout: 10 * time.Second}
	errs := make(chan error, 1)
	go func() { errs <- srv.ListenAndServe() }()
	logger.Info("task-service 已启动", "http", cfg.HTTPAddr, "temporal", cfg.TemporalAddress)

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
	return srv.Shutdown(shutdown)
}
