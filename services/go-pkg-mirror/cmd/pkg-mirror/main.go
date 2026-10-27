// Command pkg-mirror 是沙箱装依赖用的包镜像（设计书改动稿 W7；docs/plan/cloud-deploy.md D-15）：PyPI、npm、Go 的按需拉取
// 缓存，协议见 internal/mirror。上游写死官方源。只用标准库与 libs/go-core。
//
//	pkg-mirror serve
//
// 环境变量：
//
//	AR_PKG_MIRROR_ADDR          对沙箱的端口，默认 :8093
//	AR_PKG_MIRROR_ADMIN_ADDR    管理端口（清除与拉黑、指标），默认 127.0.0.1:8094；不能与上面相同
//	AR_PKG_MIRROR_DIR           缓存目录（包文件、元数据、拉黑名单），默认 /var/cache/agentruntime/pkg-mirror
//	AR_PKG_MIRROR_BLOB_BUDGET   包文件的预算（字节），默认 100 GiB；AR_PKG_MIRROR_META_BUDGET 元数据的，默认 5 GiB
//	AR_PKG_MIRROR_MAX_FILE      单个文件的大小上限（字节），超过的只转发，默认 500 MiB
//	AR_PKG_MIRROR_PINNED_FILE   常驻的包（JSON 数组：{"ecosystem","name","version"}），不淘汰
//	AR_SECRETS_FILE             SecretStore：平台作用域的 sandbox/env-credential-key（校验环境凭据）与 pkg-mirror/admin-token
//	AR_DATA_DIR、AR_RELATIONAL_DSN   业务库（按租户的未命中字节记进 sandbox_usage），与 Worker 同一个
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/postgres"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/envcred"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/services/go-pkg-mirror/internal/cache"
	"github.com/agentruntime/agent-runtime/services/go-pkg-mirror/internal/mirror"
	"github.com/agentruntime/agent-runtime/services/go-pkg-mirror/internal/usage"
)

func main() {
	if len(os.Args) < 2 || os.Args[1] != "serve" {
		fmt.Fprintln(os.Stderr, "用法：pkg-mirror serve")
		os.Exit(2)
	}
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	if err := serve(log, os.Getenv); err != nil {
		log.Error("包镜像退出", "err", err.Error())
		os.Exit(1)
	}
}

func envOr(getenv func(string) string, key, fallback string) string {
	if v := getenv(key); v != "" {
		return v
	}
	return fallback
}

func bytesEnv(getenv func(string) string, key string, fallback int64) (int64, error) {
	v := getenv(key)
	if v == "" {
		return fallback, nil
	}
	n, err := strconv.ParseInt(v, 10, 64)
	if err != nil || n <= 0 {
		return 0, fmt.Errorf("%s 要是正整数（字节）", key)
	}
	return n, nil
}

func serve(log *slog.Logger, getenv func(string) string) error {
	addr := envOr(getenv, "AR_PKG_MIRROR_ADDR", ":8093")
	adminAddr := envOr(getenv, "AR_PKG_MIRROR_ADMIN_ADDR", "127.0.0.1:8094")
	if addr == adminAddr {
		return errors.New("AR_PKG_MIRROR_ADMIN_ADDR 不能与 AR_PKG_MIRROR_ADDR 相同（管理端口不对沙箱开放）")
	}
	dir := envOr(getenv, "AR_PKG_MIRROR_DIR", "/var/cache/agentruntime/pkg-mirror")
	blobBudget, err := bytesEnv(getenv, "AR_PKG_MIRROR_BLOB_BUDGET", 100<<30)
	if err != nil {
		return err
	}
	metaBudget, err := bytesEnv(getenv, "AR_PKG_MIRROR_META_BUDGET", 5<<30)
	if err != nil {
		return err
	}
	maxFile, err := bytesEnv(getenv, "AR_PKG_MIRROR_MAX_FILE", 500<<20)
	if err != nil {
		return err
	}
	pinned, err := loadPinned(getenv("AR_PKG_MIRROR_PINNED_FILE"))
	if err != nil {
		return err
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
	platform, err := secrets.BindPlatform("pkg-mirror")
	if err != nil {
		return err
	}
	key, err := platform.Read(ctx, envcred.SecretName)
	if err != nil {
		return fmt.Errorf("SecretStore 的平台作用域里没有 %s", envcred.SecretName)
	}
	adminToken := ""
	if v, err := platform.Read(ctx, "pkg-mirror/admin-token"); err == nil {
		adminToken = string(v.Reveal())
	}
	store, err := businessStore(ctx, getenv)
	if err != nil {
		return err
	}
	defer store.Close()
	alert := func(kind, detail string) { log.Warn("包镜像告警", "kind", kind, "detail", detail) }
	blobs, err := cache.Open(cache.Options{Dir: dir, Budget: blobBudget, MaxFile: maxFile, Alert: alert, Pinned: pinned})
	if err != nil {
		return err
	}
	meta, err := cache.OpenMeta(dir, metaBudget)
	if err != nil {
		return err
	}
	tracker := usage.New(store)
	srv := mirror.New(mirror.Config{
		Blobs: blobs, Meta: meta, Upstreams: mirror.Official(), CredentialKey: key.Reveal(), Usage: tracker,
		Deny: mirror.NewDenylist(filepath.Join(dir, "denylist.json")), Log: log,
	})
	public := &http.Server{Addr: addr, Handler: logged(log, srv), ReadHeaderTimeout: 10 * time.Second}
	admin := &http.Server{Addr: adminAddr, Handler: srv.Admin(adminToken), ReadHeaderTimeout: 10 * time.Second}
	errs := make(chan error, 2)
	go func() { errs <- public.ListenAndServe() }()
	go func() { errs <- admin.ListenAndServe() }()
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	log.Info("包镜像在运行", "addr", addr, "admin", adminAddr, "dir", dir)
	for {
		select {
		case err := <-errs:
			return err
		case <-ticker.C:
			// 未命中的字节写进业务库、状态写盘、清掉一小时前下载到一半的临时文件。
			if failed := tracker.Flush(ctx, time.Now()); failed > 0 {
				log.Warn("未命中的字节暂时写不进业务库", "tenants", failed)
			}
			_ = blobs.Save()
			blobs.CleanTemp(time.Hour)
		case <-ctx.Done():
			shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			_ = public.Shutdown(shutdown)
			_ = admin.Shutdown(shutdown)
			tracker.Flush(shutdown, time.Now())
			return blobs.Save()
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

func loadPinned(path string) (func(cache.Origin) bool, error) {
	if path == "" {
		return nil, nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("读不了常驻包的清单：%w", err)
	}
	var items []cache.Origin
	if err := json.Unmarshal(data, &items); err != nil {
		return nil, fmt.Errorf("常驻包的清单格式不对：%w", err)
	}
	return func(o cache.Origin) bool {
		for _, it := range items {
			if it.Ecosystem == o.Ecosystem && strings.EqualFold(it.Name, o.Name) && (it.Version == "" || it.Version == o.Version) {
				return true
			}
		}
		return false
	}, nil
}

// logged 记每个请求（路径去掉凭据）。
func logged(log *slog.Logger, h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &status{ResponseWriter: w, code: 200}
		h.ServeHTTP(rec, r)
		if r.URL.Path != "/healthz" {
			log.Info("请求", "method", r.Method, "path", mirror.SanitizePath(r.URL.Path), "status", rec.code, "ms", time.Since(start).Milliseconds())
		}
	})
}

type status struct {
	http.ResponseWriter
	code int
}

func (s *status) WriteHeader(code int) {
	s.code = code
	s.ResponseWriter.WriteHeader(code)
}
