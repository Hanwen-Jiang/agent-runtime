package filesecrets_test

// SEC-API（用户密钥的写入、列举、删除）要的可写 SecretStore：开发实现实现 secretstore.Writer。
//
//  1. 写进绑定的作用域，Read 读得到；再写一次是替换；Delete 之后 NotFound，再删一次 deleted 为假。
//  2. 别的租户的作用域看不到、删不到。
//  3. 别的进程（同一份文件的另一个 Provider）马上读得到新写的、读不到删掉的：出网网关按名字现读，不缓存（ENV-02）。
//  4. 写的时候不覆盖别的进程写进文件的内容（别的作用域的密钥、kek）。

import (
	"context"
	"path/filepath"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/secretstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

func writer(t *testing.T, p *filesecrets.Provider, tenant string) (secretstore.Store, secretstore.Writer) {
	t.Helper()
	s := conformance.Must(p.Bind(&commonv1.TenantContext{TenantId: tenant}))
	w, ok := s.(secretstore.Writer)
	if !ok {
		t.Fatal("开发实现的 Store 要实现 secretstore.Writer")
	}
	return s, w
}

func readValue(t *testing.T, s secretstore.Store, name string) (string, bool) {
	t.Helper()
	v, err := s.Read(context.Background(), name)
	if providererr.CategoryOf(err) == providererr.NotFound {
		return "", false
	}
	if err != nil {
		t.Fatalf("Read：%v", err)
	}
	return v.RevealString(), true
}

func TestWriteReplaceAndDelete(t *testing.T) {
	ctx := context.Background()
	p := conformance.Must(filesecrets.New(filesecrets.Options{Path: filepath.Join(t.TempDir(), "secrets.json")}))
	s, w := writer(t, p, "t1")
	if err := w.Write(ctx, "user-secrets/u1/gh", secret.New("first")); err != nil {
		t.Fatal(err)
	}
	if got, ok := readValue(t, s, "user-secrets/u1/gh"); !ok || got != "first" {
		t.Fatalf("写进去的要读得到：%q %v", got, ok)
	}
	if err := w.Write(ctx, "user-secrets/u1/gh", secret.New("second")); err != nil {
		t.Fatal(err)
	}
	if got, _ := readValue(t, s, "user-secrets/u1/gh"); got != "second" {
		t.Fatalf("再写一次是替换：%q", got)
	}
	other, otherW := writer(t, p, "t2")
	if _, ok := readValue(t, other, "user-secrets/u1/gh"); ok {
		t.Fatal("别的租户的作用域看不到")
	}
	if deleted, err := otherW.Delete(ctx, "user-secrets/u1/gh"); err != nil || deleted {
		t.Fatalf("别的租户删不到：%v %v", deleted, err)
	}
	if deleted, err := w.Delete(ctx, "user-secrets/u1/gh"); err != nil || !deleted {
		t.Fatalf("删掉：%v %v", deleted, err)
	}
	if _, ok := readValue(t, s, "user-secrets/u1/gh"); ok {
		t.Fatal("删掉之后读不到")
	}
	if deleted, err := w.Delete(ctx, "user-secrets/u1/gh"); err != nil || deleted {
		t.Fatalf("再删一次 deleted 为假：%v %v", deleted, err)
	}
}

func TestAnotherProcessSeesWritesAndDeletesAtOnce(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "secrets.json")
	api := conformance.Must(filesecrets.New(filesecrets.Options{Path: path}))
	gateway := conformance.Must(filesecrets.New(filesecrets.Options{Path: path}))
	reader := conformance.Must(gateway.Bind(&commonv1.TenantContext{TenantId: "t1"}))
	if _, ok := readValue(t, reader, "user-secrets/u1/gh"); ok {
		t.Fatal("还没写")
	}
	_, w := writer(t, api, "t1")
	if err := w.Write(ctx, "user-secrets/u1/gh", secret.New("v1")); err != nil {
		t.Fatal(err)
	}
	if got, ok := readValue(t, reader, "user-secrets/u1/gh"); !ok || got != "v1" {
		t.Fatalf("另一个进程马上读得到新写的：%q %v", got, ok)
	}
	if err := w.Write(ctx, "user-secrets/u1/gh", secret.New("v2")); err != nil {
		t.Fatal(err)
	}
	if got, _ := readValue(t, reader, "user-secrets/u1/gh"); got != "v2" {
		t.Fatalf("另一个进程马上读得到替换后的：%q", got)
	}
	if _, err := w.Delete(ctx, "user-secrets/u1/gh"); err != nil {
		t.Fatal(err)
	}
	if _, ok := readValue(t, reader, "user-secrets/u1/gh"); ok {
		t.Fatal("另一个进程马上读不到删掉的")
	}
}

func TestWritesKeepWhatOtherProcessesWrote(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "secrets.json")
	a := conformance.Must(filesecrets.New(filesecrets.Options{Path: path}))
	b := conformance.Must(filesecrets.New(filesecrets.Options{Path: path}))
	t1 := &commonv1.TenantContext{TenantId: "t1"}
	key := conformance.Must(conformance.Must(b.Bind(t1)).GenerateDataKey(ctx, "claim_check")) // b 写进 t1 的 kek
	b.SeedSecret("", "platform/key", "keep-me")
	_, w := writer(t, a, "t1")
	if err := w.Write(ctx, "user-secrets/u1/gh", secret.New("v")); err != nil {
		t.Fatal(err)
	}
	fresh := conformance.Must(filesecrets.New(filesecrets.Options{Path: path}))
	got, err := conformance.Must(fresh.Bind(t1)).UnwrapDataKey(ctx, key.Wrapped, "claim_check")
	if err != nil || !got.Equal(key.Plaintext) {
		t.Fatalf("写用户密钥不覆盖别的进程写的 kek：%v", err)
	}
	platform := conformance.Must(fresh.BindPlatform("test"))
	if v, err := platform.Read(ctx, "platform/key"); err != nil || v.RevealString() != "keep-me" {
		t.Fatalf("写用户密钥不覆盖别的作用域的密钥：%v", err)
	}
}
