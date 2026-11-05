// Package conformance 是 Provider 一致性测试的公共部分（计划 S-02）。每个接口一组套件在子包 xxxtest 里，
// 开发实现与生产实现都必须通过。用法：
//
//	func TestMemoryCache(t *testing.T) {
//		cachetest.Run(t, func(t *testing.T) cachetest.Harness { return newHarness(t) })
//	}
//
// 所有套件都覆盖：
//   - 租户绑定：缺少租户上下文 Denied，格式不对 Invalid，平台作用域必须写明用途，绑定时复制上下文。
//   - 租户隔离：A 写入，B 读不到；平台作用域与租户作用域互不可见。
//   - 错误分类：Transient 可重试，其余不可重试；按接口逐项检查 NotFound、Conflict、Invalid、Denied 的触发条件。
//   - 幂等写，以及过期与删除语义（接口有这些语义时）。
//
// 每个测试拿到一个干净的 provider：开发实现每次新建；生产实现在测试之间清空租户 A、租户 B 与平台作用域的数据。
// 与 Python 侧 agentruntime_core.conformance 的用例一一对应。
package conformance

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

const (
	TenantAID = "t-alpha"
	TenantBID = "t-beta"
)

// MalformedTenantIDs 是格式不对的 tenant_id：大写、路径穿越、分隔符、空白、超长、首字符非法、换行。
var MalformedTenantIDs = []string{"T1", "../t-beta", "t-alpha/x", "t alpha", strings.Repeat("a", 64), "-t1", "t1\n"}

// ErrUnsupported 表示 harness 不支持某项操作（例如生产实现无法注入故障），对应用例跳过。
var ErrUnsupported = errors.New("conformance: not supported by this implementation")

// Tenant 构造只有 tenant_id 的租户上下文。
func Tenant(id string) *commonv1.TenantContext { return &commonv1.TenantContext{TenantId: id} }

// TenantA 返回租户 A 的上下文（每次新建）。
func TenantA() *commonv1.TenantContext {
	return &commonv1.TenantContext{TenantId: TenantAID, UserId: "u-alice", WorkspaceId: "w-1"}
}

// TenantB 返回租户 B 的上下文（每次新建）。
func TenantB() *commonv1.TenantContext {
	return &commonv1.TenantContext{TenantId: TenantBID, UserId: "u-bob", WorkspaceId: "w-2"}
}

// ExpectError 断言 err 是指定类别的 *providererr.Error，且 Retryable 与类别一致；code 为空时不检查 code。
func ExpectError(t testing.TB, err error, category providererr.Category, code string) {
	t.Helper()
	var e *providererr.Error
	if !errors.As(err, &e) {
		t.Fatalf("want *providererr.Error with category %v, got %v", category, err)
	}
	if e.Category != category {
		t.Fatalf("category = %v, want %v (%v)", e.Category, category, e)
	}
	if e.Retryable() != (category == providererr.Transient) {
		t.Fatalf("Retryable() = %v for %v", e.Retryable(), category)
	}
	if code != "" && e.Code != code {
		t.Fatalf("code = %q, want %q", e.Code, code)
	}
}

type mustFailure struct{ err error }

// Must 返回 value；err 非空时让当前用例失败。只能在 Case 包起来的用例里使用。
// （Go 不允许把多返回值的调用和别的参数一起传入，所以 Must 不接收 t。）
func Must[T any](value T, err error) T {
	if err != nil {
		panic(mustFailure{err})
	}
	return value
}

// Case 等同于 t.Run，并把 Must 遇到的错误转成 t.Fatal。
func Case(t *testing.T, name string, fn func(t *testing.T)) {
	t.Helper()
	t.Run(name, func(t *testing.T) {
		defer func() {
			if r := recover(); r != nil {
				if f, ok := r.(mustFailure); ok {
					t.Fatalf("unexpected error: %v", f.err)
				}
				panic(r)
			}
		}()
		fn(t)
	})
}

// NoError 在 err 非空时让测试失败。
func NoError(t testing.TB, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

// SkipIfUnsupported 在 err 为 ErrUnsupported 时跳过用例，其余错误让测试失败。
func SkipIfUnsupported(t testing.TB, err error) {
	t.Helper()
	if errors.Is(err, ErrUnsupported) {
		t.Skip("该实现不支持此项，由 S-07 的故障注入套件覆盖")
	}
	if err != nil {
		t.Fatal(err)
	}
}

// Binder 是所有 Provider 工厂共有的绑定方法。
type Binder[C any] interface {
	Bind(tenant *commonv1.TenantContext) (C, error)
	BindPlatform(purpose string) (C, error)
}

// Faultable 让下一次调用 operation 返回该类别的错误；不支持时返回 ErrUnsupported。
type Faultable interface {
	InjectFault(operation string, category providererr.Category) error
}

// Probe 描述如何在客户端上写入与探测一个数据，用于通用的隔离与错误用例。
type Probe[C any] struct {
	Write     func(ctx context.Context, client C, name string) error
	Visible   func(ctx context.Context, client C, name string) (bool, error)
	Operation string
}

// RunBindingRules 检查租户绑定的规则。
func RunBindingRules[C any](t *testing.T, newBinder func(t *testing.T) Binder[C]) {
	Case(t, "BindWithoutTenantIsDenied", func(t *testing.T) {
		b := newBinder(t)
		_, err := b.Bind(nil)
		ExpectError(t, err, providererr.Denied, "missing_tenant")
		_, err = b.Bind(&commonv1.TenantContext{UserId: "u-alice"})
		ExpectError(t, err, providererr.Denied, "missing_tenant")
	})
	Case(t, "BindWithMalformedTenantIsInvalid", func(t *testing.T) {
		b := newBinder(t)
		for _, id := range MalformedTenantIDs {
			_, err := b.Bind(Tenant(id))
			ExpectError(t, err, providererr.Invalid, "malformed_tenant")
		}
	})
	Case(t, "BindPlatformRequiresPurpose", func(t *testing.T) {
		b := newBinder(t)
		for _, purpose := range []string{"", "   "} {
			_, err := b.BindPlatform(purpose)
			ExpectError(t, err, providererr.Invalid, "missing_purpose")
		}
	})
}

// RunIsolationRules 检查租户隔离：A 写入 B 读不到；平台与租户互不可见；绑定时复制上下文。
func RunIsolationRules[C any](t *testing.T, newBinder func(t *testing.T) Binder[C], probe Probe[C]) {
	ctx := context.Background()
	Case(t, "TenantBCannotSeeTenantAData", func(t *testing.T) {
		b := newBinder(t)
		a := Must(b.Bind(TenantA()))
		other := Must(b.Bind(TenantB()))
		NoError(t, probe.Write(ctx, a, "probe-isolation"))
		if !Must(probe.Visible(ctx, a, "probe-isolation")) {
			t.Fatal("写入方自己应当能读到")
		}
		if Must(probe.Visible(ctx, other, "probe-isolation")) {
			t.Fatal("租户 B 读到了租户 A 的数据")
		}
	})
	Case(t, "PlatformAndTenantScopesAreIsolated", func(t *testing.T) {
		b := newBinder(t)
		a := Must(b.Bind(TenantA()))
		platform := Must(b.BindPlatform("conformance"))
		NoError(t, probe.Write(ctx, platform, "probe-platform"))
		NoError(t, probe.Write(ctx, a, "probe-tenant"))
		if !Must(probe.Visible(ctx, platform, "probe-platform")) {
			t.Fatal("平台作用域自己应当能读到")
		}
		if Must(probe.Visible(ctx, a, "probe-platform")) {
			t.Fatal("租户读到了平台作用域的数据")
		}
		if Must(probe.Visible(ctx, platform, "probe-tenant")) {
			t.Fatal("平台作用域读到了租户的数据")
		}
	})
	Case(t, "BindCopiesTenantContext", func(t *testing.T) {
		b := newBinder(t)
		tenant := TenantA()
		client := Must(b.Bind(tenant))
		tenant.TenantId = TenantBID // 绑定之后修改原消息，不应改变客户端的租户
		NoError(t, probe.Write(ctx, client, "probe-copy"))
		if !Must(probe.Visible(ctx, Must(b.Bind(TenantA())), "probe-copy")) {
			t.Fatal("数据应写在租户 A 下")
		}
		if Must(probe.Visible(ctx, Must(b.Bind(TenantB())), "probe-copy")) {
			t.Fatal("修改原消息改变了已绑定客户端的租户")
		}
	})
}

// RunErrorRules 检查错误分类：Transient 可重试，其余不可重试；故障过去后客户端照常可用。
func RunErrorRules[C any](t *testing.T, newHarness func(t *testing.T) (Binder[C], Faultable), probe Probe[C]) {
	ctx := context.Background()
	Case(t, "TransientErrorIsRetryable", func(t *testing.T) {
		b, f := newHarness(t)
		client := Must(b.Bind(TenantA()))
		SkipIfUnsupported(t, f.InjectFault(probe.Operation, providererr.Transient))
		ExpectError(t, probe.Write(ctx, client, "probe-fault"), providererr.Transient, "")
		NoError(t, probe.Write(ctx, client, "probe-fault"))
	})
	for _, category := range []providererr.Category{providererr.Invalid, providererr.Denied, providererr.NotFound, providererr.Conflict} {
		Case(t, "NotRetryable_"+category.String(), func(t *testing.T) {
			b, f := newHarness(t)
			client := Must(b.Bind(TenantA()))
			SkipIfUnsupported(t, f.InjectFault(probe.Operation, category))
			ExpectError(t, probe.Write(ctx, client, "probe-fault"), category, "")
		})
	}
}

// Must2 同 Must，给返回两个值的调用。
func Must2[A, B any](a A, b B, err error) (A, B) {
	if err != nil {
		panic(mustFailure{err})
	}
	return a, b
}
