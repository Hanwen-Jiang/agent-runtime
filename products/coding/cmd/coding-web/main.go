// Command coding-web 是在线多租户 coding agent 的 MVP 服务（docs/plan/coding-agent-mvp.md）：网页、项目（Git 工作区）、
// 经 Task API 下任务、代理 SSE 进度、看改动与提交。Runtime 一侧用 task-service 与 Worker（scripts/dev_stack.py up --coding）。
//
// 环境变量：
//
//	CODING_ADDR                 监听地址，默认 127.0.0.1:8095
//	CODING_TASK_API             task-service 的地址，例如 http://127.0.0.1:8080（必填）
//	CODING_WORKSPACE_ROOT       工作区根，与 Worker 的 AR_TOOL_WORKSPACE_ROOT 相同（必填）
//	CODING_DATA_DIR             项目与任务的元数据目录（必填）
//	CODING_DEV_LOGIN=1          打开开发登录（租户 + 用户名换令牌，仅供测试）；要同时给 AR_DEV_SIGNING_KEY
//	AR_DEV_SIGNING_KEY          与 task-service 相同的开发签名密钥
//	CODING_TENANT_POLICIES_FILE task-service 的开发租户策略文件；给了就在租户首次登录时把 effect 上限设为 write_reversible
//	CODING_GIT                  git 可执行文件，默认 PATH 上的 git
//	CODING_SECURE_COOKIE=1      Cookie 只走 HTTPS
package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/agentruntime/agent-runtime/products/coding/internal/server"
)

func main() {
	cfg := server.Config{
		TaskAPI:       os.Getenv("CODING_TASK_API"),
		WorkspaceRoot: os.Getenv("CODING_WORKSPACE_ROOT"),
		DataDir:       os.Getenv("CODING_DATA_DIR"),
		DevLogin:      os.Getenv("CODING_DEV_LOGIN") == "1",
		SigningKey:    []byte(os.Getenv("AR_DEV_SIGNING_KEY")),
		PoliciesFile:  os.Getenv("CODING_TENANT_POLICIES_FILE"),
		Git:           os.Getenv("CODING_GIT"),
		SecureCookie:  os.Getenv("CODING_SECURE_COOKIE") == "1",
	}
	srv, err := server.New(cfg)
	if err != nil {
		log.Fatalf("coding-web: %v", err)
	}
	addr := os.Getenv("CODING_ADDR")
	if addr == "" {
		addr = "127.0.0.1:8095"
	}
	httpServer := &http.Server{Addr: addr, Handler: srv.Handler(), ReadHeaderTimeout: 10 * time.Second}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = httpServer.Shutdown(shutdown)
	}()
	log.Printf("coding-web 在 http://%s 上运行", addr)
	if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("coding-web: %v", err)
	}
}
