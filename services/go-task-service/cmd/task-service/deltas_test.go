package main

// DELTA 的令牌：调用方的令牌在 AR_SECRETS_FILE 平台作用域的 delta/<调用方>；没有时 task-service 启动时生成一个写进去
// （Worker 读同一份文件），已有的不动；令牌不出现在日志里。

import (
	"bytes"
	"context"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/memcache"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/config"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/deltas"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
)

func TestDeltaTokensAreCreatedOnceAndAccepted(t *testing.T) {
	ctx := context.Background()
	file := filepath.Join(t.TempDir(), "secrets.json")
	secrets, err := filesecrets.New(filesecrets.Options{Path: file})
	if err != nil {
		t.Fatal(err)
	}
	platform, _ := secrets.BindPlatform("delta")
	if err := platform.(interface {
		Write(context.Context, string, secret.Value) error
	}).Write(ctx, deltas.SecretPrefix+"ops", secret.New("existing-ops-token")); err != nil {
		t.Fatal(err)
	}

	store, err := sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	if err := store.Migrate(ctx, taskstore.Migrations()); err != nil {
		t.Fatal(err)
	}
	var logs bytes.Buffer
	logger := slog.New(slog.NewTextHandler(&logs, nil))
	cfg := config.Config{SecretsFile: file, InternalAddr: "127.0.0.1:0", DeltaCallers: []string{"worker", "ops"}}
	clk := testkit.NewFakeClock()
	srv, err := deltaServer(ctx, cfg, memcache.New(memcache.Options{Clock: clk}), store, clk, logger)
	if err != nil {
		t.Fatal(err)
	}

	worker, err := platform.Read(ctx, deltas.SecretPrefix+"worker")
	if err != nil || len(worker.Reveal()) < 32 {
		t.Fatalf("没有的令牌启动时生成：%v", err)
	}
	if ops, err := platform.Read(ctx, deltas.SecretPrefix+"ops"); err != nil || ops.RevealString() != "existing-ops-token" {
		t.Fatalf("已有的令牌不动：%v", err)
	}
	if _, err := deltaServer(ctx, cfg, memcache.New(memcache.Options{Clock: clk}), store, clk, logger); err != nil {
		t.Fatal(err)
	}
	if again, _ := platform.Read(ctx, deltas.SecretPrefix+"worker"); !again.Equal(worker) {
		t.Fatal("再启动一次不换令牌")
	}
	if strings.Contains(logs.String(), worker.RevealString()) {
		t.Fatal("令牌不进日志")
	}

	hs := httptest.NewServer(srv.Handler)
	t.Cleanup(hs.Close)
	body := `{"deltas":[{"tenant_id":"t1","task_id":"tsk_01J` + strings.Repeat("0", 22) + `T","node_id":"answer","type":"token_delta","offset":0,"text":"x"}]}`
	for token, want := range map[string]int{worker.RevealString(): http.StatusOK, "existing-ops-token": http.StatusOK, "nope": http.StatusUnauthorized} {
		req, _ := http.NewRequest(http.MethodPost, hs.URL+deltas.Path, strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+token)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode != want {
			t.Errorf("令牌的结果应为 %d，实际 %d", want, resp.StatusCode)
		}
	}
	if resp, err := http.Get(hs.URL + "/v1/tasks/x/events"); err != nil || resp.StatusCode != http.StatusNotFound {
		t.Errorf("内部端口上只有增量入口：%v", err)
	}
}
