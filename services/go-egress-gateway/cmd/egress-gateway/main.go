// Command egress-gateway 是沙箱的出网网关（设计书改动稿 W8；docs/plan/cloud-deploy.md D-16）：沙箱默认断网，联网经它，
// 按 Task 的联网授权放行，自己解析并锁定地址、拒绝内网，限用量、逐连接审计；另有进网的预览入口。协议见 internal/proxy。
// 只用标准库与 libs/go-core。
//
//	egress-gateway serve
//
// 环境变量：
//
//	AR_EGRESS_ADDR            对沙箱的代理端口，默认 :8095
//	AR_EGRESS_ADMIN_ADDR      管理端口（收回授权、指标），默认 127.0.0.1:8096；不能与上面相同
//	AR_EGRESS_PREVIEW_ADDR    预览入口的端口（经集群的入口转发），空表示不开；AR_EGRESS_PREVIEW_DOMAIN 是预览地址的域名
//	AR_EGRESS_DENY_CIDRS      另外拒绝的网段（逗号分隔），填集群的 Pod 与 Service 网段，例如 10.42.0.0/16,10.43.0.0/16
//	AR_EGRESS_GRANT_TTL_MS    授权缓存的时长（毫秒），默认 2000
//	AR_SECRETS_FILE           SecretStore：平台作用域的 sandbox/env-credential-key（校验环境凭据）与 egress-gateway/admin-token；
//	                          可选 sandbox/egress-ca-cert 与 sandbox/egress-ca-key（网络密钥终结 TLS 用的网关 CA，ENV-02）；
//	                          网络密钥的值在租户作用域的 user-secrets/<用户>/<名字>
//	AR_DATA_DIR、AR_RELATIONAL_DSN   业务库（环境表、联网授权、用量、审计），与 Worker 同一个
//	OTEL_EXPORTER_OTLP_ENDPOINT      设了时每个连接一个 runtime.egress span 发给 Collector
package main

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/oteltelemetry"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/postgres"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/envcred"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/telemetry"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-egress-gateway/internal/grants"
	"github.com/agentruntime/agent-runtime/services/go-egress-gateway/internal/netguard"
	"github.com/agentruntime/agent-runtime/services/go-egress-gateway/internal/proxy"
)

func main() {
	if len(os.Args) < 2 || os.Args[1] != "serve" {
		fmt.Fprintln(os.Stderr, "用法：egress-gateway serve")
		os.Exit(2)
	}
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	if err := serve(log, os.Getenv, os.Stderr); err != nil {
		log.Error("出网网关退出", "err", err.Error())
		os.Exit(1)
	}
}

func envOr(getenv func(string) string, key, fallback string) string {
	if v := getenv(key); v != "" {
		return v
	}
	return fallback
}

func serve(log *slog.Logger, getenv func(string) string, stderr io.Writer) error {
	addr := envOr(getenv, "AR_EGRESS_ADDR", ":8095")
	adminAddr := envOr(getenv, "AR_EGRESS_ADMIN_ADDR", "127.0.0.1:8096")
	previewAddr := getenv("AR_EGRESS_PREVIEW_ADDR")
	if addr == adminAddr || (previewAddr != "" && (previewAddr == addr || previewAddr == adminAddr)) {
		return errors.New("AR_EGRESS_ADDR、AR_EGRESS_ADMIN_ADDR、AR_EGRESS_PREVIEW_ADDR 要各不相同（管理端口不对沙箱开放）")
	}
	deny, err := netguard.ParseCIDRs(getenv("AR_EGRESS_DENY_CIDRS"))
	if err != nil {
		return fmt.Errorf("AR_EGRESS_DENY_CIDRS 格式不对：%w", err)
	}
	ttl := 2 * time.Second
	if v := getenv("AR_EGRESS_GRANT_TTL_MS"); v != "" {
		ms, err := strconv.Atoi(v)
		if err != nil || ms < 100 || ms > 30000 {
			return errors.New("AR_EGRESS_GRANT_TTL_MS 要在 100 到 30000 之间")
		}
		ttl = time.Duration(ms) * time.Millisecond
	}
	secretsFile := getenv("AR_SECRETS_FILE")
	if secretsFile == "" {
		return errors.New("要给 AR_SECRETS_FILE（校验环境凭据的密钥在那里）")
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	secrets, err := filesecrets.New(filesecrets.Options{Path: secretsFile})
	if err != nil {
		return err
	}
	platform, err := secrets.BindPlatform("egress-gateway")
	if err != nil {
		return err
	}
	key, err := platform.Read(ctx, envcred.SecretName)
	if err != nil {
		return fmt.Errorf("SecretStore 的平台作用域里没有 %s", envcred.SecretName)
	}
	adminToken := ""
	if v, err := platform.Read(ctx, "egress-gateway/admin-token"); err == nil {
		adminToken = string(v.Reveal())
	}
	store, err := businessStore(ctx, getenv)
	if err != nil {
		return err
	}
	defer store.Close()
	var tel telemetry.Provider
	if strings.TrimSpace(getenv(oteltelemetry.EnvEndpoint)) != "" {
		if tel, err = oteltelemetry.FromEnv("egress-gateway", getenv, stderr); err != nil {
			return err
		}
		defer tel.Close()
	}
	grantStore := grants.New(store, ttl, nil)
	// ENV-02 的网络密钥：网关 CA（没有时配了网络密钥的主机一律拒绝）；密钥按租户作用域每个请求现读。
	var ca *tls.Certificate
	if cert, err := platform.Read(ctx, "sandbox/egress-ca-cert"); err == nil {
		caKey, err := platform.Read(ctx, "sandbox/egress-ca-key")
		if err != nil {
			return errors.New("SecretStore 的平台作用域里有 sandbox/egress-ca-cert 却没有 sandbox/egress-ca-key")
		}
		if ca, err = proxy.ParseCA(cert.Reveal(), caKey.Reveal()); err != nil {
			return err
		}
	}
	readSecret := func(ctx context.Context, tenantID, name string) ([]byte, error) {
		scope, err := secrets.Bind(&commonv1.TenantContext{TenantId: tenantID})
		if err != nil {
			return nil, err
		}
		v, err := scope.Read(ctx, name)
		if err != nil {
			return nil, err
		}
		return v.Reveal(), nil
	}
	gw := proxy.New(proxy.Config{
		Key: key.Reveal(), Store: grantStore, Deny: deny, Log: log, Telemetry: tel,
		Dialer:        &net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second},
		PreviewDomain: getenv("AR_EGRESS_PREVIEW_DOMAIN"),
		CA:            ca, Secrets: readSecret,
	})
	servers := []*http.Server{
		{Addr: addr, Handler: gw, ReadHeaderTimeout: 10 * time.Second},
		{Addr: adminAddr, Handler: gw.Admin(adminToken), ReadHeaderTimeout: 10 * time.Second},
	}
	if previewAddr != "" {
		servers = append(servers, &http.Server{Addr: previewAddr, Handler: gw.Preview(), ReadHeaderTimeout: 10 * time.Second})
	}
	errs := make(chan error, len(servers))
	for _, srv := range servers {
		go func() { errs <- srv.ListenAndServe() }()
	}
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	log.Info("出网网关在运行", "addr", addr, "admin", adminAddr, "preview", previewAddr)
	for {
		select {
		case err := <-errs:
			return err
		case <-ticker.C:
			// 授权的用量与每天的出网字节写回业务库。
			if failed := grantStore.Flush(ctx); failed > 0 {
				log.Warn("出网用量暂时写不进业务库", "pending", failed)
			}
		case <-ctx.Done():
			shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			for _, srv := range servers {
				_ = srv.Shutdown(shutdown)
			}
			grantStore.Flush(shutdown)
			return nil
		}
	}
}

func businessStore(ctx context.Context, getenv func(string) string) (relational.Provider, error) {
	if dsn := getenv("AR_RELATIONAL_DSN"); dsn != "" {
		return postgres.New(ctx, postgres.Options{DSN: dsn})
	}
	dataDir := envOr(getenv, "AR_DATA_DIR", "/var/lib/agentruntime")
	dir := filepath.Join(dataDir, "relational")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	return sqlitestore.New(sqlitestore.Options{Dir: dir})
}
