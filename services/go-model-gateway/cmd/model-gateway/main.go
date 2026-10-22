// Command model-gateway 是所有模型调用的唯一出口（设计书 3.3）：
//
//	model-gateway serve               在 AR_MODEL_GATEWAY_ADDR 上提供 ModelGatewayService（gRPC，明文 HTTP/2）与 GET /healthz
//	model-gateway dev-secret <名称>    从标准输入读密钥，写进 AR_SECRETS_FILE（开发 SecretStore）的平台作用域
//
// 配置见 internal/config 的环境变量。serve 按 AR_MODEL_PROVIDER 接供应商适配层：openai_compat
// （libs/go-core/adapters/openaicompat，密钥经 SecretStore 读取）或 mock（libs/go-core/dev/mockmodel）。
// serve 每次成功的调用向标准输出写一行计量记录（JSON），日志写标准错误；两者都不含消息内容与密钥。
// dev-secret 的密钥只从标准输入读，不经过命令行参数，也不回显到标准输出或标准错误。
package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/openaicompat"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/mockmodel"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1/modelgatewayv1connect"

	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/config"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/gateway"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/profiles"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/router"
)

const usage = `用法：
  model-gateway serve
  model-gateway dev-secret <名称>     （密钥从标准输入读取）
`

// shutdownGrace 是 ctx 结束后等待进行中的请求完成的时间。
const shutdownGrace = 5 * time.Second

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	code := run(ctx, os.Args[1:], os.Stdin, os.Stdout, os.Stderr, os.Getenv)
	stop()
	os.Exit(code)
}

// run 执行一个子命令，返回退出码：0 成功，1 执行失败，2 用法或配置错误。
//
//   - serve：读配置（不合法返回 2，标准错误里指出变量名），加载 Model Profile（读不到或不合法返回 1），构造供应商适配层
//     与选档器，在 AR_MODEL_GATEWAY_ADDR 上监听（失败返回 1），一直运行到 ctx 结束后优雅退出并返回 0。
//     计量记录写 stdout，日志写 stderr。
//   - dev-secret <名称>：名称必须恰好一个（否则 2）；读完 stdin 并去掉首尾空白，为空返回 1；
//     写入 AR_SECRETS_FILE（缺少时返回 2）的平台作用域，写完重新读回核对。不输出密钥。
func run(ctx context.Context, args []string, stdin io.Reader, stdout, stderr io.Writer, getenv func(string) string) int {
	if len(args) == 0 {
		fmt.Fprint(stderr, usage)
		return 2
	}
	switch args[0] {
	case "serve":
		return serve(ctx, stdout, stderr, getenv)
	case "dev-secret":
		return devSecret(ctx, args[1:], stdin, stdout, stderr, getenv)
	default:
		fmt.Fprintf(stderr, "不认识的子命令 %q\n%s", args[0], usage)
		return 2
	}
}

func serve(ctx context.Context, stdout, stderr io.Writer, getenv func(string) string) int {
	cfg, err := config.FromEnv(getenv)
	if err != nil {
		fmt.Fprintln(stderr, "model-gateway：", err)
		return 2
	}
	catalog, err := profiles.Load(cfg.ProfilesFile)
	if err != nil {
		fmt.Fprintln(stderr, "model-gateway：加载 Model Profile 失败：", err)
		return 1
	}
	provider, err := newProvider(cfg)
	if err != nil {
		fmt.Fprintln(stderr, "model-gateway：", err)
		return 1
	}
	defer provider.Close()
	logger := slog.New(slog.NewTextHandler(stderr, nil))
	srv, err := gateway.New(gateway.Options{
		Provider: provider,
		Router:   router.New(catalog, router.FileAllowlist{Path: cfg.TenantPoliciesFile}),
		Meter:    stdout,
		Logger:   logger,
	})
	if err != nil {
		fmt.Fprintln(stderr, "model-gateway：", err)
		return 1
	}
	mux := http.NewServeMux()
	path, handler := modelgatewayv1connect.NewModelGatewayServiceHandler(srv)
	mux.Handle(path, handler)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, "ok\n") })
	protocols := new(http.Protocols)
	protocols.SetHTTP1(true)
	protocols.SetUnencryptedHTTP2(true)
	server := &http.Server{Handler: mux, Protocols: protocols, ReadHeaderTimeout: 10 * time.Second}

	ln, err := net.Listen("tcp", cfg.Addr)
	if err != nil {
		fmt.Fprintln(stderr, "model-gateway：监听失败：", err)
		return 1
	}
	logger.Info("model-gateway 已启动", "addr", ln.Addr().String(), "provider", cfg.Provider, "profiles", len(catalog.All()))
	served := make(chan error, 1)
	go func() { served <- server.Serve(ln) }()
	select {
	case err := <-served:
		fmt.Fprintln(stderr, "model-gateway：服务中止：", err)
		return 1
	case <-ctx.Done():
	}
	shutdown, cancel := context.WithTimeout(context.Background(), shutdownGrace)
	defer cancel()
	if err := server.Shutdown(shutdown); err != nil {
		_ = server.Close()
	}
	if err := <-served; err != nil && !errors.Is(err, http.ErrServerClosed) {
		fmt.Fprintln(stderr, "model-gateway：", err)
		return 1
	}
	return 0
}

// newProvider 按配置构造供应商适配层。
func newProvider(cfg config.Config) (modelprovider.Provider, error) {
	if cfg.Provider == config.ProviderMock {
		return mockmodel.New(mockmodel.Options{}), nil
	}
	secrets, err := filesecrets.New(filesecrets.Options{Path: cfg.SecretsFile})
	if err != nil {
		return nil, fmt.Errorf("读 %s 失败", config.EnvSecretsFile)
	}
	return openaicompat.New(openaicompat.Options{BaseURL: cfg.ProviderBaseURL, Secrets: secrets, KeyName: cfg.ProviderKeyName})
}

// maxSecret 是 dev-secret 从标准输入读取的上限。
const maxSecret = 64 << 10

func devSecret(ctx context.Context, args []string, stdin io.Reader, stdout, stderr io.Writer, getenv func(string) string) int {
	if len(args) != 1 || strings.TrimSpace(args[0]) == "" {
		fmt.Fprint(stderr, "dev-secret 要恰好一个名称\n", usage)
		return 2
	}
	name := args[0]
	path, err := config.SecretsFile(getenv)
	if err != nil {
		fmt.Fprintln(stderr, "model-gateway：", err)
		return 2
	}
	raw, err := io.ReadAll(io.LimitReader(stdin, maxSecret))
	if err != nil {
		fmt.Fprintln(stderr, "model-gateway：读标准输入失败")
		return 1
	}
	value := strings.TrimSpace(string(raw))
	if value == "" {
		fmt.Fprintln(stderr, "model-gateway：标准输入为空，没有写入")
		return 1
	}
	secrets, err := filesecrets.New(filesecrets.Options{Path: path})
	if err != nil {
		fmt.Fprintf(stderr, "model-gateway：读 %s 失败\n", config.EnvSecretsFile)
		return 1
	}
	secrets.SeedSecret("", name, value)
	// SeedSecret 不返回错误：从文件重新读回核对。
	reread, err := filesecrets.New(filesecrets.Options{Path: path})
	if err != nil {
		fmt.Fprintln(stderr, "model-gateway：写入后读不回来")
		return 1
	}
	store, err := reread.BindPlatform("dev-secret")
	if err != nil {
		fmt.Fprintln(stderr, "model-gateway：写入后读不回来")
		return 1
	}
	got, err := store.Read(ctx, name)
	if err != nil || got.RevealString() != value {
		fmt.Fprintln(stderr, "model-gateway：写入后读回的密钥不一致")
		return 1
	}
	fmt.Fprintf(stdout, "已写入平台作用域的密钥 %s\n", name)
	return 0
}
