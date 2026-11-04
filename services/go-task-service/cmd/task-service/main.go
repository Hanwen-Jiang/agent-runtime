// Command task-service 是 Control Plane 的入口（设计书 3.1）：
//
//	task-service serve                                        Task API、Workspace API（D-06）、SSE 端点与 Session 工作流的 Worker
//	task-service dev-token --tenant t1 --user u1 [--ttl 1h] [--scope task.approve]   用 AR_DEV_SIGNING_KEY 签发开发令牌
//	task-service dev-token ... --jwt-key <私钥文件> --iss <签发方> --kid <kid>          改用 Ed25519 私钥签 JWT（AUTH-01）
//	task-service keygen --iss <签发方> --kid <kid> --out <私钥文件>                     生成密钥对：私钥写文件，标准输出是签发方登记
//	task-service replay --dir <事件历史目录>                    用当前代码回放 Session 工作流的事件历史
//	task-service registry-seed <种子文件>                       把一批文档发布到 Registry 并设为默认（开发与端到端测试用）
//
// 配置见 internal/config 的环境变量。业务库配了 AR_RELATIONAL_DSN 用 Postgres、Cache 配了 AR_VALKEY_URL 用 Valkey，
// 否则是开发实现（SQLite、内存 Cache）。Ingress：配了 AR_INGRESS_ISSUERS_FILE 时验签发方用 Ed25519 签的 JWT（只持公钥，
// libs/go-core/adapters/jwtingress）；AR_INGRESS_DEV_TOKENS 决定还收不收开发令牌（生产模式默认不收）。
package main

import (
	"context"
	"crypto/rand"
	"encoding/json"
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

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/jwtingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/postgres"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/s3objects"
	temporaladapter "github.com/agentruntime/agent-runtime/libs/go-core/adapters/temporal"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/temporal/claimcheck"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/valkey"
	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/diskobjects"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/localingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/memcache"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/modelbudget"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/cache"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/sealing"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/config"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/environmentapi"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/events"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/ids"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/registry"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/sandboxgrants"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/session"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskapi"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/workspaceapi"
)

const usage = `用法：
  task-service serve
  task-service dev-token --tenant <租户> --user <用户> [--ttl 1h] [--scope <scope>]... [--jwt-key <私钥文件> --iss <签发方> --kid <kid>]
  task-service keygen --iss <签发方> --kid <kid> --out <私钥文件>
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
	case "keygen":
		return keygen(args[1:], stdout, stderr)
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
	jwtKey := fs.String("jwt-key", "", "Ed25519 私钥文件（keygen 生成的）：给了就签 JWT，不用 AR_DEV_SIGNING_KEY")
	issuer := fs.String("iss", "", "签 JWT 时的签发方（与签发方登记的 iss 一致）")
	kid := fs.String("kid", "", "签 JWT 时的密钥编号（与签发方登记的 kid 一致）")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *tenant == "" || *user == "" || *ttl <= 0 {
		fmt.Fprint(stderr, usage)
		return 2
	}
	if *jwtKey != "" {
		if *issuer == "" || *kid == "" {
			fmt.Fprint(stderr, usage)
			return 2
		}
		raw, err := os.ReadFile(*jwtKey)
		if err != nil {
			fmt.Fprintf(stderr, "读不了私钥文件：%v\n", err)
			return 1
		}
		private, err := jwtingress.ParsePrivateKey(raw)
		if err != nil {
			fmt.Fprintf(stderr, "私钥文件不对：%v\n", err)
			return 1
		}
		token, err := jwtingress.NewSigner(*issuer, *kid, private, nil).Issue(jwtingress.Claims{Tenant: *tenant, User: *user, Scopes: scopes, TTL: *ttl})
		if err != nil {
			fmt.Fprintln(stderr, err)
			return 1
		}
		fmt.Fprintln(stdout, token)
		return 0
	}
	key := getenv(config.EnvDevSigningKey)
	if key == "" {
		fmt.Fprintf(stderr, "缺少 %s\n", config.EnvDevSigningKey)
		return 1
	}
	fmt.Fprintln(stdout, localingress.New(localingress.Options{SigningKey: []byte(key)}).IssueTokenWithScopes(*tenant, *user, *ttl, scopes...))
	return 0
}

// keygen 生成签发方的一对 Ed25519 密钥（AUTH-01）：私钥（PKCS#8 PEM）写进 --out，只给签发方（例如 coding-agent），
// 不进 Runtime；标准输出是只含公钥的签发方登记，放进 AR_INGRESS_ISSUERS_FILE（换密钥时把新的 kid 并进已有的登记）。
func keygen(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("keygen", flag.ContinueOnError)
	fs.SetOutput(stderr)
	issuer := fs.String("iss", "", "签发方")
	kid := fs.String("kid", "", "密钥编号")
	out := fs.String("out", "", "私钥写到哪个文件（已有时不覆盖）")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *issuer == "" || *kid == "" || *out == "" {
		fmt.Fprint(stderr, usage)
		return 2
	}
	public, private, err := jwtingress.GenerateKey()
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	f, err := os.OpenFile(*out, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		fmt.Fprintf(stderr, "写不了私钥文件（已有的不覆盖）：%v\n", err)
		return 1
	}
	if _, err := f.Write(private); err != nil {
		f.Close()
		fmt.Fprintln(stderr, err)
		return 1
	}
	if err := f.Close(); err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	entry := jwtingress.Config{Audience: jwtingress.DefaultAudience, Issuers: []jwtingress.Issuer{{Issuer: *issuer, Keys: map[string]string{*kid: public}}}}
	enc := json.NewEncoder(stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(entry); err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	return 0
}

// newIngress 按配置构造 Ingress（AUTH-01）：配了签发方登记时验 JWT，限流经 Cache（配了 Valkey 时几个副本共用一份计数）；
// 收开发令牌时把它们交给内置的开发实现（迁移期间，生产模式下记一条警告）。只收开发令牌时就是开发实现本身。
func newIngress(cfg config.Config, caches cache.Provider, clk clock.Clock, logger *slog.Logger) (ingress.Ingress, error) {
	var dev ingress.Ingress
	if cfg.DevTokens {
		dev = localingress.New(localingress.Options{SigningKey: cfg.DevSigningKey.Reveal(), Clock: clk, Limit: cfg.RateLimitPerMinute, Window: time.Minute})
		if cfg.Production {
			logger.Warn("生产模式仍收开发令牌：AUTH-01 迁移期间，coding-agent 改发 JWT 后去掉", "env", config.EnvIngressDevTokens)
		}
	}
	if cfg.IngressIssuersFile == "" {
		if dev == nil {
			return nil, fmt.Errorf("既不收开发令牌、又没有 %s：没有能用的 Ingress", config.EnvIngressIssuersFile)
		}
		return dev, nil
	}
	issuers, err := jwtingress.LoadConfig(cfg.IngressIssuersFile)
	if err != nil {
		return nil, fmt.Errorf("%s：%w", config.EnvIngressIssuersFile, err)
	}
	limits, err := caches.BindPlatform("ingress")
	if err != nil {
		return nil, err
	}
	return jwtingress.New(jwtingress.Options{Config: issuers, Clock: clk, Limits: limits, Limit: cfg.RateLimitPerMinute, Window: time.Minute, Fallback: dev})
}

func quietLogger() tlog.Logger {
	return tlog.NewStructuredLogger(slog.New(slog.NewTextHandler(io.Discard, nil)))
}

// newClaimCheck 用 ObjectStore、SecretStore 的开发实现构造 Claim-Check（S-06）；与 Python Worker 指向同一处。
// 同一个 ObjectStore 也是交付文件所在处（N-18：Worker 的 deliver_file 写，Task API 的下载读）。
func newClaimCheck(s3 *s3objects.Options, objectStoreDir, secretsFile string, threshold int) (*claimcheck.ClaimCheck, objectstore.Provider, error) {
	var objects objectstore.Provider
	if s3 != nil {
		p, err := s3objects.New(*s3)
		if err != nil {
			return nil, nil, err
		}
		objects = p
	} else {
		signingKey := make([]byte, 32)
		if _, err := rand.Read(signingKey); err != nil {
			return nil, nil, err
		}
		p, err := diskobjects.New(diskobjects.Options{Root: objectStoreDir, SigningKey: signingKey})
		if err != nil {
			return nil, nil, err
		}
		objects = p
	}
	secrets, err := filesecrets.New(filesecrets.Options{Path: secretsFile})
	if err != nil {
		return nil, nil, err
	}
	cc, err := claimcheck.New(claimcheck.Options{Objects: objects, Secrets: secrets, Threshold: threshold})
	return cc, objects, err
}

// egressNotifier：配了出网网关的管理地址（AR_EGRESS_GATEWAY_ADMIN_URL）时，Task 结束收回联网授权后通知网关立即断开（D-16）；
// 管理令牌取 SecretStore 平台作用域的 egress-gateway/admin-token（没有时不带令牌，网关会拒绝）。
func egressNotifier(ctx context.Context, cfg config.Config) (*sandboxgrants.Notifier, error) {
	if cfg.EgressAdminURL == "" {
		return nil, nil
	}
	n := &sandboxgrants.Notifier{URL: cfg.EgressAdminURL}
	secrets, err := filesecrets.New(filesecrets.Options{Path: cfg.SecretsFile})
	if err != nil {
		return nil, err
	}
	platform, err := secrets.BindPlatform("egress-gateway")
	if err != nil {
		return nil, err
	}
	if v, err := platform.Read(ctx, "egress-gateway/admin-token"); err == nil {
		n.Token = string(v.Reveal())
	}
	return n, nil
}

// platformSecret 读 SecretStore 平台作用域里的一个密钥；没有时返回 nil（不是错误）。每次都读文件：密钥可以在运行中补上。
func platformSecret(ctx context.Context, secretsFile, name string) ([]byte, error) {
	secrets, err := filesecrets.New(filesecrets.Options{Path: secretsFile})
	if err != nil {
		return nil, err
	}
	platform, err := secrets.BindPlatform("workspace")
	if err != nil {
		return nil, err
	}
	v, err := platform.Read(ctx, name)
	if providererr.CategoryOf(err) == providererr.NotFound {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return v.Reveal(), nil
}

// newRelationalStore 构造业务库：配置了 AR_RELATIONAL_DSN 时用 Postgres（行级安全，S-12a），否则用 SQLite 开发实现；
// 生产模式（AR_PROFILE=production，TS-PROD-01）下没有连接串直接报错，不回落 SQLite。
func newRelationalStore(ctx context.Context, where config.Store) (relational.Provider, error) {
	if !where.RelationalDSN.IsZero() {
		return postgres.New(ctx, postgres.Options{DSN: string(where.RelationalDSN.Reveal()), MaxConns: int32(where.MaxConns)})
	}
	if where.Production {
		return nil, fmt.Errorf("生产模式要配 %s，不回落 SQLite", config.EnvRelationalDSN)
	}
	dir := filepath.Join(where.DataDir, "relational")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	return sqlitestore.New(sqlitestore.Options{Dir: dir})
}

// newCache 构造 Cache：配了 AR_VALKEY_URL 时用 Valkey（CACHE-01），启动时 Ping 一次，配错的地址或口令尽早暴露；
// 否则用进程内的开发实现（只能单副本，重启丢流）。
func newCache(ctx context.Context, cfg config.Config, clk clock.Clock) (cache.Provider, error) {
	if cfg.ValkeyURL.IsZero() {
		return memcache.New(memcache.Options{Clock: clk}), nil
	}
	p, err := valkey.New(valkey.Options{URL: cfg.ValkeyURL, Clock: clk})
	if err != nil {
		return nil, err
	}
	pingCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	if err := p.Ping(pingCtx); err != nil {
		_ = p.Close()
		return nil, fmt.Errorf("连不上 %s 指向的 Valkey：%w", config.EnvValkeyURL, err)
	}
	return p, nil
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
	cc, _, err := newClaimCheck(s3, objectStoreDir, secretsFile, threshold)
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
	cc, objects, err := newClaimCheck(cfg.ObjectStoreS3, cfg.ObjectStoreDir, cfg.SecretsFile, cfg.ClaimCheckThreshold)
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
	store, err := newRelationalStore(ctx, cfg.Store())
	if err != nil {
		return err
	}
	defer store.Close()
	if err := store.Migrate(ctx, taskstore.Migrations()); err != nil {
		return err
	}
	if err := store.Migrate(ctx, modelbudget.Migrations()); err != nil {
		return err
	}
	// 交付文件的元数据（N-18）：与 Worker 同一个迁移 209，谁先启动谁建。
	if err := store.Migrate(ctx, taskapi.ArtifactMigrations()); err != nil {
		return err
	}
	// 固定版本：配了 Registry（AR_REGISTRY_ADDR）时由它给出，并在单独的内部端口上提供 RegistryService；否则读固定版本文件。
	var pins taskapi.PinResolver = taskapi.FilePinResolver{Path: cfg.PinnedVersionsFile}
	var registrySrv *http.Server
	// 提交时的联网申请按租户策略的 sandbox.network 核对（D-16）：有 Registry 时读它的当前租户策略（与 Worker 同一个来源），
	// 否则读开发用的租户策略文件。
	var network taskapi.NetworkPolicySource
	// effect 上限与预算：有 Registry 时同样按它的租户策略（没有自己策略的租户用平台默认的），开发用的策略文件里列出的租户以文件为准。
	var registryPolicies func(context.Context, *commonv1.TenantContext) ([]byte, bool, error)
	if cfg.RegistryAddr != "" {
		reg, err := newRegistry(ctx, store, cfg.SecretsFile, cfg.SchemasDir, logger)
		if err != nil {
			return fmt.Errorf("Registry 起不来：%w", err)
		}
		pins = registry.Pins{Registry: reg}
		registrySrv = registryServer(cfg.RegistryAddr, reg)
		registryPolicies = reg.CurrentTenantPolicy
		network = taskapi.NetworkPolicyFunc(func(ctx context.Context, tenant *commonv1.TenantContext) (taskapi.NetworkPolicy, error) {
			doc, found, err := reg.CurrentTenantPolicy(ctx, tenant)
			if err != nil || !found {
				return taskapi.NetworkPolicy{}, err
			}
			return taskapi.NetworkPolicyFromDocument(doc)
		})
	}
	egress, err := egressNotifier(ctx, cfg)
	if err != nil {
		return err
	}
	cacheProvider, err := newCache(ctx, cfg, clk)
	if err != nil {
		return err
	}
	defer cacheProvider.Close()
	ing, err := newIngress(cfg, cacheProvider, clk, logger)
	if err != nil {
		return err
	}
	defer ing.Close()

	w := worker.New(c, session.SessionTaskQueue, worker.Options{})
	session.Register(w, cfg.Session, &session.Activities{Cache: cacheProvider, Store: store, Clock: clk, Retention: 24 * time.Hour, Egress: egress})
	if err := w.Start(); err != nil {
		return fmt.Errorf("Session Worker 启动失败：%w", err)
	}
	defer w.Stop()

	defaults := taskapi.Defaults{
		Budget:        &workflowv1.Budget{UsdMicros: 2_000_000, ToolCalls: 50, WallClock: durationpb.New(20 * time.Minute)},
		EffectCeiling: commonv1.Effect_EFFECT_READ,
	}
	policies := taskapi.FilePolicyResolver{Path: cfg.TenantPoliciesFile, Fallback: defaults}
	if registryPolicies != nil {
		policies.Else = taskapi.DocumentPolicyResolver{Source: registryPolicies, Fallback: defaults}
	}
	api, err := taskapi.New(taskapi.Deps{
		ModelBudget:      modelbudget.New(store),
		BudgetPolicies:   modelbudget.Policy{Path: cfg.TenantPoliciesFile, Source: registryPolicies},
		ActiveTaskLimits: cfg.ActiveTaskLimits,
		Ingress:          ing,
		Durable:          temporaladapter.New(c),
		Store:            store,
		Pins:             pins,
		Clock:            clk,
		IDs:              ids.New(clk, nil),
		PublicBaseURL:    cfg.PublicBaseURL,
		Defaults:         defaults,
		Policies:         policies,
		Network:          network,
		Objects:          objects,
	})
	if err != nil {
		return err
	}
	// Workspace API（D-06）：同一个端口、同一个 Ingress；封凭据的密钥在 SecretStore 平台作用域的 workspace/credential-key。
	workspaces, err := workspaceapi.New(workspaceapi.Deps{
		Ingress: ing, Durable: temporaladapter.New(c), Store: store, Timeout: cfg.WorkspaceOperationTimeout, AllowLocalRemotes: cfg.WorkspaceAllowLocalRemotes,
		// 只读操作按工作区的版本号缓存（WS-READ-03）：多副本时是共用的 Valkey。
		Cache: cacheProvider,
		SealingKey: func(ctx context.Context) ([]byte, error) {
			return platformSecret(ctx, cfg.SecretsFile, sealing.SecretName)
		},
	})
	if err != nil {
		return err
	}
	environments, err := environmentapi.New(environmentapi.Deps{Ingress: ing, Store: store})
	if err != nil {
		return err
	}
	mux := http.NewServeMux()
	api.Mount(mux)
	workspaces.Mount(mux)
	environments.Mount(mux)
	streams := events.NewHandler(events.HandlerDeps{Ingress: ing, Cache: cacheProvider, Store: store, Clock: clk})
	mux.Handle("/v1/tasks/", streams)
	mux.Handle("GET /v1/sessions/{session_id}/snapshot", streams)
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
