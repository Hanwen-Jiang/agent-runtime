package valkey_test

// CACHE-01 验收（docs/plan/workspace-read-latency-and-disk-2026-11-03.md 第 2 节）：Cache 接口的 Valkey 适配器。
//   - 过 Cache 的一致性测试（与开发实现同一套；时间真实流逝）。
//   - 两个 Provider 连同一个 Valkey（相当于 task-service 的两个副本）：事件流、限流计数与锁是共享的，
//     A 追加的事件 B 能补发也能实时收到——这是 task-service 能多副本的前提（TSP-2）。
//   - 口令不出现在错误信息与打印里；连不上、认证失败按错误模型分类。
//   - 订阅关闭后 Next 立即返回；等待超时按 Transient。

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/valkey"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/valkey/valkeytest"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance/cachetest"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/cache"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
)

func TestMain(m *testing.M) { os.Exit(valkeytest.Main(m)) }

func newProvider(t *testing.T, prefix string, faults *testkit.Faults) *valkey.Provider {
	t.Helper()
	srv := valkeytest.Current(t)
	p, err := valkey.New(valkey.Options{URL: secret.New(srv.URL()), Prefix: prefix, Faults: faults})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = p.Close() })
	return p
}

type harness struct {
	provider *valkey.Provider
	faults   *testkit.Faults
}

func (h *harness) Provider() cache.Provider { return h.provider }

// Advance 真实等待：过期由 Valkey 按它自己的时钟判定。
func (h *harness) Advance(_ context.Context, d time.Duration) { time.Sleep(d) }

func (h *harness) InjectFault(op string, c providererr.Category) error {
	h.faults.FailNext(op, c)
	return nil
}

func TestConformance(t *testing.T) {
	cachetest.Run(t, func(t *testing.T) cachetest.Harness {
		faults := testkit.NewFaults()
		return &harness{provider: newProvider(t, valkeytest.Prefix(t), faults), faults: faults}
	})
}

// 两个副本：A 追加、B 读与订阅；限流与锁跨副本生效。
func TestTwoProvidersShareStreamsCountersAndLocks(t *testing.T) {
	ctx := context.Background()
	prefix := valkeytest.Prefix(t)
	a := conformance.Must(newProvider(t, prefix, nil).Bind(conformance.TenantA()))
	b := conformance.Must(newProvider(t, prefix, nil).Bind(conformance.TenantA()))

	first := conformance.Must(a.AppendEvent(ctx, "task-1", []byte("a"), time.Hour))
	conformance.Must(a.AppendEvent(ctx, "task-1", []byte("b"), time.Hour))
	page := conformance.Must(b.ReadEvents(ctx, "task-1", 0, 10))
	if len(page.Events) != 2 || string(page.Events[1].Data) != "b" {
		t.Fatalf("B 读不到 A 追加的事件：%+v", page)
	}

	sub := conformance.Must(b.Subscribe(ctx, "task-1", first))
	defer sub.Close()
	go func() {
		time.Sleep(100 * time.Millisecond)
		_, _ = a.AppendEvent(ctx, "task-1", []byte("c"), time.Hour)
	}()
	waitCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	var got []string
	for len(got) < 2 {
		e := conformance.Must(sub.Next(waitCtx))
		got = append(got, string(e.Data))
	}
	if fmt.Sprint(got) != "[b c]" {
		t.Fatalf("B 的订阅先补发再实时推送，得到 %v", got)
	}

	for i, p := range []cache.Cache{a, b, a} {
		d := conformance.Must(p.RateLimit(ctx, "api", 2, time.Minute, 1))
		if d.Allowed != (i < 2) {
			t.Fatalf("第 %d 次请求 allowed=%v：限流计数要跨副本共享", i+1, d.Allowed)
		}
	}
	if conformance.Must(a.AcquireLock(ctx, "job", time.Minute)) == nil || conformance.Must(b.AcquireLock(ctx, "job", time.Minute)) != nil {
		t.Fatal("锁要跨副本互斥")
	}
}

// 不同前缀（不同环境共用一个 Valkey）互不可见。
func TestPrefixesAreIsolated(t *testing.T) {
	ctx := context.Background()
	a := conformance.Must(newProvider(t, valkeytest.Prefix(t), nil).Bind(conformance.TenantA()))
	b := conformance.Must(newProvider(t, valkeytest.Prefix(t), nil).Bind(conformance.TenantA()))
	conformance.NoError(t, a.Set(ctx, "k", []byte("v"), time.Minute))
	if _, ok, err := b.Get(ctx, "k"); err != nil || ok {
		t.Fatalf("不同前缀读到了对方的键（%v）", err)
	}
}

func TestSubscriptionCloseAndTimeout(t *testing.T) {
	ctx := context.Background()
	c := conformance.Must(newProvider(t, valkeytest.Prefix(t), nil).Bind(conformance.TenantA()))

	sub := conformance.Must(c.Subscribe(ctx, "quiet", 0))
	waitCtx, cancel := context.WithTimeout(ctx, 300*time.Millisecond)
	defer cancel()
	start := time.Now()
	_, err := sub.Next(waitCtx)
	conformance.ExpectError(t, err, providererr.Transient, "")
	if time.Since(start) > 3*time.Second {
		t.Errorf("等待超时后应尽快返回，用了 %v", time.Since(start))
	}

	done := make(chan error, 1)
	go func() {
		_, err := sub.Next(ctx)
		done <- err
	}()
	time.Sleep(100 * time.Millisecond)
	_ = sub.Close()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("关闭后 Next 应返回错误")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("关闭订阅没有让阻塞中的 Next 返回")
	}
}

func TestBadPasswordIsDeniedAndNeverPrinted(t *testing.T) {
	srv := valkeytest.Current(t)
	const wrong = "wrong-password-should-never-be-printed"
	u := (&url.URL{Scheme: "valkey", User: url.UserPassword("default", wrong), Host: srv.Addr}).String()
	p, err := valkey.New(valkey.Options{URL: secret.New(u), Prefix: valkeytest.Prefix(t)})
	if err != nil {
		t.Fatal(err)
	}
	defer p.Close()
	for _, s := range []string{fmt.Sprint(p), fmt.Sprintf("%+v", p), fmt.Sprintf("%#v", p)} {
		if strings.Contains(s, wrong) || strings.Contains(s, srv.Password) {
			t.Fatalf("打印 Provider 时带出了口令：%s", s)
		}
	}
	err = p.Ping(context.Background())
	conformance.ExpectError(t, err, providererr.Denied, "")
	c := conformance.Must(p.Bind(conformance.TenantA()))
	_, err = c.Incr(context.Background(), "n", 1, time.Minute)
	conformance.ExpectError(t, err, providererr.Denied, "")
	for e := err; e != nil; e = errors.Unwrap(e) {
		if strings.Contains(e.Error(), wrong) {
			t.Fatalf("错误链里带出了口令：%v", e)
		}
	}
}

func TestUnreachableServerIsTransient(t *testing.T) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := l.Addr().String()
	_ = l.Close()
	p, err := valkey.New(valkey.Options{URL: secret.New("valkey://:s3cret@" + addr), DialTimeout: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	defer p.Close()
	err = p.Ping(context.Background())
	conformance.ExpectError(t, err, providererr.Transient, "")
	if strings.Contains(err.Error(), "s3cret") {
		t.Fatalf("错误信息带出了口令：%v", err)
	}
}

func TestNewRejectsBadURLs(t *testing.T) {
	for _, raw := range []string{"", "http://127.0.0.1:6379", "valkey://", "valkey://127.0.0.1:6379/notanumber", "not a url"} {
		_, err := valkey.New(valkey.Options{URL: secret.New(raw)})
		if err == nil {
			t.Errorf("%q 应当报错", raw)
		} else if providererr.CategoryOf(err) != providererr.Invalid {
			t.Errorf("%q 应当是 Invalid，实际 %v", raw, err)
		}
	}
	for _, raw := range []string{"valkey://127.0.0.1:6379", "redis://:pw@127.0.0.1:6379/2", "valkeys://u:pw@cache.internal:6380", "rediss://cache.internal:6380"} {
		p, err := valkey.New(valkey.Options{URL: secret.New(raw)})
		if err != nil {
			t.Errorf("%q 应当能构造（不连接）：%v", raw, err)
			continue
		}
		_ = p.Close()
	}
}
