package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/cache"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/secretstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/config"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/deltas"
)

// deltaTokenTTL 是令牌在内存里留多久：换了令牌，最多这么久之后生效，不用重启。
const deltaTokenTTL = 30 * time.Second

// deltaServer 是逐字增量的内部端口（DELTA，AR_INTERNAL_ADDR）。调用方的令牌在 AR_SECRETS_FILE 平台作用域的 delta/<调用方>，
// 没有的在这里生成并写进去（Worker 读同一份文件）。两个副本同时首次启动时后写的生效，先写的那个最多 deltaTokenTTL 后失效。
func deltaServer(ctx context.Context, cfg config.Config, c cache.Provider, store relational.Provider, clk clock.Clock, logger *slog.Logger) (*http.Server, error) {
	secrets, err := filesecrets.New(filesecrets.Options{Path: cfg.SecretsFile})
	if err != nil {
		return nil, err
	}
	platform, err := secrets.BindPlatform("delta")
	if err != nil {
		return nil, err
	}
	for _, caller := range cfg.DeltaCallers {
		created, err := ensureToken(ctx, platform, deltas.SecretPrefix+caller)
		if err != nil {
			return nil, fmt.Errorf("逐字增量的令牌 %s%s：%w", deltas.SecretPrefix, caller, err)
		}
		if created {
			logger.Info("生成了逐字增量的调用方令牌", "name", deltas.SecretPrefix+caller)
		}
	}
	tokens := &tokenCache{clock: clk, read: func(ctx context.Context) ([]secret.Value, error) {
		out := make([]secret.Value, 0, len(cfg.DeltaCallers))
		for _, caller := range cfg.DeltaCallers {
			v, err := platform.Read(ctx, deltas.SecretPrefix+caller)
			if providererr.CategoryOf(err) == providererr.NotFound {
				continue
			} else if err != nil {
				return nil, err
			}
			out = append(out, v)
		}
		return out, nil
	}}
	h, err := deltas.New(deltas.Deps{Cache: c, Store: store, Clock: clk, Disabled: cfg.DeltaDisabledTenants, Tokens: tokens.get})
	if err != nil {
		return nil, err
	}
	mux := http.NewServeMux()
	mux.Handle("POST "+deltas.Path, h)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, "ok") })
	return &http.Server{Addr: cfg.InternalAddr, Handler: mux, ReadHeaderTimeout: 10 * time.Second, ReadTimeout: 30 * time.Second}, nil
}

// ensureToken 在 name 不存在时生成一个随机令牌写进去，返回是否新建。
func ensureToken(ctx context.Context, store secretstore.Store, name string) (bool, error) {
	_, err := store.Read(ctx, name)
	if err == nil {
		return false, nil
	}
	if providererr.CategoryOf(err) != providererr.NotFound {
		return false, err
	}
	w, ok := store.(secretstore.Writer)
	if !ok {
		return false, fmt.Errorf("SecretStore 里没有，也不能写")
	}
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return false, err
	}
	return true, w.Write(ctx, name, secret.New(hex.EncodeToString(buf)))
}

// tokenCache 把令牌留在内存里 deltaTokenTTL，免得每个请求都重读密钥文件。
type tokenCache struct {
	clock clock.Clock
	read  func(context.Context) ([]secret.Value, error)

	mu     sync.Mutex
	tokens []secret.Value
	at     time.Time
}

func (c *tokenCache) get(ctx context.Context) ([]secret.Value, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.tokens != nil && c.clock.Now().Sub(c.at) < deltaTokenTTL {
		return c.tokens, nil
	}
	tokens, err := c.read(ctx)
	if err != nil {
		return nil, err
	}
	c.tokens, c.at = tokens, c.clock.Now()
	return tokens, nil
}
