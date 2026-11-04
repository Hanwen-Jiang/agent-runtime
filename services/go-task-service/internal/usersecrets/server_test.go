package usersecrets

// SEC-API（契约 proto/agentruntime/workspace/v1/user_secrets.proto，2026-11-04 人审通过）的验收：
//
//  1. 写入、列举、删除：值写进 SecretStore 租户作用域的 user-secrets/<用户>/<name>（出网网关按这个名字现读）；列举只有名字与
//     时间、按 name 排序；替换时 created 为假、created_at 不变、updated_at 更新；删除立即生效，再删一次 deleted 为假。
//  2. 用户来自认证身份：别的用户看不到、删不到；别的租户同样。请求里的 tenant_id 只校验一致；没认证时 denied（unauthenticated）。
//  3. 值只进不出：响应、错误信息、日志里都没有值（也没有它的片段）。
//  4. 校验：name 只收字母、数字、.、_、-，最多 128 个字符；值不能为空、最多 8 KiB、不能有控制字符（水平制表符除外）、要是 UTF-8。
//  5. 每个用户最多 100 个：第 101 个 ResourceExhausted（code user_secret_limit），替换已有的不受限。
//  6. 部署的 SecretStore 不能写时，三个方法都明确报不可用，不假装成功。

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/proto"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/postgres"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/postgres/pgtest"
	"github.com/agentruntime/agent-runtime/libs/go-core/connecterr"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/localingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/secretstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	workspacev1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1/workspacev1connect"
)

func TestMain(m *testing.M) { os.Exit(pgtest.Main(m)) }

var ctx = context.Background()

const value = "ghp-VALUE-must-never-come-back-0123456789"

type harness struct {
	ing     *localingress.Provider
	secrets *filesecrets.Provider
	path    string
	clock   *testkit.FakeClock
	logs    *bytes.Buffer
	client  workspacev1connect.UserSecretServiceClient
}

func newHarness(t *testing.T, store relational.Provider, secrets secretstore.Provider) *harness {
	t.Helper()
	if err := store.Migrate(ctx, Migrations()); err != nil {
		t.Fatal(err)
	}
	h := &harness{ing: localingress.New(localingress.Options{SigningKey: []byte(strings.Repeat("k", 32)), Limit: 10000, Window: time.Minute}),
		clock: testkit.NewFakeClock(), logs: &bytes.Buffer{}}
	if secrets == nil {
		h.path = filepath.Join(t.TempDir(), "secrets.json")
		fs, err := filesecrets.New(filesecrets.Options{Path: h.path})
		if err != nil {
			t.Fatal(err)
		}
		h.secrets, secrets = fs, fs
	}
	srv, err := New(Deps{Ingress: h.ing, Store: store, Secrets: secrets, Clock: h.clock, Logger: slog.New(slog.NewTextHandler(h.logs, nil))})
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	srv.Mount(mux)
	ts := httptest.NewServer(mux)
	t.Cleanup(ts.Close)
	h.client = workspacev1connect.NewUserSecretServiceClient(ts.Client(), ts.URL)
	return h
}

func eachStore(t *testing.T, fn func(*testing.T, relational.Provider)) {
	t.Helper()
	for _, backend := range []string{"sqlite", "postgres"} {
		t.Run(backend, func(t *testing.T) {
			var store relational.Provider
			var err error
			if backend == "sqlite" {
				store, err = sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
			} else {
				cluster := pgtest.Current(t)
				store, err = postgres.New(ctx, postgres.Options{DSN: cluster.DSN(pgtest.AppRole, cluster.NewDatabase(t))})
			}
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = store.Close() })
			fn(t, store)
		})
	}
}

func as[T any](h *harness, tenant, user string, msg *T) *connect.Request[T] {
	r := connect.NewRequest(msg)
	if user != "" {
		r.Header().Set("Authorization", "Bearer "+h.ing.IssueToken(tenant, user, time.Hour))
	}
	return r
}

func (h *harness) put(t *testing.T, tenant, user, name, v string) *workspacev1.PutUserSecretResponse {
	t.Helper()
	resp, err := h.client.PutUserSecret(ctx, as(h, tenant, user, &workspacev1.PutUserSecretRequest{Name: name, Value: v}))
	if err != nil {
		t.Fatalf("PutUserSecret(%s/%s/%s)：%v", tenant, user, name, err)
	}
	return resp.Msg
}

func (h *harness) list(t *testing.T, tenant, user string) []*workspacev1.UserSecret {
	t.Helper()
	resp, err := h.client.ListUserSecrets(ctx, as(h, tenant, user, &workspacev1.ListUserSecretsRequest{}))
	if err != nil {
		t.Fatalf("ListUserSecrets：%v", err)
	}
	return resp.Msg.GetSecrets()
}

func (h *harness) stored(t *testing.T, tenant, name string) (string, bool) {
	t.Helper()
	// 另起一个 Provider 读同一份文件：出网网关是另一个进程。
	gateway, err := filesecrets.New(filesecrets.Options{Path: h.path})
	if err != nil {
		t.Fatal(err)
	}
	s, err := gateway.Bind(&commonv1.TenantContext{TenantId: tenant})
	if err != nil {
		t.Fatal(err)
	}
	v, err := s.Read(ctx, name)
	if err != nil {
		return "", false
	}
	return v.RevealString(), true
}

func names(secrets []*workspacev1.UserSecret) []string {
	out := make([]string, 0, len(secrets))
	for _, s := range secrets {
		out = append(out, s.GetName())
	}
	return out
}

func wantCode(t *testing.T, what string, err error, code connect.Code) {
	t.Helper()
	if connect.CodeOf(err) != code {
		t.Fatalf("%s：应 %v，实际 %v", what, code, err)
	}
	if err != nil && strings.Contains(err.Error(), value) {
		t.Fatalf("%s：错误信息里不能有值", what)
	}
}

func errorCode(err error) string {
	var ce *connect.Error
	if !errors.As(err, &ce) {
		return ""
	}
	raw, err := connect.DecodeBinaryHeader(ce.Meta().Get(connecterr.Trailer))
	if err != nil {
		return ""
	}
	detail := &commonv1.Error{}
	if proto.Unmarshal(raw, detail) != nil {
		return ""
	}
	return detail.GetCode()
}

func TestPutListReplaceAndDelete(t *testing.T) {
	eachStore(t, func(t *testing.T, store relational.Provider) {
		h := newHarness(t, store, nil)
		first := h.put(t, "t1", "u1", "github.token", value)
		if !first.GetCreated() || first.GetSecret().GetName() != "github.token" || first.GetSecret().GetCreatedAt() == nil {
			t.Fatalf("新建：%v", first)
		}
		if got, ok := h.stored(t, "t1", "user-secrets/u1/github.token"); !ok || got != value {
			t.Fatalf("值写进租户作用域的 user-secrets/<用户>/<name>，另一个进程读得到：%v", ok)
		}
		h.put(t, "t1", "u1", "a-key", "other")
		if got := names(h.list(t, "t1", "u1")); strings.Join(got, ",") != "a-key,github.token" {
			t.Fatalf("按 name 排序：%v", got)
		}
		h.clock.Advance(time.Hour)
		again := h.put(t, "t1", "u1", "github.token", value+"-2")
		if again.GetCreated() || !again.GetSecret().GetCreatedAt().AsTime().Equal(first.GetSecret().GetCreatedAt().AsTime()) ||
			!again.GetSecret().GetUpdatedAt().AsTime().After(first.GetSecret().GetUpdatedAt().AsTime()) {
			t.Fatalf("替换：created 为假、created_at 不变、updated_at 更新：%v / %v", first, again)
		}
		if got, _ := h.stored(t, "t1", "user-secrets/u1/github.token"); got != value+"-2" {
			t.Fatal("替换之后另一个进程读到新值")
		}
		resp, err := h.client.DeleteUserSecret(ctx, as(h, "t1", "u1", &workspacev1.DeleteUserSecretRequest{Name: "github.token"}))
		if err != nil || !resp.Msg.GetDeleted() {
			t.Fatalf("删除：%v %v", resp, err)
		}
		if _, ok := h.stored(t, "t1", "user-secrets/u1/github.token"); ok {
			t.Fatal("删除立即生效：另一个进程读不到")
		}
		if got := names(h.list(t, "t1", "u1")); strings.Join(got, ",") != "a-key" {
			t.Fatalf("删掉的不再列出：%v", got)
		}
		resp, err = h.client.DeleteUserSecret(ctx, as(h, "t1", "u1", &workspacev1.DeleteUserSecretRequest{Name: "github.token"}))
		if err != nil || resp.Msg.GetDeleted() {
			t.Fatalf("再删一次 deleted 为假：%v %v", resp, err)
		}
	})
}

func TestOnlyTheCallersOwnSecrets(t *testing.T) {
	eachStore(t, func(t *testing.T, store relational.Provider) {
		h := newHarness(t, store, nil)
		h.put(t, "t1", "u1", "gh", value)
		if got := h.list(t, "t1", "u2"); len(got) != 0 {
			t.Fatalf("别的用户看不到：%v", names(got))
		}
		if got := h.list(t, "t2", "u1"); len(got) != 0 {
			t.Fatalf("别的租户同名的用户看不到：%v", names(got))
		}
		resp, err := h.client.DeleteUserSecret(ctx, as(h, "t1", "u2", &workspacev1.DeleteUserSecretRequest{Name: "gh"}))
		if err != nil || resp.Msg.GetDeleted() {
			t.Fatalf("别的用户删不到：%v %v", resp, err)
		}
		if _, ok := h.stored(t, "t1", "user-secrets/u1/gh"); !ok {
			t.Fatal("别的用户的删除不影响 u1 的值")
		}
		_, err = h.client.ListUserSecrets(ctx, as(h, "t1", "u1", &workspacev1.ListUserSecretsRequest{TenantId: "t2"}))
		wantCode(t, "tenant_id 与认证身份不一致", err, connect.CodePermissionDenied)
		// 没认证：与 Task API 的其他方法一样，统一错误模型的 denied（code unauthenticated）。
		_, err = h.client.PutUserSecret(ctx, as(h, "t1", "", &workspacev1.PutUserSecretRequest{Name: "gh", Value: value}))
		wantCode(t, "没认证", err, connect.CodePermissionDenied)
		if code := errorCode(err); code != "unauthenticated" {
			t.Fatalf("没认证的 code：%q", code)
		}
	})
}

func TestValuesNeverComeBack(t *testing.T) {
	eachStore(t, func(t *testing.T, store relational.Provider) {
		h := newHarness(t, store, nil)
		put := h.put(t, "t1", "u1", "gh", value)
		list := h.list(t, "t1", "u1")
		for what, msg := range map[string]proto.Message{"写入的响应": put, "列举的响应": &workspacev1.ListUserSecretsResponse{Secrets: list}} {
			raw, _ := proto.Marshal(msg)
			if bytes.Contains(raw, []byte(value)) || bytes.Contains(raw, []byte(value[:12])) {
				t.Fatalf("%s里不能有值或它的片段", what)
			}
		}
		if strings.Contains(h.logs.String(), value[:12]) {
			t.Fatal("日志里不能有值")
		}
		if !strings.Contains(h.logs.String(), "gh") {
			t.Fatal("审计记下名字与动作")
		}
	})
}

func TestNamesAndValuesAreValidated(t *testing.T) {
	eachStore(t, func(t *testing.T, store relational.Provider) {
		h := newHarness(t, store, nil)
		for _, name := range []string{"", strings.Repeat("a", 129), "a/b", "a b", "密钥", "a\x00"} {
			_, err := h.client.PutUserSecret(ctx, as(h, "t1", "u1", &workspacev1.PutUserSecretRequest{Name: name, Value: value}))
			wantCode(t, "name "+name, err, connect.CodeInvalidArgument)
			_, err = h.client.DeleteUserSecret(ctx, as(h, "t1", "u1", &workspacev1.DeleteUserSecretRequest{Name: name}))
			wantCode(t, "删除的 name "+name, err, connect.CodeInvalidArgument)
		}
		h.put(t, "t1", "u1", strings.Repeat("a", 128), "ok")
		h.put(t, "t1", "u1", "tab", "a\tb")
		h.put(t, "t1", "u1", "max", strings.Repeat("v", 8<<10))
		// 不是 UTF-8 的值到不了服务端（proto3 的 string 在客户端就编码不出来），服务端照样核对，这里不测。
		for what, v := range map[string]string{
			"空值": "", "超过 8 KiB": strings.Repeat("v", 8<<10+1), "换行": value + "\n", "回车": "a\rb", "NUL": "a\x00b", "DEL": "a\x7fb",
		} {
			_, err := h.client.PutUserSecret(ctx, as(h, "t1", "u1", &workspacev1.PutUserSecretRequest{Name: "bad", Value: v}))
			wantCode(t, what, err, connect.CodeInvalidArgument)
		}
		if got := names(h.list(t, "t1", "u1")); len(got) != 3 {
			t.Fatalf("被拒的不写：%v", got)
		}
		if _, ok := h.stored(t, "t1", "user-secrets/u1/bad"); ok {
			t.Fatal("被拒的值不进 SecretStore")
		}
	})
}

func TestAtMostAHundredPerUser(t *testing.T) {
	eachStore(t, func(t *testing.T, store relational.Provider) {
		h := newHarness(t, store, nil)
		for i := range 100 {
			h.put(t, "t1", "u1", "k"+strings.Repeat("x", i), "v")
		}
		_, err := h.client.PutUserSecret(ctx, as(h, "t1", "u1", &workspacev1.PutUserSecretRequest{Name: "one-more", Value: "v"}))
		wantCode(t, "第 101 个", err, connect.CodeResourceExhausted)
		if code := errorCode(err); code != "user_secret_limit" {
			t.Fatalf("code 是 user_secret_limit，实际 %q", code)
		}
		if _, ok := h.stored(t, "t1", "user-secrets/u1/one-more"); ok {
			t.Fatal("超出上限的值不进 SecretStore")
		}
		if again := h.put(t, "t1", "u1", "k", "new"); again.GetCreated() {
			t.Fatal("替换已有的不受上限影响")
		}
		h.put(t, "t1", "u2", "one-more", "v") // 上限按用户算
	})
}

// readOnlySecrets 是不能写的 SecretStore（例如以后的生产实现没有写的能力）。
type readOnlySecrets struct{ secretstore.Provider }

type readOnlyStore struct{ secretstore.Store }

func (r readOnlySecrets) Bind(t *commonv1.TenantContext) (secretstore.Store, error) {
	s, err := r.Provider.Bind(t)
	return readOnlyStore{s}, err
}

func TestUnavailableWhenTheSecretStoreCannotWrite(t *testing.T) {
	eachStore(t, func(t *testing.T, store relational.Provider) {
		fs, err := filesecrets.New(filesecrets.Options{Path: filepath.Join(t.TempDir(), "secrets.json")})
		if err != nil {
			t.Fatal(err)
		}
		h := newHarness(t, store, readOnlySecrets{fs})
		_, err = h.client.PutUserSecret(ctx, as(h, "t1", "u1", &workspacev1.PutUserSecretRequest{Name: "gh", Value: value}))
		wantCode(t, "写入", err, connect.CodeUnimplemented)
		_, err = h.client.DeleteUserSecret(ctx, as(h, "t1", "u1", &workspacev1.DeleteUserSecretRequest{Name: "gh"}))
		wantCode(t, "删除", err, connect.CodeUnimplemented)
		_, err = h.client.ListUserSecrets(ctx, as(h, "t1", "u1", &workspacev1.ListUserSecretsRequest{}))
		wantCode(t, "列举", err, connect.CodeUnimplemented)
		if code := errorCode(err); code != "user_secrets_unavailable" {
			t.Fatalf("code 是 user_secrets_unavailable，实际 %q", code)
		}
	})
}
